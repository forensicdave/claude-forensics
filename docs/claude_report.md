# claude_report.py

Render a report from the JSONL streams produced by `claude_forensics.py`.
Pure transform: takes JSONL in, writes a `.md` or `.html` file out.
Stdlib-only Python 3.

Three output shapes, controlled by `--format` and `--summary`:

- **Markdown full report** (default) — long-form per-project or chronological breakdown, suitable for terminals and GitHub viewing.
- **HTML full report** (`--format html`) — self-contained `.html` with inline CSS/JS, a search box, and collapsible session cards. No external resources fetched.
- **Executive summary** (`--summary`) — one-page exec view: headline stats, top-5 projects and sessions, anomaly counts. Available in both formats.

## Usage

```sh
python3 claude_report.py \
    --sessions   sessions.jsonl \
    [--prompts    prompts.jsonl] \
    [--cost-table prices.json] \
    [--order      project|chronological] \
    [--format     md|html] \
    [--summary] \
    [--paste-jsonl       paste-cache.jsonl] \
    [--file-history-jsonl file-history.jsonl] \
    [--top        N] \
    [--include-exchanges] [--full] \
    --output     report.md
```

## Flags

- `--sessions PATH` (required) — sessions JSONL from `claude_forensics.py --output`.
- `--prompts PATH` — prompts JSONL from `--prompts-out`. Unlocks the prompt timeline (chronological mode), the orphan-prompt anomaly section, and the headline `Prompts in history.jsonl` line.
- `--cost-table PATH` — JSON file of per-model token prices. When supplied, the report includes estimated spend per session, per project, and overall, plus a *Pricing notes* section. See `prices.example.json` for the schema.
- `--format md|html` — output format (default `md`). `html` emits a self-contained document with inline CSS/JS and an in-page search box.
- `--summary` — one-page executive summary instead of the full report. Works in both formats.
- `--paste-jsonl PATH` — paste-cache JSONL from `claude_forensics.py --paste-cache-out`. Currently only used to print a paste count in `--summary`.
- `--file-history-jsonl PATH` — file-history JSONL from `--file-history-out`. Used to print a file-history record count in `--summary`.
- `--cowork-jsonl PATH` — Cowork sessions JSONL from `--cowork-out`. When set, each session row in the report is annotated with its Cowork title and archive state (in both markdown and HTML).
- `--title TITLE` — override the default report title. The orchestrator uses this to label its second report set ("Cowork agent usage report") so the two surfaces are visually distinct.
- `--last-cleanup TIMESTAMP` — verbatim contents of `.claude/.last-cleanup`. When supplied with `--summary`, the **Retention** section names this as the last transcript rotation time. The orchestrator fills it in automatically from the snapshot; pass it manually if you're running the reporter standalone.
- `--per-session-dir DIR` — write one self-contained markdown **and** one self-contained HTML file per session into `DIR`, named `YYYYMMDD-HHMMSS_<sid8>_<slug>.{md,html}` (sortable, identifiable, collision-free). When set, the main `--output` is skipped — this mode is for sharing or archiving individual sessions. Cowork agent records render with an extra "Cowork agent metadata" block (title, owner, space, egress allow-list, runtime cost, system-prompt size).
- `--order project|chronological` — body layout. Default `project`: ranked by-project breakdown. `chronological`: sessions in start-time order plus a unified prompt timeline at the bottom.
- `--top N` — top-N cutoff for ranked sections (default 10).
- `--include-exchanges` — inline every Q/A pair per session.
- `--full` — disable truncation and use a heading-plus-blockquote layout that preserves newlines. Only meaningful with `--include-exchanges` in the markdown format; in HTML each exchange is already rendered as a preformatted block.
- `--output, -o PATH` — destination file (default `./claude-report.md`). Pick the extension to match `--format`.
- `--debug` — verbose logging to stderr.

## Report layout

1. `# Claude usage report` and generated timestamp
2. `## Pricing notes` — only when `--cost-table` is set: effective date, currency, `_note`, priced models, unpriced models seen in data
3. `## Overview` — date range, totals, distinct projects, versions, entrypoints, combined token totals, and (with pricing) total estimated spend + per-model split
4. `## Top tools used` — ranked table of tool name / call count / sessions using
5. Body — one of:
   - `## Projects` (default) — per-project blocks ranked by session count
   - `## Sessions in chronological order` — sessions in start-time order; followed by `## Prompt timeline` if `--prompts` was supplied
6. `## Anomalies` — orphan prompts (by project), sessions spanning multiple Claude versions, sessions that changed cwd mid-run

Per-session blocks include token breakdown (input / output / cache-read / cache-write-5m / cache-write-1h) and, when priced, an `Estimated spend` line.

## Cost table schema (`--cost-table`)

See `prices.example.json` for the canonical template. Shape:

```json
{
  "_note": "free-text disclaimer rendered into the report",
  "effective_date": "YYYY-MM-DD",
  "currency": "USD",
  "models": {
    "claude-opus-4-7": {
      "input_per_mtok":          0.0,
      "output_per_mtok":         0.0,
      "cache_read_per_mtok":     0.0,
      "cache_write_5m_per_mtok": 0.0,
      "cache_write_1h_per_mtok": 0.0
    }
  },
  "server_tools": {
    "web_search_per_request": 0.0,
    "web_fetch_per_request":  0.0
  }
}
```

Behaviour:

- Rates are dollars per million tokens (`_per_mtok`). Missing fields default to zero.
- Web tool rates are flat per-request.
- Models present in the data but missing from `models` are **excluded** from the cost total and listed by name under *Pricing notes → Unpriced models seen in data*. This is intentional — silently zeroing an unknown model would hide spend in a forensic context.
- The report header includes the pricing file's `effective_date` and `_note` so the estimate is reproducible.

## Honest limitations

- The cost figures are estimates computed locally from token counts. **Authoritative billing comes from the Anthropic Console**, not this report. The Pricing notes section says so out loud — leave it intact in any output you share.
- Cache pricing depends on whether the write was a 5-minute or 1-hour cache; these are tracked separately. Older transcripts that only have the rollup `cache_creation_input_tokens` and no `cache_creation.ephemeral_*` split will under-count cache costs.
- `service_tier=priority` is not separately priced. If your investigation needs tier-aware pricing, extend the schema to allow per-tier rates per model.

## Examples

```sh
# Headline numbers only, no per-session detail:
python3 claude_report.py --sessions sessions.jsonl --output overview.md

# Investigation-grade: per-project breakdown, full Q/A, spend per project:
python3 claude_report.py \
    --sessions sessions.jsonl --prompts prompts.jsonl \
    --include-exchanges --full \
    --cost-table prices.json \
    --output report-by-project.md

# Timeline view (useful for incident reconstruction):
python3 claude_report.py \
    --sessions sessions.jsonl --prompts prompts.jsonl \
    --order chronological --include-exchanges --full \
    --cost-table prices.json \
    --output report-chronological.md

# Self-contained HTML for sharing with non-CLI reviewers:
python3 claude_report.py \
    --sessions sessions.jsonl --prompts prompts.jsonl \
    --include-exchanges --format html \
    --cost-table prices.json \
    --output report-by-project.html

# One-page executive summary (great for tickets / dashboards):
python3 claude_report.py \
    --sessions sessions.jsonl --prompts prompts.jsonl \
    --summary --cost-table prices.json \
    --paste-jsonl paste-cache.jsonl \
    --file-history-jsonl file-history.jsonl \
    --output summary.md

# Cowork agent report (same renderer, different data, distinct title):
python3 claude_report.py \
    --sessions cowork-agent-sessions.jsonl \
    --title "Cowork agent usage report" \
    --include-exchanges --format html \
    --output cowork-agent-report.html

# One markdown + one HTML file per session, suitable for sharing individually:
python3 claude_report.py \
    --sessions sessions.jsonl \
    --per-session-dir per-session-reports/
```
