#!/usr/bin/env bash
#
# claude-forensics.sh — full extraction + report workflow for a .claude dir.
#
# Usage:
#   ./claude-forensics.sh [-c PRICING_TABLE] PATH_TO_CLAUDE_DIR [OUTPUT_DIR]
#
# Produces a timestamped working directory containing:
#   claude-snapshot/          read-only copy of the source .claude tree
#   inventory.txt             ls, du, transcript inventory (date / size / path)
#   sessions.jsonl            one record per Claude session (Q/A, tools, cwd)
#   prompts.jsonl             one record per prompt in history.jsonl
#   processes.jsonl           one record per sessions/<pid>.json
#   shell-snapshots.jsonl     one record per shell-snapshots/*.sh
#   report-by-project.md      per-project narrative report
#   report-chronological.md   timeline narrative report
#   bash-commands.txt         every Bash command Claude actually ran   (needs jq)
#   files-touched.txt         every file Claude Read/Write/Edited      (needs jq)
#   orphan-prompts.jsonl      prompts whose transcripts no longer exist (needs jq)
#   extract.log               extractor stderr (debug / parse warnings)
#   claude-forensics-*.tgz    tarball of everything except the snapshot
#
# Requirements: python3 (stdlib only) and the two scripts:
#   claude_forensics.py
#   claude_report.py
# both located in the same directory as this script (override with TOOLS_DIR).
# jq is optional; without it, three of the derived files above are skipped.
#
# Cost estimation (optional):
#   To include an estimated USD spend per session/project in the reports,
#   provide a pricing JSON. The script looks for one in this order:
#     1. -c FILE on the command line
#     2. $PRICING_TABLE (env var, if set and the file exists)
#     3. $TOOLS_DIR/prices.json (alongside the python tools)
#   See prices.example.json for the schema. Without a pricing file the
#   reports still include token counts; only the dollar figures are skipped.
#   prices.example.json is deliberately NOT auto-picked — its rates are zero
#   and silently emitting $0 figures would be misleading in a forensic
#   context. Copy it to prices.json and fill in real rates.
#
# Chain-of-custody manifest:
#   After all phases complete, a deterministic SHA-256 of every file in
#   the working directory is written to MANIFEST.sha256 (standard format,
#   verifiable with `sha256sum -c`). If $GPG_KEY is set and gpg is on
#   PATH, a detached signature MANIFEST.sha256.asc is also produced.
#   The bundle includes both, so the whole archive is attestable.

set -euo pipefail

CLAUDE_FORENSICS_VERSION="0.1.0"

# Print where the partially-built workdir lives if we abort partway through.
# We never auto-delete: the snapshot is chmod -R a-w, so cleanup needs human
# judgement (and the partial output is often interesting in its own right).
on_exit() {
    local status=$?
    if [ "$status" -ne 0 ] && [ -n "${WORK:-}" ] && [ -d "$WORK" ]; then
        echo >&2
        echo "[!] aborted with exit $status" >&2
        echo "    partial output at: $WORK" >&2
        echo "    inspect or remove manually (snapshot dirs are chmod a-w)" >&2
    fi
    return "$status"
}
trap on_exit EXIT

# -------------------------------------------------------------------- args ---

usage() {
    cat <<EOF >&2
Usage: $0 [options] PATH_TO_CLAUDE_DIR [OUTPUT_DIR]
       $0 --verify BUNDLE.tgz

Options:
  -c, --cost-table FILE   JSON pricing table for spend estimation
  -w, --cowork-dir PATH   Path to a copied Claude Desktop data dir
                          (use this when analysing evidence from another host)
  -W, --include-cowork    Auto-use ~/Library/Application Support/Claude/
                          (macOS only — use this on your own machine)
      --verify FILE.tgz   Verify an existing claude-forensics bundle by
                          checking SHA-256 of every file against MANIFEST.sha256
                          (and the detached GPG signature if present)
  -V, --version           Print version and exit
  -h, --help              Show this help

Environment:
  FORCE=1                 Bypass the disk-space precheck
  GPG_KEY=<keyid>         Sign the manifest with this key
  TOOLS_DIR=PATH          Where claude_forensics.py + claude_report.py live
                          (default: directory containing this script)
  PRICING_TABLE=PATH      Cost-table fallback if -c not supplied
EOF
}

# --verify FILE.tgz: extract the bundle to a temp dir, run `sha256sum -c`
# against the embedded MANIFEST.sha256, then `gpg --verify` if a signature
# is present. Exits non-zero on any failure. Cleans up the temp dir.
verify_bundle() {
    local bundle=$1
    if [ ! -f "$bundle" ]; then
        echo "error: not a file: $bundle" >&2
        return 2
    fi
    local hash_cmd=""
    if   command -v sha256sum >/dev/null 2>&1; then hash_cmd="sha256sum"
    elif command -v shasum    >/dev/null 2>&1; then hash_cmd="shasum -a 256"
    else
        echo "error: neither sha256sum nor shasum found" >&2
        return 2
    fi

    local bundle_hash
    bundle_hash=$($hash_cmd "$bundle" | awk '{print $1}')
    echo "[*] bundle:      $bundle"
    echo "[*] bundle hash: $bundle_hash"

    local tmp
    tmp=$(mktemp -d -t claude-forensics-verify.XXXXXX)
    trap 'rm -rf "$tmp"' RETURN

    echo "[*] extracting to $tmp ..."
    if ! tar -xzf "$bundle" -C "$tmp" 2>/dev/null; then
        echo "[!] failed to extract bundle" >&2
        return 1
    fi

    if [ ! -f "$tmp/MANIFEST.sha256" ]; then
        echo "[!] no MANIFEST.sha256 inside bundle — cannot verify integrity" >&2
        return 1
    fi

    echo "[*] verifying file checksums against MANIFEST.sha256 ..."
    local pass=1
    if ! (cd "$tmp" && $hash_cmd -c MANIFEST.sha256 >/dev/null 2>&1); then
        # Re-run without --quiet equivalent to surface the failed file names.
        (cd "$tmp" && $hash_cmd -c MANIFEST.sha256 2>&1 \
            | grep -E 'FAILED|No such file' >&2) || true
        echo "[!] one or more checksums FAILED" >&2
        pass=0
    fi

    if [ -f "$tmp/MANIFEST.sha256.asc" ]; then
        echo "[*] verifying detached GPG signature ..."
        if command -v gpg >/dev/null 2>&1; then
            if gpg --verify "$tmp/MANIFEST.sha256.asc" \
                            "$tmp/MANIFEST.sha256" >/dev/null 2>&1; then
                echo "[*] GPG signature: GOOD"
            else
                echo "[!] GPG signature FAILED" >&2
                pass=0
            fi
        else
            echo "[!] gpg not on PATH; cannot verify signature (file integrity"
            echo "    was still checked, but the manifest itself is not attested)"
        fi
    else
        echo "[*] no GPG signature in bundle (file integrity verified only)"
    fi

    if [ "$pass" -eq 1 ]; then
        echo "[*] VERIFIED OK"
        return 0
    fi
    echo "[!] VERIFICATION FAILED"
    return 1
}

COST_FILE=""
COWORK_DIR=""
INCLUDE_COWORK=0
VERIFY_BUNDLE=""
while [ $# -gt 0 ]; do
    case "$1" in
        -c|--cost-table)
            [ "$#" -lt 2 ] && { echo "$1 requires an argument" >&2; usage; exit 2; }
            COST_FILE=$2; shift 2 ;;
        -w|--cowork-dir)
            [ "$#" -lt 2 ] && { echo "$1 requires an argument" >&2; usage; exit 2; }
            COWORK_DIR=$2; shift 2 ;;
        -W|--include-cowork)
            INCLUDE_COWORK=1; shift ;;
        --verify)
            [ "$#" -lt 2 ] && { echo "$1 requires an argument" >&2; usage; exit 2; }
            VERIFY_BUNDLE=$2; shift 2 ;;
        -V|--version)
            echo "claude-forensics $CLAUDE_FORENSICS_VERSION"
            exit 0 ;;
        -h|--help)
            usage; exit 0 ;;
        --) shift; break ;;
        -*) echo "unknown option: $1" >&2; usage; exit 2 ;;
        *) break ;;
    esac
done

# --verify is mutually exclusive with the analysis pipeline. Run it and exit.
if [ -n "$VERIFY_BUNDLE" ]; then
    verify_bundle "$VERIFY_BUNDLE"
    exit $?
fi

# -W with no -w: fill in the macOS default. Other OSes are unknown territory;
# we error out rather than guess (use -w PATH explicitly).
if [ -z "$COWORK_DIR" ] && [ "$INCLUDE_COWORK" = 1 ]; then
    case "$(uname -s)" in
        Darwin) COWORK_DIR="$HOME/Library/Application Support/Claude" ;;
        *) echo "error: -W only knows the macOS default path; use -w PATH on $(uname -s)" >&2
           exit 2 ;;
    esac
fi
if [ -n "$COWORK_DIR" ] && [ ! -d "$COWORK_DIR" ]; then
    echo "error: cowork dir does not exist: $COWORK_DIR" >&2
    exit 2
fi

if [ "$#" -lt 1 ]; then
    usage
    exit 2
fi

TARGET=$1
OUT_BASE=${2:-$PWD}

# Resolve $0 through any symlinks so TOOLS_DIR points at the real script dir
# even when this script was installed via install.sh (which symlinks it into
# $PREFIX/claude-forensics). macOS readlink lacks -f, so we walk the chain
# manually with portable POSIX shell.
_self=$0
while [ -L "$_self" ]; do
    _target=$(readlink "$_self")
    case "$_target" in
        /*) _self=$_target ;;
        *)  _self=$(cd "$(dirname "$_self")" && pwd)/$_target ;;
    esac
done
TOOLS_DIR=${TOOLS_DIR:-$(cd "$(dirname "$_self")" && pwd)}

if [ ! -d "$TARGET" ]; then
    echo "error: not a directory: $TARGET" >&2
    exit 2
fi
for tool in claude_forensics.py claude_report.py; do
    if [ ! -f "$TOOLS_DIR/$tool" ]; then
        echo "error: missing $TOOLS_DIR/$tool (set TOOLS_DIR=... to override)" >&2
        exit 2
    fi
done

# Resolve TARGET and OUT_BASE to absolute paths now. Without this, a
# relative TARGET like ".claude" stops resolving once we `cd` into the
# new working directory below.
TARGET=$(cd "$TARGET" && pwd)
mkdir -p "$OUT_BASE"
OUT_BASE=$(cd "$OUT_BASE" && pwd)
if [ -n "$COWORK_DIR" ]; then
    COWORK_DIR=$(cd "$COWORK_DIR" && pwd)
fi

# Disk-space precheck: sum the source sizes we're about to copy (TARGET
# plus the small Cowork subset if applicable), pad by 30% for derived
# artifacts (reports, JSONL streams, per-session files, bundle), and abort
# early if $OUT_BASE doesn't have that much free. FORCE=1 in the env
# bypasses the check entirely.
sources_kb=$(du -sk "$TARGET" 2>/dev/null | awk '{print $1}')
if [ -n "$COWORK_DIR" ]; then
    for d in claude-code-sessions local-agent-mode-sessions; do
        if [ -d "$COWORK_DIR/$d" ]; then
            sub_kb=$(du -sk "$COWORK_DIR/$d" 2>/dev/null | awk '{print $1}')
            sources_kb=$((sources_kb + sub_kb))
        fi
    done
fi
needed_kb=$((sources_kb * 13 / 10))
avail_kb=$(df -k "$OUT_BASE" | awk 'NR==2 {print $4}')
if [ "$avail_kb" -lt "$needed_kb" ]; then
    echo "[!] insufficient disk space in $OUT_BASE" >&2
    echo "    available: $((avail_kb / 1024)) MB" >&2
    echo "    estimated need: $((needed_kb / 1024)) MB (source $((sources_kb / 1024)) MB + 30% margin)" >&2
    if [ -z "${FORCE:-}" ]; then
        echo "    set FORCE=1 to bypass this check, or free more space" >&2
        exit 2
    fi
    echo "[!] FORCE=1 — proceeding despite shortfall" >&2
fi
echo "[*] disk space: $((avail_kb / 1024)) MB free in $OUT_BASE, $((needed_kb / 1024)) MB estimated"

WORK="$OUT_BASE/claude-forensics-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$WORK"
cd "$WORK"
echo "[*] working in $WORK"

# ---------------------------------------------- phase 0: preserve evidence ---
#
# Copy the .claude tree to a read-only snapshot. -Rp preserves mtimes and
# permissions so subsequent mtime-based reasoning isn't poisoned. chmod a-w
# makes the snapshot immutable to every downstream step — any attempt to
# write will fail loudly rather than silently mutating evidence.

echo "[*] preserving snapshot..."
cp -Rp "$TARGET" ./claude-snapshot
chmod -R a-w ./claude-snapshot
SNAP=$PWD/claude-snapshot

# Optional Cowork snapshot. We deliberately copy only the small forensically-
# interesting subset of the Claude Desktop data dir; the full tree includes
# multi-GB caches (vm_bundles/, Cache/, Code Cache/) that bloat the bundle
# with no investigative value. Edit the lists below to widen the scope.
COWORK_SNAP=""
if [ -n "$COWORK_DIR" ]; then
    echo "[*] preserving Cowork subset from $COWORK_DIR..."
    mkdir -p ./cowork-snapshot
    for d in claude-code-sessions local-agent-mode-sessions; do
        if [ -d "$COWORK_DIR/$d" ]; then
            cp -Rp "$COWORK_DIR/$d" ./cowork-snapshot/
        fi
    done
    for f in cowork-enabled-cli-ops.json claude_desktop_config.json \
             config.json buddy-tokens.json ant-did; do
        if [ -f "$COWORK_DIR/$f" ]; then
            cp -p "$COWORK_DIR/$f" ./cowork-snapshot/
        fi
    done
    chmod -R a-w ./cowork-snapshot
    COWORK_SNAP=$PWD/cowork-snapshot
fi

# ----------------------------------------------------- phase 1: inventory ---
#
# Top-level layout, per-subtree sizes, and a transcript inventory listing
# every .jsonl with its date and size. The transcript inventory is the
# cheapest pivot for spotting crashed sessions (tiny files) or unusual
# time clusters (deletion events, automation bursts).

echo "[*] writing inventory.txt..."
{
    echo "## ls -la"
    ls -la "$SNAP"
    echo
    echo "## du -sh subtrees"
    du -sh "$SNAP"/* 2>/dev/null | sort -h
    echo
    echo "## transcript inventory (date / size / path)"
    find "$SNAP/projects" -name '*.jsonl' -type f \
        -exec stat -f '%Sm %z %N' -t '%Y-%m-%d' {} + 2>/dev/null \
        | sort \
        || find "$SNAP/projects" -name '*.jsonl' -type f \
               -printf '%TY-%Tm-%Td %s %p\n' 2>/dev/null | sort
} > inventory.txt

# --------------------------------------- phase 2: extract structured data ---
#
# One extractor pass produces four JSONL streams plus a debug log. --debug
# captures every transcript file read and every malformed JSON line — those
# malformed lines are evidence of mid-write crashes or manual editing of
# the transcripts.

cowork_extract_args=()
if [ -n "$COWORK_SNAP" ]; then
    cowork_extract_args=(--cowork-dir "$COWORK_SNAP"
                         --cowork-out  cowork-sessions.jsonl
                         --cowork-agent-out   cowork-agent-sessions.jsonl)
fi

echo "[*] extracting JSONL..."
python3 "$TOOLS_DIR/claude_forensics.py" \
    --claude-dir       "$SNAP" \
    --output           sessions.jsonl \
    --prompts-out      prompts.jsonl \
    --processes-out    processes.jsonl \
    --shell-out        shell-snapshots.jsonl \
    --paste-cache-out  paste-cache.jsonl \
    --file-history-out file-history.jsonl \
    ${cowork_extract_args[@]+"${cowork_extract_args[@]}"} \
    --debug 2> extract.log

# ------------------------------------------------- phase 3: render reports --
#
# Both reports use --full so nothing gets truncated. The chronological one
# also embeds the unified prompt timeline from history.jsonl with a ✓/✗
# column showing whether each prompt's transcript still exists.

# Resolve the optional cost-table file. Precedence: -c flag, then
# $PRICING_TABLE env var, then $TOOLS_DIR/prices.json. We deliberately do
# NOT auto-pick prices.example.json — its values are zeros and silently
# emitting $0 figures would be misleading in a forensic context.
cost_args=()
if [ -n "$COST_FILE" ] && [ -f "$COST_FILE" ]; then
    cost_args=(--cost-table "$COST_FILE")
    echo "[*] pricing table: $COST_FILE (from -c)"
elif [ -n "${PRICING_TABLE:-}" ] && [ -f "$PRICING_TABLE" ]; then
    cost_args=(--cost-table "$PRICING_TABLE")
    echo "[*] pricing table: $PRICING_TABLE (from \$PRICING_TABLE)"
elif [ -f "$TOOLS_DIR/prices.json" ]; then
    cost_args=(--cost-table "$TOOLS_DIR/prices.json")
    echo "[*] pricing table: $TOOLS_DIR/prices.json (auto-detected)"
else
    echo "[!] no pricing table found; reports will show tokens but not spend."
    echo "    To include estimated spend:"
    echo "      cp $TOOLS_DIR/prices.example.json $TOOLS_DIR/prices.json"
    echo "      edit prices.json with real per-million-token rates, then re-run"
    echo "    or pass -c /path/to/your-prices.json on the command line."
fi

# --prompts is only meaningful if the extractor actually produced one
# (history.jsonl exists in the source). Otherwise the reporter would
# abort on a missing file.
prompt_args=()
if [ -f prompts.jsonl ]; then
    prompt_args=(--prompts prompts.jsonl)
fi

# Pass the optional auxiliary JSONLs to the summary so the count rows
# render (paste-cache entries, file-history records).
aux_args=()
[ -f paste-cache.jsonl  ] && aux_args+=(--paste-jsonl        paste-cache.jsonl)
[ -f file-history.jsonl ] && aux_args+=(--file-history-jsonl file-history.jsonl)

# If the extractor produced Cowork session metadata, annotate every session
# row in the rendered reports with title / archived state / owner-account.
cowork_report_args=()
[ -f cowork-sessions.jsonl ] && cowork_report_args=(--cowork-jsonl cowork-sessions.jsonl)

# .claude/.last-cleanup holds the timestamp of Claude Code's last transcript
# rotation. Surface it to the executive summary so the Retention section can
# name it explicitly. Missing file → empty string → summary section just
# infers retention from prompt orphans instead.
cleanup_args=()
if [ -f "$SNAP/.last-cleanup" ]; then
    last_cleanup=$(cat "$SNAP/.last-cleanup")
    if [ -n "$last_cleanup" ]; then
        cleanup_args=(--last-cleanup "$last_cleanup")
    fi
fi

echo "[*] rendering per-project report (markdown)..."
python3 "$TOOLS_DIR/claude_report.py" \
    --sessions sessions.jsonl \
    ${prompt_args[@]+"${prompt_args[@]}"} \
    ${cowork_report_args[@]+"${cowork_report_args[@]}"} \
    --order project --include-exchanges --full \
    ${cost_args[@]+"${cost_args[@]}"} \
    --output report-by-project.md

echo "[*] rendering per-project report (html)..."
python3 "$TOOLS_DIR/claude_report.py" \
    --sessions sessions.jsonl \
    ${prompt_args[@]+"${prompt_args[@]}"} \
    ${cowork_report_args[@]+"${cowork_report_args[@]}"} \
    --order project --include-exchanges --format html \
    ${cost_args[@]+"${cost_args[@]}"} \
    --output report-by-project.html

echo "[*] rendering chronological report (markdown)..."
python3 "$TOOLS_DIR/claude_report.py" \
    --sessions sessions.jsonl \
    ${prompt_args[@]+"${prompt_args[@]}"} \
    ${cowork_report_args[@]+"${cowork_report_args[@]}"} \
    --order chronological --include-exchanges --full \
    ${cost_args[@]+"${cost_args[@]}"} \
    --output report-chronological.md

echo "[*] rendering per-session reports (claude-code-sessions/)..."
python3 "$TOOLS_DIR/claude_report.py" \
    --sessions sessions.jsonl \
    ${cost_args[@]+"${cost_args[@]}"} \
    --per-session-dir claude-code-sessions

echo "[*] rendering executive summary (markdown + html)..."
python3 "$TOOLS_DIR/claude_report.py" \
    --sessions sessions.jsonl \
    ${prompt_args[@]+"${prompt_args[@]}"} \
    --summary \
    ${cost_args[@]+"${cost_args[@]}"} \
    ${aux_args[@]+"${aux_args[@]}"} \
    ${cleanup_args[@]+"${cleanup_args[@]}"} \
    --output summary.md
python3 "$TOOLS_DIR/claude_report.py" \
    --sessions sessions.jsonl \
    ${prompt_args[@]+"${prompt_args[@]}"} \
    --summary --format html \
    ${cost_args[@]+"${cost_args[@]}"} \
    ${aux_args[@]+"${aux_args[@]}"} \
    ${cleanup_args[@]+"${cleanup_args[@]}"} \
    --output summary.html

# Parallel agent-session report set. Same renderer, distinct titles so the
# two surfaces (Claude Code CLI vs Cowork local agent) are visually separate.
# Skipped when no cowork-agent-sessions.jsonl was produced (no Cowork dir given,
# or it contained no local-agent-mode-sessions/).
if [ -f cowork-agent-sessions.jsonl ]; then
    echo "[*] rendering Cowork agent per-session reports (claude-cowork-sessions/)..."
    python3 "$TOOLS_DIR/claude_report.py" \
        --sessions cowork-agent-sessions.jsonl \
        ${cost_args[@]+"${cost_args[@]}"} \
        --per-session-dir claude-cowork-sessions

    echo "[*] rendering Cowork agent report (per-account markdown)..."
    python3 "$TOOLS_DIR/claude_report.py" \
        --sessions cowork-agent-sessions.jsonl \
        --title "Cowork agent usage report" \
        --order project --include-exchanges --full \
        ${cost_args[@]+"${cost_args[@]}"} \
        --output cowork-agent-report-by-account.md

    echo "[*] rendering Cowork agent report (per-account html)..."
    python3 "$TOOLS_DIR/claude_report.py" \
        --sessions cowork-agent-sessions.jsonl \
        --title "Cowork agent usage report" \
        --order project --include-exchanges --format html \
        ${cost_args[@]+"${cost_args[@]}"} \
        --output cowork-agent-report-by-account.html

    echo "[*] rendering Cowork agent report (chronological markdown)..."
    python3 "$TOOLS_DIR/claude_report.py" \
        --sessions cowork-agent-sessions.jsonl \
        --title "Cowork agent usage report" \
        --order chronological --include-exchanges --full \
        ${cost_args[@]+"${cost_args[@]}"} \
        --output cowork-agent-report-chronological.md

    echo "[*] rendering Cowork agent summary (markdown + html)..."
    python3 "$TOOLS_DIR/claude_report.py" \
        --sessions cowork-agent-sessions.jsonl \
        --title "Cowork agent — executive summary" \
        --summary ${cost_args[@]+"${cost_args[@]}"} \
        --output cowork-agent-summary.md
    python3 "$TOOLS_DIR/claude_report.py" \
        --sessions cowork-agent-sessions.jsonl \
        --title "Cowork agent — executive summary" \
        --summary --format html ${cost_args[@]+"${cost_args[@]}"} \
        --output cowork-agent-summary.html
fi

# ------------------------------------------- phase 4: derived facts via jq --
#
# Only runs if jq is on PATH. The bash-commands and files-touched lists
# read directly from the snapshot transcripts so they capture every tool
# invocation — useful when you want to ask "what did Claude actually do"
# rather than just "how many times".

if command -v jq >/dev/null 2>&1; then
    echo "[*] extracting bash-commands.txt..."
    find "$SNAP/projects" -name '*.jsonl' -exec cat {} + \
        | jq -rc 'select(.type=="assistant")
                  | .message.content[]?
                  | select(.type=="tool_use" and .name=="Bash")
                  | .input.command' 2>/dev/null > bash-commands.txt || true

    echo "[*] extracting files-touched.txt..."
    find "$SNAP/projects" -name '*.jsonl' -exec cat {} + \
        | jq -rc 'select(.type=="assistant")
                  | .message.content[]?
                  | select(.type=="tool_use"
                           and (.name=="Read" or .name=="Write" or .name=="Edit"))
                  | .input.file_path' 2>/dev/null \
        | sort -u > files-touched.txt || true

    if [ -f prompts.jsonl ]; then
        echo "[*] extracting orphan-prompts.jsonl..."
        jq -c 'select(.transcript_present==false)' prompts.jsonl \
            > orphan-prompts.jsonl
    fi
else
    echo "[!] jq not on PATH; skipping bash-commands / files-touched / orphans"
fi

# ------------------------- phase 5: assemble bundle file list + manifest ----
#
# We build the bundle file list FIRST, then compute the manifest over only
# those files. The snapshot directory is deliberately NOT in either: it is
# the canonical immutable evidence root, integrity-protected by
# `chmod -R a-w` in phase 0. Investigators who need long-term snapshot
# integrity should hash the snapshot directory separately as part of their
# own chain-of-custody process — keeping it out of the bundle manifest
# means `--verify` against a received bundle gives a clean pass/fail
# without spurious "file not found" noise.

# Files that are always written: inventory, the per-session JSONL, the two
# reports, and the extractor log. Everything else is conditional — prompts /
# processes / shell snapshots only exist if the source .claude has the
# corresponding subtree; the jq-derived files only exist if jq was on PATH.
files=(
    inventory.txt
    sessions.jsonl
    report-by-project.md report-by-project.html
    report-chronological.md
    summary.md summary.html
    extract.log
)
for f in prompts.jsonl processes.jsonl shell-snapshots.jsonl \
         paste-cache.jsonl file-history.jsonl \
         cowork-sessions.jsonl cowork-agent-sessions.jsonl \
         cowork-agent-report-by-account.md cowork-agent-report-by-account.html \
         cowork-agent-report-chronological.md \
         cowork-agent-summary.md cowork-agent-summary.html \
         bash-commands.txt files-touched.txt orphan-prompts.jsonl; do
    if [ -f "$f" ]; then
        files+=("$f")
    fi
done
# Per-session directories — tar follows them recursively.
for d in claude-code-sessions claude-cowork-sessions; do
    if [ -d "$d" ]; then
        files+=("$d")
    fi
done

# Compute the manifest now — over the leaf files inside the bundle file
# list — so it precisely matches what ends up in the tarball. Verify will
# then give a clean pass/fail when run against the bundle alone.
HASH_CMD=""
if   command -v sha256sum >/dev/null 2>&1; then HASH_CMD="sha256sum"
elif command -v shasum    >/dev/null 2>&1; then HASH_CMD="shasum -a 256"
fi

if [ -n "$HASH_CMD" ]; then
    echo "[*] computing SHA-256 manifest..."
    {
        for entry in "${files[@]}"; do
            if [ -d "$entry" ]; then
                find "$entry" -type f -print0
            elif [ -f "$entry" ]; then
                printf '%s\0' "$entry"
            fi
        done
    } | LC_ALL=C sort -z | xargs -0 $HASH_CMD > MANIFEST.sha256
    files+=(MANIFEST.sha256)

    if [ -n "${GPG_KEY:-}" ] && command -v gpg >/dev/null 2>&1; then
        echo "[*] signing manifest with GPG key $GPG_KEY..."
        if gpg --batch --yes --armor --detach-sign \
               --local-user "$GPG_KEY" \
               --output MANIFEST.sha256.asc \
               MANIFEST.sha256 2>/dev/null; then
            files+=(MANIFEST.sha256.asc)
        else
            echo "[!] gpg signing failed; manifest is unsigned"
            rm -f MANIFEST.sha256.asc
        fi
    elif [ -n "${GPG_KEY:-}" ]; then
        echo "[!] GPG_KEY is set but gpg is not on PATH; manifest is unsigned"
    fi
else
    echo "[!] neither sha256sum nor shasum found; skipping manifest"
fi

# ----------------------------------------------------- phase 6: bundle ------
#
# Single tarball of the assembled file list above (manifest included). The
# snapshot directories are intentionally omitted — they're the canonical
# read-only evidence roots and you usually want to manage them separately.

BUNDLE="claude-forensics-$(date +%Y%m%d-%H%M%S).tgz"
echo "[*] writing $BUNDLE..."
tar -czf "$BUNDLE" "${files[@]}"

# The bundle's own SHA-256 — record this externally (ticket, email, ledger)
# so anyone receiving the .tgz later can confirm it's the same archive.
BUNDLE_HASH=""
if [ -n "$HASH_CMD" ]; then
    BUNDLE_HASH=$($HASH_CMD "$BUNDLE" | awk '{print $1}')
fi

# Final summary. The artifact list is the single source of truth for what
# this script can produce — descriptions match the per-tool docs. Each
# entry is "name|description"; entries whose file doesn't exist are
# silently skipped, so the printed list reflects what actually ran.
echo
echo "[*] done. Output in $WORK"
echo
echo "Artifacts:"

artifacts=(
    "claude-snapshot|read-only copy of the source .claude tree (immutable)"
    "cowork-snapshot|read-only copy of the Claude Desktop subset (Cowork)"
    "inventory.txt|ls / du / per-transcript inventory of the source"
    "sessions.jsonl|one record per Claude session (Q/A, tools, tokens, model)"
    "prompts.jsonl|one record per prompt in history.jsonl, joined to transcripts"
    "processes.jsonl|one record per sessions/<pid>.json (PID, status, version)"
    "shell-snapshots.jsonl|one record per shell-snapshots/*.sh (PATH, aliases, exports)"
    "paste-cache.jsonl|one record per paste-cache/<hash>.txt, joined to prompts"
    "file-history.jsonl|one record per (session, file) with all versioned backups"
    "cowork-sessions.jsonl|one record per Cowork session (title, owner, joined to transcript)"
    "cowork-agent-sessions.jsonl|one record per Cowork agent session, with full audit.jsonl transcript"
    "cowork-agent-report-by-account.md|Cowork agent sessions, grouped by VM cwd (markdown)"
    "cowork-agent-report-by-account.html|Cowork agent sessions, grouped by VM cwd (HTML)"
    "cowork-agent-report-chronological.md|Cowork agent sessions in time order (markdown)"
    "cowork-agent-summary.md|Cowork agent one-page summary (markdown)"
    "cowork-agent-summary.html|Cowork agent one-page summary (HTML)"
    "claude-code-sessions|per-session reports for the Claude Code CLI (md + html each)"
    "claude-cowork-sessions|per-session reports for Cowork agent sessions (md + html each)"
    "summary.md|one-page executive summary (markdown)"
    "summary.html|one-page executive summary (self-contained HTML)"
    "report-by-project.md|per-project narrative report (markdown)"
    "report-by-project.html|per-project narrative report (self-contained HTML)"
    "report-chronological.md|timeline narrative report (markdown)"
    "bash-commands.txt|every Bash command Claude actually ran"
    "files-touched.txt|every file Claude Read/Write/Edited, sorted unique"
    "orphan-prompts.jsonl|prompts whose transcripts no longer exist"
    "extract.log|extractor stderr (parse warnings, debug info)"
    "MANIFEST.sha256|SHA-256 of every artifact above; verify with sha256sum -c"
    "MANIFEST.sha256.asc|detached GPG signature of MANIFEST.sha256"
    "$BUNDLE|evidence bundle (tarball of all derived artifacts above)"
)

for entry in "${artifacts[@]}"; do
    name=${entry%%|*}
    desc=${entry#*|}
    if [ -e "$name" ]; then
        printf "    %-36s - %s\n" "$name" "$desc"
    fi
done

if [ -n "$BUNDLE_HASH" ]; then
    echo
    echo "Bundle SHA-256: $BUNDLE_HASH"
    echo "(record this externally as a chain-of-custody anchor)"
fi
