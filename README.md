# 🎬 Nova Player

**Nova Player** is a Python-based desktop video player with **playback-overhead offline subtitle generation**.
Subtitles are generated automatically in the background *while you watch*, using [faster-whisper](https://github.com/SYSTRAN/faster-whisper) with a forensic 8-stage pipeline.

---

## ✨ Features

- 🎥 **LibVLC-powered** playback — supports 500+ formats (mp4, mkv, avi, mov, webm …)
- 🤖 **Automatic subtitle generation** — starts the moment you open a file, no button needed
- ⏩ **Playback-overhead scheduler** — always stays 2 chunks (120s) ahead of your playhead
- 💾 **Persistent .dsrt cache** — subtitles survive app restarts; backward seeks are instant (O log n)
- 🔍 **8-stage forensic pipeline** — VAD → ASR → end-time fix → drift → alignment → validate
- 📤 **Export to .srt** — one click
- 🌙 **Dark theme** — easy on the eyes

---

## 🚀 Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Make sure FFmpeg and VLC are installed on your system
#    Windows: https://ffmpeg.org/download.html  |  https://videolan.org
#    Ubuntu:  sudo apt install ffmpeg vlc
#    macOS:   brew install ffmpeg vlc

# 3. Run
python main.py

# 4. Or open a file directly
python main.py /path/to/video.mp4
```

---

## 🧠 How Subtitle Generation Works

```
File opened
    │
    ▼
LookaheadScheduler starts
    │
    ├─ Chunk at playhead not cached?
    │      └─ Fire micro-chunk (10s, tiny model) → subtitle in ~1s
    │         Then full-quality re-process in background
    │
    ├─ Keep 2 chunks ahead always transcribed
    │
    ├─ User seeks BACKWARD → .dsrt already has cues → instant ✓
    │
    └─ User seeks FORWARD past buffer → abort far jobs, micro-chunk fires
```

### .dsrt Format

Each video gets a `.dsrt` file (JSON) saved next to it:

```json
{
  "version": 2,
  "mediaFile": "/videos/movie.mp4",
  "totalMs": 5400000,
  "chunkMs": 60000,
  "chunks": [ { "index": 0, "status": "COMPLETE", ... } ],
  "cues":   [ { "id": 1, "startMs": 1240, "endMs": 3800, "text": "Hello." } ]
}
```

---

## 🏗️ Project Structure

```
nova-player/
├── main.py
├── requirements.txt
├── assets/dark_theme.qss
└── nova_player/
    ├── player/
    │   ├── vlc_widget.py          # LibVLC embed in QFrame
    │   └── player_state.py        # PlayerState enum
    ├── subtitle/
    │   ├── dsrt_file.py           # Persistent subtitle cache
    │   └── subtitle_overlay.py    # QLabel overlay + SubtitleSync
    ├── ai/
    │   ├── pipeline.py            # 8-stage forensic ASR pipeline
    │   ├── audio_extractor.py     # FFmpeg wrapper
    │   ├── chunk_worker.py        # Per-chunk background thread
    │   └── lookahead_scheduler.py # Playback-overhead brain
    └── ui/
        └── main_window.py         # Main PyQt6 window
```

---

## 🎛️ Whisper Models

| Model | Size | Speed (CPU) | Best For |
|-------|------|-------------|----------|
| tiny | 75MB | ~32× RT | Instant preview |
| base | 140MB | ~16× RT | Fast background |
| **small** | 460MB | ~6× RT | **Default — best balance** |
| medium | 1.5GB | ~2× RT | High accuracy |
| large-v3 | 3GB | ~1× RT | Max accuracy |

---

## 📦 Packaging

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name NovaPlayer main.py
# Output: dist/NovaPlayer.exe (Windows) or dist/NovaPlayer (Linux/macOS)
```

---

## 📄 License

MIT — © 2026 RAHUL S
