# volume_tuner.py — Real-time Volume + Graphic EQ + True Gate + Persistent Settings + Presets
# Requirements:
#   pip install sounddevice numpy
# Needs FFmpeg in PATH.

import os, json, queue, subprocess, math, time, threading
from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import numpy as np
import sounddevice as sd

# ---------- Audio engine constants ----------
APP_SR = 48000
BLOCK = 1024


def db_to_gain(db):
    return float(pow(10.0, db / 20.0))


def get_base_dir() -> Path:
    # If frozen by PyInstaller, use the exe folder, otherwise script folder
    if getattr(sys, "frozen", False):
        return Path(sys.argv[0]).resolve().parent
    return Path(__file__).resolve().parent


# ---------- Color helpers ----------
def _hex_to_rgb(hx):
    hx = hx.lstrip("#")
    return tuple(int(hx[i:i+2], 16) for i in (0, 2, 4))


def _rgb_to_hex(rgb):
    return "#%02x%02x%02x" % rgb


def _darken(hx, pct=0.12):
    r, g, b = _hex_to_rgb(hx)
    r = int(max(0, r * (1 - pct)))
    g = int(max(0, g * (1 - pct)))
    b = int(max(0, b * (1 - pct)))
    return _rgb_to_hex((r, g, b))


# ---------- Biquad (RBJ peaking) for realtime EQ ----------
class Biquad:
    def __init__(self):
        self.b0 = 1.0
        self.b1 = 0.0
        self.b2 = 0.0
        self.a1 = 0.0
        self.a2 = 0.0
        self.z1 = 0.0
        self.z2 = 0.0

    def set_peaking_eq(self, fs, f0, Q, gain_db):
        A = pow(10.0, gain_db / 40.0)
        w0 = 2 * math.pi * f0 / fs
        alpha = math.sin(w0) / (2 * Q)
        cosw = math.cos(w0)

        b0 = 1 + alpha * A
        b1 = -2 * cosw
        b2 = 1 - alpha * A
        a0 = 1 + alpha / A
        a1 = -2 * cosw
        a2 = 1 - alpha / A

        self.b0 = b0 / a0
        self.b1 = b1 / a0
        self.b2 = b2 / a0
        self.a1 = a1 / a0
        self.a2 = a2 / a0

    def process_inplace(self, x: np.ndarray):
        z1 = self.z1
        z2 = self.z2
        b0, b1, b2, a1, a2 = self.b0, self.b1, self.b2, self.a1, self.a2
        for i in range(x.size):
            w = x[i] - a1 * z1 - a2 * z2
            y = b0 * w + b1 * z1 + b2 * z2
            z2 = z1
            z1 = w
            x[i] = y
        self.z1 = z1
        self.z2 = z2


# ---------- FFmpeg PCM reader (f32 mono @ 48k) ----------
class FFmpegPCMReader(threading.Thread):
    def __init__(self, src_path, out_q, stop_event, paused_flag):
        super().__init__(daemon=True)
        self.src_path = src_path
        self.out_q = out_q
        self.stop_event = stop_event
        self.paused_flag = paused_flag
        self.proc = None

    def run(self):
        cmd = [
            "ffmpeg",
            "-v", "error",
            "-hide_banner",
            "-i", self.src_path,
            "-vn",
            "-ac", "1",
            "-f", "f32le",
            "-ar", str(APP_SR),
            "pipe:1",
        ]
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )
        except Exception as e:
            self.out_q.put(("__ERROR__", str(e)))
            return

        leftover = b""
        try:
            while not self.stop_event.is_set():
                if self.paused_flag.is_set():
                    time.sleep(0.02)
                    continue
                chunk = self.proc.stdout.read(BLOCK * 4)
                if not chunk:
                    break
                leftover += chunk
                while len(leftover) >= BLOCK * 4:
                    frame = leftover[:BLOCK * 4]
                    leftover = leftover[BLOCK * 4:]
                    self.out_q.put(frame)
        finally:
            try:
                if self.proc:
                    self.proc.stdout.close()
                    self.proc.stderr.close()
                    self.proc.kill()
            except Exception:
                pass
            self.out_q.put(None)


# ---------- Settings persistence (includes preset history) ----------
class SettingsManager:
    def __init__(self, path: Path):
        self.path = path
        self.data = {
            "volume_db": 0.0,
            "eq": [0.0, 0.0, 0.0, 0.0, 0.0],          # 100, 300, 1k, 3k, 8k
            "gate_enabled": False,
            "gate_mode": "Gate",                      # or "Expander"
            "open_thr_db": -42.0,
            "close_thr_db": -48.0,
            "floor_db": -40.0,
            "expander_ratio": 4.0,
            # history of EQ / gate / volume setups
            "presets": []                             # list of {name, volume_db, eq, gate_enabled, ...}
        }
        self.load()

    def load(self):
        try:
            if self.path.exists():
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:
            pass

    def save(self):
        try:
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            return True, ""
        except Exception as e:
            return False, str(e)


# ---------- Audio Engine (realtime DSP) ----------
class AudioEngine:
    def __init__(self):
        self.q = queue.Queue(maxsize=64)
        self.stream = None
        self.stop_event = threading.Event()
        self.paused_flag = threading.Event()

        self.master_gain = 1.0
        self.eq_points = [
            {"freq": 100.0,  "Q": 1.0, "gain": 0.0},
            {"freq": 300.0,  "Q": 1.0, "gain": 0.0},
            {"freq": 1000.0, "Q": 1.0, "gain": 0.0},
            {"freq": 3000.0, "Q": 1.0, "gain": 0.0},
            {"freq": 8000.0, "Q": 1.0, "gain": 0.0},
        ]
        self.eq_filters = [Biquad() for _ in self.eq_points]
        self._refresh_eq_filters()

        # Gate/expander (realtime)
        self.gate_enabled = False
        self.gate_mode = "Gate"
        self.open_thr_db = -42.0
        self.close_thr_db = -48.0
        self.floor_db = -40.0
        self.expander_ratio = 4.0
        self.attack_open_ms = 3.0
        self.release_close_ms = 120.0
        self.env = 0.0
        self.current_gain = 1.0
        self._update_time_consts()

    def _update_time_consts(self):
        self.attack_open = math.exp(-1.0 / (APP_SR * (self.attack_open_ms / 1000.0)))
        self.release_close = math.exp(-1.0 / (APP_SR * (self.release_close_ms / 1000.0)))

    def _refresh_eq_filters(self):
        for biq, band in zip(self.eq_filters, self.eq_points):
            biq.set_peaking_eq(APP_SR, band["freq"], band["Q"], band["gain"])

    # setters used by UI
    def set_volume_db(self, db):
        self.master_gain = db_to_gain(db)

    def set_eq_gain(self, idx, db):
        self.eq_points[idx]["gain"] = float(db)
        self.eq_filters[idx].set_peaking_eq(
            APP_SR,
            self.eq_points[idx]["freq"],
            self.eq_points[idx]["Q"],
            self.eq_points[idx]["gain"]
        )

    def set_gate_enabled(self, enabled):
        self.gate_enabled = enabled

    def set_gate_mode(self, mode):
        self.gate_mode = "Expander" if mode == "Expander" else "Gate"

    def set_gate_open_thr(self, db):
        self.open_thr_db = float(db)

    def set_gate_close_thr(self, db):
        self.close_thr_db = float(db)

    def set_gate_floor(self, db):
        self.floor_db = float(db)

    def set_expander_ratio(self, r):
        self.expander_ratio = float(r)

    def start(self):
        if self.stream is not None:
            return
        self.stop_event.clear()
        self.paused_flag.clear()
        self.stream = sd.OutputStream(
            samplerate=APP_SR,
            channels=1,
            dtype="float32",
            blocksize=BLOCK,
            callback=self._callback
        )
        self.stream.start()

    def pause(self, flag):
        if flag:
            self.paused_flag.set()
        else:
            self.paused_flag.clear()

    def stop(self):
        try:
            self.stop_event.set()
        except Exception:
            pass
        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
        finally:
            self.stream = None
        with self.q.mutex:
            self.q.queue.clear()

    # realtime gate/expander
    def _gate_or_expander(self, x: np.ndarray):
        atk = self.attack_open
        rel = self.release_close
        open_thr = self.open_thr_db
        close_thr = self.close_thr_db
        floor_lin = db_to_gain(self.floor_db)
        ratio = max(1.001, float(self.expander_ratio))

        env = self.env
        g = self.current_gain

        for i in range(x.size):
            s = abs(x[i])
            if s > env:
                env = atk * env + (1 - atk) * s
            else:
                env = rel * env + (1 - rel) * s

            env_db = 20.0 * math.log10(max(env, 1e-8))

            if self.gate_mode == "Gate":
                if env_db >= open_thr:
                    target = 1.0
                elif env_db <= close_thr:
                    target = floor_lin
                else:
                    target = g
            else:
                if env_db >= open_thr:
                    target = 1.0
                elif env_db <= close_thr:
                    target = floor_lin
                else:
                    out_db = open_thr + (env_db - open_thr) / ratio
                    gain_db = out_db - env_db
                    target = max(floor_lin, db_to_gain(gain_db))

            if target > g:
                g = 0.9 * g + 0.1 * target
            else:
                g = 0.98 * g + 0.02 * target

            x[i] *= g

        self.env = env
        self.current_gain = g

    def _callback(self, outdata, frames, time_info, status):
        if status:
            pass
        if self.paused_flag.is_set():
            outdata.fill(0)
            return
        try:
            item = self.q.get(timeout=0.2)
        except queue.Empty:
            outdata.fill(0)
            return
        if item is None or (isinstance(item, tuple) and item and item[0] == "__ERROR__"):
            outdata.fill(0)
            return
        buf = np.frombuffer(item, dtype=np.float32).copy()

        if self.gate_enabled:
            self._gate_or_expander(buf)

        for biq in self.eq_filters:
            biq.process_inplace(buf)

        buf *= self.master_gain
        np.clip(buf, -1.0, 1.0, out=buf)
        outdata[:] = buf.reshape(-1, 1)


# ---------- Colored Buttons ----------
class ColorButton(tk.Button):
    def __init__(self, master, text, color, command=None, **kw):
        super().__init__(master, text=text, command=command, **kw)
        self.base_color = color
        self.hover_color = _darken(color, 0.15)
        self.configure(
            bg=self.base_color,
            activebackground=self.hover_color,
            fg="white",
            activeforeground="white",
            relief="flat",
            bd=0,
            padx=12,
            pady=6,
            cursor="hand2",
            highlightthickness=0,
        )
        self.bind("<Enter>", lambda e: self.configure(bg=self.hover_color))
        self.bind("<Leave>", lambda e: self.configure(bg=self.base_color))


class ToggleButton(tk.Button):
    def __init__(self, master, text_on, text_off, color_on, color_off, command=None, **kw):
        super().__init__(master, **kw)
        self.text_on = text_on
        self.text_off = text_off
        self.color_on = color_on
        self.color_off = color_off
        self.hover_on = _darken(color_on, 0.15)
        self.hover_off = _darken(color_off, 0.15)
        self.state_on = False
        self.user_command = command
        self.configure(
            text=self.text_off,
            bg=self.color_off,
            activebackground=self.hover_off,
            fg="white",
            activeforeground="white",
            relief="flat",
            bd=0,
            padx=12,
            pady=6,
            cursor="hand2",
            highlightthickness=0,
        )
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.configure(command=self._toggle)

    def _on_enter(self, _):
        self.configure(bg=self.hover_on if self.state_on else self.hover_off)

    def _on_leave(self, _):
        self.configure(bg=self.color_on if self.state_on else self.color_off)

    def _toggle(self):
        self.state_on = not self.state_on
        self.configure(
            text=self.text_on if self.state_on else self.text_off,
            bg=self.color_on if self.state_on else self.color_off,
            activebackground=self.hover_on if self.state_on else self.hover_off,
        )
        if self.user_command:
            self.user_command(self.state_on)


# ---------- GUI ----------
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Video Volume Tuner — realtime")
        self.root.geometry("860x620")

        # ttk theme
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # managers
        self.settings = SettingsManager(get_base_dir() / "settings.json")
        self.audio = AudioEngine()
        self.reader = None
        self.current_path = None
        self.monitor_running = False

        self._build_ui()
        self._apply_settings_to_ui_and_engine()
        self._refresh_presets_ui()

        self.root.protocol("WM_DELETE_WINDOW", self.on_quit)

    # ---- UI ----
    def _build_ui(self):
        pad = {"padx": 10, "pady": 8}

        # Top bar: open/play/pause/stop + gate toggle + SAVE
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ColorButton(top, "Open Video…", "#3A79B4", command=self.on_open).pack(side="left")
        ColorButton(top, "Play",  "#2EAF5D", command=self.on_play).pack(side="left", padx=8)
        ColorButton(top, "Pause", "#F39C12", command=self.on_pause).pack(side="left")
        ColorButton(top, "Stop",  "#E74C3C", command=self.on_stop).pack(side="left", padx=8)

        self.gate_btn = ToggleButton(
            top,
            "Noise Gate: ON", "Noise Gate: OFF",
            "#7D3C98", "#7F8C8D",
            command=self.on_gate_toggle_btn
        )
        self.gate_btn.pack(side="left", padx=12)

        ColorButton(top, "Save Settings", "#2D98DA", command=self.on_save).pack(side="right")

        # Volume
        mid = ttk.Frame(self.root)
        mid.pack(fill="x", **pad)
        ttk.Label(mid, text="Volume (dB)").pack(anchor="w")
        self.vol_scale = ttk.Scale(
            mid,
            from_=-24, to=+24,
            value=0.0,
            orient="horizontal",
            command=self.on_volume_change
        )
        self.vol_scale.pack(fill="x")
        self.db_label = ttk.Label(mid, text="+0.0 dB")
        self.db_label.pack(anchor="e")

        # Gate/Expander
        gate = ttk.LabelFrame(self.root, text="Noise Gate / Expander (Hysteresis realtime)")
        gate.pack(fill="x", **pad)

        row1 = ttk.Frame(gate)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="Mode:").pack(side="left")
        self.mode_var = tk.StringVar(value="Gate")
        mode_cb = ttk.Combobox(
            row1,
            textvariable=self.mode_var,
            values=["Gate", "Expander"],
            width=10,
            state="readonly"
        )
        mode_cb.bind("<<ComboboxSelected>>", lambda _e: self.on_gate_mode())
        mode_cb.pack(side="left", padx=6)

        ttk.Label(row1, text="Open Thresh (dBFS):").pack(side="left", padx=(12, 4))
        self.open_thr = ttk.Scale(
            row1,
            from_=-60, to=-10,
            value=-42,
            orient="horizontal",
            command=lambda _e=None: self.on_open_thr()
        )
        self.open_thr.pack(side="left", fill="x", expand=True)
        self.open_lbl = ttk.Label(row1, text="-42.0 dBFS")
        self.open_lbl.pack(side="left", padx=6)

        row2 = ttk.Frame(gate)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="Close Thresh (dBFS):").pack(side="left")
        self.close_thr = ttk.Scale(
            row2,
            from_=-70, to=-10,
            value=-48,
            orient="horizontal",
            command=lambda _e=None: self.on_close_thr()
        )
        self.close_thr.pack(side="left", fill="x", expand=True, padx=6)
        self.close_lbl = ttk.Label(row2, text="-48.0 dBFS")
        self.close_lbl.pack(side="left", padx=6)

        row3 = ttk.Frame(gate)
        row3.pack(fill="x", pady=2)
        ttk.Label(row3, text="Floor (dB):").pack(side="left")
        self.floor_sl = ttk.Scale(
            row3,
            from_=-80, to=-6,
            value=-40,
            orient="horizontal",
            command=lambda _e=None: self.on_floor()
        )
        self.floor_sl.pack(side="left", fill="x", expand=True, padx=6)
        self.floor_lbl = ttk.Label(row3, text="-40.0 dB")
        self.floor_lbl.pack(side="left", padx=6)

        row4 = ttk.Frame(gate)
        row4.pack(fill="x", pady=2)
        ttk.Label(row4, text="Expander Ratio:").pack(side="left")
        self.ratio_sl = ttk.Scale(
            row4,
            from_=1.1, to=8.0,
            value=4.0,
            orient="horizontal",
            command=lambda _e=None: self.on_ratio()
        )
        self.ratio_sl.pack(side="left", fill="x", expand=True, padx=6)
        self.ratio_lbl = ttk.Label(row4, text="4.0:1")
        self.ratio_lbl.pack(side="left", padx=6)

        # EQ
        eq = ttk.LabelFrame(self.root, text="Graphic EQ (±12 dB)")
        eq.pack(fill="x", padx=10, pady=6)
        self.eq_sliders = []
        eq_points = [("100 Hz", 0), ("300 Hz", 1), ("1 kHz", 2), ("3 kHz", 3), ("8 kHz", 4)]
        eq_row = ttk.Frame(eq)
        eq_row.pack(fill="x")
        for label, idx in eq_points:
            col = ttk.Frame(eq_row)
            col.pack(side="left", expand=True, fill="y", padx=10)
            ttk.Label(col, text=label).pack()
            s = ttk.Scale(
                col,
                from_=+12, to=-12,
                value=0.0,
                orient="vertical",
                command=lambda _evt=None, i=idx: self.on_eq_change(i)
            )
            s.pack(fill="y", ipady=35)
            val_lbl = ttk.Label(col, text="0.0 dB")
            val_lbl.pack(pady=2)
            self.eq_sliders.append((s, val_lbl))

        # Presets section
        presets = ttk.LabelFrame(self.root, text="Presets (history of EQ / volume / gate)")
        presets.pack(fill="x", padx=10, pady=6)

        top_row = ttk.Frame(presets)
        top_row.pack(fill="x", pady=2)
        ttk.Label(top_row, text="Save current as:").pack(side="left")
        self.preset_name_var = tk.StringVar()
        self.preset_name_entry = ttk.Entry(top_row, textvariable=self.preset_name_var, width=24)
        self.preset_name_entry.pack(side="left", padx=4)
        ttk.Button(top_row, text="Save Preset", command=self.on_save_preset).pack(side="left", padx=4)

        bottom_row = ttk.Frame(presets)
        bottom_row.pack(fill="x", pady=2)
        ttk.Label(bottom_row, text="Presets:").pack(side="left")
        self.preset_select_var = tk.StringVar()
        self.preset_combo = ttk.Combobox(
            bottom_row,
            textvariable=self.preset_select_var,
            state="readonly",
            width=24
        )
        self.preset_combo.pack(side="left", padx=4)
        ttk.Button(bottom_row, text="Load", command=self.on_load_preset).pack(side="left", padx=4)
        ttk.Button(bottom_row, text="Delete", command=self.on_delete_preset).pack(side="left", padx=4)

        # Status
        self.status = ttk.Label(
            self.root,
            text="Open a video, tune in realtime, Save settings, and manage your presets.",
            foreground="#666"
        )
        self.status.pack(fill="x", padx=10, pady=10)

    # ---- Apply/load/save settings ----
    def _apply_settings_to_ui_and_engine(self):
        s = self.settings.data

        # Volume
        self.vol_scale.set(s["volume_db"])
        self.db_label["text"] = f"{s['volume_db']:+.1f} dB"
        self.audio.set_volume_db(s["volume_db"])

        # EQ
        for i, (slider, val_lbl) in enumerate(self.eq_sliders):
            g = s["eq"][i]
            slider.set(g)
            val_lbl["text"] = f"{g:+.1f} dB"
            self.audio.set_eq_gain(i, g)

        # Gate
        self.mode_var.set(s["gate_mode"])
        self.open_thr.set(s["open_thr_db"])
        self.open_lbl["text"] = f"{s['open_thr_db']:.1f} dBFS"
        self.close_thr.set(s["close_thr_db"])
        self.close_lbl["text"] = f"{s['close_thr_db']:.1f} dBFS"
        self.floor_sl.set(s["floor_db"])
        self.floor_lbl["text"] = f"{s['floor_db']:.1f} dB"
        self.ratio_sl.set(s["expander_ratio"])
        self.ratio_lbl["text"] = f"{s['expander_ratio']:.1f}:1"

        self.audio.set_gate_mode(s["gate_mode"])
        self.audio.set_gate_open_thr(s["open_thr_db"])
        self.audio.set_gate_close_thr(s["close_thr_db"])
        self.audio.set_gate_floor(s["floor_db"])
        self.audio.set_expander_ratio(s["expander_ratio"])
        self.audio.set_gate_enabled(s["gate_enabled"])

        # sync gate button visual
        if s["gate_enabled"] != self.gate_btn.state_on:
            self.gate_btn._toggle()

    def _current_state_from_ui(self):
        return {
            "volume_db": float(self.vol_scale.get()),
            "eq": [float(self.eq_sliders[i][0].get()) for i in range(5)],
            "gate_enabled": self.gate_btn.state_on,
            "gate_mode": self.mode_var.get(),
            "open_thr_db": float(self.open_thr.get()),
            "close_thr_db": float(self.close_thr.get()),
            "floor_db": float(self.floor_sl.get()),
            "expander_ratio": float(self.ratio_sl.get()),
        }

    def on_save(self):
        # Pull current UI -> settings (this is the "current" profile)
        s = self.settings.data
        s.update(self._current_state_from_ui())
        ok, err = self.settings.save()
        if ok:
            self.status["text"] = "Settings saved. These will be restored next time."
        else:
            messagebox.showerror("Save error", err)

    # ---- Presets logic ----
    def _refresh_presets_ui(self):
        presets = self.settings.data.get("presets", [])
        names = [p.get("name", "") for p in presets if p.get("name")]
        self.preset_combo["values"] = names
        if names and self.preset_select_var.get() not in names:
            self.preset_select_var.set(names[0])

    def on_save_preset(self):
        name = self.preset_name_var.get().strip()
        if not name:
            messagebox.showwarning("Preset name", "Please enter a preset name.")
            return
        state = self._current_state_from_ui()
        state["name"] = name
        presets = self.settings.data.setdefault("presets", [])
        # overwrite if exists
        for i, p in enumerate(presets):
            if p.get("name") == name:
                presets[i] = state
                break
        else:
            presets.append(state)
        ok, err = self.settings.save()
        if ok:
            self.status["text"] = f"Preset '{name}' saved."
            self._refresh_presets_ui()
        else:
            messagebox.showerror("Save error", err)

    def on_load_preset(self):
        name = self.preset_select_var.get().strip()
        if not name:
            messagebox.showwarning("No preset", "Select a preset to load.")
            return
        presets = self.settings.data.get("presets", [])
        preset = next((p for p in presets if p.get("name") == name), None)
        if not preset:
            messagebox.showerror("Not found", f"Preset '{name}' not found.")
            return
        # copy into main settings
        for key in [
            "volume_db", "eq", "gate_enabled", "gate_mode",
            "open_thr_db", "close_thr_db", "floor_db", "expander_ratio"
        ]:
            if key in preset:
                self.settings.data[key] = preset[key]
        self._apply_settings_to_ui_and_engine()
        self.settings.save()
        self.status["text"] = f"Preset '{name}' loaded."

    def on_delete_preset(self):
        name = self.preset_select_var.get().strip()
        if not name:
            messagebox.showwarning("No preset", "Select a preset to delete.")
            return
        presets = self.settings.data.get("presets", [])
        new_presets = [p for p in presets if p.get("name") != name]
        if len(new_presets) == len(presets):
            messagebox.showerror("Not found", f"Preset '{name}' not found.")
            return
        self.settings.data["presets"] = new_presets
        ok, err = self.settings.save()
        if ok:
            self.status["text"] = f"Preset '{name}' deleted."
            self._refresh_presets_ui()
        else:
            messagebox.showerror("Save error", err)

    # ---- Transport ----
    def on_open(self):
        f = filedialog.askopenfilename(
            title="Choose a video",
            filetypes=[
                ("Video/Audio", "*.mp4;*.mov;*.mkv;*.avi;*.m4v;*.wav;*.mp3;*.flac"),
                ("All", "*.*"),
            ],
        )
        if not f:
            return
        self.current_path = f
        self.status["text"] = f"Loaded: {os.path.basename(f)}"

    def _start_reader(self, path):
        self._stop_reader()
        self.reader = FFmpegPCMReader(path, self.audio.q, self.audio.stop_event, self.audio.paused_flag)
        self.reader.start()
        self.monitor_running = True

    def _stop_reader(self):
        if self.reader and self.reader.is_alive():
            self.audio.stop_event.set()
            try:
                self.reader.join(timeout=0.5)
            except Exception:
                pass
        self.reader = None
        self.audio.stop_event.clear()

    def on_play(self):
        if not self.current_path:
            messagebox.showinfo("No file", "Open a video first.")
            return
        self.audio.start()
        self.audio.pause(False)
        self._start_reader(self.current_path)
        self.status["text"] = f"Playing: {os.path.basename(self.current_path)}"

    def on_pause(self):
        if not self.monitor_running:
            return
        self.audio.pause(True)
        self.status["text"] = "Paused."

    def on_stop(self):
        self.audio.stop()
        self._stop_reader()
        self.monitor_running = False
        self.status["text"] = "Stopped."

    # ---- Control handlers (realtime) ----
    def on_volume_change(self, _evt=None):
        db = float(self.vol_scale.get())
        self.audio.set_volume_db(db)
        self.db_label["text"] = f"{db:+.1f} dB"

    def on_gate_toggle_btn(self, is_on):
        self.audio.set_gate_enabled(is_on)

    def on_gate_mode(self):
        self.audio.set_gate_mode(self.mode_var.get())

    def on_open_thr(self):
        v = float(self.open_thr.get())
        self.audio.set_gate_open_thr(v)
        self.open_lbl["text"] = f"{v:.1f} dBFS"

    def on_close_thr(self):
        v = float(self.close_thr.get())
        self.audio.set_gate_close_thr(v)
        self.close_lbl["text"] = f"{v:.1f} dBFS"

    def on_floor(self):
        v = float(self.floor_sl.get())
        self.audio.set_gate_floor(v)
        self.floor_lbl["text"] = f"{v:.1f} dB"

    def on_ratio(self):
        v = float(self.ratio_sl.get())
        self.audio.set_expander_ratio(v)
        self.ratio_lbl["text"] = f"{v:.1f}:1"

    def on_eq_change(self, idx):
        sldr, lbl = self.eq_sliders[idx]
        val = float(sldr.get())
        lbl["text"] = f"{val:+.1f} dB"
        self.audio.set_eq_gain(idx, val)

    def on_quit(self):
        try:
            self.on_stop()
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

