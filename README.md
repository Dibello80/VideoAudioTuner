# Dog Audio Tuner & Watcher

This project contains two small tools designed to clean up and tune audio for dog-related videos:

1. **Volume Tuner (`volume_tuner.py`)**  
   A real-time GUI app that lets you adjust:
   - Master volume (±24 dB)
   - 5-band EQ (100 Hz, 300 Hz, 1 kHz, 3 kHz, 8 kHz)
   - Noise gate / expander with hysteresis (open/close thresholds, floor, ratio)

   It plays the audio in real time so you can hear exactly how it will sound, and stores your preferred settings in `settings.json`.

2. **Dog Audio Watcher (`dog_audio_watcher_headless.py`)**  
   A headless background process that:
   - Watches `C:\DogAudio\inbox` for new video/audio files
   - Applies the same volume / EQ / gate settings saved by the tuner
   - Writes processed files to `C:\DogAudio\processed`
   - Moves problematic files to `C:\DogAudio\errors`

The idea is:

- Use **Volume Tuner** to dial in your sound.
- Save the settings.
- Let **Dog Audio Watcher** automatically process incoming videos in the background.

---

## Features

### Volume Tuner (GUI)

- Real-time audio playback using `sounddevice`
- 5-band graphic EQ using custom Biquad filters
- Noise gate or expander with hysteresis
- Adjustable:
  - Open threshold
  - Close threshold
  - Noise floor
  - Expander ratio
- Preset system:
  - Save / load / delete named presets
- Settings stored in `settings.json` in the same folder as the executable/script

### Dog Audio Watcher (Headless)

- Watches `C:\DogAudio\inbox` for new files
- Supported extensions: `.mp4`, `.mov`, `.mkv`, `.avi`, `.m4v`, `.mp3`, `.wav`, `.flac`
- For each stable file, it:
  - Reloads `settings.json`
  - Builds a matching FFmpeg filter chain (EQ + gate + volume)
  - Runs FFmpeg and writes `<original_name>_tuned.mp4` to `C:\DogAudio\processed`
  - Moves broken/problem files to `C:\DogAudio\errors`

---

## Requirements

- Windows 10/11
- Python 3.10+ (for development/build)
- [FFmpeg](https://ffmpeg.org/download.html) available as `ffmpeg` on the system:
  - Either installed in `PATH`, or
  - Placed next to the EXEs in the app folder
- Python packages:
  - `numpy`
  - `sounddevice`
- Tkinter (bundled with standard Python on Windows)

Install dependencies for development:

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
