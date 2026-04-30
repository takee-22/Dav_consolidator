# DAV Consolidator v3.0

Forensic-accurate DAV → MP4 concatenation with zero frame loss,
exact duration output, and watermark ↔ player-timeline alignment.

---

## What was fixed in v3

| # | Bug | Root cause | Fix |
|---|-----|-----------|-----|
| 1 | Wrong FPS parsed from DAV | `r_frame_rate` returns `90000/1` (timebase), not FPS | Try `avg_frame_rate` first; validate 1–120 fps range |
| 2 | 59:49 instead of 60:00 | No PTS reconstruction — broken DAV timestamps passed through | Added `fps=N`, `tpad=clone`, `setpts=N/FRAME_RATE/TB` filter chain |
| 3 | Stream copy on DAV (unsafe) | `build_plan` allowed `-c copy` for h264 DAV files | DAV files always transcode — stream copy disabled for `.dav` extension |
| 4 | Concat duration drift | `write_concat_list` used probe duration (~299.08 s) | Always write `SEGMENT_DURATION_SEC` (300.0 s) in playlist |
| 5 | Wrong expected total shown | `sum(v.duration)` used broken probe values | `SEGMENT_DURATION_SEC × len(files)` gives exact total |
| 6 | Audio sync drift | No audio PTS rebuild | Added `aresample=async=1` + `asetpts=N/SR/TB` |

---

## Project Structure

```
Dav_consolidator/
├── main.py                  ← Entry point
├── requirements.txt
├── build.bat                ← PyInstaller build script
├── ffmpeg.exe               ← Place here (not system PATH)
├── ffprobe.exe              ← Place here (not system PATH)
│
├── gui/
│   ├── __init__.py
│   └── main_window.py       ← PyQt6 UI, theme toggle, worker thread
│
├── core/
│   ├── __init__.py
│   └── processor.py         ← FFmpeg pipeline with DAV PTS reconstruction
│
└── utils/
    ├── __init__.py
    ├── ffmpeg_utils.py      ← Binary resolution, file discovery, DAV naming
    └── file_utils.py        ← Backward-compat re-exports
```

---

## Quick Start

```bash
pip install -r requirements.txt
# Place ffmpeg.exe + ffprobe.exe in project root
python main.py
```

---

## PyInstaller Build

```bat
build.bat
```

Or manually:

```bat
pyinstaller --onefile --windowed --name DAVConsolidator ^
    --add-binary "ffmpeg.exe;." ^
    --add-binary "ffprobe.exe;." ^
    --hidden-import PyQt6.sip ^
    --collect-all PyQt6 --clean main.py
```

---

## Processing Pipeline

```
For each .dav segment:
  ffmpeg
    -fflags +genpts+igndts+discardcorrupt   ← fix broken DAV timestamps
    -err_detect ignore_err                  ← skip corrupt packets
    -vf "fps=N,                             ← enforce constant frame rate
         tpad=stop_mode=clone:...,          ← pad short segments to 300 s
         setpts=N/FRAME_RATE/TB"            ← rebuild PTS from frame index
    -af "aresample=async=1,                 ← fix audio gaps
         asetpts=N/SR/TB"                   ← rebuild audio PTS
    -t 300                                  ← hard cap at exactly 300 s
    → intermediate_NNNN.mp4

ffmpeg -f concat -c copy                    ← merge (no re-encode)
    → 08.00.00-09.00.00.mp4
```

---

## Output Filename Convention

Input files named `08.00.00-08.05.00[R][0@0][0].dav` → `08.55.00-09.00.00[R][0@0][0].dav`
produce output named `08.00.00-09.00.00.mp4` automatically.

---

## Verifying Output Duration

```bat
ffprobe -v error -show_entries format=duration ^
        -of default=noprint_wrappers=1 output.mp4
```

Expected: `3600.000000` for 12 × 5-minute segments.

---

## New GUI Features (v3)

- **Dark / Light theme toggle** (🌙 ↔ ☀) in the header
- **Auto output filename** filled from DAV file naming convention
- **GPU status badge** — tests NVENC when checkbox is ticked
- **Elapsed time counter** while conversion is running
- **Drag & drop** — drop a folder onto the Input Folder field
- **Expected duration shown** before starting (e.g. "12 files → 1:00:00")
