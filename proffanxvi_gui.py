"""
ProFFanXVI GUI -- select which profanity to mute and build the mod, no command
line needed. Shells out to the scripts in ./scripts. Ships only stdlib (tkinter).

Flow:
  1. Set paths (auto-detects Steam FFXVI + bundled ./tools when possible).
  2. Tick the profanity concepts to mute.
  3. Pick mode: Accurate (word-level, self-verified) or Safe (whole-line).
  4. "Scan game text" to see per-concept line counts, then "Build mod".
"""
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"
WORDLIST = SCRIPTS / "profanity_wordlist.json"
CONFIG = ROOT / "gui_config.json"

STEAM_GUESSES = [
    r"C:\Program Files (x86)\Steam\steamapps\common\FINAL FANTASY XVI\data",
    r"C:\Program Files\Steam\steamapps\common\FINAL FANTASY XVI\data",
]


def autodetect():
    cfg = {}
    for g in STEAM_GUESSES:
        if Path(g).exists():
            cfg["game_data"] = g
            break
    # bundled tools next to the repo
    tools = ROOT / "tools"
    cand = {
        "ff16_cli": ["FF16Tools/win-x64/FF16Tools.CLI.exe", "FF16Tools.CLI.exe"],
        "vgaudio": ["VGAudioCli.exe"],
        "vgmstream": ["vgmstream/vgmstream-cli.exe", "vgmstream-cli.exe"],
        "reloaded_mods": [],
    }
    for key, rels in cand.items():
        for rel in rels:
            p = tools / rel
            if p.exists():
                cfg[key] = str(p)
                break
    return cfg


class App:
    def __init__(self, root):
        self.root = root
        root.title("ProFFanXVI - Profanity Filter Builder")
        root.geometry("920x760")
        self.cfg = self.load_cfg()
        self.concepts = json.loads(WORDLIST.read_text(encoding="utf-8"))["concepts"]
        self.vars = {}
        self.count_labels = {}
        self.q = queue.Queue()
        self._build_ui()
        self.root.after(100, self._drain)

    def load_cfg(self):
        cfg = autodetect()
        if CONFIG.exists():
            try:
                cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
            except Exception:
                pass
        return cfg

    def save_cfg(self):
        data = {k: self.path_vars[k].get() for k in self.path_vars}
        CONFIG.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)

        # --- Paths tab ---
        paths = ttk.Frame(nb, padding=12)
        nb.add(paths, text="1. Paths")
        self.path_vars = {}
        fields = [
            ("game_data", "FFXVI game 'data' folder"),
            ("ff16_cli", "FF16Tools.CLI.exe"),
            ("vgaudio", "VGAudioCli.exe"),
            ("vgmstream", "vgmstream-cli.exe"),
            ("reloaded_mods", "Reloaded-II 'Mods' folder (output)"),
        ]
        for i, (key, label) in enumerate(fields):
            ttk.Label(paths, text=label).grid(row=i, column=0, sticky="w", pady=4)
            v = tk.StringVar(value=self.cfg.get(key, ""))
            self.path_vars[key] = v
            ttk.Entry(paths, textvariable=v, width=70).grid(row=i, column=1, padx=6)
            ttk.Button(paths, text="Browse", command=lambda k=key: self._browse(k)).grid(row=i, column=2)
        ttk.Button(paths, text="Auto-detect", command=self._autodetect).grid(row=len(fields), column=1, sticky="w", pady=8)
        ttk.Label(paths, text="Tools download links are in the README. Point each field at the matching .exe / folder.",
                  foreground="#555").grid(row=len(fields)+1, column=0, columnspan=3, sticky="w")

        # --- Words tab ---
        words = ttk.Frame(nb, padding=12)
        nb.add(words, text="2. Profanity to mute")
        top = ttk.Frame(words); top.pack(fill="x")
        ttk.Button(top, text="Scan game text (show counts)", command=self._scan).pack(side="left")
        ttk.Button(top, text="Select all", command=lambda: self._set_all(True)).pack(side="left", padx=6)
        ttk.Button(top, text="Select none", command=lambda: self._set_all(False)).pack(side="left")
        ttk.Button(top, text="Reset to defaults", command=self._reset_defaults).pack(side="left", padx=6)

        canvas = tk.Canvas(words, highlightthickness=0)
        sb = ttk.Scrollbar(words, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True, pady=8)
        sb.pack(side="right", fill="y")

        cats = {}
        for c in self.concepts:
            cats.setdefault(c.get("category", "other"), []).append(c)
        for cat, items in cats.items():
            ttk.Label(inner, text=cat.upper(), font=("Segoe UI", 9, "bold"),
                      foreground="#334").grid(sticky="w", pady=(8, 2), column=0, columnspan=3)
            for c in items:
                v = tk.BooleanVar(value=bool(c.get("default")))
                self.vars[c["id"]] = v
                row = inner.grid_size()[1]
                ttk.Checkbutton(inner, text=c["label"], variable=v).grid(row=row, column=0, sticky="w")
                cl = ttk.Label(inner, text="", foreground="#888")
                cl.grid(row=row, column=1, sticky="w", padx=10)
                self.count_labels[c["id"]] = cl
                if c.get("note"):
                    ttk.Label(inner, text="("+c["note"]+")", foreground="#a70").grid(row=row, column=2, sticky="w")

        # --- Build tab ---
        build = ttk.Frame(nb, padding=12)
        nb.add(build, text="3. Build")
        mode = ttk.LabelFrame(build, text="Muting mode", padding=8)
        mode.pack(fill="x")
        self.mode = tk.StringVar(value="fast_cutlist")
        ttk.Radiobutton(mode, text="Fast: precompiled cutlist (seconds, no ML download). Recommended.",
                        variable=self.mode, value="fast_cutlist").pack(anchor="w")
        ttk.Radiobutton(mode, text="Accurate (full pipeline): re-derive word-level cuts yourself (slow, needs the ML deps in requirements.txt).",
                        variable=self.mode, value="word_level").pack(anchor="w")
        ttk.Radiobutton(mode, text="Safe (full pipeline): mute the whole line containing any profanity (bulletproof; loses some clean dialogue).",
                        variable=self.mode, value="whole_line").pack(anchor="w")
        ttk.Label(mode, text="Fast mode covers everything already muted in this repo's cutlist.json. If the game has been\n"
                             "patched since it was built, changed clips fall back to a safe whole-line mute automatically\n"
                             "-- use Accurate/Safe (full pipeline) to re-derive precise cuts for those.",
                  foreground="#555", justify="left").pack(anchor="w", pady=(4, 0))
        mrow = ttk.Frame(build); mrow.pack(fill="x", pady=6)
        ttk.Label(mrow, text="Whisper model (full pipeline only):").pack(side="left")
        self.model = tk.StringVar(value="large-v3")
        ttk.Combobox(mrow, textvariable=self.model, width=14,
                     values=["large-v3", "medium.en", "small.en", "base.en"]).pack(side="left", padx=6)

        brow = ttk.Frame(build); brow.pack(fill="x", pady=6)
        ttk.Button(brow, text="Build mod", command=self._build).pack(side="left")
        ttk.Button(brow, text="Build listen-kit (review audio)", command=self._listen).pack(side="left", padx=8)
        self.log = tk.Text(build, height=22, bg="#101010", fg="#ddd", insertbackground="#ddd")
        self.log.pack(fill="both", expand=True, pady=8)

    # --- helpers ---
    def _browse(self, key):
        if key in ("game_data", "reloaded_mods"):
            p = filedialog.askdirectory()
        else:
            p = filedialog.askopenfilename()
        if p:
            self.path_vars[key].set(p)
            self.save_cfg()

    def _autodetect(self):
        for k, v in autodetect().items():
            if k in self.path_vars:
                self.path_vars[k].set(v)
        self.save_cfg()
        self._logln("Auto-detect done.")

    def _set_all(self, val):
        for v in self.vars.values():
            v.set(val)

    def _reset_defaults(self):
        for c in self.concepts:
            self.vars[c["id"]].set(bool(c.get("default")))

    def enabled_ids(self):
        return [cid for cid, v in self.vars.items() if v.get()]

    def _logln(self, s):
        self.log.insert("end", s + "\n"); self.log.see("end")

    def _drain(self):
        try:
            while True:
                s = self.q.get_nowait()
                self.log.insert("end", s); self.log.see("end")
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def _run_bg(self, argv, env=None, done=None):
        def worker():
            self.q.put("\n$ " + " ".join(str(a) for a in argv) + "\n")
            e = dict(os.environ)
            if env:
                e.update(env)
            try:
                p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, env=e, bufsize=1)
                for line in p.stdout:
                    self.q.put(line)
                p.wait()
                self.q.put(f"[exit {p.returncode}]\n")
                if done:
                    done(p.returncode)
            except Exception as ex:
                self.q.put(f"ERROR: {ex}\n")
        threading.Thread(target=worker, daemon=True).start()

    def _paths_ok(self, need):
        for k in need:
            if not self.path_vars[k].get():
                messagebox.showerror("Missing path", f"Set the path for: {k} (Paths tab)")
                return False
        return True

    def _work_dir(self):
        d = ROOT / "_gui_work"
        d.mkdir(exist_ok=True)
        return d

    def _scan(self):
        if not self._paths_ok(["game_data", "ff16_cli"]):
            return
        self.save_cfg()
        work = self._work_dir()
        extracted = work / "extracted"
        editlist = work / "editlist.json"
        ids = ",".join(self.enabled_ids())
        env = {"DOTNET_ROLL_FORWARD": "LatestMajor"}

        def after_scan(rc):
            if rc != 0:
                return
            try:
                data = json.loads(editlist.read_text(encoding="utf-8"))
                by = data.get("matches_by_concept", {})
                for cid, lbl in self.count_labels.items():
                    self.q.put("")  # keep drain alive
                    lbl.after(0, lambda l=lbl, n=by.get(cid, 0): l.config(text=f"{n} lines"))
                self.q.put(f"\nScan complete: {data.get('total_matches')} lines match your selection.\n")
            except Exception as ex:
                self.q.put(f"scan parse error: {ex}\n")

        def after_extract(rc):
            self._run_bg([sys.executable, str(SCRIPTS / "scan_profanity.py"), str(extracted),
                          "-w", str(WORDLIST), "--enable", ids, "-o", str(editlist)],
                         done=after_scan)

        self._logln("Extracting dialogue text (first run downloads/does the heavy part)...")
        self._run_bg([sys.executable, str(SCRIPTS / "extract_dialogue_text.py"),
                      "--game-data", self.path_vars["game_data"].get(),
                      "--ff16-cli", self.path_vars["ff16_cli"].get(),
                      "--out", str(extracted)], env=env, done=after_extract)

    def _build(self):
        if not self._paths_ok(["game_data", "ff16_cli", "vgaudio", "vgmstream", "reloaded_mods"]):
            return
        self.save_cfg()
        work = self._work_dir()
        extracted = work / "extracted"
        editlist = work / "editlist.json"
        ids = ",".join(self.enabled_ids())
        if not ids:
            messagebox.showerror("Nothing selected", "Tick at least one profanity concept.")
            return
        mod_data = Path(self.path_vars["reloaded_mods"].get()) / "ff16.audio.profanity-filter" / "FFXVI" / "data"
        env = {
            "DOTNET_ROLL_FORWARD": "LatestMajor",
            "FF16_CLI": self.path_vars["ff16_cli"].get(),
            "VGAUDIOCLI": self.path_vars["vgaudio"].get(),
            "VGMSTREAM_CLI": self.path_vars["vgmstream"].get(),
            "FFXVI_DATA_DIR": self.path_vars["game_data"].get(),
            "MOD_OUTPUT_DIR": str(mod_data),
            "BATCH_EXTRACT_DIR": str(work / "batch_extracted"),
            "SAB_MUTE_SCRIPT": str(SCRIPTS / "sab_mute.py"),
            "MUTE_BANK_SCRIPT": str(SCRIPTS / "mute_bank_subsongs.py"),
            "SAFE_MODE": self.mode.get(),
            "WHISPER_MODEL": self.model.get(),
        }

        if self.mode.get() == "fast_cutlist":
            cutlist = ROOT / "data" / "cutlist.json"
            if not cutlist.exists():
                messagebox.showerror("Missing cutlist.json", f"Expected {cutlist} -- did you clone the full repo?")
                return
            self._logln("Building from precompiled cutlist (fast path, no ML deps needed)...\n")
            # separate filename from the full-pipeline's report.json -- shapes differ
            # (this is a stats summary, not per-line detail) and "Build listen-kit"
            # expects the full-pipeline shape, so this must not overwrite that one.
            self._run_bg([sys.executable, str(SCRIPTS / "apply_cutlist.py"), str(cutlist),
                          "--enable", ids, "-o", str(work / "cutlist_report.json")], env=env)
            return

        def after_scan(rc):
            if rc != 0:
                return
            self.q.put("\nBuilding mod (this is the long part; accurate mode + large-v3 is slow but thorough)...\n")
            self._run_bg([sys.executable, str(SCRIPTS / "batch_mute_pipeline.py"), str(editlist),
                          "", str(work / "report.json")], env=env)

        def after_extract(rc):
            self._run_bg([sys.executable, str(SCRIPTS / "scan_profanity.py"), str(extracted),
                          "-w", str(WORDLIST), "--enable", ids, "-o", str(editlist)],
                         done=after_scan)

        self._logln("Extracting dialogue text...")
        self._run_bg([sys.executable, str(SCRIPTS / "extract_dialogue_text.py"),
                      "--game-data", self.path_vars["game_data"].get(),
                      "--ff16-cli", self.path_vars["ff16_cli"].get(),
                      "--out", str(extracted)], env={"DOTNET_ROLL_FORWARD": "LatestMajor"}, done=after_extract)

    def _listen(self):
        if not self._paths_ok(["vgmstream", "reloaded_mods"]):
            return
        work = self._work_dir()
        mod_data = Path(self.path_vars["reloaded_mods"].get()) / "ff16.audio.profanity-filter" / "FFXVI" / "data"
        kit = work / "listen_kit"
        self._run_bg([sys.executable, str(SCRIPTS / "build_listen_kit.py"),
                      "-r", str(work / "report.json"), "-e", str(work / "editlist.json"),
                      "--extract-dir", str(work / "batch_extracted"), "--mod-dir", str(mod_data),
                      "--vgmstream", self.path_vars["vgmstream"].get(), "-o", str(kit)],
                     done=lambda rc: self.q.put(f"\nOpen {kit/'index.html'} in a browser.\n"))


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
