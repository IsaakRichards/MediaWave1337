"""
setup_wizard.py

Step-by-step installer for MediaWave1337 (Windows and Linux).
Run it with setup.bat / setup.sh, or directly: python setup_wizard.py

Uses only tkinter, so it works before any dependencies are installed.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

import setup_core as core

PAGES = ["Welcome", "System check", "Python packages", "FFmpeg", "Indexer", "Finish"]
ICONS = {"ok": "✔", "warn": "⚠", "fail": "✖"}
COLORS = {"ok": "#1a7f37", "warn": "#b35900", "fail": "#c62828"}


class Wizard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{core.APP_NAME} Setup")
        self.geometry("760x560")
        self.minsize(640, 480)
        self._set_icon()

        self.events: "queue.Queue" = queue.Queue()
        self.busy = False
        self.page_index = 0
        self.python = core.venv_python() if os.path.isfile(core.venv_python()) else core.base_python()
        self.install_mode = tk.StringVar(value="venv")
        self.packages_ok = False
        self.package_results = {}
        self.ffmpeg_ok = False
        self.indexer_url = tk.StringVar(value="http://localhost:9117")
        self.indexer_key = tk.StringVar()
        self.indexer_type = tk.StringVar(value="Auto")
        self.indexer_status = tk.StringVar(value="Not tested yet.")
        self.make_desktop = tk.BooleanVar(value=True)
        self.make_menu = tk.BooleanVar(value=True)
        self.launch_now = tk.BooleanVar(value=True)

        self._build_frame()
        self.show_page(0)
        self.after(100, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self.on_cancel)

    # ---------------------------------------------------------------- layout

    def _set_icon(self):
        png = os.path.join(core.APP_DIR, "icons", "generated", "mediawave2000", "64.png")
        try:
            self._icon_image = tk.PhotoImage(file=png)
            self.iconphoto(True, self._icon_image)
        except tk.TclError:
            pass

    def _build_frame(self):
        style = ttk.Style(self)
        if "clam" in style.theme_names() and not core.IS_WINDOWS:
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("TkDefaultFont", 16, "bold"))
        style.configure("Step.TLabel", foreground="#666666")
        style.configure("Current.TLabel", font=("TkDefaultFont", 10, "bold"))

        side = ttk.Frame(self, padding=(16, 16, 8, 16))
        side.pack(side="left", fill="y")
        ttk.Label(side, text=core.APP_NAME, style="Title.TLabel").pack(anchor="w", pady=(0, 16))
        self.step_labels = []
        for i, name in enumerate(PAGES):
            label = ttk.Label(side, text=f"{i + 1}. {name}", style="Step.TLabel")
            label.pack(anchor="w", pady=3)
            self.step_labels.append(label)
        ttk.Separator(self, orient="vertical").pack(side="left", fill="y")

        main = ttk.Frame(self, padding=16)
        main.pack(side="left", fill="both", expand=True)
        self.heading = ttk.Label(main, style="Title.TLabel")
        self.heading.pack(anchor="w")
        self.body = ttk.Frame(main)
        self.body.pack(fill="both", expand=True, pady=(12, 12))

        buttons = ttk.Frame(main)
        buttons.pack(fill="x")
        self.cancel_btn = ttk.Button(buttons, text="Cancel", command=self.on_cancel)
        self.cancel_btn.pack(side="left")
        self.next_btn = ttk.Button(buttons, text="Next >", command=self.on_next)
        self.next_btn.pack(side="right")
        self.back_btn = ttk.Button(buttons, text="< Back", command=self.on_back)
        self.back_btn.pack(side="right", padx=(0, 8))

    def _clear_body(self):
        for child in self.body.winfo_children():
            child.destroy()

    def _text(self, parent, text, **kw):
        label = ttk.Label(parent, text=text, wraplength=500, justify="left", **kw)
        label.pack(anchor="w", pady=(0, 8))
        return label

    def _log_widget(self, parent, height=12):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, pady=(8, 0))
        log = tk.Text(frame, height=height, wrap="word", font=("TkFixedFont", 9), state="disabled",
                      background="#111111", foreground="#dddddd", insertbackground="#dddddd")
        scroll = ttk.Scrollbar(frame, command=log.yview)
        log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        log.pack(side="left", fill="both", expand=True)
        self.log = log
        return log

    def append_log(self, line):
        log = getattr(self, "log", None)
        if log is None or not log.winfo_exists():
            return
        log.configure(state="normal")
        log.insert("end", line + "\n")
        log.see("end")
        log.configure(state="disabled")

    def show_page(self, index):
        self.page_index = index
        for i, label in enumerate(self.step_labels):
            label.configure(style="Current.TLabel" if i == index else "Step.TLabel")
        self.heading.configure(text=PAGES[index])
        self._clear_body()
        self.log = None
        getattr(self, f"page_{index}")()
        self.update_buttons()

    def update_buttons(self):
        last = self.page_index == len(PAGES) - 1
        self.back_btn.configure(state="disabled" if self.busy or self.page_index == 0 else "normal")
        self.next_btn.configure(text="Finish" if last else "Next >")
        self.next_btn.configure(state="disabled" if self.busy or not self.can_continue() else "normal")
        self.cancel_btn.configure(state="disabled" if self.busy else "normal")

    def can_continue(self):
        if self.page_index == 1:
            return self.checks_ok
        if self.page_index == 2:
            return self.packages_ok
        return True

    # ---------------------------------------------------------------- background work

    def run_task(self, work, done=None):
        """Run work(output) on a thread; output() lines go to the log; done(result) runs on the UI thread."""
        self.busy = True
        self.update_buttons()

        def output(line):
            self.events.put(("log", line))

        def runner():
            try:
                result = work(output)
            except Exception as exc:  # keep the wizard alive and show what happened
                output(f"Error: {exc}")
                result = None
            self.events.put(("done", (done, result)))

        threading.Thread(target=runner, daemon=True).start()

    def _drain_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.append_log(payload)
                else:
                    done, result = payload
                    self.busy = False
                    if done:
                        done(result)
                    self.update_buttons()
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    # ---------------------------------------------------------------- page 0: welcome

    def page_0(self):
        self._text(self.body, f"This wizard installs everything {core.APP_NAME} needs and gets you to your first channel.")
        steps = (
            "1. Check your system (Python, disk space, FFmpeg)\n"
            "2. Install the Python packages: PySide6 (the player UI), requests and libtorrent (streaming)\n"
            "3. Install FFmpeg, used to read episode lengths\n"
            "4. Connect to your Jackett or Prowlarr indexer (optional, can be done later)\n"
            "5. Create shortcuts and launch the app"
        )
        self._text(self.body, steps)
        self._text(self.body, f"Install folder: {core.APP_DIR}")
        self._text(self.body, f"System: {core.platform_label()}  |  Python {sys.version.split()[0]}")
        self._text(self.body, "Some steps download files from the internet. Nothing is changed until you press a button on each page.",
                   style="Step.TLabel")

    # ---------------------------------------------------------------- page 1: system check

    checks_ok = False

    def page_1(self):
        self._text(self.body, "Checking your system...")
        self.check_frame = ttk.Frame(self.body)
        self.check_frame.pack(fill="x")
        controls = ttk.Frame(self.body)
        controls.pack(fill="x", pady=(8, 0))
        ttk.Button(controls, text="Check again", command=self.refresh_checks).pack(side="left")
        self._log_widget(self.body, height=8)
        self.refresh_checks()

    def refresh_checks(self):
        self.run_task(lambda output: core.run_system_checks(), self.show_checks)

    def show_checks(self, results):
        if not self.check_frame.winfo_exists() or results is None:
            return
        for child in self.check_frame.winfo_children():
            child.destroy()
        self.check_results = {r.name: r for r in results}
        for row, result in enumerate(results):
            tk.Label(self.check_frame, text=ICONS[result.status], fg=COLORS[result.status],
                     font=("TkDefaultFont", 12, "bold")).grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=4)
            ttk.Label(self.check_frame, text=result.name, style="Current.TLabel").grid(row=row, column=1, sticky="nw", pady=4)
            ttk.Label(self.check_frame, text=result.detail, wraplength=320, justify="left").grid(row=row, column=2, sticky="nw", padx=8, pady=4)
            fix = self._fix_button(result)
            if fix:
                fix.grid(row=row, column=3, sticky="ne", pady=4)
        self.check_frame.columnconfigure(2, weight=1)
        self.checks_ok = all(r.status != "fail" for r in results)
        if not self.checks_ok:
            self.append_log("Python is too old. Install Python 3.9 or newer from https://www.python.org/downloads/ and run setup again.")

    def _fix_button(self, result):
        if result.fix == "python":
            return ttk.Button(self.check_frame, text="Get Python", command=lambda: webbrowser.open("https://www.python.org/downloads/"))
        if result.fix == "venv":
            return ttk.Button(self.check_frame, text="Install it", command=lambda: self.run_task(
                lambda output: core.install_system_packages(["venv"], output), lambda _ok: self.refresh_checks()))
        if result.fix == "ffmpeg":
            return ttk.Button(self.check_frame, text="Install it", command=lambda: self.run_task(
                core.install_ffmpeg, lambda _ok: self.refresh_checks()))
        return None

    # ---------------------------------------------------------------- page 2: packages

    def page_2(self):
        venv_ok = getattr(self, "check_results", {}).get("Virtual environments")
        venv_available = venv_ok is None or venv_ok.status == "ok"
        if not venv_available and self.install_mode.get() == "venv":
            self.install_mode.set("user")
        self._text(self.body, "Choose where to install the Python packages, then press Install. This can take a few minutes "
                              "(PySide6 is a large download, about 200 MB).")
        ttk.Radiobutton(self.body, text=f"Private environment in {os.path.join(core.APP_DIR, 'venv')} (recommended)",
                        variable=self.install_mode, value="venv",
                        state="normal" if venv_available else "disabled").pack(anchor="w")
        ttk.Radiobutton(self.body, text="My user account (pip --user)", variable=self.install_mode, value="user").pack(anchor="w")
        if not venv_available:
            self._text(self.body, "Private environments are unavailable: go Back and press \"Install it\" next to Virtual environments to enable them.",
                       style="Step.TLabel")
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(8, 0))
        self.install_btn = ttk.Button(row, text="Install", command=self.install_packages)
        self.install_btn.pack(side="left")
        self.package_summary = ttk.Label(row, text="")
        self.package_summary.pack(side="left", padx=12)
        self.package_fix_row = ttk.Frame(self.body)
        self.package_fix_row.pack(fill="x")
        self._log_widget(self.body)
        if self.package_results:
            self.show_package_results(self.package_results, after_install=False)
        else:
            python = self.python
            self.run_task(lambda output: (python, core.check_packages(python)), self._initial_packages)

    def _initial_packages(self, result):
        if result is None or self.log is None:
            return
        python, results = result
        if all(ok for ok, _v in results.values()):
            self.python, self.package_results = python, results
            self.append_log(f"Everything is already installed for {python}. Press Install to update, or Next to continue.")
        self.show_package_results(results, after_install=False)

    def install_packages(self):
        mode = self.install_mode.get()
        saw_managed_error = []

        def work(output):
            def tee(line):
                if "externally-managed-environment" in line:
                    saw_managed_error.append(True)
                output(line)

            if mode == "venv":
                tee("Creating the private environment...")
                if not core.create_venv(tee):
                    tee("Could not create the environment. Go Back to the system check, or choose 'My user account'.")
                    return None
                python = core.venv_python()
            else:
                python = core.base_python()
            core.pip_install(python, tee, user=(mode == "user"))
            tee("")
            tee("Verifying...")
            return python, core.check_packages(python)

        def done(result):
            if result is None:
                return
            self.python, self.package_results = result
            if saw_managed_error:
                self.append_log("Your system blocks pip for user installs. Go Back and enable private environments "
                                "(\"Install it\" next to Virtual environments), then choose the recommended option.")
            if self.log is not None:
                self.show_package_results(self.package_results)

        self.run_task(work, done)

    def show_package_results(self, results, after_install=True):
        parts = [f"{ICONS['ok' if ok else 'fail']} {name}" for name, (ok, _v) in results.items()]
        self.package_summary.configure(text="   ".join(parts))
        for child in self.package_fix_row.winfo_children():
            child.destroy()
        required_ok = all(ok for name, (ok, _v) in results.items() if name != "libtorrent")
        torrent_ok = results.get("libtorrent", (True, ""))[0]
        self.packages_ok = required_ok
        if not required_ok and after_install:
            self.append_log("Some required packages failed to install. Check the log above, fix the problem (often a network issue), and press Install again.")
        elif not torrent_ok:
            text = "libtorrent did not install, so torrent channels will be unavailable."
            if not core.IS_WINDOWS and core.detect_linux_package_manager():
                ttk.Label(self.package_fix_row, text=text, foreground=COLORS["warn"]).pack(side="left")
                ttk.Button(self.package_fix_row, text="Use system libtorrent", command=self.install_system_libtorrent).pack(side="right")
            else:
                ttk.Label(self.package_fix_row, text=text + " Try a Python version that has libtorrent wheels (3.9-3.12).",
                          foreground=COLORS["warn"], wraplength=500).pack(side="left")
        elif after_install:
            self.append_log("All packages are installed.")
        self.update_buttons()

    def install_system_libtorrent(self):
        python = self.python

        def work(output):
            if not core.install_system_packages(["libtorrent"], output):
                return None
            if python == core.venv_python():
                core.enable_system_site_packages()
                output("Environment can now use system packages.")
            return core.check_packages(python)

        def done(results):
            if results is not None:
                self.package_results = results
                self.show_package_results(results)

        self.run_task(work, done)

    # ---------------------------------------------------------------- page 3: ffmpeg

    def page_3(self):
        self._text(self.body, "FFmpeg reads the real length of each downloaded episode so the TV guide is accurate. "
                              "It is also used by MediaWave Converter. It is optional but recommended.")
        self.ffmpeg_label = ttk.Label(self.body, text="Checking...")
        self.ffmpeg_label.pack(anchor="w", pady=(4, 8))
        row = ttk.Frame(self.body)
        row.pack(fill="x")
        how = "with winget" if core.IS_WINDOWS else f"with {core.detect_linux_package_manager() or 'your package manager'}"
        self.ffmpeg_btn = ttk.Button(row, text=f"Install FFmpeg {how}", command=lambda: self.run_task(core.install_ffmpeg, lambda _ok: self.refresh_ffmpeg()))
        self.ffmpeg_btn.pack(side="left")
        ttk.Button(row, text="Check again", command=self.refresh_ffmpeg).pack(side="left", padx=8)
        if core.IS_WINDOWS:
            self._text(self.body, "\nNo winget? Download a build from gyan.dev and copy ffmpeg.exe and ffprobe.exe into the bin folder.")
            ttk.Button(self.body, text="Open FFmpeg downloads", command=lambda: webbrowser.open("https://www.gyan.dev/ffmpeg/builds/")).pack(anchor="w")
        else:
            self._text(self.body, "\nThe install asks for your password, because system packages need administrator rights.", style="Step.TLabel")
        self._log_widget(self.body, height=10)
        self.refresh_ffmpeg()

    def refresh_ffmpeg(self):
        def done(result):
            if result is None or not self.ffmpeg_label.winfo_exists():
                return
            self.ffmpeg_ok = result.status == "ok"
            self.ffmpeg_label.configure(text=f"{ICONS[result.status]}  {result.detail}", foreground=COLORS[result.status])
            self.ffmpeg_btn.configure(state="disabled" if self.ffmpeg_ok or not result.fix else "normal")

        self.run_task(lambda output: core.check_ffmpeg(), done)

    # ---------------------------------------------------------------- page 4: indexer

    def page_4(self):
        self._text(self.body, f"{core.APP_NAME} finds episodes on a local indexer: Jackett (port 9117) or Prowlarr (port 9696). "
                              "Copy the API key from the indexer's web page (shown at the top in Jackett, under Settings > General in Prowlarr).")
        links = ttk.Frame(self.body)
        links.pack(anchor="w", pady=(0, 8))
        ttk.Button(links, text="Get Jackett", command=lambda: webbrowser.open("https://github.com/Jackett/Jackett#installation")).pack(side="left")
        ttk.Button(links, text="Get Prowlarr", command=lambda: webbrowser.open("https://wiki.servarr.com/prowlarr/installation")).pack(side="left", padx=8)

        form = ttk.Frame(self.body)
        form.pack(fill="x")
        ttk.Label(form, text="Indexer URL").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.indexer_url, width=44).grid(row=0, column=1, sticky="we", pady=4)
        ttk.Label(form, text="API key").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.indexer_key, width=44, show="•").grid(row=1, column=1, sticky="we", pady=4)
        ttk.Label(form, text="Type").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Combobox(form, textvariable=self.indexer_type, values=["Auto", "Jackett", "Prowlarr"], state="readonly", width=12).grid(row=2, column=1, sticky="w", pady=4)
        form.columnconfigure(1, weight=1)

        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Test connection", command=self.test_indexer).pack(side="left")
        self.indexer_label = ttk.Label(row, textvariable=self.indexer_status, wraplength=380)
        self.indexer_label.pack(side="left", padx=12)
        self._text(self.body, "\nYou can skip this and set it later in the app: Advanced Config > Companion Channels > Torrent Channels. "
                              "The settings are saved when you press Next.", style="Step.TLabel")

    def test_indexer(self):
        url, key, kind = self.indexer_url.get(), self.indexer_key.get(), self.indexer_type.get().lower()
        self.indexer_status.set("Testing...")

        def done(result):
            if result is None:
                return
            ok, message, detected = result
            self.indexer_status.set(("✔ " if ok else "✖ ") + message)
            self.indexer_label.configure(foreground=COLORS["ok" if ok else "fail"])
            if ok and self.indexer_type.get() == "Auto":
                self.indexer_type.set(detected.title())

        self.run_task(lambda output: core.test_indexer(url, key, kind), done)

    def save_indexer(self):
        if not self.indexer_url.get().strip():
            return
        try:
            core.save_indexer_settings(self.indexer_url.get(), self.indexer_key.get(), self.indexer_type.get().lower())
        except OSError as exc:
            messagebox.showwarning("Settings", f"Could not save the indexer settings: {exc}")

    # ---------------------------------------------------------------- page 5: finish

    def page_5(self):
        torrent_ok = self.package_results.get("libtorrent", (False, ""))[0]
        summary = [
            f"{ICONS['ok']} Python packages installed ({'private environment' if self.python == core.venv_python() else 'user account'})",
            f"{ICONS['ok' if torrent_ok else 'warn']} libtorrent {'ready' if torrent_ok else 'missing: torrent channels are unavailable'}",
            f"{ICONS['ok' if self.ffmpeg_ok else 'warn']} FFmpeg {'found' if self.ffmpeg_ok else 'not installed (optional)'}",
            f"{ICONS['ok' if self.indexer_url.get().strip() else 'warn']} Indexer {self.indexer_url.get().strip() or 'not set'}",
        ]
        self._text(self.body, "\n".join(summary))
        ttk.Checkbutton(self.body, text="Create a desktop shortcut", variable=self.make_desktop).pack(anchor="w")
        ttk.Checkbutton(self.body, text="Add to the Start Menu" if core.IS_WINDOWS else "Add to the applications menu",
                        variable=self.make_menu).pack(anchor="w")
        ttk.Checkbutton(self.body, text=f"Launch {core.APP_NAME} now", variable=self.launch_now).pack(anchor="w")
        launcher, _ = core.launcher_script(self.python)
        self._text(self.body, f"\nA launcher will also be saved as {launcher}. Channels live in the channels folder: one XML file per channel "
                              "(see README.md). The sample Twilight Zone channel is ready to try.", style="Step.TLabel")
        self._log_widget(self.body, height=5)

    def finish(self):
        # Tk variables may only be read on the UI thread.
        python = self.python
        desktop, menu, launch = self.make_desktop.get(), self.make_menu.get(), self.launch_now.get()

        def work(output):
            created = core.create_shortcuts(python, desktop, menu, output)
            for path in created:
                output(f"Created {path}")
            if launch:
                output(f"Starting {core.APP_NAME}...")
                core.launch_app(python)
            return True

        self.run_task(work, lambda ok: self.after(1200, self.destroy) if ok else None)

    # ---------------------------------------------------------------- navigation

    def on_next(self):
        if self.page_index == 4:
            self.save_indexer()
        if self.page_index == len(PAGES) - 1:
            self.finish()
            return
        self.show_page(self.page_index + 1)

    def on_back(self):
        if self.page_index > 0:
            self.show_page(self.page_index - 1)

    def on_cancel(self):
        if self.busy:
            return
        if self.page_index == len(PAGES) - 1 or messagebox.askyesno("Cancel setup", "Quit setup? You can run it again any time."):
            self.destroy()


def main():
    Wizard().mainloop()


if __name__ == "__main__":
    main()
