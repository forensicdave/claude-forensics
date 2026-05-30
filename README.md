# claude-forensics

First release of tooling to extract a complete, evidence-grade record of how Claude (Chat or Code or Cowork) has been used on a
machine, from any `.claude` directory. Point it at a captured `.claude/`
tree (and the Claude Desktop data dir alongside it) and get back a
searchable, sortable, fully reconstructed history: every session, every
prompt, every tool call, every shell environment Claude ran against, and
an estimate of token spend.

> **Authoritative billing comes from the Anthropic Console**, not from this
> tool. The dollar figures here are estimates computed locally from the
> token counts in the transcripts using a user-supplied pricing table.
> Treat them as evidence to be verified, not as a bill of record.

## Use cases

- **Personal backup / archive.** Claude Code rotates per-session transcripts on a roughly 30-day window, but the `history.jsonl` prompt ledger outlives them by months. This tool preserves both — before a machine wipe, a migration, or just so you keep a long-term record of your own work. Run it against your own `~/.claude` periodically and you have a permanent, searchable history that survives the rotation.

- **Internal investigation.** AI compute misuse, data exfiltration via pasted content, sensitive-repo access via local agents. The per-session token + cost breakdown turns "we think someone might be burning budget" into a precise, per-user, per-project audit trail. AI compute spend is a board-level concern right now — runaway agent costs, employees burning budget on expensive models, sensitive code paths handed to external services without controls — and the tool gives CISOs, FinOps, and IT leadership a defensible, auditable answer to "where did the AI spend go, and who drove it?"

- **DFIR.** What did the attacker do on a compromised endpoint that had Claude Code installed? Recover every CLI session and Cowork agent transcript on the host, every prompt typed, every file touched, every shell command Claude ran. The read-only snapshot + SHA-256 manifest + optional GPG signing give you a defensible evidence chain; `--verify BUNDLE.tgz` lets recipients confirm nothing has been altered since the run.

- **Threat intelligence.** Triaging seized threat-actor infrastructure that contains Claude state. The actor's prompts reveal intent, the file-history backups reveal artefacts produced, the OAuth account block in `backups/` reveals identity, and the paste cache often reveals the most sensitive content of all. Treat a captured `.claude/` like you'd treat a captured `.bash_history` + browser profile combined.

## Quick start

```sh
# Make the tools usable from your PATH (optional)
./install.sh                  # symlinks claude-forensics into /usr/local/bin

# Run the full workflow against a .claude directory
./claude-forensics.sh ~/.claude ~/investigations

# Or, with cost estimation enabled:
cp prices.example.json prices.json
$EDITOR prices.json           # fill in real per-million-token rates
./claude-forensics.sh -c prices.json ~/.claude ~/investigations

# Include Claude Desktop "Cowork" session metadata too:
./claude-forensics.sh -W ~/.claude                       # live, macOS only
./claude-forensics.sh -w /mnt/copy/Claude ~/.claude      # from a copied data dir
```

Each run produces a timestamped working directory containing a read-only
evidence snapshot, four JSONL streams, two Markdown reports, jq-derived
fact files, and a single tarball ready to archive.

## What you get

```
claude-forensics-YYYYMMDD-HHMMSS/
├── claude-snapshot/              read-only copy of the source .claude tree
├── cowork-snapshot/              Claude Desktop subset (when -W or -w supplied)
├── inventory.txt                 ls / du / per-transcript dates + sizes
├── sessions.jsonl                one record per Claude session (Q/A, tools, tokens)
├── prompts.jsonl                 one record per prompt in history.jsonl
├── processes.jsonl               one record per sessions/<pid>.json
├── shell-snapshots.jsonl         one record per shell-snapshots/*.sh
├── paste-cache.jsonl             one record per pasted blob (joins to prompts)
├── file-history.jsonl            one record per (session, file) with all versions
├── cowork-sessions.jsonl         one record per Cowork session (title, owner, joined)
├── cowork-agent-sessions.jsonl   one record per Cowork agent session (full transcript)
├── cowork-agent-report-by-account.{md,html}  parallel "Cowork agent usage report" set
├── cowork-agent-report-chronological.md
├── cowork-agent-summary.{md,html}
├── claude-code-sessions/         one .md + one .html per CLI session (sortable, shareable)
├── claude-cowork-sessions/       one .md + one .html per Cowork agent session
├── summary.md / summary.html     one-page executive summary in both formats
├── report-by-project.md          per-project narrative report
├── report-by-project.html        same content, self-contained HTML (search, collapsible)
├── report-chronological.md       timeline narrative report
├── bash-commands.txt             every Bash command Claude ran           (jq)
├── files-touched.txt             every file Claude Read/Write/Edited     (jq)
├── orphan-prompts.jsonl          prompts whose transcripts no longer exist (jq)
├── extract.log                   extractor stderr (parse warnings)
├── MANIFEST.sha256               SHA-256 of every file; verify with sha256sum -c
├── MANIFEST.sha256.asc           detached GPG signature (if GPG_KEY was set)
└── claude-forensics-*.tgz        evidence bundle (everything but the snapshot)
```

The closing summary also prints the bundle's own SHA-256 so you can record it externally as a chain-of-custody anchor.

## Supported sources: macOS and Windows

The tool itself runs on macOS or Linux, but it analyses `.claude` and Claude Desktop trees captured from **either macOS or Windows hosts**. Claude Code and Claude Cowork keep important state in two separate directories on each platform — you need **both** trees to get full coverage:

| OS      | Claude Code state          | Claude Desktop / Cowork data                |
|---------|----------------------------|---------------------------------------------|
| macOS   | `~/.claude`                | `~/Library/Application Support/Claude/`     |
| Windows | `\Users\<name>\.claude`    | `\Users\<name>\AppData\Roaming\Claude\`     |

For a Windows host, copy both trees off and run:

```sh
./claude-forensics.sh \
    -w /path/to/copy/of/AppData/Roaming/Claude \
    /path/to/copy/of/.claude \
    output-dir
```

`-W` (auto-detect Cowork dir) only knows the macOS default path — on Windows analysis always use `-w PATH` to point at the copied Claude Desktop tree.

Windows Claude Code (as of mid-2026) does not appear to write `history.jsonl`, `shell-snapshots/`, `paste-cache/`, or `file-history/`. The extractor logs a `WARNING no X under …` for each absent subtree and continues; the corresponding JSONL streams and report sections are simply skipped. CLI session transcripts, Cowork sidecar metadata, and full Cowork agent transcripts (incl. their `audit.jsonl`) all extract correctly.

## Tools

The repository is three small, composable tools. The orchestrator runs all
three end-to-end; the individual tools are useful on their own when you
want to script around them.

| Tool                                         | Purpose                                            | Docs                                       |
|----------------------------------------------|----------------------------------------------------|--------------------------------------------|
| [`claude-forensics.sh`](claude-forensics.sh) | End-to-end orchestrator: snapshot → extract → report → bundle | [docs/claude-forensics.md](docs/claude-forensics.md) |
| [`claude_forensics.py`](claude_forensics.py) | Extractor: `.claude` directory → four JSONL streams | [docs/claude_forensics.md](docs/claude_forensics.md) |
| [`claude_report.py`](claude_report.py)       | Reporter: JSONL → Markdown report (per-project or chronological) | [docs/claude_report.md](docs/claude_report.md) |

Cost estimation is configured through [`prices.example.json`](prices.example.json) — copy it to `prices.json` and fill in real rates. Without a pricing file the reports still include exact token counts; only dollar figures are skipped.

## Installation

No build step, no Python dependencies — everything is stdlib.

```sh
git clone https://github.com/<you>/claude-forensics.git
cd claude-forensics
```

That's enough to run the tool from the checkout directory itself. If you'd like a `claude-forensics` command on `PATH`, `install.sh` offers two modes:

### Symlink mode (default — recommended for developers)

```sh
./install.sh
```

Creates a single symlink `$PREFIX/claude-forensics → checkout/claude-forensics.sh`. The orchestrator follows the symlink at runtime to find its Python tools (`claude_forensics.py`, `claude_report.py`) in the checkout directory.

- **Pros**: zero duplication, `git pull` immediately updates everywhere, single-symlink uninstall.
- **Constraint**: **the checkout directory must remain in place**. If you `mv` or `rm -rf` it, the installed command breaks (the symlink dangles).

### Copy mode (recommended for end users)

```sh
./install.sh --copy
```

Copies the orchestrator **and** both Python tools (and `prices.example.json`) into a separate destination directory, then symlinks just the orchestrator onto `PATH`. After install, the checkout is no longer needed and can be deleted.

- **Pros**: self-contained — the install survives deleting or moving the checkout.
- **Tradeoff**: updates require re-running `./install.sh --copy` after a `git pull`.

### Overriding paths

Both modes honour environment variables:

```sh
PREFIX=$HOME/.local/bin ./install.sh                     # symlink elsewhere
PREFIX=$HOME/.local/bin ./install.sh --copy              # full copy elsewhere
TOOLS_DEST=/opt/claude-forensics ./install.sh --copy     # custom copy target
```

`PREFIX` defaults to `/usr/local/bin`. In `--copy` mode, `TOOLS_DEST` defaults to `$(dirname $PREFIX)/share/claude-forensics`.

### Uninstall

```sh
# symlink mode
rm /usr/local/bin/claude-forensics

# copy mode
rm -rf /usr/local/share/claude-forensics /usr/local/bin/claude-forensics
```

## Requirements

- **Python 3.10+** (no third-party packages).
- **bash 3.2+** (the version shipped with macOS works).
- **jq** *(optional)* — unlocks the three derived `.txt` / `.jsonl` files in phase 4. Without it those steps are skipped with a `[!]` warning instead of aborting.

## Retention

Claude Code rotates per-session transcripts on a periodic schedule (empirically about a 30-day window), leaving the matching prompts in `history.jsonl` but deleting the per-session `projects/<encoded-cwd>/*.jsonl` files. The tool exploits this asymmetry rather than fighting it:

- `prompts.jsonl` joins each `history.jsonl` entry to the on-disk transcript and surfaces those that no longer have one as **orphans**.
- `orphan-prompts.jsonl` (emitted by the orchestrator when `jq` is available) is the recovery channel for prompts whose transcripts have been rotated out — typically the only record left for anything older than the retention window.
- The executive summary's **Retention** section quotes `.claude/.last-cleanup` (the timestamp of the last rotation), the orphan/survivor counts, the date ranges of each group, and the implied retention window.

This means a stale or missing `.last-cleanup` plus a large surviving-transcript range is itself a signal worth noting (the machine has more historical data than the typical baseline).

## Forensic safety

- The orchestrator preserves evidence first: `cp -Rp` keeps mtimes and permissions, then `chmod -R a-w` makes the snapshot immutable. Every subsequent step reads from the snapshot, never the source.
- Every run produces a `MANIFEST.sha256` covering both the snapshot and all derived artifacts. Verify integrity with `sha256sum -c MANIFEST.sha256` after extracting the bundle. Set `GPG_KEY=...` to also produce a detached signature (`MANIFEST.sha256.asc`) so the manifest itself is attestable to a known investigator.
- The evidence tarball intentionally excludes the snapshot directory — keep the snapshot as the canonical read-only root and share the tarball as derived analysis.
- Malformed transcript lines are logged to `extract.log` and skipped, never silently dropped: they're evidence of mid-write crashes or manual editing.
- Models present in the data but absent from your pricing table are flagged by name in the report's *Pricing notes*, never silently zeroed.

## License

MIT — see [LICENSE](LICENSE).
