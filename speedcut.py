"""SpeedCut for Windows - applies per-range speed changes from a report to a video (offline, via FFmpeg)."""
import json, os, re, subprocess, sys, tempfile, threading
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

NOWIN = 0x08000000 if os.name == "nt" else 0

def res(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, name)
    return p if os.path.exists(p) else name

LINE = re.compile(r"^\[\s*([0-9:.]+)\s*[-–—]\s*([0-9:.]+)\s*\]\s*([0-9]*\.?[0-9]+)\s*[xX×]?")

def parse_time(t):
    p = t.split(":")
    if len(p) not in (2, 3): return None
    try:
        sec = float(p[-1]); m = int(p[-2]); h = int(p[0]) if len(p) == 3 else 0
    except ValueError:
        return None
    return round(((h * 60 + m) * 60 + sec) * 1000)

def parse_report(text):
    segs, errs = [], []
    for i, raw in enumerate(text.replace("\ufeff", "").splitlines(), 1):
        line = raw.strip()
        if not line.startswith("["): continue
        m = LINE.match(line)
        if not m: errs.append(f"Line {i}: invalid format: {line}"); continue
        s, e, sp = parse_time(m[1]), parse_time(m[2]), float(m[3])
        if s is None or e is None: errs.append(f"Line {i}: invalid timestamp")
        elif e <= s: errs.append(f"Line {i}: end time must be after start time")
        elif sp <= 0: errs.append(f"Line {i}: speed must be greater than 0")
        else: segs.append((s, e, sp))
    return sorted(segs), errs

def has_overlap(segs): return any(b[0] < a[1] for a, b in zip(segs, segs[1:]))

def resolve_overlaps(segs):
    out, cur = [], 0
    for s, e, sp in segs:
        s = max(s, cur)
        if e > s: out.append((s, e, sp)); cur = e
    return out

def build_plan(segs, dur):
    plan, cur = [], 0
    for s, e, sp in segs:
        if s > cur: plan.append((cur, s, 1.0))
        plan.append((s, e, sp)); cur = e
    if cur < dur: plan.append((cur, dur, 1.0))
    return plan

def fmt_time(ms):
    t, f = divmod(int(ms), 1000); h, r = divmod(t, 3600); m, s = divmod(r, 60)
    b = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
    return b + ("." + f"{f:03d}".rstrip("0") if f else "")

def fmt_speed(sp):
    s = f"{sp:.4f}".rstrip("0")
    if len(s.split(".")[1]) < 2: s += "0" * (2 - len(s.split(".")[1]))
    return s + "x"

def probe(path):
    r = subprocess.run([res("ffprobe.exe") if os.name == "nt" else "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type", "-of", "json", path], capture_output=True, text=True,
        creationflags=NOWIN)
    d = json.loads(r.stdout)
    dur = int(float(d["format"]["duration"]) * 1000)
    audio = any(s.get("codec_type") == "audio" for s in d.get("streams", []))
    return dur, audio

def atempo_chain(sp):
    f, r = [], sp
    while r < 0.5: f.append("0.5"); r /= 0.5
    while r > 100: f.append("100"); r /= 100
    if abs(r - 1) > 1e-9: f.append(f"{r:.8f}")
    return f

def build_filter(plan, audio):
    p = []
    for i, (s, e, sp) in enumerate(plan):
        ss, ee = f"{s/1000:.3f}", f"{e/1000:.3f}"
        p.append(f"[0:v]trim=start={ss}:end={ee},setpts=(PTS-STARTPTS)/{sp:.8f},setsar=1[v{i}]")
        if audio:
            t = "".join(f",atempo={x}" for x in atempo_chain(sp))
            p.append(f"[0:a]atrim=start={ss}:end={ee},asetpts=PTS-STARTPTS{t},aresample=44100,"
                     f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
    ins = "".join(f"[v{i}]" + (f"[a{i}]" if audio else "") for i in range(len(plan)))
    p.append(f"{ins}concat=n={len(plan)}:v=1:a={1 if audio else 0}[v]" + ("[a]" if audio else ""))
    return ";\n".join(p)

class App:
    def __init__(self, root):
        self.root = root; root.title("SpeedCut"); root.geometry("640x700")
        self.video = self.report_text = None; self.dur = 0; self.audio = False
        self.plan = []; self.overlap = False; self.proc = None; self.cancelled = False
        self.state = {"pct": 0, "msg": "", "done": None}; self.out = None
        f = ttk.Frame(root, padding=16); f.pack(fill="both", expand=True)
        ttk.Label(f, text="SpeedCut", font=("Segoe UI", 22, "bold")).pack(anchor="w")
        ttk.Label(f, text="Automatic Video Speed Editor").pack(anchor="w", pady=(0, 12))
        ttk.Button(f, text="🎬  Select Video", command=self.pick_video).pack(fill="x", ipady=8)
        self.lv = ttk.Label(f, text="No video selected"); self.lv.pack(anchor="w", pady=(2, 8))
        ttk.Button(f, text="📄  Select Speed Report", command=self.pick_report).pack(fill="x", ipady=8)
        self.lr = ttk.Label(f, text="No report selected"); self.lr.pack(anchor="w", pady=(2, 8))
        self.preview = tk.Text(f, height=16, font=("Consolas", 10), state="disabled"); self.preview.pack(fill="both", expand=True)
        self.start_btn = ttk.Button(f, text="PROCESS VIDEO", command=self.start, state="disabled")
        self.start_btn.pack(fill="x", ipady=8, pady=8)
        self.bar = ttk.Progressbar(f, maximum=100); self.bar.pack(fill="x")
        self.status = ttk.Label(f, text=""); self.status.pack(anchor="w", pady=4)
        self.cancel_btn = ttk.Button(f, text="Cancel", command=self.cancel, state="disabled"); self.cancel_btn.pack(fill="x")
        self.done = ttk.Frame(f)
        for t, c in (("▶ Play Video", self.play), ("Open Folder", self.folder)):
            ttk.Button(self.done, text=t, command=c).pack(side="left", expand=True, fill="x", padx=2, ipady=4)

    def show(self, text):
        self.preview.config(state="normal"); self.preview.delete("1.0", "end")
        self.preview.insert("end", text); self.preview.config(state="disabled")

    def pick_video(self):
        p = filedialog.askopenfilename(filetypes=[("Video", "*.mp4 *.mkv *.mov *.webm *.avi"), ("All", "*.*")])
        if not p: return
        try: self.dur, self.audio = probe(p); assert self.dur > 0
        except Exception:
            messagebox.showerror("SpeedCut", "This video could not be read. It may be corrupt or unsupported."); return
        self.video = p; self.lv.config(text=f"{os.path.basename(p)}  ({fmt_time(self.dur)})"); self.refresh()

    def pick_report(self):
        p = filedialog.askopenfilename(filetypes=[("Report", "*.txt *.csv"), ("All", "*.*")])
        if not p: return
        try:
            with open(p, "r", encoding="utf-8-sig", errors="replace") as fh: self.report_text = fh.read()
        except OSError:
            messagebox.showerror("SpeedCut", "The report file could not be read."); return
        self.lr.config(text=os.path.basename(p)); self.refresh()

    def refresh(self):
        self.start_btn.config(state="disabled"); self.done.pack_forget()
        if not (self.video and self.report_text is not None): return
        segs, errs = parse_report(self.report_text)
        if not segs and not errs: errs.append("The report is empty: no [START-END] SPEED lines found.")
        for s, e, sp in segs:
            if s >= self.dur: errs.append(f"{fmt_time(s)} - {fmt_time(e)} is outside the video (length {fmt_time(self.dur)}).")
        if errs: self.show("⚠ Please fix the report:\n\n" + "\n".join(errs)); return
        segs = [(s, min(e, self.dur), sp) for s, e, sp in segs]
        self.overlap = has_overlap(segs)
        self.plan = build_plan(resolve_overlaps(segs) if self.overlap else segs, self.dur)
        row = lambda s: f"{fmt_time(s[0])} - {fmt_time(s[1])}    {fmt_speed(s[2])}\n"
        t = "Speed Report\n\n" + "".join(map(row, segs)) + f"\nTotal speed segments: {len(segs)}\n"
        if self.overlap: t += "\n⚠ Overlapping speed ranges detected. Please check the report.\n"
        t += "\nProcessing plan\n\n" + "".join(map(row, self.plan))
        t += f"\nOutput length ≈ {fmt_time(sum((e - s) / sp for s, e, sp in self.plan))}"
        self.show(t); self.start_btn.config(state="normal")

    def start(self):
        if self.overlap and not messagebox.askyesno("Overlapping ranges",
            "Overlapping speed ranges detected. Please check the report.\n\nContinue anyway? (a later range is trimmed to begin where the earlier one ends)"):
            return
        self.start_btn.config(state="disabled"); self.cancel_btn.config(state="normal"); self.done.pack_forget()
        self.cancelled = False; self.state.update(pct=0, msg="Starting...", done=None)
        threading.Thread(target=self.work, daemon=True).start(); self.poll()

    def work(self):
        outdir = os.path.join(os.path.expanduser("~"), "Videos", "SpeedCut"); os.makedirs(outdir, exist_ok=True)
        out = os.path.join(outdir, "SpeedCut_Final_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".mp4")
        fd, fpath = tempfile.mkstemp(suffix=".txt"); os.close(fd)
        with open(fpath, "w", encoding="utf-8") as fh: fh.write(build_filter(self.plan, self.audio))
        total = sum((e - s) / sp for s, e, sp in self.plan)
        a = [res("ffmpeg.exe") if os.name == "nt" else "ffmpeg", "-y", "-hide_banner", "-nostats", "-progress", "pipe:1",
             "-i", self.video, "-/filter_complex", fpath, "-map", "[v]"] + (["-map", "[a]"] if self.audio else []) + \
            ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"] + \
            (["-c:a", "aac", "-b:a", "192k"] if self.audio else []) + ["-movflags", "+faststart", out]
        log = tempfile.TemporaryFile()
        try:
            self.proc = subprocess.Popen(a, stdout=subprocess.PIPE, stderr=log, text=True, creationflags=NOWIN)
            for line in self.proc.stdout:
                if line.startswith("out_time_us=") and line.strip().split("=")[1].lstrip("-").isdigit():
                    ms = int(line.strip().split("=")[1]) / 1000; acc = 0
                    cur = self.plan[-1]
                    for sg in self.plan:
                        acc += (sg[1] - sg[0]) / sg[2]
                        if ms < acc: cur = sg; break
                    self.state.update(pct=min(99, int(ms / total * 100)),
                        msg=f"Processing: {fmt_time(cur[0])} - {fmt_time(cur[1])}   Speed: {fmt_speed(cur[2])}")
            rc = self.proc.wait()
            if self.cancelled: res_ = ("cancel", None)
            elif rc == 0: res_ = ("ok", out)
            else:
                log.seek(0); res_ = ("err", log.read().decode(errors="replace")[-500:])
        except OSError as e:
            res_ = ("err", f"Could not start FFmpeg: {e}")
        finally:
            try: os.remove(fpath)
            except OSError: pass
        if res_[0] != "ok" and os.path.exists(out):
            try: os.remove(out)
            except OSError: pass
        self.state["done"] = res_

    def poll(self):
        self.bar["value"] = self.state["pct"]; self.status.config(text=f'{self.state["pct"]}%   {self.state["msg"]}')
        d = self.state["done"]
        if d is None: self.root.after(200, self.poll); return
        self.cancel_btn.config(state="disabled"); self.start_btn.config(state="normal")
        if d[0] == "ok":
            self.out = d[1]; self.bar["value"] = 100; self.status.config(text="Video processing completed!"); self.done.pack(fill="x", pady=8)
        elif d[0] == "cancel": self.status.config(text="Cancelled"); self.bar["value"] = 0
        else: self.bar["value"] = 0; messagebox.showerror("SpeedCut", "Processing failed. The video may be corrupt or unsupported.\n\n" + d[1])

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None: self.proc.terminate()

    def play(self): os.startfile(self.out)
    def folder(self): subprocess.Popen(["explorer", "/select,", os.path.normpath(self.out)])

if __name__ == "__main__":
    r = tk.Tk(); App(r); r.mainloop()
