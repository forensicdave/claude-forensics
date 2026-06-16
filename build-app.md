# Building Claude Forensics.app

A short guide to turning `claude_forensics_gui.py` into a double-clickable
macOS `.app` so end users never have to touch the terminal.

The build is driven by [`setup.py`](../setup.py) and `py2app`. The
produced `.app` bundles the Tkinter GUI, the bash orchestrator, both
Python tools, and the pricing template, so the user only needs to drag
the `.app` into `/Applications` to install.

## Prerequisites

- macOS (the build host must match the architecture you want to ship).
- Python **3.10+** with working **Tk 8.6** — the bundled Tk in the build
  Python is what ends up inside the `.app`. Apple's `/usr/bin/python3`
  ships Tk 8.5 and is **not** suitable for the build. Use one of:
  - `brew install python-tk@3.13` (or `@3.14`)
  - python.org installer (3.12+ ships Tk 8.6.x)
- `py2app`:

  ```sh
  python3 -m pip install --user py2app
  ```

  (Replace `python3` with the same interpreter you'll build with —
  `python3.13`, `python3.14`, etc.)

## Build

From the repo root:

```sh
python3.13 setup.py py2app
```

The bundle appears at:

```
dist/Claude Forensics.app
```

Drag it into `/Applications` (or anywhere else). Launching it opens the
GUI window directly — no terminal needed.

## What's inside the bundle

```
Claude Forensics.app/
└── Contents/
    ├── Info.plist                      ← name, version, identifier
    ├── MacOS/
    │   └── Claude Forensics            ← py2app launcher binary
    ├── Frameworks/Python.framework/    ← bundled CPython runtime
    └── Resources/
        ├── __boot__.py                 ← py2app bootstrapper
        ├── claude_forensics_gui.py     ← the GUI itself
        ├── claude-forensics.sh         ← bash orchestrator
        ├── claude_forensics.py         ← extractor
        ├── claude_report.py            ← reporter
        └── prices.example.json         ← Pricing-tab template
```

The GUI's `find_orchestrator()` looks for `claude-forensics.sh` next
to itself, which inside a bundle is the `Resources/` directory — so the
GUI finds the orchestrator without any bundle-specific path logic.

## What the bundle does NOT contain

The orchestrator's Python sub-tools are invoked via `python3` from
the user's `$PATH`. The bundle does **not** ship its own `python3` for
that — when `claude-forensics.sh` shells out to `python3
claude_forensics.py …`, it uses whichever `python3` macOS resolves
first on `$PATH`. On any modern Mac that's `/usr/bin/python3` (Apple's
3.9, available as part of the Command Line Tools). If those aren't
installed, macOS prompts the user to install them on first run.

This is intentional — bundling a second Python just to drive the
orchestrator would roughly double the bundle size, and `/usr/bin/python3`
is universally available on macOS. If you want to remove that
dependency, the cleanest path is to make `claude-forensics.sh` honour a
`PYTHON=…` environment variable and have the GUI set it to the bundled
interpreter (`Contents/Frameworks/Python.framework/.../bin/python3`).

## Unsigned vs signed builds

The default build above is **unsigned**. macOS Gatekeeper blocks
unsigned `.app`s with an "unidentified developer" dialog on first
launch. To open it anyway:

1. **Finder → right-click the .app → Open**
2. Click *Open* in the confirmation dialog.

After that one-time bypass, double-clicking works normally.

To eliminate the warning for end users you need an Apple Developer ID
and a signing + notarization pass.

### Why `codesign --deep` is NOT enough for py2app bundles

`codesign --deep` walks framework and bundle boundaries but ignores
loose Mach-O files in arbitrary `Contents/Resources/` subdirectories.
A py2app bundle contains ~80+ Python C extensions
(`Contents/Resources/lib/python3.X/lib-dynload/*.so`) plus
`Contents/Resources/zlib.cpython-*.so`, none of which `--deep` reaches.
Notarization checks every Mach-O in the bundle and will reject the
submission with errors like:

```
The binary is not signed with a valid Developer ID certificate.
  path: …/Contents/Resources/lib/python3.X/lib-dynload/_ssl.so
The signature does not include a secure timestamp.
  path: …/Contents/Resources/lib/python3.X/lib-dynload/_ssl.so
```

The fix is to sign **inside-out**: every `.so` and `.dylib`, then the
embedded `Python.framework`, then the outer `.app`. Apple's guidance
since Big Sur has been to sign explicitly rather than rely on `--deep`.

### Full sign + notarize + staple sequence

```sh
IDENT="Developer ID Application: Your Name (TEAMID)"
PROFILE="my-notary-profile"   # set up with `xcrun notarytool store-credentials`

# 0. Sanity check: no dangling symlinks. py2app sometimes drops a
#    symlink at Contents/Resources/lib/python3.X/site.pyo pointing at
#    a non-existent ../../site.pyo. Gatekeeper rejects bundles with
#    broken symlinks ("invalid destination for symbolic link in
#    bundle"), so prune them BEFORE signing.
find "dist/Claude Forensics.app" -type l ! -exec test -e {} \; -delete

# 1. Sign every .so and .dylib inside the bundle (covers the loose
#    Python C extensions that --deep does not reach).
find "dist/Claude Forensics.app" -type f \( -name "*.so" -o -name "*.dylib" \) \
  -exec codesign --force --options runtime --timestamp --sign "$IDENT" {} +

# 2. Sign the embedded Python.framework's main binary, then the
#    framework bundle itself.
codesign --force --options runtime --timestamp --sign "$IDENT" \
  "dist/Claude Forensics.app/Contents/Frameworks/Python.framework/Versions/3.14/Python"
codesign --force --options runtime --timestamp --sign "$IDENT" \
  "dist/Claude Forensics.app/Contents/Frameworks/Python.framework"

# 3. Sign the secondary python Mach-O that py2app drops in MacOS/.
codesign --force --options runtime --timestamp --sign "$IDENT" \
  "dist/Claude Forensics.app/Contents/MacOS/python"

# 4. Sign the outer .app LAST so its seal covers everything above.
codesign --force --options runtime --timestamp --sign "$IDENT" \
  "dist/Claude Forensics.app"

# 5. Submit to Apple's notary service (waits ~2-10 min).
ditto -c -k --keepParent "dist/Claude Forensics.app" "Claude-Forensics.app.zip"
xcrun notarytool submit "Claude-Forensics.app.zip" \
      --keychain-profile "$PROFILE" --wait

# 6. Staple the notarization ticket so the .app works offline.
xcrun stapler staple "dist/Claude Forensics.app"

# 7. Re-zip the now-stapled .app for distribution.
rm "Claude-Forensics.app.zip"
ditto -c -k --keepParent "dist/Claude Forensics.app" "Claude-Forensics.app.zip"

# 8. Verify Gatekeeper accepts it.
spctl -a -t exec -vv "dist/Claude Forensics.app"
# Expected: "source=Notarized Developer ID"
```

Adjust the Python framework path in step 2 to match the Python you
built with (`Versions/3.13/Python`, `Versions/3.14/Python`, etc.).

`$PROFILE` is an app-specific password stored in your keychain via
`xcrun notarytool store-credentials <profile-name> --apple-id …
--team-id …`. Full Apple docs:
<https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution>.

### If notarization is rejected

The `submit` call ends with `status: Invalid`. Get the detailed log
with:

```sh
xcrun notarytool log <submission-id> --keychain-profile "$PROFILE"
```

The JSON `issues[]` array names every binary that failed and why. The
most common cause for this project is missing the inside-out sign
pass above — the log will list a long tail of `.so` files in
`Contents/Resources/lib/python3.X/lib-dynload/`. Re-run steps 1-4 and
resubmit.

### Local `codesign --verify` quirk

`codesign --verify --deep --strict` may print

```
--prepared:…/Contents/MacOS/python
--validated:…/Contents/MacOS/python
…/Claude Forensics.app: No such file or directory
```

even when the bundle is correctly signed. py2app puts two Mach-O
files in `Contents/MacOS/` (the launcher and an embedded `python`),
which confuses codesign's bundle walker. The notarization service
does its own checks and is the authoritative answer — if notary
returns `Accepted`, the signature is fine regardless of what local
`--verify` says. Confirm with:

```sh
codesign --verify -R="anchor apple generic and certificate leaf[subject.OU] = TEAMID" \
         "dist/Claude Forensics.app"
```

That variant exits 0 when the signature satisfies its designated
requirement.

Signing/notarization is outside this repo's default `python3 setup.py
py2app` workflow. Maintain it in whatever CI handles the public
release if you go that route.

## Universal binaries

By default, py2app produces an architecture-specific build matching the
Python you ran it with. To produce a universal binary you must build
with a universal Python (e.g. python.org's 3.12 installer, which is
universal2 by default). Add to `OPTIONS` in `setup.py`:

```python
OPTIONS = {
    ...,
    "arch": "universal2",
}
```

Apple Silicon-only builds (the default with `brew install python-tk@3.13`
on an M-series Mac) work on Apple Silicon and run under Rosetta on
older Intel hardware, but a real `universal2` binary is preferable for
public distribution.

## Distribution

For internal distribution, sharing the `.app` directly via Slack / file
share / SMB works — recipients still need to do the right-click → Open
dance unless the bundle is signed.

For wider distribution:

1. Sign + notarize as above.
2. Zip the `.app` (`ditto -c -k --keepParent`).
3. Host the zip and link from the README, OR ship via a DMG built with
   `create-dmg` or `hdiutil`.

## Troubleshooting

**"The system version of Tk is deprecated" warning when building.**
You ran py2app under `/usr/bin/python3`. Use a Tk 8.6 Python instead
(see Prerequisites). The resulting `.app` will inherit whatever Tk
shipped with the build Python.

**`.app` opens to a blank window.** Same root cause — the build Python
had Tk 8.5. Rebuild under a Tk 8.6 Python.

**`.app` opens but "Run analysis" fails with `python3: command not
found`.** The host Mac doesn't have Command Line Tools installed. Run
`xcode-select --install` once on that machine; the orchestrator then
finds `/usr/bin/python3` on `$PATH`.
