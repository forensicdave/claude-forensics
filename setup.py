"""
py2app build config for Claude Forensics.app.

Build with:
    python3 -m pip install --user py2app   # one-time
    python3 setup.py py2app

The .app appears at  dist/Claude Forensics.app .
See docs/build-app.md for the longer story (universal binaries,
signing, distribution).

What ends up inside the bundle
------------------------------

Contents/Resources/
    claude_forensics_gui.py     ← main script (started by py2app)
    claude-forensics.sh         ← orchestrator the GUI shells out to
    claude_forensics.py         ← extractor the orchestrator calls
    claude_report.py            ← reporter the orchestrator calls
    prices.example.json         ← template for the Pricing tab

The GUI uses Path(__file__).parent to find the orchestrator. Inside the
bundle that resolves to Resources/, where DATA_FILES drops the bash
script and its Python tools — so claude_forensics_gui.find_orchestrator()
works without any path adjustments specific to the bundle.

The bundle does NOT ship its own python3 for the orchestrator. When the
bash script runs `python3 claude_forensics.py …`, it uses whatever
`python3` is on the user's $PATH. On any modern macOS that resolves to
/usr/bin/python3 (Apple's stock 3.9.x, available via Command Line
Tools), which is sufficient because the .py tools are stdlib-only and
use `from __future__ import annotations` for compatibility.
"""

from setuptools import setup

APP = ["claude_forensics_gui.py"]

DATA_FILES = [
    "claude-forensics.sh",
    "claude_forensics.py",
    "claude_report.py",
    "prices.example.json",
]

PLIST = {
    "CFBundleName":              "Claude Forensics",
    "CFBundleDisplayName":       "Claude Forensics",
    "CFBundleIdentifier":        "org.claude-forensics.gui",
    "CFBundleVersion":           "0.1.0",
    "CFBundleShortVersionString": "0.1.0",
    "NSHighResolutionCapable":   True,
    # We do not register any document types or URL schemes — the GUI
    # picks files via NSOpenPanel from inside the running app.
    "LSMinimumSystemVersion":    "11.0",
    "NSHumanReadableCopyright":  "MIT licensed — see LICENSE",
}

OPTIONS = {
    "plist":           PLIST,
    "argv_emulation":  False,   # no drag-and-drop onto the dock icon
    "optimize":        2,        # strip docstrings/asserts
    "strip":           True,
    # No icon yet — drop an .icns into the repo and add "iconfile" here
    # to give the .app a custom icon in the dock and Finder.
}

setup(
    name="Claude Forensics",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
