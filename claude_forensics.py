#!/usr/bin/env python3
"""
Extract a session history from a .claude directory.

Walks `<claude-dir>/projects/<encoded-cwd>/<session-uuid>.jsonl`, pairs user
prompts with the assistant replies that follow them, and writes one JSON
object per session to a JSONL file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

# Max paste-cache file size embedded inline in the JSONL record. Larger
# pastes are still surfaced (id, size, sha256, mtime), just without content.
_PASTE_INLINE_MAX_BYTES = 100_000

log = logging.getLogger("claude_forensics")


def decode_project_dir(name: str) -> str:
    """Reverse Claude's cwd-as-dirname encoding (slashes -> dashes).

    Lossy when the real path contains dashes, so callers should prefer the
    `cwd` field recorded inside the events when it is present.
    """
    if name.startswith("-"):
        return "/" + name[1:].replace("-", "/")
    return name.replace("-", "/")


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                log.debug("skip malformed line %s:%d (%s)", path, lineno, exc)


def extract_text(content: Any) -> str:
    """Flatten a message `content` field into plain text.

    Content may be a bare string, or a list of blocks where each block is
    either a string or a dict with a `type` discriminator (`text`, `tool_use`,
    `tool_result`, `thinking`, ...). We only keep `text` blocks for the
    transcript; tool calls are summarised separately.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
        return "".join(parts)
    return ""


def _add_usage(target: dict[str, int], usage: dict[str, Any]) -> None:
    """Sum a single Anthropic-API usage block into a running token total.

    Cache writes split into ephemeral_5m / ephemeral_1h because pricing
    differs; `cache_creation_total` is preserved as a fallback for old
    transcripts that don't carry the breakdown.
    """
    if not isinstance(usage, dict):
        return

    def _add(key: str, val: Any) -> None:
        if isinstance(val, int):
            target[key] = target.get(key, 0) + val

    _add("input", usage.get("input_tokens"))
    _add("output", usage.get("output_tokens"))
    _add("cache_read", usage.get("cache_read_input_tokens"))
    _add("cache_creation_total", usage.get("cache_creation_input_tokens"))

    cc = usage.get("cache_creation") or {}
    _add("cache_write_5m", cc.get("ephemeral_5m_input_tokens"))
    _add("cache_write_1h", cc.get("ephemeral_1h_input_tokens"))

    stu = usage.get("server_tool_use") or {}
    _add("web_search_requests", stu.get("web_search_requests"))
    _add("web_fetch_requests", stu.get("web_fetch_requests"))


def extract_tool_calls(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    calls: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            calls.append({
                "name": block.get("name"),
                "id": block.get("id"),
            })
    return calls


def is_real_user_prompt(event: dict[str, Any]) -> bool:
    """A `user` event is a real human prompt only when its content is text.

    Tool-result turns are also typed `user` in the transcript; those have
    `content` shaped as a list of `tool_result` blocks and should not be
    treated as questions from the human.
    """
    if event.get("type") != "user":
        return False
    if event.get("isSidechain"):
        return False
    msg = event.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return False
        return bool(extract_text(content).strip())
    return False


def summarise_session(session_id: str, project_dir_from_path: str,
                      events: list[dict[str, Any]], source_file: Path) -> dict[str, Any]:
    """Reduce a session's event stream to a single summary record."""
    exchanges: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    timestamps: list[str] = []
    cwds: set[str] = set()
    versions: set[str] = set()
    branches: set[str] = set()
    entrypoints: set[str] = set()
    tool_counts: Counter[str] = Counter()
    n_user = 0
    n_assistant = 0

    tokens_total: dict[str, int] = {}
    tokens_by_model: dict[str, dict[str, int]] = {}
    service_tiers: Counter[str] = Counter()

    for ev in events:
        ts = ev.get("timestamp")
        if isinstance(ts, str):
            timestamps.append(ts)
        if isinstance(ev.get("cwd"), str):
            cwds.add(ev["cwd"])
        if isinstance(ev.get("version"), str):
            versions.add(ev["version"])
        if isinstance(ev.get("gitBranch"), str):
            branches.add(ev["gitBranch"])
        if isinstance(ev.get("entrypoint"), str):
            entrypoints.add(ev["entrypoint"])

        etype = ev.get("type")

        if etype == "user" and is_real_user_prompt(ev):
            n_user += 1
            if current is not None:
                exchanges.append(current)
            msg = ev.get("message") or {}
            current = {
                "timestamp": ts,
                "user_prompt": extract_text(msg.get("content")),
                "assistant_response": "",
                "tools_called": [],
            }
        elif etype == "assistant":
            n_assistant += 1
            msg = ev.get("message") or {}
            text = extract_text(msg.get("content"))
            calls = extract_tool_calls(msg.get("content"))
            for c in calls:
                if c.get("name"):
                    tool_counts[c["name"]] += 1

            usage = msg.get("usage") or {}
            model = msg.get("model") if isinstance(msg.get("model"), str) else None
            _add_usage(tokens_total, usage)
            if model:
                _add_usage(tokens_by_model.setdefault(model, {}), usage)
            tier = usage.get("service_tier")
            if isinstance(tier, str):
                service_tiers[tier] += 1

            if current is not None:
                if text:
                    current["assistant_response"] = (
                        current["assistant_response"] + text
                        if current["assistant_response"] else text
                    )
                if calls:
                    current["tools_called"].extend(calls)
                current.setdefault("tokens", {})
                _add_usage(current["tokens"], usage)

    if current is not None:
        exchanges.append(current)

    timestamps.sort()
    start = timestamps[0] if timestamps else None
    end = timestamps[-1] if timestamps else None

    return {
        "session_id": session_id,
        "source_file": str(source_file),
        "project_dir": sorted(cwds)[0] if cwds else project_dir_from_path,
        "project_dir_from_path": project_dir_from_path,
        "all_cwds_seen": sorted(cwds),
        "start_time": start,
        "end_time": end,
        "claude_versions": sorted(versions),
        "git_branches": sorted(branches),
        "entrypoints": sorted(entrypoints),
        "num_events": len(events),
        "num_user_messages": n_user,
        "num_assistant_messages": n_assistant,
        "tools_used": dict(tool_counts),
        "models_used": sorted(tokens_by_model.keys()),
        "tokens": tokens_total,
        "tokens_by_model": tokens_by_model,
        "service_tiers": dict(service_tiers),
        "exchanges": exchanges,
    }


def find_session_files(claude_dir: Path) -> Iterable[tuple[Path, str]]:
    """Yield (session_jsonl_path, decoded_project_dir) pairs."""
    projects = claude_dir / "projects"
    if not projects.is_dir():
        log.warning("no projects/ directory under %s", claude_dir)
        return
    for project_dir in sorted(projects.iterdir()):
        if not project_dir.is_dir():
            continue
        decoded = decode_project_dir(project_dir.name)
        for jsonl in sorted(project_dir.glob("*.jsonl")):
            yield jsonl, decoded


def build_transcript_index(claude_dir: Path) -> dict[str, Path]:
    """Map sessionId -> transcript path by scanning projects/ once."""
    index: dict[str, Path] = {}
    for path, _ in find_session_files(claude_dir):
        index[path.stem] = path
    return index


def process_prompts(claude_dir: Path, output: Path,
                    index: dict[str, Path]) -> tuple[int, int, int]:
    """Flatten history.jsonl, joining each prompt to its transcript on disk.

    Returns (prompts_seen, transcripts_found, orphans) where orphan means a
    prompt whose sessionId has no surviving transcript under projects/.
    """
    history = claude_dir / "history.jsonl"
    if not history.is_file():
        log.warning("no history.jsonl under %s", claude_dir)
        return 0, 0, 0

    prompts_seen = 0
    transcripts_found = 0
    orphans = 0

    with output.open("w", encoding="utf-8") as out:
        for ev in iter_jsonl(history):
            prompts_seen += 1
            sid = ev.get("sessionId")
            transcript_path = index.get(sid) if isinstance(sid, str) else None
            if transcript_path is not None:
                transcripts_found += 1
            else:
                orphans += 1

            project = ev.get("project")
            project_decoded = (
                decode_project_dir(project)
                if isinstance(project, str) and "/" not in project
                else project
            )

            pasted = ev.get("pastedContents")
            num_pasted = len(pasted) if isinstance(pasted, (dict, list)) else 0

            record = {
                "timestamp": ev.get("timestamp"),
                "session_id": sid,
                "project_raw": project,
                "project_dir": project_decoded,
                "display": ev.get("display"),
                "num_pasted_blocks": num_pasted,
                "pasted_contents": pasted,
                "transcript_present": transcript_path is not None,
                "transcript_path": str(transcript_path) if transcript_path else None,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")

    return prompts_seen, transcripts_found, orphans


def _file_mtime_iso(path: Path) -> str | None:
    try:
        ts = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def process_processes(claude_dir: Path, output: Path,
                      index: dict[str, Path]) -> int:
    """Flatten sessions/<pid>.json — one record per Claude process record.

    Each input file is a small JSON doc keyed by the OS PID; it captures the
    process-side view (PID, sessionId, cwd, startedAt, status, waitingFor,
    version). We pass the original fields through and add transcript-presence
    and file-mtime so investigators can cross-reference against OS logs.
    """
    sessions_dir = claude_dir / "sessions"
    if not sessions_dir.is_dir():
        log.warning("no sessions/ directory under %s", claude_dir)
        return 0

    n = 0
    with output.open("w", encoding="utf-8") as out:
        for path in sorted(sessions_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                log.debug("skip %s (%s)", path, exc)
                continue
            if not isinstance(data, dict):
                continue
            sid = data.get("sessionId")
            transcript = index.get(sid) if isinstance(sid, str) else None
            record = {
                **data,
                "source_file": str(path),
                "filename_pid": path.stem,
                "file_mtime": _file_mtime_iso(path),
                "transcript_present": transcript is not None,
                "transcript_path": str(transcript) if transcript else None,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            n += 1
    return n


_ALIAS_RE = re.compile(r"^\s*alias\s+(?:--\s+)?([A-Za-z_][A-Za-z0-9_-]*)=(.*)$")
_EXPORT_RE = re.compile(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_FUNC_PARENS_RE = re.compile(
    r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_:.\-]*)\s*\(\)\s*\{?\s*$"
)
_FUNC_BARE_RE = re.compile(
    r"^\s*function\s+([A-Za-z_][A-Za-z0-9_:.\-]*)\s*\{?\s*$"
)
_SNAPSHOT_TS_RE = re.compile(r"snapshot-(?:zsh|bash)-(\d+)-")


def _strip_shell_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def parse_shell_snapshot(path: Path) -> dict[str, Any]:
    """Pull aliases, exports, function names, and PATH dirs from a snapshot.

    The snapshots are real shell scripts (typically zsh); we use line-level
    regex rather than a full parser. That's lossy for line-continuations and
    nested heredocs but adequate for forensic triage — we want which aliases
    overrode `git`/`curl`, what PATH was in effect, and which env vars leaked.
    """
    aliases: dict[str, str] = {}
    exports: dict[str, str] = {}
    functions: list[str] = []
    path_dirs: list[str] = []

    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        log.debug("cannot read %s (%s)", path, exc)
        return {
            "path_dirs": [], "aliases": {}, "exports": {}, "function_names": [],
        }

    with fh:
        for line in fh:
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            m = _EXPORT_RE.match(line)
            if m:
                name = m.group(1)
                val = _strip_shell_quotes(m.group(2).rstrip("\n"))
                exports[name] = val
                if name == "PATH":
                    path_dirs = val.split(":")
                continue
            m = _ALIAS_RE.match(line)
            if m:
                aliases[m.group(1)] = _strip_shell_quotes(m.group(2).rstrip("\n"))
                continue
            m = _FUNC_PARENS_RE.match(line) or _FUNC_BARE_RE.match(line)
            if m:
                functions.append(m.group(1))
                continue

    return {
        "path_dirs": path_dirs,
        "aliases": aliases,
        "exports": exports,
        "function_names": functions,
    }


def process_shell(claude_dir: Path, output: Path) -> int:
    """Flatten shell-snapshots/*.sh — one record per Claude launch.

    The filename embeds a millisecond launch timestamp
    (snapshot-zsh-<ts-ms>-<rand>.sh), which we surface alongside file mtime
    so launches can be placed on a timeline even without a transcript.
    """
    snap_dir = claude_dir / "shell-snapshots"
    if not snap_dir.is_dir():
        log.warning("no shell-snapshots/ directory under %s", claude_dir)
        return 0

    n = 0
    with output.open("w", encoding="utf-8") as out:
        for path in sorted(snap_dir.glob("*.sh")):
            ts_match = _SNAPSHOT_TS_RE.search(path.name)
            launch_ts_ms = int(ts_match.group(1)) if ts_match else None
            launch_iso = (
                datetime.fromtimestamp(launch_ts_ms / 1000, tz=timezone.utc).isoformat()
                if launch_ts_ms is not None else None
            )
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            parsed = parse_shell_snapshot(path)
            record = {
                "source_file": str(path),
                "size_bytes": size,
                "file_mtime": _file_mtime_iso(path),
                "launch_ts_ms": launch_ts_ms,
                "launch_time": launch_iso,
                **parsed,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            n += 1
    return n


def _walk_for_strings(obj: Any) -> Iterator[str]:
    """Yield every string found anywhere inside a nested dict/list structure.

    Used to discover paste-id references inside the `pastedContents` blob
    in history.jsonl entries: the exact shape isn't documented, so we walk
    keys and values and match anything that looks like a paste id.
    """
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                yield k
            yield from _walk_for_strings(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_for_strings(item)


def process_paste_cache(claude_dir: Path, output: Path) -> int:
    """Flatten paste-cache/*.txt — one record per pasted blob.

    For each file:
      - paste_id (filename stem, a content hash assigned by Claude Code)
      - size, sha256, mtime
      - inline content if the paste is small enough (<= _PASTE_INLINE_MAX_BYTES)
      - referenced_by: prompts in history.jsonl that mention the paste_id

    The paste id appears somewhere inside `pastedContents` on prompt entries;
    rather than depending on the exact dict shape (undocumented and likely
    to change), we walk every string in the blob and match against the set
    of known paste ids on disk. This is robust to schema drift.
    """
    paste_dir = claude_dir / "paste-cache"
    if not paste_dir.is_dir():
        log.warning("no paste-cache/ directory under %s", claude_dir)
        return 0

    paste_files = sorted(p for p in paste_dir.iterdir() if p.is_file())
    paste_ids = {p.stem for p in paste_files}

    refs: dict[str, list[dict[str, Any]]] = {pid: [] for pid in paste_ids}
    history = claude_dir / "history.jsonl"
    if history.is_file():
        for event in iter_jsonl(history):
            pasted = event.get("pastedContents")
            if not pasted:
                continue
            seen_here: set[str] = set()
            for s in _walk_for_strings(pasted):
                if s in paste_ids:
                    seen_here.add(s)
            for pid in seen_here:
                refs[pid].append({
                    "timestamp": event.get("timestamp"),
                    "session_id": event.get("sessionId"),
                    "project": event.get("project"),
                })

    n = 0
    with output.open("w", encoding="utf-8") as out:
        for path in paste_files:
            try:
                raw = path.read_bytes()
            except OSError as exc:
                log.debug("cannot read %s (%s)", path, exc)
                continue
            sha = hashlib.sha256(raw).hexdigest()
            size = len(raw)
            content: str | None = None
            truncated = size > _PASTE_INLINE_MAX_BYTES
            if not truncated:
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    content = raw.decode("utf-8", errors="replace")
            record = {
                "paste_id": path.stem,
                "source_file": str(path),
                "size_bytes": size,
                "sha256": sha,
                "file_mtime": _file_mtime_iso(path),
                "content": content,
                "content_truncated": truncated,
                "referenced_by": refs.get(path.stem, []),
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            n += 1
    return n


_FILE_HISTORY_VERSION_RE = re.compile(r"^(.+)@v(\d+)$")


def process_file_history(claude_dir: Path, output: Path,
                         index: dict[str, Path]) -> int:
    """Flatten file-history/<sessionId>/<filehash>@v<N> — one record per file.

    Each session subdirectory contains versioned backups of files Claude
    edited (one entry per save). We group versions by file_id (the hash
    prefix before `@v`) and emit one record per (session, file_id) pair,
    with the versions sorted by ascending version number.

    The hash-to-original-path mapping isn't stored anywhere in `.claude`;
    investigators usually pair this with `files-touched.txt` from the
    orchestrator to map hashes back to real paths.
    """
    fh_dir = claude_dir / "file-history"
    if not fh_dir.is_dir():
        log.warning("no file-history/ directory under %s", claude_dir)
        return 0

    n = 0
    with output.open("w", encoding="utf-8") as out:
        for session_dir in sorted(fh_dir.iterdir()):
            if not session_dir.is_dir():
                continue
            sid = session_dir.name

            grouped: dict[str, list[tuple[int, Path]]] = {}
            for f in session_dir.iterdir():
                if not f.is_file():
                    continue
                m = _FILE_HISTORY_VERSION_RE.match(f.name)
                if m:
                    file_id, ver = m.group(1), int(m.group(2))
                else:
                    file_id, ver = f.name, 0
                grouped.setdefault(file_id, []).append((ver, f))

            transcript = index.get(sid)
            for file_id, versions in sorted(grouped.items()):
                versions.sort(key=lambda x: x[0])
                version_records: list[dict[str, Any]] = []
                for ver, path in versions:
                    try:
                        st = path.stat()
                        size = st.st_size
                    except OSError:
                        size = None
                    version_records.append({
                        "version": ver,
                        "path": str(path),
                        "size_bytes": size,
                        "mtime": _file_mtime_iso(path),
                    })
                record = {
                    "session_id": sid,
                    "file_id": file_id,
                    "version_count": len(version_records),
                    "versions": version_records,
                    "first_seen": version_records[0]["mtime"] if version_records else None,
                    "last_seen": version_records[-1]["mtime"] if version_records else None,
                    "transcript_present": transcript is not None,
                    "transcript_path": str(transcript) if transcript else None,
                }
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                n += 1
    return n


def process_cowork(cowork_dir: Path, output: Path,
                   index: dict[str, Path]) -> int:
    """Flatten Cowork session metadata (Claude Desktop's data store).

    Expected layout (macOS default `~/Library/Application Support/Claude/`):
      claude-code-sessions/<orgUuid>/<accountUuid>/local_<sessionId>.json

    Each local_*.json is a metadata sidecar — the actual transcript still
    lives in `.claude/projects/<encoded-cwd>/<cliSessionId>.jsonl` and is
    joined here via the `cliSessionId` field. Without that join, the
    metadata (title, isArchived, model, effort, owner org/account) is
    orphaned from the corresponding Q/A turns.

    The orchestrator typically snapshots only a small subset of the
    desktop data dir (skipping vm_bundles/, Cache/) and points this
    extractor at that snapshot.
    """
    sessions_root = cowork_dir / "claude-code-sessions"
    if not sessions_root.is_dir():
        log.warning("no claude-code-sessions/ under %s", cowork_dir)
        return 0

    n = 0
    with output.open("w", encoding="utf-8") as out:
        for org_dir in sorted(sessions_root.iterdir()):
            if not org_dir.is_dir():
                continue
            for account_dir in sorted(org_dir.iterdir()):
                if not account_dir.is_dir():
                    continue
                for session_file in sorted(account_dir.glob("local_*.json")):
                    try:
                        data = json.loads(session_file.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError) as exc:
                        log.debug("skip %s (%s)", session_file, exc)
                        continue
                    if not isinstance(data, dict):
                        continue
                    cli_sid = data.get("cliSessionId")
                    transcript = (index.get(cli_sid)
                                  if isinstance(cli_sid, str) else None)
                    record = {
                        **data,
                        "source_file": str(session_file),
                        "owner_org_uuid": org_dir.name,
                        "owner_account_uuid": account_dir.name,
                        "file_mtime": _file_mtime_iso(session_file),
                        "transcript_present": transcript is not None,
                        "transcript_path": str(transcript) if transcript else None,
                    }
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
                    n += 1
    return n


def _ts_to_iso(value: Any) -> str | None:
    """Normalise an `_audit_timestamp` (ms epoch int or ISO string) to ISO."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def summarise_audit(audit_path: Path) -> dict[str, Any]:
    """Walk an agent-session audit.jsonl and return summary fields shaped
    to match `sessions.jsonl` (so the existing renderers work unchanged).

    Reuses extract_text / extract_tool_calls / _add_usage / is_real_user_prompt
    — agent audit events use the same Anthropic message format as CLI
    transcripts, just with a few extra event types (`system`, `result`,
    `rate_limit_event`) that we use for cwd / model / cost discovery.
    """
    exchanges: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    timestamps: list[str] = []
    cwds: set[str] = set()
    models: set[str] = set()
    tool_counts: Counter[str] = Counter()
    n_user = 0
    n_assistant = 0
    tokens_total: dict[str, int] = {}
    tokens_by_model: dict[str, dict[str, int]] = {}
    service_tiers: Counter[str] = Counter()
    reported_cost_usd = 0.0
    reported_num_turns = 0

    for ev in iter_jsonl(audit_path):
        ts = _ts_to_iso(ev.get("_audit_timestamp"))
        if ts:
            timestamps.append(ts)
        etype = ev.get("type")

        if etype == "system":
            if isinstance(ev.get("cwd"), str):
                cwds.add(ev["cwd"])
            if isinstance(ev.get("model"), str):
                models.add(ev["model"])

        elif etype == "user" and is_real_user_prompt(ev):
            n_user += 1
            if current is not None:
                exchanges.append(current)
            msg = ev.get("message") or {}
            current = {
                "timestamp": ts,
                "user_prompt": extract_text(msg.get("content")
                                            if isinstance(msg, dict) else msg),
                "assistant_response": "",
                "tools_called": [],
                "tokens": {},
            }

        elif etype == "assistant":
            n_assistant += 1
            msg = ev.get("message") or {}
            text = extract_text(msg.get("content"))
            calls = extract_tool_calls(msg.get("content"))
            for c in calls:
                if c.get("name"):
                    tool_counts[c["name"]] += 1
            usage = msg.get("usage") or {}
            model = msg.get("model") if isinstance(msg.get("model"), str) else None
            _add_usage(tokens_total, usage)
            if model:
                _add_usage(tokens_by_model.setdefault(model, {}), usage)
            tier = usage.get("service_tier")
            if isinstance(tier, str):
                service_tiers[tier] += 1
            if current is not None:
                if text:
                    current["assistant_response"] = (
                        current["assistant_response"] + text
                        if current["assistant_response"] else text
                    )
                if calls:
                    current["tools_called"].extend(calls)
                _add_usage(current["tokens"], usage)

        elif etype == "result":
            cost = ev.get("total_cost_usd")
            if isinstance(cost, (int, float)):
                reported_cost_usd += float(cost)
            nt = ev.get("num_turns")
            if isinstance(nt, int):
                reported_num_turns += nt

    if current is not None:
        exchanges.append(current)

    timestamps.sort()
    return {
        "exchanges": exchanges,
        "start_time": timestamps[0] if timestamps else None,
        "end_time": timestamps[-1] if timestamps else None,
        "num_events": sum(1 for _ in iter_jsonl(audit_path)),
        "num_user_messages": n_user,
        "num_assistant_messages": n_assistant,
        "tools_used": dict(tool_counts),
        "models_used": sorted(models | set(tokens_by_model.keys())),
        "tokens": tokens_total,
        "tokens_by_model": tokens_by_model,
        "service_tiers": dict(service_tiers),
        "all_cwds_seen": sorted(cwds),
        "reported_cost_usd": reported_cost_usd if reported_cost_usd > 0 else None,
        "reported_num_turns": reported_num_turns if reported_num_turns > 0 else None,
    }


def process_agent_sessions(cowork_dir: Path, output: Path,
                           index: dict[str, Path]) -> int:
    """Flatten Cowork local-agent-mode-sessions to a sessions.jsonl-shaped
    JSONL of full agent transcripts.

    Layout walked:
      local-agent-mode-sessions/<orgUuid>/<accountUuid>/
        local_<sid>.json         — session metadata (title, owner, etc.)
        local_<sid>/audit.jsonl  — the actual transcript

    Each record mirrors the field names of `sessions.jsonl` so the existing
    reporter works without branching, with agent-specific extras prefixed
    by `agent_*` for clarity (owner email, space id, system prompt size,
    runtime-reported cost). The `skills-plugin/` sibling tree is skipped —
    it is not a session store.
    """
    root = cowork_dir / "local-agent-mode-sessions"
    if not root.is_dir():
        log.warning("no local-agent-mode-sessions/ under %s", cowork_dir)
        return 0

    n = 0
    with output.open("w", encoding="utf-8") as out:
        for org_dir in sorted(root.iterdir()):
            if not org_dir.is_dir() or org_dir.name == "skills-plugin":
                continue
            for account_dir in sorted(org_dir.iterdir()):
                if not account_dir.is_dir():
                    continue

                # Per-account spaces.json: a small map of spaceId -> {name,
                # folders, projects, instructions, origin}. Loaded once per
                # account so we can enrich every session in the loop below.
                spaces_index: dict[str, dict[str, Any]] = {}
                spaces_file = account_dir / "spaces.json"
                if spaces_file.is_file():
                    try:
                        spaces_doc = json.loads(spaces_file.read_text(encoding="utf-8"))
                        for sp in (spaces_doc.get("spaces") or []):
                            sid = sp.get("id")
                            if isinstance(sid, str):
                                spaces_index[sid] = sp
                    except (json.JSONDecodeError, OSError) as exc:
                        log.debug("skip spaces.json %s (%s)", spaces_file, exc)

                for meta_file in sorted(account_dir.glob("local_*.json")):
                    if not meta_file.is_file():
                        continue
                    try:
                        meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError) as exc:
                        log.debug("skip %s (%s)", meta_file, exc)
                        continue
                    if not isinstance(meta, dict):
                        continue
                    audit_path = account_dir / meta_file.stem / "audit.jsonl"
                    if audit_path.is_file():
                        summary = summarise_audit(audit_path)
                    else:
                        summary = {
                            "exchanges": [], "start_time": None, "end_time": None,
                            "num_events": 0, "num_user_messages": 0,
                            "num_assistant_messages": 0, "tools_used": {},
                            "models_used": [], "tokens": {}, "tokens_by_model": {},
                            "service_tiers": {}, "all_cwds_seen": [],
                            "reported_cost_usd": None, "reported_num_turns": None,
                        }
                    cli_sid = meta.get("cliSessionId")
                    cli_transcript = (index.get(cli_sid)
                                      if isinstance(cli_sid, str) else None)
                    # Optional space metadata, joined from spaces.json above.
                    space_id = meta.get("spaceId")
                    space = (spaces_index.get(space_id)
                             if isinstance(space_id, str) else None) or {}
                    space_folders = [
                        f.get("path") for f in (space.get("folders") or [])
                        if isinstance(f, dict) and isinstance(f.get("path"), str)
                    ]
                    space_projects = [
                        p.get("uuid") for p in (space.get("projects") or [])
                        if isinstance(p, dict) and isinstance(p.get("uuid"), str)
                    ]
                    # Decide what to use as project_dir (the by-project grouping
                    # label and the heading in the chronological view):
                    #   - Spaced session  → "<ts> Cowork Chat <space name>"
                    #   - Unspaced Cowork → "<ts> Cowork Chat <vm cwd>"
                    #   - Anything else   → the raw vm_cwd
                    # The leading timestamp (first prompt, UTC, YYYYmmDD-HHMMSS)
                    # makes both spaced and unspaced labels chronologically
                    # sortable, while keeping the space/cwd suffix for identity.
                    vm_cwd = meta.get("cwd") or ""
                    project_dir = vm_cwd
                    ts_prefix = ""
                    start_iso = summary.get("start_time")
                    if isinstance(start_iso, str):
                        try:
                            # audit.jsonl timestamps end in `Z`, which
                            # fromisoformat doesn't accept pre-3.11.
                            dt = datetime.fromisoformat(
                                start_iso.replace("Z", "+00:00"))
                            ts_prefix = dt.strftime("%Y%m%d-%H%M%S") + " Cowork Chat "
                        except (ValueError, AttributeError):
                            pass
                    if isinstance(space.get("name"), str) and space["name"]:
                        project_dir = (ts_prefix + space["name"]) if ts_prefix \
                                      else space["name"]
                    elif vm_cwd.startswith("/sessions/") and ts_prefix:
                        project_dir = ts_prefix + vm_cwd
                    record = {
                        # sessions.jsonl-compatible identity
                        "session_id": meta.get("sessionId") or meta_file.stem,
                        "source_file": str(meta_file),
                        "project_dir": project_dir,
                        "project_dir_from_path": "",
                        # transcript-derived (matches sessions.jsonl schema)
                        **summary,
                        # agent-specific identity & config
                        "agent_audit_file": (str(audit_path)
                                             if audit_path.is_file() else None),
                        "agent_vm_cwd": vm_cwd,
                        "agent_owner_org_uuid": org_dir.name,
                        "agent_owner_account_uuid": account_dir.name,
                        "agent_owner_account_name": meta.get("accountName"),
                        "agent_owner_email": meta.get("emailAddress"),
                        "agent_title": meta.get("title"),
                        "agent_initial_message": meta.get("initialMessage"),
                        "agent_space_id": meta.get("spaceId"),
                        "agent_space_name": space.get("name"),
                        "agent_space_folders": space_folders,
                        "agent_space_projects": space_projects,
                        "agent_space_instructions": space.get("instructions"),
                        "agent_space_origin": space.get("origin"),
                        "agent_model_configured": meta.get("model"),
                        "agent_is_archived": meta.get("isArchived"),
                        "agent_memory_enabled": meta.get("memoryEnabled"),
                        "agent_user_selected_folders": meta.get("userSelectedFolders"),
                        "agent_egress_allowed_domains": meta.get("egressAllowedDomains"),
                        "agent_web_fetch_allowed_urls": meta.get("webFetchAllowedUrls"),
                        "agent_process_name": meta.get("processName"),
                        "agent_vm_process_name": meta.get("vmProcessName"),
                        "agent_system_prompt_chars": len(meta.get("systemPrompt") or ""),
                        "agent_created_at": _ts_to_iso(meta.get("createdAt")),
                        "agent_last_activity_at": _ts_to_iso(meta.get("lastActivityAt")),
                        "agent_cli_session_id": cli_sid,
                        "agent_cli_transcript_present": cli_transcript is not None,
                        "agent_cli_transcript_path":
                            str(cli_transcript) if cli_transcript else None,
                    }
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
                    n += 1
    return n


def process(claude_dir: Path, output: Path) -> tuple[int, int]:
    sessions_written = 0
    files_seen = 0
    with output.open("w", encoding="utf-8") as out:
        for path, decoded_dir in find_session_files(claude_dir):
            files_seen += 1
            session_id = path.stem
            log.debug("reading %s", path)
            events = list(iter_jsonl(path))
            if not events:
                log.debug("no events in %s, skipping", path)
                continue
            record = summarise_session(session_id, decoded_dir, events, path)
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            sessions_written += 1
            log.debug(
                "session %s: %d events, %d Q/A exchanges",
                session_id, record["num_events"], len(record["exchanges"]),
            )
    return files_seen, sessions_written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract Claude Code session history from a .claude directory.",
    )
    parser.add_argument(
        "--claude-dir",
        type=Path,
        default=Path.home() / ".claude",
        help="Path to the .claude directory to analyse (default: ~/.claude)",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=Path("claude-history.jsonl"),
        help="Path to write the per-session JSONL output (default: ./claude-history.jsonl)",
    )
    parser.add_argument(
        "--prompts-out",
        type=Path,
        default=None,
        help=(
            "If set, also flatten history.jsonl to this path: one record per "
            "prompt, joined to the transcript on disk (transcript_present flag)."
        ),
    )
    parser.add_argument(
        "--processes-out",
        type=Path,
        default=None,
        help=(
            "If set, also flatten sessions/<pid>.json to this path: one record "
            "per Claude process record (PID, sessionId, cwd, status, version)."
        ),
    )
    parser.add_argument(
        "--shell-out",
        type=Path,
        default=None,
        help=(
            "If set, also parse shell-snapshots/*.sh to this path: one record "
            "per Claude launch with PATH dirs, aliases, exports, function names."
        ),
    )
    parser.add_argument(
        "--paste-cache-out",
        type=Path,
        default=None,
        help=(
            "If set, also flatten paste-cache/*.txt to this path: one record "
            "per pasted blob with size, sha256, inline content (if small), "
            "and back-references to prompts that mention the paste id."
        ),
    )
    parser.add_argument(
        "--file-history-out",
        type=Path,
        default=None,
        help=(
            "If set, also walk file-history/<sessionId>/<filehash>@v<N> and "
            "emit one record per (session, file) with all version metadata."
        ),
    )
    parser.add_argument(
        "--cowork-dir",
        type=Path,
        default=None,
        help=(
            "Path to a Claude Desktop data dir (macOS default: "
            "~/Library/Application Support/Claude/). When set, walks "
            "claude-code-sessions/<orgUuid>/<accountUuid>/local_*.json "
            "and emits Cowork session metadata joined to CLI transcripts."
        ),
    )
    parser.add_argument(
        "--cowork-out",
        type=Path,
        default=None,
        help=(
            "Output path for cowork-sessions JSONL. Required when "
            "--cowork-dir is set; ignored otherwise."
        ),
    )
    parser.add_argument(
        "--cowork-agent-out",
        type=Path,
        default=None,
        help=(
            "If set (and --cowork-dir given), walks "
            "local-agent-mode-sessions/<org>/<account>/local_<sid>/audit.jsonl "
            "and emits full agent-session transcripts in sessions.jsonl shape."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Verbose logging to stderr",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )

    claude_dir: Path = args.claude_dir.expanduser().resolve()
    output: Path = args.output.expanduser().resolve()

    if not claude_dir.is_dir():
        log.error("not a directory: %s", claude_dir)
        return 2

    log.info("scanning %s", claude_dir)
    files_seen, sessions_written = process(claude_dir, output)
    log.info(
        "wrote %d sessions from %d transcript files to %s",
        sessions_written, files_seen, output,
    )

    needs_index = any(p is not None
                      for p in (args.prompts_out, args.processes_out,
                                args.file_history_out, args.cowork_out,
                                args.cowork_agent_out))
    index: dict[str, Path] = build_transcript_index(claude_dir) if needs_index else {}
    if needs_index:
        log.debug("transcript index: %d sessions", len(index))

    if args.prompts_out is not None:
        prompts_out: Path = args.prompts_out.expanduser().resolve()
        prompts_seen, found, orphans = process_prompts(claude_dir, prompts_out, index)
        log.info(
            "wrote %d prompts to %s (%d with transcript, %d orphan)",
            prompts_seen, prompts_out, found, orphans,
        )

    if args.processes_out is not None:
        processes_out: Path = args.processes_out.expanduser().resolve()
        n_proc = process_processes(claude_dir, processes_out, index)
        log.info("wrote %d process records to %s", n_proc, processes_out)

    if args.shell_out is not None:
        shell_out: Path = args.shell_out.expanduser().resolve()
        n_shell = process_shell(claude_dir, shell_out)
        log.info("wrote %d shell-snapshot records to %s", n_shell, shell_out)

    if args.paste_cache_out is not None:
        paste_out: Path = args.paste_cache_out.expanduser().resolve()
        n_paste = process_paste_cache(claude_dir, paste_out)
        log.info("wrote %d paste records to %s", n_paste, paste_out)

    if args.file_history_out is not None:
        fh_out: Path = args.file_history_out.expanduser().resolve()
        n_fh = process_file_history(claude_dir, fh_out, index)
        log.info("wrote %d file-history records to %s", n_fh, fh_out)

    if args.cowork_dir is not None:
        cw_dir: Path = args.cowork_dir.expanduser().resolve()
        if not cw_dir.is_dir():
            log.error("not a directory: %s", cw_dir)
            return 2
        if args.cowork_out is None and args.cowork_agent_out is None:
            log.error("--cowork-dir requires --cowork-out and/or --cowork-agent-out")
            return 2
        if args.cowork_out is not None:
            cw_out: Path = args.cowork_out.expanduser().resolve()
            n_cw = process_cowork(cw_dir, cw_out, index)
            log.info("wrote %d Cowork session records to %s", n_cw, cw_out)
        if args.cowork_agent_out is not None:
            ag_out: Path = args.cowork_agent_out.expanduser().resolve()
            n_ag = process_agent_sessions(cw_dir, ag_out, index)
            log.info("wrote %d agent-session records to %s", n_ag, ag_out)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
