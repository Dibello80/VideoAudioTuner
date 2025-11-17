"""
dog_audio_watcher_headless.py

Headless background processor:
- Reads saved settings (settings.json) from the same folder.
- Watches C:\DogAudio\inbox for new video/audio files.
- For each new file, runs FFmpeg with the tuned Volume + EQ + Gate.
- Writes output to C:\DogAudio\processed and moves broken files to C:\DogAudio\errors.
"""

import time
import json
import subprocess
import shutil
import sys
from pathlib import Path

# ---------- Folders (same as tuner) ----------
INBOX_DIR = Path(r"C:\DogAudio\inbox")
OUT_DIR   = Path(r"C:\DogAudio\processed")
ERR_DIR   = Path(r"C:\DogAudio\errors")

# ---------- Helper: resolve where settings.json lives ----------
def get_base_dir() -> Path:
    # If frozen by PyInstaller, use the exe folder, otherwise script folder
    if getattr(sys, "frozen", False):
        return Path(sys.argv[0]).resolve().parent
    return Path(__file__).resolve().parent

BASE_DIR = get_base_dir()
SETTINGS_PATH = BASE_DIR / "settings.json"

# ---------- Settings + filter builder (same logic as tuner) ----------
def load_settings():
    # Default fallback settings, in case settings.json is missing
    data = {
        "volume_db": 0.0,
        "eq": [0.0, 0.0, 0.0, 0.0, 0.0],  # 100, 300, 1k, 3k, 8k
        "gate_enabled": False,
        "gate_mode": "Gate",
        "open_thr_db": -42.0,
        "close_thr_db": -48.0,
        "floor_db": -40.0,
        "expander_ratio": 4.0,
    }
    try:
        if SETTINGS_PATH.exists():
            loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            data.update(loaded)
    except Exception as e:
        print(f"[WARN] Could not load settings.json: {e}")
    return data


def build_ffmpeg_filter_from_settings(
    vol_db,
    eq5,
    gate_on,
    gate_mode,
    open_thr,
    close_thr,
    floor_db,
    ratio,
):
    """
    Build an ffmpeg filter chain approximating the realtime chain:
      - mix to mono
      - resample 48k
      - 5-band peaking EQ via 'equalizer' filters
      - optional gate/expander via 'agate'
      - apply volume
      - duplicate back to stereo
    """
    # 1) Downmix to mono & normalize format
    chain = [
        "[0:a]pan=mono|c0=0.5*c0+0.5*c1",
        "aresample=48000",
        "aformat=sample_fmts=fltp:channel_layouts=mono",
    ]

    # 2) 5-band EQ (Q≈1 ~ width=1)
    freqs = [100, 300, 1000, 3000, 8000]
    for (f, g) in zip(freqs, eq5):
        g = float(g)
        if abs(g) < 0.01:
            continue
        chain.append(f"equalizer=f={f}:t=q:w=1:g={g:.3f}")

    # 3) Gate / Expander (ffmpeg agate has one threshold; use close_thr as threshold)
    if gate_on:
        mode = 1 if gate_mode == "Expander" else 0
        rng = max(6.0, min(80.0, abs(float(floor_db))))  # max attenuation
        thr = float(close_thr)
        if mode == 1:
            rat = max(1.1, float(ratio))
        else:
            rat = 2.0
        chain.append(
            f"agate=mode={mode}:threshold={thr:.1f}dB:ratio={rat:.2f}:"
            f"range={rng:.1f}dB:attack=3:release=120"
        )

    # 4) Volume
    vol_db = float(vol_db)
    if abs(vol_db) > 0.01:
        chain.append(f"volume={vol_db:.2f}dB")

    # 5) Back to stereo
    chain.append("pan=stereo|c0=c0|c1=c0")

    # Label output
    chain[-1] = chain[-1] + "[aout]"
    return ",".join(chain)


def process_one_with_ffmpeg(src: Path, dst: Path, filt: str):
    """
    Use FFmpeg to process src -> dst with given audio filter chain.
    Video is copied as-is, audio is re-encoded with tuning.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-v",
        "error",
        "-i",
        str(src),
        "-filter_complex",
        filt,
        "-map",
        "0:v:0",
        "-map",
        "[aout]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(dst),
    ]
    print(f"[INFO] Running FFmpeg on {src.name}")
    subprocess.run(cmd, check=True)
    print(f"[OK]  Wrote {dst.name}")


# ---------- Main watcher loop ----------
def main_loop(poll_seconds: float = 1.0):
    print("Dog Audio Watcher (headless) started.")
    print(f"Settings file: {SETTINGS_PATH}")
    print(f"Watching inbox: {INBOX_DIR}")

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ERR_DIR.mkdir(parents=True, exist_ok=True)

    # We reload settings before each file so you can retune with the GUI any time.
    seen = set()
    exts = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".mp3", ".wav", ".flac"}

    while True:
        try:
            for p in INBOX_DIR.iterdir():
                if not p.is_file():
                    continue
                if p.suffix.lower() not in exts:
                    continue
                if p.name in seen:
                    continue

                # Check if file is done copying (size stable)
                try:
                    size1 = p.stat().st_size
                    time.sleep(0.5)
                    size2 = p.stat().st_size
                except FileNotFoundError:
                    # File disappeared during copy
                    continue

                if size1 != size2:
                    continue  # still being written

                seen.add(p.name)

                # Load settings at the moment of processing
                s = load_settings()
                filt = build_ffmpeg_filter_from_settings(
                    vol_db=s["volume_db"],
                    eq5=s["eq"],
                    gate_on=s["gate_enabled"],
                    gate_mode=s["gate_mode"],
                    open_thr=s["open_thr_db"],
                    close_thr=s["close_thr_db"],
                    floor_db=s["floor_db"],
                    ratio=s["expander_ratio"],
                )

                out_name = p.stem + "_tuned.mp4"
                dst = OUT_DIR / out_name

                print(f"[INFO] Processing {p.name} -> {out_name}")
                try:
                    process_one_with_ffmpeg(p, dst, filt)
                    print(f"[DONE] {p.name}")
                except subprocess.CalledProcessError as e:
                    print(f"[ERROR] FFmpeg error on {p.name}: {e}")
                    try:
                        shutil.move(str(p), str(ERR_DIR / p.name))
                        print(f"[INFO] Moved bad file to errors: {p.name}")
                    except Exception as move_err:
                        print(f"[WARN] Could not move bad file: {move_err}")
                except Exception as e:
                    print(f"[ERROR] Unexpected error on {p.name}: {e}")
                    try:
                        shutil.move(str(p), str(ERR_DIR / p.name))
                        print(f"[INFO] Moved bad file to errors: {p.name}")
                    except Exception as move_err:
                        print(f"[WARN] Could not move bad file: {move_err}")
        except Exception as loop_err:
            print(f"[WARN] Watcher loop error: {loop_err}")

        time.sleep(poll_seconds)


if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        print("\nDog Audio Watcher stopped by user.")
