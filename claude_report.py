#!/usr/bin/env python3
"""
Render a Markdown report from the JSONL output of claude_forensics.py.

Consumes:
  --sessions  one record per session  (required, produced by --output)
  --prompts   one record per prompt   (optional, produced by --prompts-out)

Two report layouts:
  --order project          (default) headline stats, per-project breakdown
  --order chronological             headline stats, sessions in start-time order

Markdown also reads fine as plain text, so there is no separate text format.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("claude_report")

PROMPT_PREVIEW_CHARS = 160
RESPONSE_PREVIEW_CHARS = 220

# Token rate keys in the cost table. Anything missing in the JSON defaults
# to zero, so a partial pricing file just under-estimates rather than crashing.
_PRICED_KEYS: tuple[tuple[str, str], ...] = (
    ("input",           "input_per_mtok"),
    ("output",          "output_per_mtok"),
    ("cache_read",      "cache_read_per_mtok"),
    ("cache_write_5m",  "cache_write_5m_per_mtok"),
    ("cache_write_1h",  "cache_write_1h_per_mtok"),
)


def load_pricing(path: Path) -> dict[str, Any]:
    """Load a cost table JSON. Schema documented in prices.example.json."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("pricing file must be a JSON object")
    data.setdefault("models", {})
    data.setdefault("server_tools", {})
    return data


def fmt_money(amount: float, currency: str = "USD") -> str:
    sym = "$" if currency == "USD" else ""
    return f"{sym}{amount:,.4f}"


def cost_for_tokens_by_model(
    tokens_by_model: dict[str, dict[str, int]],
    pricing: dict[str, Any],
) -> tuple[float, dict[str, float], dict[str, dict[str, int]]]:
    """Estimate cost from a per-model token breakdown.

    Returns (total, per_model_cost, unpriced_models). A model present in the
    tokens but missing from the pricing table is *not* silently zero — it
    lands in `unpriced_models` so the report can flag it.
    """
    total = 0.0
    per_model: dict[str, float] = {}
    unpriced: dict[str, dict[str, int]] = {}

    server_rates = pricing.get("server_tools") or {}
    ws_rate = float(server_rates.get("web_search_per_request") or 0)
    wf_rate = float(server_rates.get("web_fetch_per_request") or 0)

    for model, toks in (tokens_by_model or {}).items():
        rates = (pricing.get("models") or {}).get(model)
        if rates is None:
            unpriced[model] = dict(toks)
            continue
        cost = 0.0
        for tok_key, rate_key in _PRICED_KEYS:
            n = int(toks.get(tok_key) or 0)
            rate = float(rates.get(rate_key) or 0)
            cost += n * rate / 1_000_000
        cost += int(toks.get("web_search_requests") or 0) * ws_rate
        cost += int(toks.get("web_fetch_requests") or 0) * wf_rate
        per_model[model] = cost
        total += cost
    return total, per_model, unpriced


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                log.warning("skip malformed line %s:%d (%s)", path, lineno, exc)
    return records


def parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 string or epoch-ms int into a UTC datetime."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    if isinstance(value, str):
        v = value.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(v)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def fmt_ts(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%SZ") if dt else "—"


def fmt_duration(start: datetime | None, end: datetime | None) -> str:
    if not start or not end:
        return "—"
    secs = int((end - start).total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    h, rem = divmod(secs, 3600)
    return f"{h}h {rem // 60}m"


def truncate(text: str | None, n: int) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def section(title: str, level: int = 2) -> str:
    return "#" * level + " " + title + "\n\n"


def render_pricing_notes(pricing: dict[str, Any],
                         unpriced_total: dict[str, dict[str, int]]) -> str:
    """Render the pricing table reference so cost figures are reproducible."""
    out = [section("Pricing notes")]
    out.append(
        "Cost figures below are estimates computed locally from token counts "
        "in the transcripts. Authoritative billing comes from the Anthropic "
        "Console, not from this report.\n\n"
    )
    out.append(f"- **Effective date:** {pricing.get('effective_date', '—')}\n")
    out.append(f"- **Currency:** {pricing.get('currency', 'USD')}\n")
    note = pricing.get("_note")
    if note:
        out.append(f"- **Note:** {note}\n")
    models = pricing.get("models") or {}
    if models:
        out.append("- **Priced models:** "
                   + ", ".join(sorted(f"`{m}`" for m in models.keys())) + "\n")
    if unpriced_total:
        out.append("- **Unpriced models seen in data** (excluded from cost): "
                   + ", ".join(sorted(f"`{m}`" for m in unpriced_total.keys()))
                   + "\n")
    out.append("\n")
    return "".join(out)


def render_headline(sessions: list[dict[str, Any]],
                    prompts: list[dict[str, Any]] | None,
                    pricing: dict[str, Any] | None = None) -> str:
    starts = [parse_ts(s.get("start_time")) for s in sessions]
    ends = [parse_ts(s.get("end_time")) for s in sessions]
    starts = [t for t in starts if t]
    ends = [t for t in ends if t]

    projects = {s.get("project_dir") for s in sessions if s.get("project_dir")}
    versions: set[str] = set()
    entrypoints: set[str] = set()
    for s in sessions:
        versions.update(s.get("claude_versions") or [])
        entrypoints.update(s.get("entrypoints") or [])

    total_user = sum(s.get("num_user_messages", 0) for s in sessions)
    total_tool_calls = sum(sum((s.get("tools_used") or {}).values()) for s in sessions)

    tok_total: Counter[str] = Counter()
    for s in sessions:
        for k, v in (s.get("tokens") or {}).items():
            if isinstance(v, int):
                tok_total[k] += v

    lines = [section("Overview")]
    lines.append(f"- **Date range:** {fmt_ts(min(starts) if starts else None)} → "
                 f"{fmt_ts(max(ends) if ends else None)}\n")
    lines.append(f"- **Sessions:** {len(sessions)}\n")
    lines.append(f"- **User prompts (from transcripts):** {total_user}\n")
    if prompts is not None:
        orphans = sum(1 for p in prompts if not p.get("transcript_present"))
        lines.append(f"- **Prompts in history.jsonl:** {len(prompts)} "
                     f"({orphans} without surviving transcript)\n")
    lines.append(f"- **Tool calls:** {total_tool_calls}\n")
    lines.append(f"- **Distinct projects:** {len(projects)}\n")
    if versions:
        lines.append(f"- **Claude versions seen:** {', '.join(sorted(versions))}\n")
    if entrypoints:
        lines.append(f"- **Entrypoints:** {', '.join(sorted(entrypoints))}\n")

    if tok_total:
        lines.append(
            "- **Tokens:** "
            f"in={tok_total['input']:,} · "
            f"out={tok_total['output']:,} · "
            f"cache-read={tok_total['cache_read']:,} · "
            f"cache-write-5m={tok_total['cache_write_5m']:,} · "
            f"cache-write-1h={tok_total['cache_write_1h']:,}\n"
        )

    if pricing is not None:
        all_tbm: dict[str, dict[str, int]] = {}
        for s in sessions:
            for model, toks in (s.get("tokens_by_model") or {}).items():
                target = all_tbm.setdefault(model, {})
                for k, v in toks.items():
                    if isinstance(v, int):
                        target[k] = target.get(k, 0) + v
        total_cost, per_model, _ = cost_for_tokens_by_model(all_tbm, pricing)
        currency = pricing.get("currency", "USD")
        lines.append(f"- **Estimated spend:** {fmt_money(total_cost, currency)}\n")
        if per_model:
            parts = ", ".join(
                f"`{m}` {fmt_money(c, currency)}"
                for m, c in sorted(per_model.items(), key=lambda kv: -kv[1])
            )
            lines.append(f"  - by model: {parts}\n")

    lines.append("\n")
    return "".join(lines)


def render_tool_ranking(sessions: list[dict[str, Any]], top: int) -> str:
    counts: Counter[str] = Counter()
    sessions_with: defaultdict[str, set[str]] = defaultdict(set)
    for s in sessions:
        sid = s.get("session_id", "")
        for name, n in (s.get("tools_used") or {}).items():
            counts[name] += n
            sessions_with[name].add(sid)
    if not counts:
        return ""
    out = [section("Top tools used")]
    out.append("| Tool | Calls | Sessions |\n|---|---:|---:|\n")
    for name, n in counts.most_common(top):
        out.append(f"| `{md_escape(name)}` | {n} | {len(sessions_with[name])} |\n")
    out.append("\n")
    return "".join(out)


def _cowork_tag_md(s: dict[str, Any],
                   cowork_index: dict[str, dict[str, Any]] | None) -> str:
    """Markdown annotation appended to a session row when joinable to Cowork."""
    if not cowork_index:
        return ""
    cw = cowork_index.get(s.get("session_id") or "")
    if not cw:
        return ""
    tag = f' · Cowork: "{cw.get("title", "")}"'
    if cw.get("isArchived"):
        tag += " [archived]"
    return tag


def _agent_space_tag_md(s: dict[str, Any]) -> str:
    """For agent-session records, surface the space's host folder path (and
    the name, when project_dir doesn't already carry it). No-op for CLI rows.
    The extractor uses the space name as `project_dir` when a session belongs
    to a named space, so repeating it here would be visual noise."""
    parts: list[str] = []
    name = s.get("agent_space_name")
    if isinstance(name, str) and name and name != s.get("project_dir"):
        parts.append(f'space: "{name}"')
    folders = s.get("agent_space_folders") or []
    if folders and isinstance(folders[0], str):
        parts.append(f"folder: `{folders[0]}`")
    return (" · " + " · ".join(parts)) if parts else ""


def build_cowork_index(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index Cowork records by cliSessionId — the join key into sessions.jsonl."""
    out: dict[str, dict[str, Any]] = {}
    for r in records:
        cli = r.get("cliSessionId")
        if isinstance(cli, str):
            out[cli] = r
    return out


def session_one_liner(s: dict[str, Any],
                      cowork_index: dict[str, dict[str, Any]] | None = None) -> str:
    start = parse_ts(s.get("start_time"))
    end = parse_ts(s.get("end_time"))
    tool_calls = sum((s.get("tools_used") or {}).values())
    return (f"`{s.get('session_id','')[:8]}…`  "
            f"{fmt_ts(start)}  "
            f"({fmt_duration(start, end)})  "
            f"{s.get('num_user_messages', 0)} prompts, "
            f"{tool_calls} tool calls"
            f"{_cowork_tag_md(s, cowork_index)}"
            f"{_agent_space_tag_md(s)}")


def _blockquote(text: str) -> str:
    """Render arbitrary text as a markdown blockquote, preserving newlines."""
    if not text:
        return ""
    lines = text.splitlines() or [""]
    return "\n".join(f"> {line}" if line else ">" for line in lines) + "\n"


def render_session_exchanges(s: dict[str, Any], full: bool = False) -> str:
    out: list[str] = []
    if full:
        for i, ex in enumerate(s.get("exchanges") or [], 1):
            t = parse_ts(ex.get("timestamp"))
            out.append(section(f"Exchange {i} — {fmt_ts(t)}", level=4))
            q = ex.get("user_prompt") or ""
            a = ex.get("assistant_response") or ""
            out.append("**Q:**\n\n")
            out.append(_blockquote(q))
            out.append("\n")
            if a:
                out.append("**A:**\n\n")
                out.append(_blockquote(a))
                out.append("\n")
            tools = ex.get("tools_called") or []
            if tools:
                names = ", ".join(f"`{t.get('name')}`" for t in tools if t.get("name"))
                out.append(f"Tools: {names}\n\n")
        return "".join(out)

    for i, ex in enumerate(s.get("exchanges") or [], 1):
        t = parse_ts(ex.get("timestamp"))
        out.append(f"  {i}. _{fmt_ts(t)}_\n")
        out.append(f"     - **Q:** {truncate(ex.get('user_prompt'), PROMPT_PREVIEW_CHARS)}\n")
        resp = truncate(ex.get("assistant_response"), RESPONSE_PREVIEW_CHARS)
        if resp:
            out.append(f"     - **A:** {resp}\n")
        tools = ex.get("tools_called") or []
        if tools:
            names = ", ".join(f"`{t.get('name')}`" for t in tools if t.get("name"))
            out.append(f"     - tools: {names}\n")
    out.append("\n")
    return "".join(out)


def _sum_tokens_by_model(sessions: Iterable[dict[str, Any]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for s in sessions:
        for model, toks in (s.get("tokens_by_model") or {}).items():
            target = out.setdefault(model, {})
            for k, v in toks.items():
                if isinstance(v, int):
                    target[k] = target.get(k, 0) + v
    return out


def render_by_project(sessions: list[dict[str, Any]],
                      top: int,
                      include_exchanges: bool,
                      full: bool = False,
                      pricing: dict[str, Any] | None = None,
                      cowork_index: dict[str, dict[str, Any]] | None = None) -> str:
    by_project: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in sessions:
        by_project[s.get("project_dir") or "(unknown)"].append(s)

    # When every "project" has exactly one session — the common Cowork
    # agent case, since each chat now gets its own timestamped project_dir —
    # ranking by activity is meaningless, so sort chronologically.
    # Otherwise keep the ranked order (most sessions / most prompts first),
    # which is what the CLI reports want.
    if all(len(ss) == 1 for ss in by_project.values()):
        ranked = sorted(by_project.items(),
                        key=lambda kv: kv[1][0].get("start_time") or "")
    else:
        ranked = sorted(
            by_project.items(),
            key=lambda kv: (
                -len(kv[1]),
                -sum(x.get("num_user_messages", 0) for x in kv[1]),
                min((x.get("start_time") or "") for x in kv[1]),
            ),
        )

    out = [section("Projects")]
    for project, ss in ranked:
        ss = sorted(ss, key=lambda x: x.get("start_time") or "")
        prompts = sum(x.get("num_user_messages", 0) for x in ss)
        tool_calls = sum(sum((x.get("tools_used") or {}).values()) for x in ss)
        starts = [parse_ts(x.get("start_time")) for x in ss]
        ends = [parse_ts(x.get("end_time")) for x in ss]
        starts = [t for t in starts if t]
        ends = [t for t in ends if t]
        first = min(starts) if starts else None
        last = max(ends) if ends else None

        tool_tot: Counter[str] = Counter()
        for x in ss:
            tool_tot.update(x.get("tools_used") or {})

        out.append(section(project, level=3))
        out.append(f"- Sessions: **{len(ss)}** · Prompts: **{prompts}** · "
                   f"Tool calls: **{tool_calls}**\n")
        out.append(f"- First activity: {fmt_ts(first)} · Last activity: {fmt_ts(last)}\n")
        if tool_tot:
            top_tools = ", ".join(f"`{n}` ({c})" for n, c in tool_tot.most_common(top))
            out.append(f"- Top tools: {top_tools}\n")

        proj_tokens: Counter[str] = Counter()
        for x in ss:
            for k, v in (x.get("tokens") or {}).items():
                if isinstance(v, int):
                    proj_tokens[k] += v
        if proj_tokens:
            out.append(
                f"- Tokens: in={proj_tokens['input']:,}, "
                f"out={proj_tokens['output']:,}, "
                f"cache-read={proj_tokens['cache_read']:,}, "
                f"cache-write-5m={proj_tokens['cache_write_5m']:,}, "
                f"cache-write-1h={proj_tokens['cache_write_1h']:,}\n"
            )

        if pricing is not None:
            tbm = _sum_tokens_by_model(ss)
            cost, _, unpriced = cost_for_tokens_by_model(tbm, pricing)
            currency = pricing.get("currency", "USD")
            note = (f" _(plus unpriced: {', '.join(sorted(unpriced))})_"
                    if unpriced else "")
            out.append(f"- **Estimated spend:** {fmt_money(cost, currency)}{note}\n")
        out.append("\n")

        for s in ss:
            out.append(f"- {session_one_liner(s, cowork_index)}\n")
            if include_exchanges:
                out.append(render_session_exchanges(s, full=full))
        out.append("\n")
    return "".join(out)


def render_chronological(sessions: list[dict[str, Any]],
                         include_exchanges: bool,
                         full: bool = False,
                         pricing: dict[str, Any] | None = None,
                         cowork_index: dict[str, dict[str, Any]] | None = None) -> str:
    ordered = sorted(sessions, key=lambda s: s.get("start_time") or "")
    out = [section("Sessions in chronological order")]
    for s in ordered:
        start = parse_ts(s.get("start_time"))
        end = parse_ts(s.get("end_time"))
        tool_calls = sum((s.get("tools_used") or {}).values())
        cw_suffix = _cowork_tag_md(s, cowork_index) + _agent_space_tag_md(s)
        out.append(section(
            f"{fmt_ts(start)} — {s.get('project_dir', '(unknown)')}{cw_suffix}",
            level=3,
        ))
        out.append(f"- Session: `{s.get('session_id','')}`\n")
        out.append(f"- Duration: {fmt_duration(start, end)} · "
                   f"Prompts: {s.get('num_user_messages', 0)} · "
                   f"Tool calls: {tool_calls}\n")
        versions = s.get("claude_versions") or []
        if versions:
            out.append(f"- Version: {', '.join(versions)}\n")
        tt = s.get("tools_used") or {}
        if tt:
            top_tools = ", ".join(f"`{n}` ({c})"
                                  for n, c in sorted(tt.items(),
                                                     key=lambda kv: -kv[1])[:5])
            out.append(f"- Tools: {top_tools}\n")
        toks = s.get("tokens") or {}
        if toks:
            out.append(
                f"- Tokens: in={toks.get('input',0):,}, "
                f"out={toks.get('output',0):,}, "
                f"cache-read={toks.get('cache_read',0):,}, "
                f"cache-write-5m={toks.get('cache_write_5m',0):,}, "
                f"cache-write-1h={toks.get('cache_write_1h',0):,}\n"
            )
        if pricing is not None:
            cost, _, unpriced = cost_for_tokens_by_model(
                s.get("tokens_by_model") or {}, pricing)
            currency = pricing.get("currency", "USD")
            note = (f" _(plus unpriced: {', '.join(sorted(unpriced))})_"
                    if unpriced else "")
            out.append(f"- **Estimated spend:** {fmt_money(cost, currency)}{note}\n")
        out.append("\n")
        if include_exchanges:
            out.append(render_session_exchanges(s, full=full))
    return "".join(out)


def render_chronological_prompts(prompts: list[dict[str, Any]]) -> str:
    """Unified timeline of every prompt typed, regardless of transcript survival."""
    ordered = sorted(prompts, key=lambda p: (p.get("timestamp") or 0))
    out = [section("Prompt timeline (history.jsonl)")]
    out.append("| Time | Project | Session | Transcript | Prompt |\n")
    out.append("|---|---|---|:---:|---|\n")
    for p in ordered:
        t = parse_ts(p.get("timestamp"))
        sid = (p.get("session_id") or "")[:8]
        present = "✓" if p.get("transcript_present") else "✗"
        display = truncate(p.get("display"), PROMPT_PREVIEW_CHARS)
        out.append(
            f"| {fmt_ts(t)} | {md_escape(p.get('project_dir') or '')} "
            f"| `{sid}…` | {present} | {md_escape(display)} |\n"
        )
    out.append("\n")
    return "".join(out)


def render_anomalies(sessions: list[dict[str, Any]],
                     prompts: list[dict[str, Any]] | None) -> str:
    out: list[str] = []
    notes: list[str] = []

    if prompts is not None:
        orphans = [p for p in prompts if not p.get("transcript_present")]
        if orphans:
            by_proj: Counter[str] = Counter()
            for p in orphans:
                by_proj[p.get("project_dir") or "(unknown)"] += 1
            notes.append(
                f"**Orphan prompts** (no surviving transcript): {len(orphans)}\n"
            )
            for proj, n in by_proj.most_common():
                notes.append(f"  - {n} in `{proj}`\n")
            notes.append("\n")

    multi_version = [s for s in sessions if len(s.get("claude_versions") or []) > 1]
    if multi_version:
        notes.append(f"**Sessions spanning multiple Claude versions:** "
                     f"{len(multi_version)}\n")
        for s in multi_version:
            notes.append(f"  - `{s.get('session_id')}` "
                         f"({', '.join(s['claude_versions'])})\n")
        notes.append("\n")

    multi_cwd = [s for s in sessions if len(s.get("all_cwds_seen") or []) > 1]
    if multi_cwd:
        notes.append(f"**Sessions that changed cwd mid-run:** {len(multi_cwd)}\n")
        for s in multi_cwd:
            notes.append(f"  - `{s.get('session_id')}` "
                         f"({', '.join(s['all_cwds_seen'])})\n")
        notes.append("\n")

    if not notes:
        return ""
    out.append(section("Anomalies"))
    out.extend(notes)
    return "".join(out)


def build_report(sessions: list[dict[str, Any]],
                 prompts: list[dict[str, Any]] | None,
                 order: str,
                 top: int,
                 include_exchanges: bool,
                 full: bool = False,
                 pricing: dict[str, Any] | None = None,
                 cowork_index: dict[str, dict[str, Any]] | None = None,
                 title: str = "Claude usage report") -> str:
    parts: list[str] = [f"# {title}\n\n"]
    parts.append(f"_Generated {fmt_ts(datetime.now(tz=timezone.utc))}_\n\n")

    if pricing is not None:
        _, _, unpriced_total = cost_for_tokens_by_model(
            _sum_tokens_by_model(sessions), pricing)
        parts.append(render_pricing_notes(pricing, unpriced_total))

    parts.append(render_headline(sessions, prompts, pricing=pricing))
    parts.append(render_tool_ranking(sessions, top))

    if order == "chronological":
        parts.append(render_chronological(sessions, include_exchanges,
                                          full=full, pricing=pricing,
                                          cowork_index=cowork_index))
        if prompts is not None:
            parts.append(render_chronological_prompts(prompts))
    else:
        parts.append(render_by_project(sessions, top, include_exchanges,
                                       full=full, pricing=pricing,
                                       cowork_index=cowork_index))

    parts.append(render_anomalies(sessions, prompts))
    return "".join(parts)


# ---------------------------------------------------------------------------
# HTML rendering — same data sources as the markdown renderers above, but
# emits a self-contained HTML document. CSS and JS are inlined; no external
# resources are fetched, so the report can be archived alongside the JSONL
# evidence and opened offline.
# ---------------------------------------------------------------------------

_HTML_CSS = """
:root {
  --bg:#fff; --fg:#222; --muted:#666; --border:#ddd; --accent:#0366d6;
  --warn:#b58900; --danger:#c0392b; --code-bg:#f6f8fa;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#0d1117; --fg:#c9d1d9; --muted:#8b949e; --border:#30363d;
    --accent:#58a6ff; --warn:#d29922; --danger:#f85149; --code-bg:#161b22;
  }
}
* { box-sizing: border-box; }
body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: var(--bg); color: var(--fg);
       margin: 0; padding: 2rem; max-width: 1200px; margin-inline: auto; }
h1 { margin-top: 0; }
h2 { border-bottom: 1px solid var(--border); padding-bottom: .25rem; margin-top: 2.5rem; }
h3 { margin-top: 1.5rem; }
h4 { margin-top: 1rem; color: var(--muted); }
a { color: var(--accent); }
.muted { color: var(--muted); }
.cost { font-weight: 600; color: var(--accent); }
.warn { color: var(--warn); }
.danger { color: var(--danger); }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
th, td { padding: .35rem .6rem; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }
th { background: var(--code-bg); position: sticky; top: 0; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
code { font: 12px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
       background: var(--code-bg); padding: .1rem .3rem; border-radius: 3px; }
pre { background: var(--code-bg); padding: .6rem .8rem; border-radius: 4px;
      overflow-x: auto; white-space: pre-wrap; word-break: break-word;
      font: 12px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px,1fr));
        gap: .8rem; margin: 1rem 0; }
.card { padding: .8rem; border: 1px solid var(--border); border-radius: 4px; }
.card .label { color: var(--muted); font-size: 11px; text-transform: uppercase;
               letter-spacing: .05em; }
.card .value { font-size: 1.3rem; font-weight: 500; margin-top: .2rem;
               font-variant-numeric: tabular-nums; }
.session { border: 1px solid var(--border); border-radius: 4px; margin: .5rem 0; }
.session > summary { padding: .55rem .8rem; cursor: pointer; user-select: none;
                     list-style: none; display: flex; gap: .8rem;
                     align-items: center; flex-wrap: wrap; }
.session > summary::-webkit-details-marker { display: none; }
.session > summary::before { content: "▸"; color: var(--muted); width: 1ch; }
.session[open] > summary::before { content: "▾"; }
.session > summary:hover { background: var(--code-bg); }
.session-body { padding: .8rem; border-top: 1px solid var(--border); }
.exchange { margin: 1rem 0; padding-left: .8rem; border-left: 3px solid var(--accent); }
.exchange-meta { color: var(--muted); font-size: 12px; margin-bottom: .3rem; }
.exchange .q-label, .exchange .a-label { font-weight: 600; margin-top: .5rem; }
.anomaly { border-left: 3px solid var(--warn); padding: .5rem .8rem;
           background: var(--code-bg); margin: .5rem 0; }
.search { width: 100%; padding: .55rem .8rem; font: inherit;
          border: 1px solid var(--border); border-radius: 4px;
          background: var(--bg); color: var(--fg); margin-bottom: 1.5rem; }
.toc { background: var(--code-bg); padding: .6rem 1rem; border-radius: 4px;
       margin-bottom: 1.5rem; }
.toc ul { margin: .3rem 0; padding-left: 1.2rem; }
"""

_HTML_JS = """
const q = document.getElementById('search');
if (q) {
  q.addEventListener('input', (e) => {
    const needle = e.target.value.toLowerCase().trim();
    document.querySelectorAll('[data-searchable]').forEach(el => {
      const hay = (el.dataset.searchText || el.textContent).toLowerCase();
      el.style.display = !needle || hay.includes(needle) ? '' : 'none';
    });
  });
}
"""


def _esc(s: Any) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def _html_head(title: str) -> str:
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        f"<title>{_esc(title)}</title>\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<style>{_HTML_CSS}</style>\n</head>\n<body>\n"
    )


def _html_tail() -> str:
    return f"<script>{_HTML_JS}</script>\n</body>\n</html>\n"


def _card(label: str, value: str) -> str:
    return (f'<div class="card"><div class="label">{_esc(label)}</div>'
            f'<div class="value">{_esc(value)}</div></div>')


def render_html_headline(sessions: list[dict[str, Any]],
                         prompts: list[dict[str, Any]] | None,
                         pricing: dict[str, Any] | None) -> str:
    starts = [parse_ts(s.get("start_time")) for s in sessions]
    ends = [parse_ts(s.get("end_time")) for s in sessions]
    starts = [t for t in starts if t]; ends = [t for t in ends if t]
    projects = {s.get("project_dir") for s in sessions if s.get("project_dir")}
    total_user = sum(s.get("num_user_messages", 0) for s in sessions)
    total_tool_calls = sum(sum((s.get("tools_used") or {}).values()) for s in sessions)
    tok_total: Counter[str] = Counter()
    for s in sessions:
        for k, v in (s.get("tokens") or {}).items():
            if isinstance(v, int):
                tok_total[k] += v

    out = ['<h2>Overview</h2>\n<div class="grid">\n']
    out.append(_card("Date range",
        f"{fmt_ts(min(starts) if starts else None)} → "
        f"{fmt_ts(max(ends) if ends else None)}"))
    out.append(_card("Sessions", f"{len(sessions):,}"))
    out.append(_card("User prompts", f"{total_user:,}"))
    if prompts is not None:
        orphans = sum(1 for p in prompts if not p.get("transcript_present"))
        out.append(_card("Prompts in history",
                         f"{len(prompts):,} ({orphans} orphan)"))
    out.append(_card("Tool calls", f"{total_tool_calls:,}"))
    out.append(_card("Projects", f"{len(projects):,}"))
    if tok_total:
        out.append(_card("Input tokens", f"{tok_total['input']:,}"))
        out.append(_card("Output tokens", f"{tok_total['output']:,}"))
        out.append(_card("Cache read", f"{tok_total['cache_read']:,}"))
        out.append(_card("Cache write (1h)", f"{tok_total['cache_write_1h']:,}"))
    if pricing is not None:
        tbm = _sum_tokens_by_model(sessions)
        total_cost, per_model, _ = cost_for_tokens_by_model(tbm, pricing)
        currency = pricing.get("currency", "USD")
        out.append(_card("Estimated spend", fmt_money(total_cost, currency)))
    out.append("</div>\n")
    return "".join(out)


def render_html_pricing_notes(pricing: dict[str, Any],
                              unpriced: dict[str, dict[str, int]]) -> str:
    out = ['<h2>Pricing notes</h2>\n']
    out.append('<p class="muted">Cost figures are estimates computed locally '
               'from token counts in the transcripts. Authoritative billing '
               'comes from the Anthropic Console, not from this report.</p>\n')
    out.append("<ul>")
    out.append(f"<li><strong>Effective date:</strong> {_esc(pricing.get('effective_date', '—'))}</li>")
    out.append(f"<li><strong>Currency:</strong> {_esc(pricing.get('currency', 'USD'))}</li>")
    note = pricing.get("_note")
    if note:
        out.append(f"<li><strong>Note:</strong> {_esc(note)}</li>")
    models = sorted((pricing.get("models") or {}).keys())
    if models:
        out.append("<li><strong>Priced models:</strong> "
                   + ", ".join(f"<code>{_esc(m)}</code>" for m in models)
                   + "</li>")
    if unpriced:
        out.append('<li class="warn"><strong>Unpriced models seen in data</strong> '
                   "(excluded from cost): "
                   + ", ".join(f"<code>{_esc(m)}</code>" for m in sorted(unpriced))
                   + "</li>")
    out.append("</ul>\n")
    return "".join(out)


def render_html_tool_ranking(sessions: list[dict[str, Any]], top: int) -> str:
    counts: Counter[str] = Counter()
    sess_with: defaultdict[str, set[str]] = defaultdict(set)
    for s in sessions:
        sid = s.get("session_id", "")
        for name, n in (s.get("tools_used") or {}).items():
            counts[name] += n
            sess_with[name].add(sid)
    if not counts:
        return ""
    rows = "".join(
        f"<tr><td><code>{_esc(name)}</code></td>"
        f"<td class=\"num\">{n:,}</td>"
        f"<td class=\"num\">{len(sess_with[name]):,}</td></tr>"
        for name, n in counts.most_common(top))
    return ('<h2>Top tools used</h2>\n'
            '<table><thead><tr><th>Tool</th><th class="num">Calls</th>'
            '<th class="num">Sessions</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>\n')


def _cowork_tag_html(s: dict[str, Any],
                     cowork_index: dict[str, dict[str, Any]] | None) -> str:
    if not cowork_index:
        return ""
    cw = cowork_index.get(s.get("session_id") or "")
    if not cw:
        return ""
    title = _esc(cw.get("title", ""))
    archived = ' <span class="warn">[archived]</span>' if cw.get("isArchived") else ""
    return f' <span class="muted">· Cowork: "{title}"{archived}</span>'


def _agent_space_tag_html(s: dict[str, Any]) -> str:
    parts: list[str] = []
    name = s.get("agent_space_name")
    if isinstance(name, str) and name and name != s.get("project_dir"):
        parts.append(f'space: "{_esc(name)}"')
    folders = s.get("agent_space_folders") or []
    if folders and isinstance(folders[0], str):
        parts.append(f"folder: <code>{_esc(folders[0])}</code>")
    if not parts:
        return ""
    return ' <span class="muted">· ' + " · ".join(parts) + "</span>"


def _html_session_summary(s: dict[str, Any], pricing: dict[str, Any] | None,
                          cowork_index: dict[str, dict[str, Any]] | None = None) -> str:
    start = parse_ts(s.get("start_time"))
    end = parse_ts(s.get("end_time"))
    tool_calls = sum((s.get("tools_used") or {}).values())
    parts = [
        f'<code>{_esc((s.get("session_id") or "")[:8])}…</code>',
        f'<span class="muted">{_esc(fmt_ts(start))}</span>',
        f'<span class="muted">({_esc(fmt_duration(start, end))})</span>',
        f'<span>{s.get("num_user_messages", 0)} prompts</span>',
        f'<span>{tool_calls} tool calls</span>',
        f'<span class="muted">{_esc(s.get("project_dir", ""))}</span>',
    ]
    if pricing is not None:
        cost, _, _ = cost_for_tokens_by_model(
            s.get("tokens_by_model") or {}, pricing)
        currency = pricing.get("currency", "USD")
        parts.append(f'<span class="cost">{_esc(fmt_money(cost, currency))}</span>')
    tag = _cowork_tag_html(s, cowork_index)
    if tag:
        parts.append(tag)
    space_tag = _agent_space_tag_html(s)
    if space_tag:
        parts.append(space_tag)
    return " ".join(parts)


def _html_exchanges(s: dict[str, Any]) -> str:
    out = []
    for i, ex in enumerate(s.get("exchanges") or [], 1):
        t = parse_ts(ex.get("timestamp"))
        out.append(f'<div class="exchange">')
        out.append(f'<div class="exchange-meta">#{i} · {_esc(fmt_ts(t))}</div>')
        out.append('<div class="q-label">Question</div>')
        out.append(f'<pre>{_esc(ex.get("user_prompt") or "")}</pre>')
        a = ex.get("assistant_response") or ""
        if a:
            out.append('<div class="a-label">Answer</div>')
            out.append(f'<pre>{_esc(a)}</pre>')
        tools = ex.get("tools_called") or []
        if tools:
            names = ", ".join(
                f'<code>{_esc(t.get("name"))}</code>'
                for t in tools if t.get("name"))
            out.append(f'<div class="exchange-meta">Tools: {names}</div>')
        out.append("</div>")
    return "".join(out)


def _session_search_text(s: dict[str, Any]) -> str:
    """Concatenate session-level text the search box should match against."""
    parts = [s.get("session_id") or "", s.get("project_dir") or ""]
    for ex in s.get("exchanges") or []:
        parts.append(ex.get("user_prompt") or "")
        parts.append(ex.get("assistant_response") or "")
    return " ".join(parts)


def _html_session_card(s: dict[str, Any], pricing: dict[str, Any] | None,
                       include_exchanges: bool, open_by_default: bool,
                       cowork_index: dict[str, dict[str, Any]] | None = None) -> str:
    summary = _html_session_summary(s, pricing, cowork_index)
    search = _esc(_session_search_text(s))
    open_attr = " open" if open_by_default else ""
    body_parts = []
    toks = s.get("tokens") or {}
    if toks:
        body_parts.append(
            '<p class="muted">'
            f"in={toks.get('input',0):,} · out={toks.get('output',0):,} · "
            f"cache-read={toks.get('cache_read',0):,} · "
            f"cache-write-5m={toks.get('cache_write_5m',0):,} · "
            f"cache-write-1h={toks.get('cache_write_1h',0):,}"
            "</p>")
    tt = s.get("tools_used") or {}
    if tt:
        items = ", ".join(f"<code>{_esc(n)}</code> ({c})"
                          for n, c in sorted(tt.items(),
                                             key=lambda kv: -kv[1])[:8])
        body_parts.append(f'<p class="muted">Tools: {items}</p>')
    if include_exchanges:
        body_parts.append(_html_exchanges(s))
    body = "".join(body_parts) or '<p class="muted">No further detail.</p>'
    return (f'<details class="session" data-searchable data-search-text="{search}"'
            f'{open_attr}>'
            f'<summary>{summary}</summary>'
            f'<div class="session-body">{body}</div>'
            '</details>')


def render_html_by_project(sessions: list[dict[str, Any]],
                           top: int, include_exchanges: bool,
                           pricing: dict[str, Any] | None,
                           cowork_index: dict[str, dict[str, Any]] | None = None) -> str:
    by_project: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in sessions:
        by_project[s.get("project_dir") or "(unknown)"].append(s)
    # Match render_by_project: all-single-session inputs sort chronologically;
    # otherwise rank by activity.
    if all(len(ss) == 1 for ss in by_project.values()):
        ranked = sorted(by_project.items(),
                        key=lambda kv: kv[1][0].get("start_time") or "")
    else:
        ranked = sorted(by_project.items(),
                        key=lambda kv: (
                            -len(kv[1]),
                            -sum(x.get("num_user_messages", 0) for x in kv[1]),
                            min((x.get("start_time") or "") for x in kv[1]),
                        ))
    out = ['<h2>Projects</h2>\n']
    for project, ss in ranked:
        ss = sorted(ss, key=lambda x: x.get("start_time") or "")
        tool_tot: Counter[str] = Counter()
        for x in ss:
            tool_tot.update(x.get("tools_used") or {})
        prompts = sum(x.get("num_user_messages", 0) for x in ss)
        tool_calls = sum(sum((x.get("tools_used") or {}).values()) for x in ss)
        cost_line = ""
        if pricing is not None:
            tbm = _sum_tokens_by_model(ss)
            cost, _, _ = cost_for_tokens_by_model(tbm, pricing)
            currency = pricing.get("currency", "USD")
            cost_line = f' · <span class="cost">{_esc(fmt_money(cost, currency))}</span>'
        starts = [parse_ts(x.get("start_time")) for x in ss]
        ends = [parse_ts(x.get("end_time")) for x in ss]
        starts = [t for t in starts if t]; ends = [t for t in ends if t]
        out.append(f'<section data-searchable data-search-text="{_esc(project)}">\n')
        out.append(f'<h3><code>{_esc(project)}</code></h3>\n')
        out.append(f'<p class="muted">{len(ss)} sessions · {prompts} prompts · '
                   f'{tool_calls} tool calls{cost_line}<br>'
                   f'First {_esc(fmt_ts(min(starts) if starts else None))} · '
                   f'Last {_esc(fmt_ts(max(ends) if ends else None))}</p>\n')
        for s in ss:
            out.append(_html_session_card(s, pricing, include_exchanges,
                                          open_by_default=False,
                                          cowork_index=cowork_index))
        out.append("</section>\n")
    return "".join(out)


def render_html_chronological(sessions: list[dict[str, Any]],
                              include_exchanges: bool,
                              pricing: dict[str, Any] | None,
                              cowork_index: dict[str, dict[str, Any]] | None = None) -> str:
    ordered = sorted(sessions, key=lambda s: s.get("start_time") or "")
    out = ['<h2>Sessions in chronological order</h2>\n']
    for s in ordered:
        out.append(_html_session_card(s, pricing, include_exchanges,
                                      open_by_default=False,
                                      cowork_index=cowork_index))
    return "".join(out)


def render_html_chronological_prompts(prompts: list[dict[str, Any]]) -> str:
    ordered = sorted(prompts, key=lambda p: (p.get("timestamp") or 0))
    out = ['<h2>Prompt timeline (history.jsonl)</h2>\n',
           '<table><thead><tr>'
           '<th>Time</th><th>Project</th><th>Session</th>'
           '<th>Transcript</th><th>Prompt</th></tr></thead><tbody>\n']
    for p in ordered:
        t = parse_ts(p.get("timestamp"))
        sid = (p.get("session_id") or "")[:8]
        present = ("✓" if p.get("transcript_present")
                   else '<span class="warn">✗</span>')
        display = p.get("display") or ""
        search = _esc(display + " " + (p.get("project_dir") or ""))
        out.append(
            f'<tr data-searchable data-search-text="{search}">'
            f'<td class="muted">{_esc(fmt_ts(t))}</td>'
            f'<td><code>{_esc(p.get("project_dir") or "")}</code></td>'
            f'<td><code>{_esc(sid)}…</code></td>'
            f'<td class="num">{present}</td>'
            f'<td>{_esc(truncate(display, PROMPT_PREVIEW_CHARS))}</td></tr>\n')
    out.append("</tbody></table>\n")
    return "".join(out)


def render_html_anomalies(sessions: list[dict[str, Any]],
                          prompts: list[dict[str, Any]] | None) -> str:
    blocks: list[str] = []
    if prompts is not None:
        orphans = [p for p in prompts if not p.get("transcript_present")]
        if orphans:
            by_proj: Counter[str] = Counter()
            for p in orphans:
                by_proj[p.get("project_dir") or "(unknown)"] += 1
            items = "".join(
                f"<li>{n} in <code>{_esc(proj)}</code></li>"
                for proj, n in by_proj.most_common())
            blocks.append(f'<div class="anomaly">'
                          f'<strong>Orphan prompts</strong> '
                          f'(no surviving transcript): {len(orphans)}'
                          f'<ul>{items}</ul></div>')
    mv = [s for s in sessions if len(s.get("claude_versions") or []) > 1]
    if mv:
        items = "".join(
            f'<li><code>{_esc(s.get("session_id"))}</code> '
            f'({_esc(", ".join(s["claude_versions"]))})</li>'
            for s in mv)
        blocks.append('<div class="anomaly">'
                      f'<strong>Sessions spanning multiple Claude versions:</strong> '
                      f'{len(mv)}<ul>{items}</ul></div>')
    mc = [s for s in sessions if len(s.get("all_cwds_seen") or []) > 1]
    if mc:
        items = "".join(
            f'<li><code>{_esc(s.get("session_id"))}</code> '
            f'({_esc(", ".join(s["all_cwds_seen"]))})</li>'
            for s in mc)
        blocks.append('<div class="anomaly">'
                      f'<strong>Sessions that changed cwd mid-run:</strong> '
                      f'{len(mc)}<ul>{items}</ul></div>')
    if not blocks:
        return ""
    return "<h2>Anomalies</h2>\n" + "".join(blocks)


def _top_projects(sessions: list[dict[str, Any]], n: int,
                  pricing: dict[str, Any] | None
                  ) -> list[tuple[str, int, int, dict[str, int], float]]:
    """Return [(project_dir, n_sessions, n_user_msgs, summed_tokens, cost)]."""
    by_project: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in sessions:
        by_project[s.get("project_dir") or "(unknown)"].append(s)
    rows: list[tuple[str, int, int, dict[str, int], float]] = []
    for project, ss in by_project.items():
        toks: dict[str, int] = {}
        for x in ss:
            for k, v in (x.get("tokens") or {}).items():
                if isinstance(v, int):
                    toks[k] = toks.get(k, 0) + v
        cost = 0.0
        if pricing is not None:
            cost, _, _ = cost_for_tokens_by_model(_sum_tokens_by_model(ss), pricing)
        prompts = sum(x.get("num_user_messages", 0) for x in ss)
        rows.append((project, len(ss), prompts, toks, cost))
    key = (lambda r: r[4]) if pricing is not None \
          else (lambda r: r[3].get("input", 0) + r[3].get("output", 0))
    rows.sort(key=key, reverse=True)
    return rows[:n]


def _top_sessions(sessions: list[dict[str, Any]], n: int,
                  pricing: dict[str, Any] | None
                  ) -> list[tuple[dict[str, Any], float]]:
    """Return [(session_record, cost)] sorted desc by cost (or token volume)."""
    rows: list[tuple[dict[str, Any], float]] = []
    for s in sessions:
        cost = 0.0
        if pricing is not None:
            cost, _, _ = cost_for_tokens_by_model(
                s.get("tokens_by_model") or {}, pricing)
        rows.append((s, cost))
    if pricing is not None:
        rows.sort(key=lambda r: r[1], reverse=True)
    else:
        rows.sort(
            key=lambda r: ((r[0].get("tokens") or {}).get("input", 0)
                           + (r[0].get("tokens") or {}).get("output", 0)),
            reverse=True)
    return rows[:n]


def _retention_stats(prompts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Compute date ranges of orphan vs surviving prompts from prompts.jsonl.

    The split between the two is the empirical evidence of Claude Code's
    transcript rotation: orphans are entries whose per-session transcript
    has been deleted by the periodic cleanup, leaving only the prompt
    text in `history.jsonl`. Returns None if both groups are empty.
    """
    orphan_ts: list[datetime] = []
    present_ts: list[datetime] = []
    for p in prompts:
        ts = parse_ts(p.get("timestamp"))
        if ts is None:
            continue
        (orphan_ts if not p.get("transcript_present") else present_ts).append(ts)
    if not orphan_ts and not present_ts:
        return None
    gap_days: int | None = None
    if orphan_ts and present_ts:
        gap_days = int((min(present_ts) - max(orphan_ts)).total_seconds() // 86400)
    return {
        "orphan_count": len(orphan_ts),
        "orphan_oldest": min(orphan_ts) if orphan_ts else None,
        "orphan_newest": max(orphan_ts) if orphan_ts else None,
        "present_count": len(present_ts),
        "present_oldest": min(present_ts) if present_ts else None,
        "present_newest": max(present_ts) if present_ts else None,
        "gap_days": gap_days,
    }


def render_retention_md(prompts: list[dict[str, Any]] | None,
                        last_cleanup: str | None) -> str:
    """Markdown 'Retention' section for the executive summary."""
    if not prompts and not last_cleanup:
        return ""
    stats = _retention_stats(prompts or [])
    out = [section("Retention")]
    out.append(
        "Claude Code rotates per-session transcripts on a periodic schedule, "
        "leaving the prompt text in `history.jsonl` but deleting the matching "
        "`projects/<encoded-cwd>/*.jsonl`. Anything older than the retention "
        "window survives only as orphan prompts — Q/A pairs, tool calls, and "
        "token counts for those sessions are unrecoverable from this snapshot.\n\n"
    )
    if last_cleanup:
        out.append(f"- **Last cleanup recorded:** {last_cleanup} "
                   f"(from `.claude/.last-cleanup`)\n")
    if stats is None:
        out.append("- No prompts available to compute retention boundaries.\n\n")
        return "".join(out)
    out.append(f"- **Orphan prompts** (transcript deleted): "
               f"{stats['orphan_count']:,}\n")
    if stats["orphan_oldest"]:
        out.append(f"  - Oldest: {fmt_ts(stats['orphan_oldest'])}\n")
    if stats["orphan_newest"]:
        out.append(f"  - Newest: {fmt_ts(stats['orphan_newest'])}\n")
    out.append(f"- **Surviving transcripts** (prompt + transcript both present): "
               f"{stats['present_count']:,}\n")
    if stats["present_oldest"]:
        out.append(f"  - Oldest: {fmt_ts(stats['present_oldest'])}\n")
    if stats["present_newest"]:
        out.append(f"  - Newest: {fmt_ts(stats['present_newest'])}\n")
    if stats["gap_days"] is not None:
        out.append(f"- **Gap** between newest orphan and oldest survivor: "
                   f"**{stats['gap_days']} days** "
                   f"(implies a retention window roughly this size or smaller)\n")
    out.append("\n")
    return "".join(out)


def render_retention_html(prompts: list[dict[str, Any]] | None,
                          last_cleanup: str | None) -> str:
    if not prompts and not last_cleanup:
        return ""
    stats = _retention_stats(prompts or [])
    out = ["<h2>Retention</h2>\n"]
    out.append(
        '<p class="muted">Claude Code rotates per-session transcripts on a '
        "periodic schedule, leaving the prompt text in <code>history.jsonl</code>"
        " but deleting the matching <code>projects/&lt;encoded-cwd&gt;/*.jsonl</code>. "
        "Anything older than the retention window survives only as orphan "
        "prompts — Q/A pairs, tool calls, and token counts for those sessions "
        "are unrecoverable from this snapshot.</p>\n"
    )
    out.append("<ul>")
    if last_cleanup:
        out.append(f"<li><strong>Last cleanup recorded:</strong> "
                   f"<code>{_esc(last_cleanup)}</code> "
                   f"(from <code>.claude/.last-cleanup</code>)</li>")
    if stats is None:
        out.append("<li>No prompts available to compute retention boundaries.</li>")
        out.append("</ul>\n")
        return "".join(out)
    out.append(f"<li><strong>Orphan prompts</strong> (transcript deleted): "
               f"{stats['orphan_count']:,}")
    if stats["orphan_oldest"]:
        out.append(f"<ul><li>Oldest: {_esc(fmt_ts(stats['orphan_oldest']))}</li>"
                   f"<li>Newest: {_esc(fmt_ts(stats['orphan_newest']))}</li></ul>")
    out.append("</li>")
    out.append(f"<li><strong>Surviving transcripts</strong>: "
               f"{stats['present_count']:,}")
    if stats["present_oldest"]:
        out.append(f"<ul><li>Oldest: {_esc(fmt_ts(stats['present_oldest']))}</li>"
                   f"<li>Newest: {_esc(fmt_ts(stats['present_newest']))}</li></ul>")
    out.append("</li>")
    if stats["gap_days"] is not None:
        out.append(f"<li><strong>Gap</strong> between newest orphan and oldest "
                   f"survivor: <strong>{stats['gap_days']} days</strong> "
                   f"(implies a retention window roughly this size or smaller)</li>")
    out.append("</ul>\n")
    return "".join(out)


def build_summary_md(sessions: list[dict[str, Any]],
                     prompts: list[dict[str, Any]] | None,
                     pricing: dict[str, Any] | None,
                     paste_count: int | None = None,
                     file_history_count: int | None = None,
                     last_cleanup: str | None = None,
                     title: str = "Claude usage — executive summary") -> str:
    """One-page exec summary in Markdown."""
    parts = [f"# {title}\n\n"]
    parts.append(f"_Generated {fmt_ts(datetime.now(tz=timezone.utc))}_\n\n")
    if pricing is not None:
        _, _, unpriced = cost_for_tokens_by_model(
            _sum_tokens_by_model(sessions), pricing)
        parts.append(render_pricing_notes(pricing, unpriced))
    parts.append(render_headline(sessions, prompts, pricing=pricing))

    rank_basis = "estimated spend" if pricing is not None else "token volume"
    parts.append(section(f"Top 5 projects (by {rank_basis})"))
    parts.append("| Project | Sessions | Prompts | Tokens (in+out) | Cost |\n"
                 "|---|---:|---:|---:|---:|\n")
    for project, n_sess, n_prompts, toks, cost in _top_projects(sessions, 5, pricing):
        total_tok = toks.get("input", 0) + toks.get("output", 0)
        cost_cell = fmt_money(cost, pricing.get("currency", "USD")) if pricing else "—"
        parts.append(f"| `{md_escape(project)}` | {n_sess} | {n_prompts} | "
                     f"{total_tok:,} | {cost_cell} |\n")
    parts.append("\n")

    parts.append(section(f"Top 20 sessions (by {rank_basis})"))
    parts.append("| Session | Project | Start | Prompts | Tokens (in+out) | Cost |\n"
                 "|---|---|---|---:|---:|---:|\n")
    for s, cost in _top_sessions(sessions, 20, pricing):
        toks = s.get("tokens") or {}
        total_tok = toks.get("input", 0) + toks.get("output", 0)
        cost_cell = fmt_money(cost, pricing.get("currency", "USD")) if pricing else "—"
        parts.append(
            f"| `{(s.get('session_id') or '')[:8]}…` "
            f"| `{md_escape(s.get('project_dir', ''))}` "
            f"| {fmt_ts(parse_ts(s.get('start_time')))} "
            f"| {s.get('num_user_messages', 0)} "
            f"| {total_tok:,} | {cost_cell} |\n")
    parts.append("\n")

    extras: list[str] = []
    if paste_count is not None:
        extras.append(f"- Paste-cache entries: **{paste_count:,}**")
    if file_history_count is not None:
        extras.append(f"- File-history records: **{file_history_count:,}**")
    if extras:
        parts.append(section("Auxiliary data"))
        parts.append("\n".join(extras) + "\n\n")

    parts.append(render_retention_md(prompts, last_cleanup))
    parts.append(render_anomalies(sessions, prompts))
    return "".join(parts)


def build_summary_html(sessions: list[dict[str, Any]],
                       prompts: list[dict[str, Any]] | None,
                       pricing: dict[str, Any] | None,
                       paste_count: int | None = None,
                       file_history_count: int | None = None,
                       last_cleanup: str | None = None,
                       title: str = "Claude usage — executive summary") -> str:
    """One-page exec summary in HTML."""
    out = [_html_head(title)]
    out.append(f'<h1>{_esc(title)}</h1>\n')
    out.append(f'<p class="muted">Generated {_esc(fmt_ts(datetime.now(tz=timezone.utc)))}</p>\n')
    if pricing is not None:
        _, _, unpriced = cost_for_tokens_by_model(
            _sum_tokens_by_model(sessions), pricing)
        out.append(render_html_pricing_notes(pricing, unpriced))
    out.append(render_html_headline(sessions, prompts, pricing))

    rank_basis = "estimated spend" if pricing is not None else "token volume"
    currency = pricing.get("currency", "USD") if pricing else "USD"

    out.append(f'<h2>Top 5 projects (by {_esc(rank_basis)})</h2>\n')
    out.append('<table><thead><tr><th>Project</th><th class="num">Sessions</th>'
               '<th class="num">Prompts</th><th class="num">Tokens (in+out)</th>'
               '<th class="num">Cost</th></tr></thead><tbody>\n')
    for project, n_sess, n_prompts, toks, cost in _top_projects(sessions, 5, pricing):
        total_tok = toks.get("input", 0) + toks.get("output", 0)
        cost_cell = fmt_money(cost, currency) if pricing is not None else "—"
        out.append(f'<tr><td><code>{_esc(project)}</code></td>'
                   f'<td class="num">{n_sess}</td><td class="num">{n_prompts}</td>'
                   f'<td class="num">{total_tok:,}</td>'
                   f'<td class="num cost">{_esc(cost_cell)}</td></tr>\n')
    out.append('</tbody></table>\n')

    out.append(f'<h2>Top 20 sessions (by {_esc(rank_basis)})</h2>\n')
    out.append('<table><thead><tr><th>Session</th><th>Project</th><th>Start</th>'
               '<th class="num">Prompts</th><th class="num">Tokens (in+out)</th>'
               '<th class="num">Cost</th></tr></thead><tbody>\n')
    for s, cost in _top_sessions(sessions, 20, pricing):
        toks = s.get("tokens") or {}
        total_tok = toks.get("input", 0) + toks.get("output", 0)
        cost_cell = fmt_money(cost, currency) if pricing is not None else "—"
        out.append(f'<tr><td><code>{_esc((s.get("session_id") or "")[:8])}…</code></td>'
                   f'<td><code>{_esc(s.get("project_dir", ""))}</code></td>'
                   f'<td>{_esc(fmt_ts(parse_ts(s.get("start_time"))))}</td>'
                   f'<td class="num">{s.get("num_user_messages", 0)}</td>'
                   f'<td class="num">{total_tok:,}</td>'
                   f'<td class="num cost">{_esc(cost_cell)}</td></tr>\n')
    out.append('</tbody></table>\n')

    if paste_count is not None or file_history_count is not None:
        out.append('<h2>Auxiliary data</h2>\n<ul>')
        if paste_count is not None:
            out.append(f'<li>Paste-cache entries: <strong>{paste_count:,}</strong></li>')
        if file_history_count is not None:
            out.append(f'<li>File-history records: <strong>{file_history_count:,}</strong></li>')
        out.append('</ul>\n')

    out.append(render_retention_html(prompts, last_cleanup))
    out.append(render_html_anomalies(sessions, prompts))
    out.append(_html_tail())
    return "".join(out)


def build_report_html(sessions: list[dict[str, Any]],
                      prompts: list[dict[str, Any]] | None,
                      order: str, top: int, include_exchanges: bool,
                      pricing: dict[str, Any] | None,
                      cowork_index: dict[str, dict[str, Any]] | None = None,
                      title: str = "Claude usage report") -> str:
    out = [_html_head(title)]
    out.append(f'<h1>{_esc(title)}</h1>\n')
    out.append(f'<p class="muted">Generated {_esc(fmt_ts(datetime.now(tz=timezone.utc)))}</p>\n')
    out.append('<input type="search" id="search" class="search" '
               'placeholder="Filter sessions, projects, and prompts…">\n')
    if pricing is not None:
        _, _, unpriced = cost_for_tokens_by_model(
            _sum_tokens_by_model(sessions), pricing)
        out.append(render_html_pricing_notes(pricing, unpriced))
    out.append(render_html_headline(sessions, prompts, pricing))
    out.append(render_html_tool_ranking(sessions, top))
    if order == "chronological":
        out.append(render_html_chronological(sessions, include_exchanges, pricing,
                                             cowork_index=cowork_index))
        if prompts is not None:
            out.append(render_html_chronological_prompts(prompts))
    else:
        out.append(render_html_by_project(sessions, top, include_exchanges, pricing,
                                          cowork_index=cowork_index))
    out.append(render_html_anomalies(sessions, prompts))
    out.append(_html_tail())
    return "".join(out)


# ---------------------------------------------------------------------------
# Per-session report mode — emits one self-contained markdown + one
# self-contained HTML file per session into a target directory. Useful when
# an investigator wants to share or archive a single session of interest
# without distributing the full transcript bundle.
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")
# Project_dir values for unspaced Cowork agent sessions are already prefixed
# with "YYYYmmDD-HHMMSS Cowork Chat "; stripping it before slugging avoids
# repeating the timestamp inside the filename.
_COWORK_PREFIX_RE = re.compile(r"^\d{8}-\d{6} Cowork Chat ")


def _filename_slug(s: str, max_len: int = 60) -> str:
    if not s:
        return ""
    slug = _SLUG_RE.sub("-", s).strip("-").lower()
    return slug[:max_len].rstrip("-")


def _per_session_filename(s: dict[str, Any], ext: str) -> str:
    """Sortable, unique, identifiable: YYYYMMDD-HHMMSS_<sid8>_<slug>.<ext>

    Cowork agent session ids start with `local_<uuid>` — strip the prefix
    before slicing so the file-name id is the recognisable UUID, not
    `local_c6` (the first 8 chars of `local_c61ca898…`).
    """
    start = parse_ts(s.get("start_time"))
    ts_part = start.strftime("%Y%m%d-%H%M%S") if start else "00000000-000000"
    sid = s.get("session_id") or "unknown"
    if sid.startswith("local_"):
        sid = sid[len("local_"):]
    sid8 = sid[:8]
    proj = _COWORK_PREFIX_RE.sub("", s.get("project_dir") or "")
    slug = _filename_slug(proj)
    parts = [ts_part, sid8]
    if slug:
        parts.append(slug)
    return "_".join(parts) + "." + ext


def _is_agent_record(s: dict[str, Any]) -> bool:
    return bool(s.get("agent_audit_file") or s.get("agent_owner_account_uuid"))


def _agent_meta_md(s: dict[str, Any]) -> str:
    """Render the agent-only metadata block as markdown; empty for CLI rows."""
    if not _is_agent_record(s):
        return ""
    out = [section("Cowork agent metadata")]
    for key, label in (
        ("agent_title", "Title"),
        ("agent_initial_message", "Initial message"),
        ("agent_owner_email", "Owner email"),
        ("agent_owner_account_name", "Account name"),
        ("agent_space_name", "Space"),
        ("agent_model_configured", "Model configured"),
        ("agent_is_archived", "Archived"),
        ("agent_memory_enabled", "Memory enabled"),
        ("agent_vm_cwd", "VM cwd"),
        ("agent_process_name", "Process name"),
    ):
        v = s.get(key)
        if v not in (None, "", [], False):
            out.append(f"- **{label}:** {v}\n")
    folders = s.get("agent_space_folders") or []
    if folders:
        out.append(f"- **Space folders:** "
                   + ", ".join(f"`{f}`" for f in folders) + "\n")
    egress = s.get("agent_egress_allowed_domains") or []
    if egress:
        out.append(f"- **Egress allow-list:** "
                   + ", ".join(f"`{d}`" for d in egress) + "\n")
    fetch = s.get("agent_web_fetch_allowed_urls") or []
    if fetch:
        out.append(f"- **Web-fetch allow-list:** "
                   + ", ".join(f"`{u}`" for u in fetch) + "\n")
    if s.get("agent_system_prompt_chars"):
        out.append(f"- **System prompt size:** "
                   f"{s['agent_system_prompt_chars']:,} chars\n")
    if s.get("reported_cost_usd") is not None:
        out.append(f"- **Reported cost (runtime):** "
                   f"${s['reported_cost_usd']:.4f}\n")
    if s.get("reported_num_turns") is not None:
        out.append(f"- **Reported turns (runtime):** "
                   f"{s['reported_num_turns']}\n")
    out.append("\n")
    return "".join(out)


def render_single_session_md(s: dict[str, Any],
                             pricing: dict[str, Any] | None) -> str:
    """One self-contained markdown report for a single session."""
    start = parse_ts(s.get("start_time"))
    end = parse_ts(s.get("end_time"))
    title = (s.get("agent_title")
             if _is_agent_record(s) and s.get("agent_title")
             else (s.get("project_dir") or s.get("session_id") or "Session"))

    out = [f"# {title}\n\n", f"_{fmt_ts(start)}_\n\n"]

    out.append("| | |\n|---|---|\n")
    out.append(f"| Session ID | `{s.get('session_id', '')}` |\n")
    out.append(f"| Project | `{md_escape(s.get('project_dir') or '')}` |\n")
    out.append(f"| Start | {fmt_ts(start)} |\n")
    out.append(f"| End | {fmt_ts(end)} |\n")
    out.append(f"| Duration | {fmt_duration(start, end)} |\n")
    out.append(f"| User prompts | {s.get('num_user_messages', 0)} |\n")
    out.append(f"| Assistant turns | {s.get('num_assistant_messages', 0)} |\n")
    models = s.get("models_used") or []
    if models:
        out.append(f"| Models | {', '.join(f'`{m}`' for m in models)} |\n")
    tools = s.get("tools_used") or {}
    if tools:
        out.append(f"| Tool calls | {sum(tools.values())} |\n")
    toks = s.get("tokens") or {}
    if toks:
        out.append(
            f"| Tokens (in / out / cache-read / cache-write-1h) | "
            f"{toks.get('input', 0):,} / {toks.get('output', 0):,} / "
            f"{toks.get('cache_read', 0):,} / "
            f"{toks.get('cache_write_1h', 0):,} |\n"
        )
    if pricing is not None:
        cost, _, _ = cost_for_tokens_by_model(
            s.get("tokens_by_model") or {}, pricing)
        currency = pricing.get("currency", "USD")
        out.append(f"| Estimated cost | {fmt_money(cost, currency)} |\n")
    out.append("\n")

    out.append(_agent_meta_md(s))

    if tools:
        out.append(section("Tools"))
        out.append("| Tool | Calls |\n|---|---:|\n")
        for name, n in sorted(tools.items(), key=lambda kv: -kv[1]):
            out.append(f"| `{md_escape(name)}` | {n:,} |\n")
        out.append("\n")

    out.append(section("Exchanges"))
    out.append(render_session_exchanges(s, full=True))
    return "".join(out)


def _agent_meta_html(s: dict[str, Any]) -> str:
    if not _is_agent_record(s):
        return ""
    rows: list[str] = []
    for key, label in (
        ("agent_title", "Title"),
        ("agent_initial_message", "Initial message"),
        ("agent_owner_email", "Owner email"),
        ("agent_owner_account_name", "Account name"),
        ("agent_space_name", "Space"),
        ("agent_model_configured", "Model configured"),
        ("agent_is_archived", "Archived"),
        ("agent_memory_enabled", "Memory enabled"),
        ("agent_vm_cwd", "VM cwd"),
        ("agent_process_name", "Process name"),
    ):
        v = s.get(key)
        if v not in (None, "", [], False):
            rows.append(f"<li><strong>{_esc(label)}:</strong> {_esc(v)}</li>")
    folders = s.get("agent_space_folders") or []
    if folders:
        rows.append("<li><strong>Space folders:</strong> "
                    + ", ".join(f"<code>{_esc(f)}</code>" for f in folders)
                    + "</li>")
    egress = s.get("agent_egress_allowed_domains") or []
    if egress:
        rows.append("<li><strong>Egress allow-list:</strong> "
                    + ", ".join(f"<code>{_esc(d)}</code>" for d in egress)
                    + "</li>")
    fetch = s.get("agent_web_fetch_allowed_urls") or []
    if fetch:
        rows.append("<li><strong>Web-fetch allow-list:</strong> "
                    + ", ".join(f"<code>{_esc(u)}</code>" for u in fetch)
                    + "</li>")
    if s.get("agent_system_prompt_chars"):
        rows.append(f"<li><strong>System prompt size:</strong> "
                    f"{s['agent_system_prompt_chars']:,} chars</li>")
    if s.get("reported_cost_usd") is not None:
        rows.append(f"<li><strong>Reported cost (runtime):</strong> "
                    f"${s['reported_cost_usd']:.4f}</li>")
    if s.get("reported_num_turns") is not None:
        rows.append(f"<li><strong>Reported turns (runtime):</strong> "
                    f"{s['reported_num_turns']}</li>")
    if not rows:
        return ""
    return "<h2>Cowork agent metadata</h2>\n<ul>" + "".join(rows) + "</ul>\n"


def render_single_session_html(s: dict[str, Any],
                               pricing: dict[str, Any] | None) -> str:
    """One self-contained HTML report for a single session."""
    start = parse_ts(s.get("start_time"))
    end = parse_ts(s.get("end_time"))
    title = (s.get("agent_title")
             if _is_agent_record(s) and s.get("agent_title")
             else (s.get("project_dir") or s.get("session_id") or "Session"))

    out = [_html_head(title)]
    out.append(f"<h1>{_esc(title)}</h1>\n")
    out.append(f'<p class="muted">{_esc(fmt_ts(start))}</p>\n')

    out.append('<div class="grid">\n')
    out.append(_card("Session ID", (s.get("session_id") or "")[:36]))
    out.append(_card("Project", s.get("project_dir") or ""))
    out.append(_card("Started", fmt_ts(start)))
    out.append(_card("Ended", fmt_ts(end)))
    out.append(_card("Duration", fmt_duration(start, end)))
    out.append(_card("User prompts", f"{s.get('num_user_messages', 0):,}"))
    tools = s.get("tools_used") or {}
    if tools:
        out.append(_card("Tool calls", f"{sum(tools.values()):,}"))
    toks = s.get("tokens") or {}
    if toks:
        out.append(_card("Input tokens", f"{toks.get('input', 0):,}"))
        out.append(_card("Output tokens", f"{toks.get('output', 0):,}"))
        out.append(_card("Cache read", f"{toks.get('cache_read', 0):,}"))
    if pricing is not None:
        cost, _, _ = cost_for_tokens_by_model(
            s.get("tokens_by_model") or {}, pricing)
        currency = pricing.get("currency", "USD")
        out.append(_card("Estimated cost", fmt_money(cost, currency)))
    out.append("</div>\n")

    out.append(_agent_meta_html(s))

    if tools:
        out.append("<h2>Tools</h2>\n")
        out.append('<table><thead><tr><th>Tool</th>'
                   '<th class="num">Calls</th></tr></thead><tbody>\n')
        for name, n in sorted(tools.items(), key=lambda kv: -kv[1]):
            out.append(f'<tr><td><code>{_esc(name)}</code></td>'
                       f'<td class="num">{n:,}</td></tr>\n')
        out.append("</tbody></table>\n")

    out.append("<h2>Exchanges</h2>\n")
    out.append(_html_exchanges(s))
    out.append(_html_tail())
    return "".join(out)


def write_per_session_reports(sessions: list[dict[str, Any]],
                              out_dir: Path,
                              pricing: dict[str, Any] | None) -> tuple[int, int]:
    """Write one .md and one .html per session to out_dir. Returns (md, html)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    n_md = n_html = 0
    for s in sessions:
        md = render_single_session_md(s, pricing)
        (out_dir / _per_session_filename(s, "md")).write_text(md, encoding="utf-8")
        n_md += 1
        h = render_single_session_html(s, pricing)
        (out_dir / _per_session_filename(s, "html")).write_text(h, encoding="utf-8")
        n_html += 1
    return n_md, n_html


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a Markdown report from claude_forensics.py output.",
    )
    parser.add_argument("--sessions", type=Path, required=True,
                        help="Path to sessions JSONL (from claude_forensics.py --output)")
    parser.add_argument("--prompts", type=Path, default=None,
                        help="Path to prompts JSONL (from --prompts-out). Optional.")
    parser.add_argument("--output", "-o", type=Path, default=Path("claude-report.md"),
                        help="Where to write the Markdown report.")
    parser.add_argument("--order", choices=("project", "chronological"),
                        default="project",
                        help="Body layout: per-project (default) or chronological.")
    parser.add_argument("--top", type=int, default=10,
                        help="Top-N cutoff for ranked sections (default: 10).")
    parser.add_argument("--include-exchanges", action="store_true",
                        help="Inline every Q/A pair (verbose).")
    parser.add_argument("--full", action="store_true",
                        help=("Disable truncation of prompts and responses. "
                              "Switches the exchange layout to a blockquote "
                              "format that preserves newlines. "
                              "Only meaningful with --include-exchanges."))
    parser.add_argument("--cost-table", type=Path, default=None,
                        help=("JSON file of per-model token prices. When set, "
                              "the report includes estimated spend per "
                              "session, per project, and overall. See "
                              "prices.example.json for the schema."))
    parser.add_argument("--format", choices=("md", "html"), default="md",
                        help="Output format (default: md). HTML is self-contained "
                             "with inline CSS/JS and an in-page search box.")
    parser.add_argument("--summary", action="store_true",
                        help="Emit a one-page executive summary instead of the "
                             "full report. Works in both md and html formats.")
    parser.add_argument("--paste-jsonl", type=Path, default=None,
                        help="Path to paste-cache JSONL from claude_forensics.py "
                             "--paste-cache-out. Only used to print a count in "
                             "the summary; safe to omit.")
    parser.add_argument("--file-history-jsonl", type=Path, default=None,
                        help="Path to file-history JSONL from claude_forensics.py "
                             "--file-history-out. Only used to print a count in "
                             "the summary; safe to omit.")
    parser.add_argument("--cowork-jsonl", type=Path, default=None,
                        help="Path to cowork-sessions JSONL from "
                             "claude_forensics.py --cowork-out. When set, "
                             "annotates each session in the report with its "
                             "Cowork title, owner account, and archived state.")
    parser.add_argument("--per-session-dir", type=Path, default=None,
                        help="When set, write one self-contained markdown + "
                             "one self-contained HTML file per session into "
                             "this directory and skip the main --output "
                             "report. Useful for sharing individual sessions.")
    parser.add_argument("--last-cleanup", type=str, default=None,
                        help="Verbatim contents of `.claude/.last-cleanup` "
                             "(an ISO timestamp). When supplied with --summary, "
                             "the Retention section names this as the last "
                             "rotation time.")
    parser.add_argument("--title", type=str, default=None,
                        help="Override the report title (default: "
                             "'Claude usage report' or 'Claude usage — "
                             "executive summary' for --summary). Used by the "
                             "orchestrator to label the parallel agent reports.")
    parser.add_argument("--debug", action="store_true",
                        help="Verbose logging to stderr.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )

    sessions_path: Path = args.sessions.expanduser().resolve()
    if not sessions_path.is_file():
        log.error("not a file: %s", sessions_path)
        return 2
    sessions = load_jsonl(sessions_path)
    log.info("loaded %d sessions from %s", len(sessions), sessions_path)

    prompts: list[dict[str, Any]] | None = None
    if args.prompts is not None:
        prompts_path: Path = args.prompts.expanduser().resolve()
        if not prompts_path.is_file():
            log.error("not a file: %s", prompts_path)
            return 2
        prompts = load_jsonl(prompts_path)
        log.info("loaded %d prompts from %s", len(prompts), prompts_path)

    pricing: dict[str, Any] | None = None
    if args.cost_table is not None:
        pricing_path: Path = args.cost_table.expanduser().resolve()
        if not pricing_path.is_file():
            log.error("not a file: %s", pricing_path)
            return 2
        pricing = load_pricing(pricing_path)
        log.info("loaded pricing table from %s (effective %s)",
                 pricing_path, pricing.get("effective_date", "—"))

    paste_count: int | None = None
    file_history_count: int | None = None
    if args.paste_jsonl is not None and args.paste_jsonl.is_file():
        paste_count = sum(1 for _ in load_jsonl(args.paste_jsonl))
    if args.file_history_jsonl is not None and args.file_history_jsonl.is_file():
        file_history_count = sum(1 for _ in load_jsonl(args.file_history_jsonl))

    cowork_index: dict[str, dict[str, Any]] | None = None
    if args.cowork_jsonl is not None and args.cowork_jsonl.is_file():
        cowork_records = load_jsonl(args.cowork_jsonl)
        cowork_index = build_cowork_index(cowork_records)
        log.info("loaded %d Cowork records (%d joined to sessions)",
                 len(cowork_records), len(cowork_index))

    if args.per_session_dir is not None:
        out_dir: Path = args.per_session_dir.expanduser().resolve()
        n_md, n_html = write_per_session_reports(sessions, out_dir, pricing)
        log.info("wrote %d markdown + %d html per-session reports to %s",
                 n_md, n_html, out_dir)
        return 0

    if args.summary:
        default_title = "Claude usage — executive summary"
        title = args.title or default_title
        if args.format == "html":
            report = build_summary_html(sessions, prompts, pricing,
                                        paste_count, file_history_count,
                                        last_cleanup=args.last_cleanup,
                                        title=title)
        else:
            report = build_summary_md(sessions, prompts, pricing,
                                      paste_count, file_history_count,
                                      last_cleanup=args.last_cleanup,
                                      title=title)
    else:
        default_title = "Claude usage report"
        title = args.title or default_title
        if args.format == "html":
            report = build_report_html(sessions, prompts, args.order, args.top,
                                       args.include_exchanges, pricing=pricing,
                                       cowork_index=cowork_index, title=title)
        else:
            report = build_report(sessions, prompts, args.order, args.top,
                                  args.include_exchanges, full=args.full,
                                  pricing=pricing, cowork_index=cowork_index,
                                  title=title)

    output: Path = args.output.expanduser().resolve()
    output.write_text(report, encoding="utf-8")
    log.info("wrote %d chars to %s", len(report), output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
