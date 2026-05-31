#!/usr/bin/env bash
#
# install.sh — make claude-forensics callable from PATH.
#
# Installs two commands:
#   claude-forensics      — the bash orchestrator (CLI)
#   claude-forensics-gui  — the Tkinter front-end (GUI window)
#
# Two install modes:
#
#   ./install.sh                  (default — symlink mode)
#     Creates symlinks from $PREFIX/{claude-forensics,
#     claude-forensics-gui} to the checkout's claude-forensics.sh and
#     claude_forensics_gui.py. The checkout directory must remain in
#     place — the orchestrator resolves the symlink to find the Python
#     tools alongside it. `git pull` updates everywhere instantly.
#     Best for developers, contributors, or anyone keeping a long-lived
#     clone.
#
#   ./install.sh --copy           (self-contained mode)
#     Copies claude-forensics.sh, claude_forensics.py, claude_report.py,
#     claude_forensics_gui.py, and prices.example.json into $TOOLS_DEST
#     (default: $(dirname $PREFIX)/share/claude-forensics), then
#     symlinks both commands into $PREFIX. The checkout can then be
#     deleted. Re-run `./install.sh --copy` after a `git pull` to
#     update. Best for end users who'll never touch the source.
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

# Files uploaded via GitHub's web UI land at mode 0644 — they don't
# carry the executable bit and won't run via their shebang. Restore +x
# on the two files that need it before doing anything else, so both
# install modes work after a web-UI re-upload of the repo.
chmod +x "$SRC/claude-forensics.sh" "$SRC/claude_forensics_gui.py" 2>/dev/null || true

case "$MODE" in
symlink)
    TARGET="$PREFIX/claude-forensics"
    GUI_TARGET="$PREFIX/claude-forensics-gui"
    ln -sf "$SRC/claude-forensics.sh"     "$TARGET"
    ln -sf "$SRC/claude_forensics_gui.py" "$GUI_TARGET"
    echo "installed (symlink mode):"
    echo "  $TARGET     -> $SRC/claude-forensics.sh"
    echo "  $GUI_TARGET -> $SRC/claude_forensics_gui.py"
    echo
    echo "Important: the checkout directory ($SRC) must remain in place."
    echo "Both the CLI and the GUI resolve the symlink to find the Python"
    echo "tools (claude_forensics.py, claude_report.py) alongside them."
    echo "If you move or delete the checkout, the installed commands break."
    echo
    echo "Try it:    claude-forensics ~/.claude"
    echo "GUI:       claude-forensics-gui     # needs Tk 8.6 (see below)"
    echo "Uninstall: rm $TARGET $GUI_TARGET"
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

    # Copy the orchestrator, both Python tools, the GUI, and the pricing
    # template to the destination directory. The orchestrator looks for
    # the Python tools at $TOOLS_DIR (its own resolved directory), and
    # the GUI looks for claude-forensics.sh next to itself, so placing
    # them all together lets everything run self-contained.
    cp "$SRC/claude-forensics.sh"     "$DEST/"
    cp "$SRC/claude_forensics.py"     "$DEST/"
    cp "$SRC/claude_report.py"        "$DEST/"
    cp "$SRC/claude_forensics_gui.py" "$DEST/"
    cp "$SRC/prices.example.json"     "$DEST/"
    chmod +x "$DEST/claude-forensics.sh" "$DEST/claude_forensics_gui.py"

    TARGET="$PREFIX/claude-forensics"
    GUI_TARGET="$PREFIX/claude-forensics-gui"
    ln -sf "$DEST/claude-forensics.sh"     "$TARGET"
    ln -sf "$DEST/claude_forensics_gui.py" "$GUI_TARGET"

    echo "installed (copy mode):"
    echo "  $TARGET     -> $DEST/claude-forensics.sh"
    echo "  $GUI_TARGET -> $DEST/claude_forensics_gui.py"
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
    echo "GUI:       claude-forensics-gui     # needs Tk 8.6 (see below)"
    echo "Uninstall: rm -rf $DEST $TARGET $GUI_TARGET"
    ;;
esac

# The GUI uses Tkinter. Apple's bundled Tk 8.5 on /usr/bin/python3 is
# broken on recent macOS; the GUI auto-falls-back to the clam theme so
# it remains usable, but the native experience needs a Tk 8.6 build.
# Flag this once at the end of every install so users know the
# difference between "the symlink is on PATH" and "the GUI actually
# renders nicely".
if [ "$(uname -s)" = "Darwin" ]; then
    echo
    echo "Note: claude-forensics-gui needs a Python with Tk 8.6 for a native"
    echo "      look. Apple's bundled /usr/bin/python3 ships Tk 8.5 which is"
    echo "      deprecated and renders ttk widgets poorly on recent macOS."
    echo "      For a clean install: brew install python-tk@3.13 (or @3.14)"
    echo "      then run: python3.13 \$(which claude-forensics-gui)"
    echo "      Non-technical end users should use the bundled .app — see"
    echo "      docs/build-app.md for the py2app build."
fi
