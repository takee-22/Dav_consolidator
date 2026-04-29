# DAV Consolidator v2.0

Merge and process IMOU `.dav` camera recordings into a single MP4 with
**zero frame loss** — GPU-accelerated or CPU fallback, fully self-contained.

---

## Project Structure

```
Dav_consolidator/
├── main.py                  ← Entry point (dev + PyInstaller)
├── requirements.txt
├── build.bat                ← One-click PyInstaller build (Windows)
├── ffmpeg.exe               ← Bundle here (NOT system-installed)
├── ffprobe.exe              ← Bundle here (NOT system-installed)
│
├── gui/
│   ├── __init__.py
│   └── main_window.py       ← PyQt6 UI only — zero business logic
│
├── core/
│   ├── __init__.py
│   └── processor.py         ← FFmpeg pipeline, probe, plan, transcode, concat
│
└── utils/
    ├── __init__.py
    └── ffmpeg_utils.py      ← Binary resolution, file discovery, path helpers
```

---

## Quick Start (Development)

```bash
pip install -r requirements.txt

# Place ffmpeg.exe + ffprobe.exe in the project root, then:
python main.py
```

---

## PyInstaller Build (Windows)

### Option A — batch script (recommended)
```bat
build.bat
```

### Option B — manual command
```bat
pyinstaller ^
    --onefile ^
    --windowed ^
    --name DAVConsolidator ^
    --add-binary "ffmpeg.exe;." ^
    --add-binary "ffprobe.exe;." ^
    --hidden-import PyQt6.sip ^
    --hidden-import PyQt6.QtCore ^
    --hidden-import PyQt6.QtGui ^
    --hidden-import PyQt6.QtWidgets ^
    --collect-all PyQt6 ^
    --clean ^
    main.py
```

Output: `dist\DAVConsolidator.exe` — fully self-contained, no external
dependencies needed on the target machine.

### Flag reference

| Flag | Purpose |
|------|---------|
| `--onefile` | Single `.exe` — everything packed inside |
| `--windowed` | No console window on launch |
| `--add-binary "ffmpeg.exe;."` | Bundle ffmpeg into the exe root (`sys._MEIPASS`) |
| `--add-binary "ffprobe.exe;."` | Bundle ffprobe likewise |
| `--hidden-import PyQt6.*` | Force PyQt6 modules that PyInstaller may miss |
| `--collect-all PyQt6` | Include all Qt plugins (styles, image formats) |
| `--clean` | Wipe previous build cache before starting |

---

## FFmpeg Binary Resolution

`utils/ffmpeg_utils.py` resolves binaries in this priority order:

1. **`sys._MEIPASS`** — PyInstaller one-file mode extracts bundled binaries here at runtime
2. **Project root** — the directory containing `main.py` (development mode)
3. **System PATH** — last-resort string fallback (`"ffmpeg"` / `"ffprobe"`)

The same code path works transparently in both modes — no `#ifdef`-style branching.

---

## Processing Strategy

After probing all source files, the engine automatically selects the
optimal encoding plan:

| Condition | Strategy | Speed |
|-----------|----------|-------|
| All segments: same codec + same resolution + codec is H.264/HEVC/VP8/VP9/AV1 | **Stream copy** — `-c copy`, zero re-encode | ⚡⚡⚡ Fastest |
| GPU checkbox checked + NVIDIA NVENC available | **NVENC transcode** — `h264_nvenc -cq 18` | ⚡⚡ Fast |
| Anything else | **CPU transcode** — `libx264 -crf 18 -preset fast` | ⚡ Universal |

The selected plan is displayed in the UI before processing starts.

### VFR / Frame-loss safety

IMOU cameras produce 20 fps (day) and 15 fps (night) segments.
`-fps_mode vfr` (FFmpeg ≥ 5.1) or `-vsync vfr` (older) is applied during
transcoding to preserve each frame's original PTS — no frames are ever
duplicated or dropped at VFR boundaries.

---

## Architecture Notes

### Strict layer separation

| Layer | Responsibility |
|-------|---------------|
| `gui/` | Render signals → UI updates only. Zero FFmpeg knowledge. |
| `core/` | All subprocess calls, encoding decisions, cancel logic. |
| `utils/` | Binary resolution, file discovery, natural sort, path helpers. |

### Parallel probing

`FFmpegProcessor.probe_all_parallel()` uses a `ThreadPoolExecutor` (4 workers
by default) to probe all source files concurrently. On a folder with 48 segments
this cuts probing time from ~48 s → ~12 s.

### Thread-safety

`FFmpegProcessor.cancel()` is safe to call from any thread. It sets a
`threading.Event` and immediately `kill()`s the active subprocess via a lock-
protected reference — the worker loop exits cleanly within milliseconds.

### Logging

All significant events are logged to both:
- **Python `logging`** → stdout (visible in terminal / debug builds)
- **GUI log pane** → colour-coded by severity via Qt signals

---

## Verifying Output Duration

```bat
ffprobe -v error -show_entries format=duration ^
        -of default=noprint_wrappers=1 "output.mp4"
```

Expected: the sum of all source segment durations (within a few milliseconds).
