# claude_forensics.py

Extract structured data from a `.claude` directory. Reads the Claude Code
state files left on a machine and emits JSONL streams suitable for
investigation, archiving, or further analysis.

Stdlib-only Python 3 — no `pip install` required.

## What it reads

The target `.claude` directory is `~/.claude` on macOS / Linux and `\Users\<name>\.claude` on Windows. Inside it:

| Source                              | Purpose                                                  |
|-------------------------------------|----------------------------------------------------------|
| `projects/<encoded-cwd>/*.jsonl`    | Per-session transcripts (user prompts, assistant turns)  |
| `history.jsonl`                     | Append-only ledger of every prompt typed on the machine  |
| `sessions/<pid>.json`               | Per-process state (PID, sessionId, cwd, status, version) |
| `shell-snapshots/*.sh`              | The shell environment Claude's Bash tool ran against     |
| `paste-cache/<hash>.txt`            | Content the user pasted into prompts                     |
| `file-history/<sessionId>/<hash>@v<N>` | Versioned backups of files Claude edited per session   |

On Windows the layout of `.claude` is sparser — empirically `history.jsonl`, `shell-snapshots/`, `paste-cache/`, and `file-history/` are not written. Missing subtrees produce a `WARNING no X under …` line and are skipped without error; CLI session transcripts under `projects/` and the `backups/` tree extract normally.

It can also extract Cowork session metadata from a **separate** Claude
Desktop data dir (macOS: `~/Library/Application Support/Claude/`; Windows:
`\Users\<name>\AppData\Roaming\Claude\`) when given `--cowork-dir`:

| Source                                                                       | Purpose                                                  |
|------------------------------------------------------------------------------|----------------------------------------------------------|
| `claude-code-sessions/<orgUuid>/<accountUuid>/local_<sessionId>.json`        | Cowork session metadata (title, owner, model, archived)  |
| `local-agent-mode-sessions/<orgUuid>/<accountUuid>/local_<sessionId>.json`   | Cowork **agent** session metadata (title, system prompt, allow-lists, owner) |
| `local-agent-mode-sessions/<orgUuid>/<accountUuid>/local_<sessionId>/audit.jsonl` | Full agent transcript (user/assistant/system events) + runtime cost |

## Usage

```sh
python3 claude_forensics.py \
    --claude-dir       PATH/TO/.claude \
    --output           sessions.jsonl \
    [--prompts-out       prompts.jsonl] \
    [--processes-out     processes.jsonl] \
    [--shell-out         shell-snapshots.jsonl] \
    [--paste-cache-out   paste-cache.jsonl] \
    [--file-history-out  file-history.jsonl] \
    [--cowork-dir        PATH/TO/Claude] \
    [--cowork-out        cowork-sessions.jsonl] \
    [--cowork-agent-out         cowork-agent-sessions.jsonl] \
    [--debug]
```

## Flags

- `--claude-dir PATH` — `.claude` directory to analyse (default: `~/.claude`).
- `--output, -o PATH` — primary per-session output (default `./claude-history.jsonl`).
- `--prompts-out PATH` — also flatten `history.jsonl` into a per-prompt JSONL, joining each prompt to its transcript by `sessionId`.
- `--processes-out PATH` — also flatten `sessions/<pid>.json` into a per-process JSONL.
- `--shell-out PATH` — also parse `shell-snapshots/*.sh` and emit one record per snapshot.
- `--paste-cache-out PATH` — also walk `paste-cache/` and emit one record per pasted blob, with back-references to prompts that mention the paste id.
- `--file-history-out PATH` — also walk `file-history/<sessionId>/` and emit one record per (session, file) with all versions.
- `--cowork-dir PATH` — path to a Claude Desktop data dir (macOS default: `~/Library/Application Support/Claude/`). Pair with `--cowork-out` and/or `--cowork-agent-out`.
- `--cowork-out PATH` — output path for Cowork session metadata JSONL (the small claude-code-sessions sidecars).
- `--cowork-agent-out PATH` — output path for Cowork agent-session JSONL with full audit transcripts. At least one of `--cowork-out` / `--cowork-agent-out` is required when `--cowork-dir` is set.
- `--debug` — verbose logging to stderr (every transcript file, every malformed line).

## Output: `sessions.jsonl` (one record per session)

Key fields:

- `session_id`, `source_file`
- `project_dir` — `cwd` recorded inside the events (authoritative)
- `project_dir_from_path` — decoded from the directory name (lossy when real paths contain `-`)
- `all_cwds_seen` — list, useful for spotting mid-session cwd changes
- `start_time`, `end_time`
- `claude_versions`, `git_branches`, `entrypoints` — sets, captured across the session's events
- `num_events`, `num_user_messages`, `num_assistant_messages`
- `tools_used` — `{tool_name: call_count}`
- `models_used`
- `tokens` — combined token totals (`input`, `output`, `cache_read`, `cache_creation_total`, `cache_write_5m`, `cache_write_1h`, `web_search_requests`, `web_fetch_requests`)
- `tokens_by_model` — same shape, keyed per model
- `service_tiers` — `{tier_name: turn_count}` (e.g. `standard`, `priority`)
- `exchanges` — ordered list of `{timestamp, user_prompt, assistant_response, tools_called, tokens}` — one entry per real user turn

A `user` event whose `content` is a list of `tool_result` blocks is **not** counted as a prompt; only real human turns appear in `exchanges`.

## Output: `prompts.jsonl` (one record per prompt)

- `timestamp`, `session_id`
- `project_raw` (as stored in `history.jsonl`) and `project_dir` (decoded if it was the dash form)
- `display` — the rendered prompt text
- `pasted_contents`, `num_pasted_blocks`
- `transcript_present` (bool), `transcript_path` — set by joining `session_id` against the on-disk transcript index. **`transcript_present: false` means the prompt exists in `history.jsonl` but no transcript survives** — investigate.

## Output: `processes.jsonl` (one record per `sessions/<pid>.json`)

Fields from the source file (`pid`, `sessionId`, `cwd`, `startedAt`, `procStart`, `status`, `updatedAt`, `version`, `entrypoint`, `kind`, …) plus:

- `source_file`, `filename_pid` (PID parsed from the filename)
- `file_mtime` — ISO-8601 UTC, useful as a fallback "last seen"
- `transcript_present`, `transcript_path` — joined by `sessionId`

## Output: `shell-snapshots.jsonl` (one record per snapshot file)

- `source_file`, `size_bytes`, `file_mtime`
- `launch_ts_ms`, `launch_time` — parsed from the filename (`snapshot-zsh-<ms>-<rand>.sh`)
- `path_dirs` — `PATH` split on `:`
- `aliases` — `{name: expansion}`
- `exports` — `{name: value}` (includes `PATH`)
- `function_names` — list of names declared in the snapshot

## Output: `paste-cache.jsonl` (one record per pasted blob)

- `paste_id` — the filename stem (a Claude-assigned content hash)
- `source_file`, `size_bytes`, `sha256`, `file_mtime`
- `content` — inline UTF-8 text if `size_bytes <= 100,000`; `null` otherwise
- `content_truncated` — `true` when the inline content was suppressed
- `referenced_by` — list of `{timestamp, session_id, project}` from `history.jsonl` entries that mention the paste id

The paste-id join walks every string inside each `pastedContents` blob in `history.jsonl` rather than depending on the (undocumented) dict shape — robust to future schema drift.

## Output: `file-history.jsonl` (one record per (session, file) pair)

- `session_id` — taken from the parent directory name
- `file_id` — the hash prefix shared by all versions
- `version_count`
- `versions` — list of `{version, path, size_bytes, mtime}`, sorted by version
- `first_seen`, `last_seen`
- `transcript_present`, `transcript_path` — joined via `session_id`

The hash→original-path mapping is not stored in `.claude`. Pair this stream with `files-touched.txt` (from the orchestrator's jq phase) to reverse-map hashes to real paths.

## Output: `cowork-sessions.jsonl` (one record per Cowork session)

Produced only when `--cowork-dir` + `--cowork-out` are supplied. Each record passes through every field from the source `local_*.json` plus:

- `source_file`
- `owner_org_uuid` (the parent-parent directory name)
- `owner_account_uuid` (the parent directory name)
- `file_mtime`
- `transcript_present`, `transcript_path` — joined to the CLI transcript via the `cliSessionId` field

Notable source fields: `sessionId` (Cowork id, like `local_<uuid>`), `cliSessionId` (matches `sessions.jsonl.session_id`), `cwd`, `originCwd`, `createdAt`, `lastActivityAt`, `model`, `effort`, `isArchived`, `title`, `titleSource`, `permissionMode`, `remoteMcpServersConfig`.

The `cliSessionId → session_id` join is the key insight: Cowork sessions write their transcripts to `.claude/projects/` just like CLI sessions; the only new information here is the metadata sidecar (title, owner org/account, archive state).

## Output: `cowork-agent-sessions.jsonl` (one record per Cowork agent session)

Produced when `--cowork-dir` + `--cowork-agent-out` are supplied. Unlike the Cowork sidecar above, agent sessions are a **separate product surface** with their own transcript format — `audit.jsonl` inside each session's directory — so this stream is a *full transcript*, not just metadata.

The record schema deliberately mirrors `sessions.jsonl` so the existing reporter works on it without branching:

- Same core fields: `session_id`, `source_file`, `project_dir`, `start_time`, `end_time`, `num_user_messages`, `num_assistant_messages`, `tools_used`, `tokens`, `tokens_by_model`, `service_tiers`, `exchanges`
- `project_dir` is set to the human-readable space name (e.g. `superannuation`) when the session belongs to a named space, so the by-project report groups sessions by space rather than by the long `…/local_<sid>/outputs` path. Sessions with no space keep the agent's VM cwd as `project_dir` (e.g. `/sessions/gallant-great-pascal`). The original metadata `cwd` is preserved as `agent_vm_cwd`.
- `reported_cost_usd` — pre-computed cost from the agent runtime's `result` events (rare gift; treat as authoritative for that session)
- `reported_num_turns` — also from `result` events
- All agent-specific fields are prefixed `agent_*` to keep them visually distinct:
  - `agent_audit_file` — path to the parsed audit.jsonl
  - `agent_owner_org_uuid`, `agent_owner_account_uuid`, `agent_owner_account_name`, `agent_owner_email` — **identity disclosure**: the agent's metadata contains the user's Anthropic email and display name
  - `agent_title`, `agent_initial_message`
  - `agent_space_id` — the Cowork "space" this session belongs to
  - `agent_space_name`, `agent_space_folders`, `agent_space_projects`, `agent_space_instructions`, `agent_space_origin` — joined from the per-account `spaces.json` when present, so investigators don't have to manually correlate `spaceId` to the human-readable space name; the renderer surfaces `agent_space_name` next to every session row
  - `agent_model_configured`, `agent_is_archived`, `agent_memory_enabled`
  - `agent_user_selected_folders`, `agent_egress_allowed_domains`, `agent_web_fetch_allowed_urls` — what the agent was allowed to access
  - `agent_process_name`, `agent_vm_process_name`
  - `agent_system_prompt_chars` — system prompt size (the prompt itself is *not* included; it can be ~40 KB+)
  - `agent_created_at`, `agent_last_activity_at`
  - `agent_cli_session_id`, `agent_cli_transcript_present`, `agent_cli_transcript_path` — when the agent shared work with a CLI session

The `skills-plugin/` sibling tree under `local-agent-mode-sessions/` is intentionally **not** walked — it is plugin support data, not a session store.

## Parser caveats

- Decoded `project_dir_from_path` is lossy: a real path with `-` in it round-trips wrong. The `project_dir` taken from event `cwd` fields is authoritative when present.
- Shell-snapshot parsing is line-level regex, not a real shell parser: line-continuations and nested heredocs are not handled. It's adequate for triage (aliases shadowing `git`/`curl`, `PATH` redirections, exported tokens) but not for full shell-equivalence.
- Token aggregation uses the top-level `usage` block per assistant event (the rollup); `usage.iterations[]` is **not** summed separately to avoid double-counting.
- Malformed transcript lines are logged at DEBUG and skipped — those are evidence of mid-write crashes or manual editing.

## Example

```sh
# Full extract of a snapshot directory:
python3 claude_forensics.py \
    --claude-dir       ./claude-snapshot \
    --output           sessions.jsonl \
    --prompts-out      prompts.jsonl \
    --processes-out    processes.jsonl \
    --shell-out        shell-snapshots.jsonl \
    --paste-cache-out  paste-cache.jsonl \
    --file-history-out file-history.jsonl \
    --debug 2> extract.log
```
