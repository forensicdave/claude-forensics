#!/usr/bin/env bash
#
# install.sh — make claude-forensics callable from PATH.
#
# Two install modes:
#
#   ./install.sh                  (default — symlink mode)
#     Creates a single symlink from $PREFIX/claude-forensics to the
#     checkout's claude-forensics.sh. The checkout directory must
#     remain in place — the orchestrator resolves the symlink to find
#     the Python tools alongside it. `git pull` updates everywhere
#     instantly. Best for developers, contributors, or anyone keeping
#     a long-lived clone.
#
#   ./install.sh --copy           (self-contained mode)
#     Copies claude-forensics.sh, claude_forensics.py, claude_report.py,
#     and prices.example.json into $TOOLS_DEST (default:
#     $(dirname $PREFIX)/share/claude-forensics), then symlinks the
#     orchestrator into $PREFIX. The checkout can then be deleted.
#     Re-run `./install.sh --copy` after a `git pull` to update.
#     Best for end users who'll never touch the source.
#
# Usage:
#   ./install.sh [--copy|--symlink]
#
# Environment:
#   PREFIX        Where the user-facing command is symlinked.
#                 Default: /usr/local/bin
#   TOOLS_DEST    Override the destination dir for --copy mode.
#                 Default: $(dirname $PREFIX)/share/claude-forensics

set -euo pipefail

usage() {
    sed -n '2,/^set -euo/{/^set -euo/q;p;}' "$0" \
        | sed 's/^# \{0,1\}//' >&2
}

MODE="symlink"
case "${1:-}" in
    --copy)    MODE="copy"    ;;
    --symlink) MODE="symlink" ;;
    "")        ;;
    -h|--help) usage; exit 0  ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
esac

PREFIX="${PREFIX:-/usr/local/bin}"
SRC="$(cd "$(dirname "$0")" && pwd)"

if [ ! -d "$PREFIX" ]; then
    echo "error: $PREFIX is not a directory" >&2
    echo "       set PREFIX=/some/bin/dir to install elsewhere" >&2
    exit 2
fi
if [ ! -w "$PREFIX" ]; then
    echo "error: $PREFIX is not writable by $(whoami)" >&2
    echo "       try: sudo PREFIX=$PREFIX $0 ${1:-}" >&2
    echo "       or:  PREFIX=\$HOME/.local/bin $0 ${1:-}" >&2
    exit 2
fi

case "$MODE" in
symlink)
    TARGET="$PREFIX/claude-forensics"
    ln -sf "$SRC/claude-forensics.sh" "$TARGET"
    echo "installed (symlink mode):"
    echo "  $TARGET -> $SRC/claude-forensics.sh"
    echo
    echo "Important: the checkout directory ($SRC) must remain in place."
    echo "The orchestrator resolves the symlink to find the Python tools"
    echo "(claude_forensics.py, claude_report.py) alongside it. If you"
    echo "move or delete the checkout, the installed command will break."
    echo
    echo "Try it:    claude-forensics ~/.claude"
    echo "Uninstall: rm $TARGET"
    ;;
copy)
    DEST="${TOOLS_DEST:-$(dirname "$PREFIX")/share/claude-forensics}"
    if ! mkdir -p "$DEST" 2>/dev/null; then
        echo "error: cannot create $DEST" >&2
        echo "       set TOOLS_DEST=/writable/path to install elsewhere" >&2
        exit 2
    fi
    if [ ! -w "$DEST" ]; then
        echo "error: $DEST is not writable by $(whoami)" >&2
        echo "       set TOOLS_DEST=/writable/path or rerun under sudo" >&2
        exit 2
    fi

    # Copy the orchestrator, both Python tools, and the pricing template
    # to the destination directory. The orchestrator looks for the Python
    # tools at $TOOLS_DIR (its own resolved directory), so placing them
    # all together lets the script run self-contained.
    cp "$SRC/claude-forensics.sh" "$DEST/"
    cp "$SRC/claude_forensics.py" "$DEST/"
    cp "$SRC/claude_report.py"    "$DEST/"
    cp "$SRC/prices.example.json" "$DEST/"
    chmod +x "$DEST/claude-forensics.sh"

    TARGET="$PREFIX/claude-forensics"
    ln -sf "$DEST/claude-forensics.sh" "$TARGET"

    echo "installed (copy mode):"
    echo "  $TARGET -> $DEST/claude-forensics.sh"
    echo "  support files copied into $DEST/:"
    echo "    claude_forensics.py"
    echo "    claude_report.py"
    echo "    prices.example.json"
    echo
    echo "The checkout directory ($SRC) is no longer needed and may be deleted."
    echo "To update after a 'git pull', re-run: ./install.sh --copy"
    echo
    echo "For cost estimation:"
    echo "  cp $DEST/prices.example.json $DEST/prices.json"
    echo "  \$EDITOR $DEST/prices.json   # fill in real per-million-token rates"
    echo "  (or pass -c /path/to/your-prices.json on the command line)"
    echo
    echo "Try it:    claude-forensics ~/.claude"
    echo "Uninstall: rm -rf $DEST $TARGET"
    ;;
esac
