#!/usr/bin/env python3
"""
claude_forensics_gui.py — Tkinter front-end for claude-forensics.sh.

A point-and-click wrapper around the bash orchestrator so investigators
without terminal experience can run a full forensic extraction, verify
a received bundle, or edit a pricing table. Three tabs:

  * Analyze — configure and run claude-forensics.sh against a .claude dir
  * Verify  — check the integrity of a claude-forensics-*.tgz bundle
  * Pricing — edit a prices.json cost table (schema: prices.example.json)

The GUI shells out to the same claude-forensics.sh that ships next to it,
so behaviour stays in sync with the CLI. Live stdout is streamed into a
read-only log; the produced working directory is parsed out of the
"[*] working in ..." line so the post-run "Open output folder" /
"Open summary.html" buttons know what to open.

Requires: Python 3.10+ (stdlib only). On macOS, the bundled Tk works.
"""

from __future__ import annotations

import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText


# ---------------------------------------------------------------- discovery ---

HERE = Path(__file__).resolve().parent


def find_orchestrator() -> Path | None:
    """Locate claude-forensics.sh next to this file, then on PATH.

    Returns None if not found; the GUI surfaces the error in a dialog so
    the user knows what to do (re-place the file, or run install.sh).
    """
    here = HERE / "claude-forensics.sh"
    if here.is_file():
        return here
    on_path = shutil.which("claude-forensics")
    if on_path:
        return Path(on_path)
    return None


def is_macos() -> bool:
    return platform.system() == "Darwin"


def open_in_finder(path: Path) -> None:
    """Open a file or directory using the OS's default handler."""
    p = str(path)
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", p], check=False)
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", p], check=False)
        else:
            webbrowser.open(p)
    except Exception as exc:
        messagebox.showerror("Could not open", f"{path}\n\n{exc}")


# ---------------------------------------------------------- subprocess runner -

class CommandRunner:
    """Run a subprocess in a background thread, stream lines via queue.

    Lines are tagged so the consumer can distinguish output from the
    final exit code. The GUI polls the queue from the Tk main loop with
    root.after() and updates the log/state on each tick.
    """

    LINE = "line"
    EXIT = "exit"
    ERROR = "error"

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue[tuple[str, str | int]] = queue.Queue()
        self._lock = threading.Lock()

    def start(self, argv: list[str], env: dict[str, str] | None = None,
              cwd: str | None = None) -> None:
        with self._lock:
            if self.proc and self.proc.poll() is None:
                raise RuntimeError("a command is already running")

            def worker() -> None:
                try:
                    self.proc = subprocess.Popen(
                        argv, env=env, cwd=cwd,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        bufsize=1, universal_newlines=True,
                    )
                    assert self.proc.stdout is not None
                    for line in self.proc.stdout:
                        self.q.put((self.LINE, line.rstrip("\n")))
                    rc = self.proc.wait()
                    self.q.put((self.EXIT, rc))
                except Exception as exc:
                    self.q.put((self.ERROR, str(exc)))

            threading.Thread(target=worker, daemon=True).start()

    def stop(self) -> None:
        with self._lock:
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.terminate()
                except Exception:
                    pass

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


# --------------------------------------------------------------- Analyze tab --

# "[*] working in /path/to/claude-forensics-YYYYMMDD-HHMMSS" — the orchestrator
# always prints this once it has created the timestamped working directory.
_WORKING_DIR_RE = re.compile(r"\[\*\]\s+working in\s+(.+?)\s*$")


class RunTab(ttk.Frame):
    """Configure and launch claude-forensics.sh."""

    def __init__(self, parent: tk.Misc, orchestrator: Path | None) -> None:
        super().__init__(parent, padding=10)
        self.orchestrator = orchestrator
        self.runner = CommandRunner()
        self.work_dir: Path | None = None

        self.source_var = tk.StringVar(value=str(Path.home() / ".claude"))
        self.output_var = tk.StringVar(value=str(Path.home() / "Desktop"))
        self.cowork_mode = tk.StringVar(value="none")
        self.cowork_path = tk.StringVar()
        self.pricing_path = tk.StringVar()
        self.gpg_key = tk.StringVar()
        self.force = tk.BooleanVar(value=False)

        self._build()

    # ------------------------------------------------------------ layout ----

    def _build(self) -> None:
        row = 0
        ttk.Label(self, text="Source .claude directory",
                  font=("", 11, "bold")).grid(row=row, column=0, sticky="w",
                                              columnspan=3)
        row += 1
        ttk.Entry(self, textvariable=self.source_var, width=60).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(self, text="Browse…",
                   command=self._pick_source).grid(row=row, column=2,
                                                   sticky="ew", padx=(6, 0))
        row += 1

        ttk.Label(self, text="Output directory (where results are written)",
                  font=("", 11, "bold")).grid(row=row, column=0, sticky="w",
                                              columnspan=3, pady=(10, 0))
        row += 1
        ttk.Entry(self, textvariable=self.output_var, width=60).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(self, text="Browse…",
                   command=self._pick_output).grid(row=row, column=2,
                                                   sticky="ew", padx=(6, 0))
        row += 1

        # ---- Cowork group --------------------------------------------------
        ttk.Label(self, text="Claude Desktop (Cowork) data",
                  font=("", 11, "bold")).grid(row=row, column=0, sticky="w",
                                              columnspan=3, pady=(10, 0))
        row += 1
        ttk.Radiobutton(self, text="Do not include",
                        value="none", variable=self.cowork_mode,
                        command=self._sync_cowork).grid(row=row, column=0,
                                                        sticky="w")
        row += 1
        rb_auto = ttk.Radiobutton(
            self, text="Auto-detect (macOS: ~/Library/Application Support/Claude)",
            value="auto", variable=self.cowork_mode,
            command=self._sync_cowork)
        rb_auto.grid(row=row, column=0, columnspan=3, sticky="w")
        if not is_macos():
            rb_auto.state(["disabled"])
        row += 1
        ttk.Radiobutton(self, text="Specify a copied Claude Desktop directory:",
                        value="path", variable=self.cowork_mode,
                        command=self._sync_cowork).grid(row=row, column=0,
                                                        columnspan=3,
                                                        sticky="w")
        row += 1
        self.cowork_entry = ttk.Entry(self, textvariable=self.cowork_path,
                                      width=60)
        self.cowork_entry.grid(row=row, column=0, columnspan=2, sticky="ew",
                               pady=2, padx=(20, 0))
        self.cowork_btn = ttk.Button(self, text="Browse…",
                                     command=self._pick_cowork)
        self.cowork_btn.grid(row=row, column=2, sticky="ew", padx=(6, 0))
        row += 1

        # ---- Pricing -------------------------------------------------------
        ttk.Label(self, text="Pricing table (optional, for cost estimation)",
                  font=("", 11, "bold")).grid(row=row, column=0, sticky="w",
                                              columnspan=3, pady=(10, 0))
        row += 1
        ttk.Entry(self, textvariable=self.pricing_path, width=60).grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(self, text="Browse…",
                   command=self._pick_pricing).grid(row=row, column=2,
                                                    sticky="ew", padx=(6, 0))
        row += 1
        ttk.Label(self,
                  text="(Leave blank to skip cost estimates. Use the Pricing tab to create one.)",
                  foreground="gray").grid(row=row, column=0, columnspan=3,
                                          sticky="w")
        row += 1

        # ---- GPG / FORCE ---------------------------------------------------
        ttk.Label(self, text="Advanced options",
                  font=("", 11, "bold")).grid(row=row, column=0, sticky="w",
                                              columnspan=3, pady=(10, 0))
        row += 1
        ttk.Label(self, text="GPG signing key (optional):").grid(
            row=row, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.gpg_key, width=40).grid(
            row=row, column=1, columnspan=2, sticky="ew", pady=2)
        row += 1
        ttk.Checkbutton(self, text="Bypass disk-space precheck (FORCE=1)",
                        variable=self.force).grid(row=row, column=0,
                                                  columnspan=3, sticky="w")
        row += 1

        # ---- Buttons + log -------------------------------------------------
        btns = ttk.Frame(self)
        btns.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        self.run_btn = ttk.Button(btns, text="Run analysis",
                                  command=self._on_run)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Stop", command=self._on_stop,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        self.status_label = ttk.Label(btns, text="", foreground="gray")
        self.status_label.pack(side="left", padx=(12, 0))
        row += 1

        ttk.Label(self, text="Log", font=("", 11, "bold")).grid(
            row=row, column=0, sticky="w", pady=(8, 0))
        row += 1
        self.log = ScrolledText(self, height=14, wrap="word",
                                font=("Menlo", 11))
        self.log.grid(row=row, column=0, columnspan=3, sticky="nsew", pady=2)
        self.log.configure(state="disabled")
        self.rowconfigure(row, weight=1)
        row += 1

        # ---- Post-run actions ----------------------------------------------
        self.post_frame = ttk.Frame(self)
        self.post_frame.grid(row=row, column=0, columnspan=3, sticky="ew",
                             pady=(4, 0))
        self.open_dir_btn = ttk.Button(self.post_frame,
                                       text="Open output folder",
                                       command=self._open_work_dir,
                                       state="disabled")
        self.open_dir_btn.pack(side="left")
        self.open_summary_btn = ttk.Button(self.post_frame,
                                           text="Open summary.html",
                                           command=self._open_summary,
                                           state="disabled")
        self.open_summary_btn.pack(side="left", padx=(6, 0))
        self.open_report_btn = ttk.Button(self.post_frame,
                                          text="Open report-by-project.html",
                                          command=self._open_report,
                                          state="disabled")
        self.open_report_btn.pack(side="left", padx=(6, 0))

        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self._sync_cowork()

    # ------------------------------------------------------------ pickers ---

    def _pick_source(self) -> None:
        p = filedialog.askdirectory(
            title="Select the .claude directory to analyse",
            initialdir=self.source_var.get() or str(Path.home()))
        if p:
            self.source_var.set(p)

    def _pick_output(self) -> None:
        p = filedialog.askdirectory(
            title="Select an output directory",
            initialdir=self.output_var.get() or str(Path.home()))
        if p:
            self.output_var.set(p)

    def _pick_cowork(self) -> None:
        p = filedialog.askdirectory(
            title="Select a copied Claude Desktop data directory",
            initialdir=self.cowork_path.get() or str(Path.home()))
        if p:
            self.cowork_path.set(p)
            self.cowork_mode.set("path")
            self._sync_cowork()

    def _pick_pricing(self) -> None:
        p = filedialog.askopenfilename(
            title="Select a pricing JSON",
            initialdir=str(HERE),
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if p:
            self.pricing_path.set(p)

    def _sync_cowork(self) -> None:
        enabled = self.cowork_mode.get() == "path"
        state = "normal" if enabled else "disabled"
        self.cowork_entry.configure(state=state)
        self.cowork_btn.configure(state=state)

    # --------------------------------------------------------------- run ----

    def _on_run(self) -> None:
        if self.orchestrator is None:
            messagebox.showerror(
                "claude-forensics.sh not found",
                "This GUI could not locate claude-forensics.sh.\n\n"
                "Place claude_forensics_gui.py next to claude-forensics.sh, "
                "or install the orchestrator first (./install.sh).")
            return

        source = self.source_var.get().strip()
        output = self.output_var.get().strip()
        if not source or not Path(source).is_dir():
            messagebox.showerror("Source directory missing",
                                 "Pick a .claude directory to analyse.")
            return
        if not output:
            messagebox.showerror("Output directory missing",
                                 "Pick an output directory.")
            return
        Path(output).mkdir(parents=True, exist_ok=True)

        argv: list[str] = [str(self.orchestrator)]
        pricing = self.pricing_path.get().strip()
        if pricing:
            if not Path(pricing).is_file():
                messagebox.showerror("Pricing file missing",
                                     f"Not a file:\n{pricing}")
                return
            argv += ["-c", pricing]
        mode = self.cowork_mode.get()
        if mode == "auto":
            argv += ["-W"]
        elif mode == "path":
            cp = self.cowork_path.get().strip()
            if not cp or not Path(cp).is_dir():
                messagebox.showerror("Cowork path missing",
                                     "Pick a copied Claude Desktop directory.")
                return
            argv += ["-w", cp]
        argv += [source, output]

        env = os.environ.copy()
        if self.force.get():
            env["FORCE"] = "1"
        gpg = self.gpg_key.get().strip()
        if gpg:
            env["GPG_KEY"] = gpg

        self._clear_log()
        self._append_log(f"$ {' '.join(_shell_quote(a) for a in argv)}\n")
        self.work_dir = None
        self._set_post_buttons("disabled")
        self.status_label.configure(text="running…", foreground="orange")
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

        try:
            self.runner.start(argv, env=env)
        except Exception as exc:
            messagebox.showerror("Failed to start", str(exc))
            self._on_finished(-1)
            return
        self.after(80, self._drain)

    def _on_stop(self) -> None:
        self.runner.stop()
        self.status_label.configure(text="stopping…", foreground="orange")

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.runner.q.get_nowait()
                if kind == CommandRunner.LINE:
                    line = str(payload)
                    self._append_log(line + "\n")
                    m = _WORKING_DIR_RE.search(line)
                    if m:
                        self.work_dir = Path(m.group(1).strip())
                elif kind == CommandRunner.EXIT:
                    self._on_finished(int(payload))
                    return
                elif kind == CommandRunner.ERROR:
                    self._append_log(f"\n[GUI] error: {payload}\n")
                    self._on_finished(-1)
                    return
        except queue.Empty:
            pass
        if self.runner.is_running() or not self.runner.q.empty():
            self.after(80, self._drain)

    def _on_finished(self, rc: int) -> None:
        self.run_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        if rc == 0:
            self.status_label.configure(text=f"done (exit 0)",
                                        foreground="green")
            if self.work_dir and self.work_dir.is_dir():
                self._set_post_buttons("normal")
        else:
            self.status_label.configure(text=f"failed (exit {rc})",
                                        foreground="red")
            if self.work_dir and self.work_dir.is_dir():
                self.open_dir_btn.configure(state="normal")

    # ----------------------------------------------------------- post-run --

    def _set_post_buttons(self, state: str) -> None:
        for b in (self.open_dir_btn, self.open_summary_btn, self.open_report_btn):
            b.configure(state=state)

    def _open_work_dir(self) -> None:
        if self.work_dir:
            open_in_finder(self.work_dir)

    def _open_summary(self) -> None:
        if self.work_dir:
            p = self.work_dir / "summary.html"
            if p.is_file():
                open_in_finder(p)
            else:
                messagebox.showinfo("Not found",
                                    f"{p.name} was not produced.")

    def _open_report(self) -> None:
        if self.work_dir:
            p = self.work_dir / "report-by-project.html"
            if p.is_file():
                open_in_finder(p)
            else:
                messagebox.showinfo("Not found",
                                    f"{p.name} was not produced.")

    # ---------------------------------------------------------------- log --

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")


# ---------------------------------------------------------------- Verify tab -

class VerifyTab(ttk.Frame):
    """Run `claude-forensics.sh --verify BUNDLE.tgz`."""

    def __init__(self, parent: tk.Misc, orchestrator: Path | None) -> None:
        super().__init__(parent, padding=10)
        self.orchestrator = orchestrator
        self.runner = CommandRunner()
        self.bundle_path = tk.StringVar()
        self._build()

    def _build(self) -> None:
        ttk.Label(self, text="Bundle to verify (.tgz)",
                  font=("", 11, "bold")).grid(row=0, column=0, sticky="w",
                                              columnspan=3)
        ttk.Entry(self, textvariable=self.bundle_path, width=60).grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(self, text="Browse…", command=self._pick).grid(
            row=1, column=2, sticky="ew", padx=(6, 0))

        btns = ttk.Frame(self)
        btns.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        self.verify_btn = ttk.Button(btns, text="Verify bundle",
                                     command=self._on_verify)
        self.verify_btn.pack(side="left")
        self.result_label = ttk.Label(btns, text="", font=("", 12, "bold"))
        self.result_label.pack(side="left", padx=(12, 0))

        ttk.Label(self, text="Log", font=("", 11, "bold")).grid(
            row=3, column=0, sticky="w", pady=(8, 0))
        self.log = ScrolledText(self, height=20, wrap="word",
                                font=("Menlo", 11))
        self.log.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=2)
        self.log.configure(state="disabled")

        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(4, weight=1)

    def _pick(self) -> None:
        p = filedialog.askopenfilename(
            title="Select a claude-forensics-*.tgz bundle",
            filetypes=[("Tar gzip", "*.tgz *.tar.gz"), ("All files", "*.*")],
            initialdir=str(Path.home()))
        if p:
            self.bundle_path.set(p)

    def _on_verify(self) -> None:
        if self.orchestrator is None:
            messagebox.showerror(
                "claude-forensics.sh not found",
                "This GUI could not locate claude-forensics.sh.")
            return
        bundle = self.bundle_path.get().strip()
        if not bundle or not Path(bundle).is_file():
            messagebox.showerror("Bundle missing",
                                 "Pick a .tgz bundle to verify.")
            return
        argv = [str(self.orchestrator), "--verify", bundle]

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", f"$ {' '.join(_shell_quote(a) for a in argv)}\n")
        self.log.configure(state="disabled")
        self.result_label.configure(text="verifying…", foreground="orange")
        self.verify_btn.configure(state="disabled")

        try:
            self.runner.start(argv)
        except Exception as exc:
            messagebox.showerror("Failed to start", str(exc))
            self.verify_btn.configure(state="normal")
            self.result_label.configure(text="")
            return
        self.after(80, self._drain)

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.runner.q.get_nowait()
                if kind == CommandRunner.LINE:
                    self._append(str(payload) + "\n")
                elif kind == CommandRunner.EXIT:
                    self._on_finished(int(payload))
                    return
                elif kind == CommandRunner.ERROR:
                    self._append(f"\n[GUI] error: {payload}\n")
                    self._on_finished(-1)
                    return
        except queue.Empty:
            pass
        if self.runner.is_running() or not self.runner.q.empty():
            self.after(80, self._drain)

    def _on_finished(self, rc: int) -> None:
        self.verify_btn.configure(state="normal")
        if rc == 0:
            self.result_label.configure(text="✅ VERIFIED OK",
                                        foreground="green")
        else:
            self.result_label.configure(text="❌ VERIFICATION FAILED",
                                        foreground="red")

    def _append(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")


# --------------------------------------------------------------- Pricing tab -

# Mirror of the schema in prices.example.json / claude_report.py.
_RATE_KEYS: tuple[str, ...] = (
    "input_per_mtok",
    "output_per_mtok",
    "cache_read_per_mtok",
    "cache_write_5m_per_mtok",
    "cache_write_1h_per_mtok",
)
_RATE_LABELS: dict[str, str] = {
    "input_per_mtok":          "input",
    "output_per_mtok":         "output",
    "cache_read_per_mtok":     "cache read",
    "cache_write_5m_per_mtok": "cache write 5m",
    "cache_write_1h_per_mtok": "cache write 1h",
}


class ModelRow:
    """One row of the pricing-editor table: name + five rate Entries."""

    def __init__(self, parent: ttk.Frame, name: str = "",
                 rates: dict[str, float] | None = None) -> None:
        self.frame = ttk.Frame(parent)
        self.name = tk.StringVar(value=name)
        self.values: dict[str, tk.StringVar] = {}
        ttk.Entry(self.frame, textvariable=self.name, width=24).grid(
            row=0, column=0, padx=2)
        for col, key in enumerate(_RATE_KEYS, start=1):
            v = tk.StringVar(
                value=_fmt_rate((rates or {}).get(key, 0.0)))
            self.values[key] = v
            ttk.Entry(self.frame, textvariable=v, width=10,
                      justify="right").grid(row=0, column=col, padx=2)
        self.remove_btn = ttk.Button(self.frame, text="✕", width=2,
                                     command=self._noop)
        self.remove_btn.grid(row=0, column=len(_RATE_KEYS) + 1, padx=(8, 0))

    def _noop(self) -> None:
        pass

    def grid(self, **kw: object) -> None:
        self.frame.grid(**kw)

    def destroy(self) -> None:
        self.frame.destroy()

    def to_dict(self) -> tuple[str, dict[str, float]]:
        name = self.name.get().strip()
        rates: dict[str, float] = {}
        for key in _RATE_KEYS:
            try:
                rates[key] = float(self.values[key].get() or 0)
            except ValueError:
                rates[key] = 0.0
        return name, rates


def _fmt_rate(value: float) -> str:
    """Render a rate as a short numeric string for an Entry widget."""
    if value == int(value):
        return f"{value:.1f}"
    return f"{value:g}"


class PricingTab(ttk.Frame):
    """Form-based editor for prices.json files."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, padding=10)
        self.rows: list[ModelRow] = []
        self.current_path: Path | None = None

        self.effective_date = tk.StringVar(value=str(date.today()))
        self.currency = tk.StringVar(value="USD")
        self.web_search = tk.StringVar(value="0.0")
        self.web_fetch = tk.StringVar(value="0.0")

        self._build()
        self._add_row("claude-opus-4-7")
        self._add_row("claude-sonnet-4-6")
        self._add_row("claude-haiku-4-5")

    # ------------------------------------------------------------ layout ----

    def _build(self) -> None:
        row = 0
        ttk.Label(self, text="Pricing table",
                  font=("", 12, "bold")).grid(row=row, column=0, sticky="w",
                                              columnspan=4)
        ttk.Label(self, text="(prices.json schema — leave at 0 to skip a rate)",
                  foreground="gray").grid(row=row, column=4, columnspan=3,
                                          sticky="w")
        row += 1

        # File toolbar
        bar = ttk.Frame(self)
        bar.grid(row=row, column=0, columnspan=8, sticky="ew", pady=(6, 0))
        ttk.Button(bar, text="Load…", command=self._on_load).pack(side="left")
        ttk.Button(bar, text="Save",
                   command=self._on_save).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Save as…",
                   command=self._on_save_as).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Load template",
                   command=self._on_load_template).pack(side="left",
                                                        padx=(18, 0))
        self.file_label = ttk.Label(bar, text="(unsaved)", foreground="gray")
        self.file_label.pack(side="left", padx=(18, 0))
        row += 1

        # Metadata
        meta = ttk.LabelFrame(self, text="Metadata", padding=8)
        meta.grid(row=row, column=0, columnspan=8, sticky="ew", pady=(10, 0))
        ttk.Label(meta, text="Effective date:").grid(row=0, column=0,
                                                     sticky="w")
        ttk.Entry(meta, textvariable=self.effective_date,
                  width=14).grid(row=0, column=1, padx=(4, 16))
        ttk.Label(meta, text="Currency:").grid(row=0, column=2, sticky="w")
        ttk.Entry(meta, textvariable=self.currency, width=8).grid(
            row=0, column=3, padx=(4, 0))
        row += 1

        # Models header
        ttk.Label(self, text="Models (per-million-token rates)",
                  font=("", 11, "bold")).grid(row=row, column=0,
                                              columnspan=8, sticky="w",
                                              pady=(12, 2))
        row += 1
        hdr = ttk.Frame(self)
        hdr.grid(row=row, column=0, columnspan=8, sticky="ew")
        ttk.Label(hdr, text="model id", width=24,
                  font=("", 10, "bold")).grid(row=0, column=0, padx=2)
        for col, key in enumerate(_RATE_KEYS, start=1):
            ttk.Label(hdr, text=_RATE_LABELS[key], width=10,
                      anchor="e", font=("", 10, "bold")).grid(row=0,
                                                              column=col,
                                                              padx=2)
        row += 1

        # Scrollable container for model rows
        self.rows_frame = ttk.Frame(self)
        self.rows_frame.grid(row=row, column=0, columnspan=8,
                             sticky="nsew", pady=2)
        self.rowconfigure(row, weight=1)
        row += 1

        ttk.Button(self, text="+ Add model",
                   command=lambda: self._add_row("")).grid(
            row=row, column=0, sticky="w", pady=(4, 0))
        row += 1

        # Server tools
        srv = ttk.LabelFrame(self, text="Server tools (per request)", padding=8)
        srv.grid(row=row, column=0, columnspan=8, sticky="ew", pady=(12, 0))
        ttk.Label(srv, text="web_search_per_request:").grid(row=0, column=0,
                                                            sticky="w")
        ttk.Entry(srv, textvariable=self.web_search, width=12,
                  justify="right").grid(row=0, column=1, padx=(4, 16))
        ttk.Label(srv, text="web_fetch_per_request:").grid(row=0, column=2,
                                                           sticky="w")
        ttk.Entry(srv, textvariable=self.web_fetch, width=12,
                  justify="right").grid(row=0, column=3, padx=(4, 0))
        row += 1

        for c in range(8):
            self.columnconfigure(c, weight=1)

    # ----------------------------------------------------------- model rows --

    def _add_row(self, name: str = "",
                 rates: dict[str, float] | None = None) -> None:
        r = ModelRow(self.rows_frame, name=name, rates=rates)
        r.grid(row=len(self.rows), column=0, sticky="ew", pady=1)
        r.remove_btn.configure(command=lambda row=r: self._remove_row(row))
        self.rows.append(r)

    def _remove_row(self, row: ModelRow) -> None:
        row.destroy()
        self.rows.remove(row)

    def _clear_rows(self) -> None:
        for r in self.rows:
            r.destroy()
        self.rows.clear()

    # ----------------------------------------------------------- file ops ---

    def _on_load(self) -> None:
        p = filedialog.askopenfilename(
            title="Load a pricing JSON",
            initialdir=str(HERE),
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if not p:
            return
        try:
            self._load_from(Path(p))
        except Exception as exc:
            messagebox.showerror("Could not load", str(exc))

    def _on_load_template(self) -> None:
        tpl = HERE / "prices.example.json"
        if not tpl.is_file():
            messagebox.showerror("Template missing",
                                 f"prices.example.json was not found at\n{tpl}")
            return
        try:
            self._load_from(tpl)
        except Exception as exc:
            messagebox.showerror("Could not load template", str(exc))
        # Loading the template starts fresh — don't bind to the template path.
        self.current_path = None
        self.file_label.configure(text="(unsaved — based on template)")

    def _load_from(self, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("pricing file must be a JSON object")
        self.effective_date.set(str(data.get("effective_date", "")))
        self.currency.set(str(data.get("currency", "USD")))
        srv = data.get("server_tools") or {}
        self.web_search.set(_fmt_rate(float(srv.get("web_search_per_request")
                                            or 0)))
        self.web_fetch.set(_fmt_rate(float(srv.get("web_fetch_per_request")
                                           or 0)))
        self._clear_rows()
        for name, rates in (data.get("models") or {}).items():
            self._add_row(name, rates if isinstance(rates, dict) else {})
        self.current_path = path
        self.file_label.configure(text=str(path))

    def _build_doc(self) -> dict:
        models: dict[str, dict[str, float]] = {}
        for r in self.rows:
            name, rates = r.to_dict()
            if not name:
                continue
            models[name] = rates
        try:
            ws = float(self.web_search.get() or 0)
        except ValueError:
            ws = 0.0
        try:
            wf = float(self.web_fetch.get() or 0)
        except ValueError:
            wf = 0.0
        return {
            "effective_date": self.effective_date.get().strip(),
            "currency": self.currency.get().strip() or "USD",
            "models": models,
            "server_tools": {
                "web_search_per_request": ws,
                "web_fetch_per_request": wf,
            },
        }

    def _on_save(self) -> None:
        if self.current_path is None:
            self._on_save_as()
            return
        self._write(self.current_path)

    def _on_save_as(self) -> None:
        p = filedialog.asksaveasfilename(
            title="Save pricing JSON as…",
            initialdir=str(self.current_path.parent if self.current_path else HERE),
            initialfile="prices.json",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if p:
            self._write(Path(p))

    def _write(self, path: Path) -> None:
        try:
            doc = self._build_doc()
            path.write_text(json.dumps(doc, indent=2) + "\n",
                            encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("Could not save", str(exc))
            return
        self.current_path = path
        self.file_label.configure(text=str(path))
        messagebox.showinfo("Saved", f"Wrote {path}")


# ------------------------------------------------------------------- helpers -

_SHELL_SAFE = re.compile(r"^[A-Za-z0-9_\-./=:@%+,]+$")


def _shell_quote(s: str) -> str:
    """Render a command-line arg for the log so it can be copied and reused."""
    if not s:
        return "''"
    if _SHELL_SAFE.match(s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


# ----------------------------------------------------------------------- App -

def _apply_safe_theme(root: tk.Tk) -> tuple[str, str]:
    """Pick a ttk theme that actually renders on the current Tk build.

    The `aqua` theme in Tk 8.5 (Apple's bundled Tk on /usr/bin/python3
    3.9) is broken on recent macOS releases — widgets are constructed
    but never drawn, leaving a blank window. The fix is either a newer
    Python (python.org 3.12+, brew python-tk@3.13/14) or a non-aqua
    theme. We auto-switch to `clam` on Tk < 8.6 so the GUI is at least
    usable, even though it'll look generic instead of native.

    Returns (tk_patchlevel, theme_in_use) so the caller can log it.
    """
    patch = root.tk.eval("info patchlevel")
    style = ttk.Style()
    override = os.environ.get("TK_THEME")
    if override and override in style.theme_names():
        style.theme_use(override)
        return patch, override
    # Tk patchlevels look like "8.5.9" / "8.6.13" — compare as tuples.
    parts = tuple(int(p) for p in patch.split(".") if p.isdigit())
    if parts < (8, 6) and "clam" in style.theme_names():
        style.theme_use("clam")
    return patch, style.theme_use()


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Claude Forensics")
        self.geometry("900x780")
        self.minsize(720, 600)

        tk_patch, theme = _apply_safe_theme(self)
        print(f"[gui] Tk {tk_patch}, theme={theme}, "
              f"python={sys.version.split()[0]}", file=sys.stderr)
        if tk_patch.startswith("8.5"):
            print("[gui] Note: Tk 8.5 is unsupported on recent macOS — "
                  "widgets fell back to the 'clam' theme.\n"
                  "      For a native look, run this GUI with python.org "
                  "Python 3.12+ or 'brew install python-tk@3.13' (or @3.14).",
                  file=sys.stderr)

        orchestrator = find_orchestrator()

        # Footer first so `side=bottom` claims its slice before the expanding
        # notebook below; otherwise an `expand=True` notebook packed first can
        # squeeze the bottom widget to zero height.
        if orchestrator is None:
            msg = ("⚠ claude-forensics.sh not found. "
                   "Place this file next to it, or run ./install.sh.")
            colour = "red"
        else:
            msg = f"Using: {orchestrator}"
            colour = "gray"
        ttk.Label(self, text=msg, foreground=colour,
                  padding=(10, 0, 10, 6)).pack(side="bottom", anchor="w",
                                               fill="x")

        nb = ttk.Notebook(self)
        nb.pack(side="top", fill="both", expand=True, padx=8, pady=8)

        self.run_tab = RunTab(nb, orchestrator)
        self.verify_tab = VerifyTab(nb, orchestrator)
        self.pricing_tab = PricingTab(nb)

        nb.add(self.run_tab, text="Analyze")
        nb.add(self.verify_tab, text="Verify bundle")
        nb.add(self.pricing_tab, text="Pricing")

        # Inside a .app bundle, LaunchServices activates the window for us.
        # When launched from a plain Python interpreter on macOS (no .app),
        # the window can open behind the terminal; Tk's own lift/topmost/
        # focus_force handle that without needing any AppleScript bridge.
        # We deliberately avoid `osascript tell "System Events"` — it works
        # but triggers the macOS Automation permission prompt, which is a
        # frightening first launch for a tool labelled "forensics".
        self.after(50, self._activate)

    def _activate(self) -> None:
        try:
            self.lift()
            self.attributes("-topmost", True)
            self.after(250, lambda: self.attributes("-topmost", False))
            self.focus_force()
        except Exception:
            pass


def main() -> int:
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
