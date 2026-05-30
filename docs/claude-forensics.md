# claude-forensics.sh

End-to-end orchestrator. Points at a `.claude` directory and produces a
timestamped working directory containing a frozen evidence snapshot, every
extracted JSONL stream, both Markdown reports, derived facts (Bash
commands actually run, files touched, orphan prompts), and a single tarball
suitable for archiving.

Pure bash — no extra Python dependencies beyond what `claude_forensics.py`
and `claude_report.py` already need.

## Usage

```sh
./claude-forensics.sh [options] PATH_TO_CLAUDE_DIR [OUTPUT_DIR]
```

Options:

- `-c PRICING_TABLE`, `--cost-table PRICING_TABLE` — path to a cost-table JSON (schema in `prices.example.json`). Takes precedence over the env var and auto-detection below.
- `-w PATH`, `--cowork-dir PATH` — explicit path to a Claude Desktop data dir. Use this when you have a copy of someone else's data dir on a forensic workstation, **or for any Windows source** (where the live path is `\Users\<name>\AppData\Roaming\Claude\`).
- `-W`, `--include-cowork` — on macOS, auto-use `~/Library/Application Support/Claude/` as the Cowork source. Use this when running against live data on your own macOS machine. Errors on non-macOS — supply `-w PATH` instead.
- `--verify FILE.tgz` — verify an existing claude-forensics bundle without running the analysis pipeline. Extracts the tarball to a temp dir, runs `sha256sum -c MANIFEST.sha256` over every file, and verifies the detached GPG signature (`MANIFEST.sha256.asc`) if present. Exits 0 on pass, 1 on any failure. The bundle's own SHA-256 is printed at the top of the output so you can cross-check it against an external record.
- `-V`, `--version` — print version and exit.
- `-h`, `--help` — print usage and exit.

Positional:

- `PATH_TO_CLAUDE_DIR` — required. The `.claude` directory to analyse.
- `OUTPUT_DIR` — optional. Parent directory where the per-run working dir is created. Defaults to the current directory.

The working directory is named `claude-forensics-YYYYMMDD-HHMMSS/`.

## Environment variables

- `TOOLS_DIR` — directory containing `claude_forensics.py` and `claude_report.py`. Defaults to the directory the shell script lives in. Override if you keep the Python tools elsewhere.
- `PRICING_TABLE` — path to a cost-table JSON. Used when `-c` is not given. Resolution order is: `-c FILE` → `$PRICING_TABLE` → `$TOOLS_DIR/prices.json`. If none exist, the reports still include token counts but skip dollar figures. The example template (`prices.example.json`) is **never** auto-picked: its rates are zero, and emitting `$0` figures in a forensic report would be misleading.
- `GPG_KEY` — if set and `gpg` is on PATH, the SHA-256 manifest is also detached-signed with this key and written to `MANIFEST.sha256.asc`. The key id can be any form gpg's `--local-user` accepts (long fingerprint, short id, email). Without it, the manifest is unsigned but still valid for tamper-detection.
- `FORCE` — set to anything non-empty to bypass the disk-space precheck. By default, if `du -sk` on the sources × 1.3 exceeds the free space in `$OUT_BASE`, the script aborts before copying anything. `FORCE=1` proceeds anyway (e.g., when you know the heuristic is too pessimistic).

## Requirements

- Python 3 (stdlib only).
- `claude_forensics.py` and `claude_report.py` in `$TOOLS_DIR`.
- `jq` is optional. Without it the three jq-derived files in phase 4 are skipped and the script announces `[!] jq not on PATH; skipping …` rather than aborting.

## Phases

The script is laid out in commented phases. Each phase is independent enough to read and modify on its own:

0. **Preserve evidence.** Copies `$TARGET` to `./claude-snapshot/` with `cp -Rp` (preserves mtimes and permissions) and `chmod -R a-w` so downstream steps fail loudly if anything tries to write to it. When `-W` or `-w PATH` is given, also copies a small forensically-interesting subset of the Claude Desktop data dir (`claude-code-sessions/`, `local-agent-mode-sessions/`, a handful of config JSONs) to `./cowork-snapshot/`. The 12 GB `vm_bundles/` tree and other bulk caches are intentionally **not** snapshotted; edit the lists in phase 0 if you want wider coverage.
1. **Inventory.** Top-level `ls -la`, `du -sh` of each subtree, and a per-transcript inventory (date / size / path). Uses macOS `stat -f` with a GNU `find -printf` fallback for portability.
2. **Extract structured data.** One call to `claude_forensics.py` produces the six core JSONL streams (`sessions`, `prompts`, `processes`, `shell-snapshots`, `paste-cache`, `file-history`) and, when Cowork was preserved in phase 0, also `cowork-sessions.jsonl` (CLI-side Cowork metadata) and `cowork-agent-sessions.jsonl` (full Cowork agent transcripts parsed from each session's `audit.jsonl`). `--debug` captures every transcript file and every malformed JSON line to `extract.log`.
3. **Render reports.** Both summary outputs include a **Retention** section computed from `prompts.jsonl` and the snapshot's `.claude/.last-cleanup`: orphan/survivor date ranges, the implied retention window, and the timestamp of Claude Code's last transcript rotation. This makes the asymmetry between long-lived `history.jsonl` and short-lived `projects/*/*.jsonl` an explicit, named part of the report rather than a buried anomaly. Two parallel report sets are produced — one for the Claude Code CLI surface, one for Cowork agent sessions — so the two products stay visually distinct. In addition, two `*-sessions/` directories are populated with one self-contained markdown + one self-contained HTML report **per session**, named `YYYYMMDD-HHMMSS_<sid8>_<slug>.{md,html}` for sortable, identifiable, individually-shareable evidence:
   - **Code side** (always): `report-by-project.{md,html}`, `report-chronological.md`, `summary.{md,html}`, titled *Claude usage report* / *Claude usage — executive summary*.
   - **Agent side** (only when `cowork-agent-sessions.jsonl` was produced): `cowork-agent-report-by-account.{md,html}`, `cowork-agent-report-chronological.md`, `cowork-agent-summary.{md,html}`, titled *Cowork agent usage report* / *Cowork agent — executive summary*.

   The same `claude_report.py` invocation produces both; the agent set is created by passing `cowork-agent-sessions.jsonl` as `--sessions` together with `--title "Cowork agent usage report"`. Pricing flags propagate to both sets; the Cowork title annotation (`--cowork-jsonl`) is applied to the code set only.
4. **Derived facts (jq).** If `jq` is available, extracts:
   - `bash-commands.txt` — every Bash command Claude ran
   - `files-touched.txt` — every file Read/Write/Edited, sorted unique
   - `orphan-prompts.jsonl` — prompts whose transcripts no longer exist
5. **Chain-of-custody manifest.** SHA-256s every regular file that will go into the bundle (derived artifacts only — the snapshot is **not** included, since it's the canonical read-only evidence root protected by `chmod -R a-w` in phase 0). Written to `MANIFEST.sha256` in standard `sha256sum` format, with files sorted under `LC_ALL=C` for byte-stable ordering. If `$GPG_KEY` is set and `gpg` is on PATH, also writes a detached signature `MANIFEST.sha256.asc`. Verify either with `./claude-forensics.sh --verify BUNDLE.tgz` (extracts to a temp dir and runs both checks) or manually with `sha256sum -c MANIFEST.sha256` after extracting the bundle.
6. **Bundle.** Tar everything except the snapshot into `claude-forensics-<TS>.tgz` and print the tarball's own SHA-256 as a separate chain-of-custody anchor (record it externally — ticket, email, signed ledger — so the bundle's identity is provable later).

## Output layout

```
claude-forensics-YYYYMMDD-HHMMSS/
├── claude-snapshot/              read-only copy of the source .claude tree
├── cowork-snapshot/              read-only copy of the Claude Desktop subset (when -W/-w)
├── inventory.txt                 ls / du / per-transcript dates + sizes
├── sessions.jsonl                one record per Claude session
├── prompts.jsonl                 one record per prompt in history.jsonl
├── processes.jsonl               one record per sessions/<pid>.json
├── shell-snapshots.jsonl         one record per shell-snapshots/*.sh
├── paste-cache.jsonl             one record per paste-cache/<hash>.txt
├── file-history.jsonl            one record per (session, file) with versions
├── cowork-sessions.jsonl         one record per Cowork session (title, owner, joined)
├── cowork-agent-sessions.jsonl   one record per Cowork agent session, with full audit transcript
├── cowork-agent-report-by-account.md    Cowork agent report grouped by VM cwd (markdown)
├── cowork-agent-report-by-account.html  Cowork agent report grouped by VM cwd (HTML)
├── cowork-agent-report-chronological.md Cowork agent report in time order (markdown)
├── cowork-agent-summary.md              Cowork agent one-page summary (markdown)
├── cowork-agent-summary.html            Cowork agent one-page summary (HTML)
├── claude-code-sessions/                per-session reports for Claude Code CLI (one .md + .html each)
│   └── YYYYMMDD-HHMMSS_<sid8>_<slug>.{md,html}
├── claude-cowork-sessions/              per-session reports for Cowork agent sessions
│   └── YYYYMMDD-HHMMSS_<sid8>_<slug>.{md,html}
├── summary.md                    one-page executive summary (markdown)
├── summary.html                  one-page executive summary (self-contained html)
├── report-by-project.md          per-project narrative report (markdown)
├── report-by-project.html        per-project narrative report (self-contained html)
├── report-chronological.md       timeline narrative report (markdown)
├── bash-commands.txt             every Bash command Claude actually ran   (jq)
├── files-touched.txt             every file Claude Read/Write/Edited      (jq)
├── orphan-prompts.jsonl          prompts whose transcripts no longer exist (jq)
├── extract.log                   extractor stderr (debug + parse warnings)
├── MANIFEST.sha256               SHA-256 of every file; verify with sha256sum -c
├── MANIFEST.sha256.asc           detached GPG signature of the manifest (if GPG_KEY)
└── claude-forensics-*.tgz        tarball of everything except the snapshot
```

## Examples

```sh
# Local laptop, default output dir, no spend estimate:
./claude-forensics.sh ~/.claude

# Analysing a copy of someone else's .claude, with cost estimation:
PRICING_TABLE=~/investigations/prices-2026-05.json \
    ./claude-forensics.sh /mnt/evidence/dave.claude ~/investigations

# Tools live elsewhere:
TOOLS_DIR=~/src/claude-forensics ./claude-forensics.sh ~/.claude

# Signed manifest for defensible chain of custody:
GPG_KEY=ABC1234567890DEF ./claude-forensics.sh /mnt/evidence/dave.claude

# Include Cowork session metadata (live, on the user's own macOS machine):
./claude-forensics.sh -W ~/.claude

# Cowork from a copy taken from another host (path on the forensic workstation):
./claude-forensics.sh -w /mnt/evidence/dave-desktop-claude /mnt/evidence/dave.claude

# Windows host (both trees copied off and pointed at explicitly):
./claude-forensics.sh \
    -w /mnt/evidence/win-AppData-Roaming-Claude \
    /mnt/evidence/win-dot-claude \
    /mnt/evidence/win-out
```

## Verifying a bundle

After receiving a `claude-forensics-*.tgz`, the recipient can confirm
nothing has been altered since the run. The script has a built-in verify
mode that handles extract + manifest check + signature check in one step:

```sh
./claude-forensics.sh --verify claude-forensics-20260527-184407.tgz
```

Output:

```
[*] bundle:      claude-forensics-20260527-184407.tgz
[*] bundle hash: 977bf0d0cacbae34b617ec1762a4eb94fa4a0e7b9ecd2766b0f7c6d08c8276eb
[*] extracting to /tmp/claude-forensics-verify.XXXXXX ...
[*] verifying file checksums against MANIFEST.sha256 ...
[*] GPG signature: GOOD
[*] VERIFIED OK
```

Exit status is `0` on pass, `1` on any failure (mismatched checksum, bad
signature, missing manifest). The bundle's own SHA-256 is printed at the
top so you can cross-check it against an external chain-of-custody record
(ticket, signed email, ledger entry) before trusting the contents.

If you prefer to verify manually:

```sh
tar -xzf claude-forensics-20260527-184407.tgz -C verify-out
cd verify-out
sha256sum -c MANIFEST.sha256          # every artifact: OK
gpg --verify MANIFEST.sha256.asc      # signed by the original investigator
```

(The built-in verify is equivalent — it extracts to a temp dir and cleans up.)

The bundle's own SHA-256 (printed at the end of the run) lets you cross-
check the tarball itself against an external record (ticket, signed email,
ledger entry) before extracting.

## Safety notes

- The script uses `set -euo pipefail` — any uncaught failure aborts, rather than continuing with partial output.
- An `EXIT` trap reports the path of any partial working directory if the script aborts. The directory is **not** auto-deleted (the snapshot inside it is `chmod -R a-w`, so cleanup needs human judgement; the partial output is also often useful for diagnosing the failure).
- Before any copying, a disk-space precheck (`du -sk` on the sources × 1.3) is compared against `df -k` on `$OUT_BASE`; the script aborts early if there isn't enough room. Set `FORCE=1` to bypass.
- The snapshot is chmod'd `a-w` immediately after the copy, before any other step runs.
- Mtimes are preserved on copy so later mtime-based reasoning (when did Claude last run, when was a transcript written) is sound.
- The bundle excludes the snapshot; treat the snapshot directory itself as the canonical evidence root and the tarball as derived analysis you can share.
