# DAV Consolidator

> Zero-frame-loss consolidation of IMOU camera `.dav` recordings into a
> single `.mp4` file.  Built with Python 3.11+, PySide6, and FFmpeg.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Installation](#2-installation)
3. [Running the Application](#3-running-the-application)
4. [Building a Standalone .exe](#4-building-a-standalone-exe)
5. [Project Structure](#5-project-structure)
6. [How It Works — Technical Deep Dive](#6-how-it-works)
7. [Verifying the Output](#7-verifying-the-output)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.11+ | [python.org](https://www.python.org) |
| PySide6 | 6.6+ | Installed via pip |
| FFmpeg | 4.4+ (5.1+ recommended) | Must include `ffprobe` |

### Installing FFmpeg on Windows

**Option A — Winget (recommended)**
```powershell
winget install Gyan.FFmpeg
```

**Option B — Manual**
1. Download a build from <https://www.gyan.dev/ffmpeg/builds/>
2. Extract to `C:\ffmpeg`
3. Add `C:\ffmpeg\bin` to your system `PATH`

Verify:
```powershell
ffmpeg -version
ffprobe -version
```

---

## 2. Installation

```powershell
# Clone / extract the project
cd dav_consolidator

# Create a virtual environment (recommended)
python -m venv .venv
.venv\Scripts\activate

# Install Python dependencies
pip install -r requirements.txt
```

---

## 3. Running the Application

```powershell
python main.py
```

### Workflow inside the GUI

1. **Input Folder** — Click *Browse…* and select the folder containing your
   `.dav` files.  The app will recursively find all `.dav` files and display
   the count.

2. **Output File** — Click *Browse…* and choose where to save the final
   `.mp4`.

3. **FFmpeg Path** — Leave as `ffmpeg` if it is on your system `PATH`.
   Otherwise browse to the `ffmpeg.exe` binary directly.

4. Click **▶ Start Conversion** and watch the log window for real-time
   FFmpeg progress.

5. On completion, a dialog shows the output path and the `ffprobe` command
   to verify the duration.

---

## 4. Building a Standalone .exe

```powershell
pip install pyinstaller

# Build using the provided spec file
pyinstaller dav_consolidator.spec
```

The executable is created at `dist\DAVConsolidator.exe`.

> **Note:** The `.exe` bundles Python and PySide6 but **not** FFmpeg.
> Distribute FFmpeg alongside the `.exe` or instruct end-users to install it
> separately.  To bundle FFmpeg, add its binaries to the `binaries` list in
> `dav_consolidator.spec`:
>
> ```python
> binaries=[("C:/ffmpeg/bin/ffmpeg.exe", "."),
>           ("C:/ffmpeg/bin/ffprobe.exe", ".")],
> ```
> Then in the GUI, set the FFmpeg path to `./ffmpeg.exe`.

---

## 5. Project Structure

```
dav_consolidator/
│
├── main.py                      # Entry point
│
├── gui/
│   ├── __init__.py
│   └── main_window.py           # PySide6 MainWindow + ConversionWorker thread
│
├── ffmpeg_wrapper/
│   ├── __init__.py
│   └── processor.py             # FFmpegProcessor: probe, transcode, concat
│
├── utils/
│   ├── __init__.py
│   └── file_utils.py            # Natural sort, file discovery, temp helpers
│
├── requirements.txt
├── dav_consolidator.spec        # PyInstaller spec
└── README.md
```

---

## 6. How It Works — Technical Deep Dive

### The VFR Problem

IMOU cameras record at **20 fps during the day** and **15 fps at night**.
Naïvely forcing a single constant frame rate across all segments during
concatenation would require the muxer to **duplicate frames** (when going
from 15 → 20 fps) or **drop frames** (when going from 20 → 15 fps) at every
day/night boundary.  Over a 12-segment, 60-minute recording this would
introduce measurable drift.

### Solution: VFR-Preserving Pipeline

The pipeline has three steps:

#### Step 1 — Per-Segment Transcode (VFR)

```
ffmpeg -i segment.dav \
       -map 0:v? -map 0:a? \
       -c:v libx264 -crf 18 -preset fast \
       -fps_mode vfr \            ← key flag
       -movflags +faststart \
       -c:a aac -b:a 128k \
       temp_0001.mp4
```

`-fps_mode vfr` (FFmpeg ≥ 5.1; `-vsync vfr` on older builds) tells the
muxer: *write each frame with its original source timestamp — never
duplicate or drop*.  The result is an MP4 whose duration exactly matches
the source `.dav` segment.

#### Step 2 — Concat Demuxer (Zero Re-Encode)

A playlist `concat_list.txt` is written:

```
file '/path/to/0000.mp4'
duration 300.000000
file '/path/to/0001.mp4'
duration 300.000000
...
```

The `duration` directive is critical for the **last segment** — without it
FFmpeg may under-report the total container duration by the duration of the
final GOP.

```
ffmpeg -f concat -safe 0 -i concat_list.txt \
       -c copy \                   ← bitstream copy, no re-encode
       -movflags +faststart \
       output.mp4
```

The concat demuxer offsets each segment's PTS by the cumulative duration of
all preceding segments:

```
PTS_out(frame) = PTS_in(frame) + Σ(durations[0..i-1])
```

This guarantees:

```
output_duration == segment_0_duration + segment_1_duration + … + segment_N_duration
```

With no frame duplication or dropping at any segment boundary.

#### Step 3 — Cleanup

All intermediate `.mp4` files and `concat_list.txt` are deleted after a
successful merge.

### Why Not the `concat` Filter?

The concat *filter* (`-filter_complex "[0:v][1:v]concat=n=2:v=1[out]"`)
re-encodes the output and resamples all segments to a single common frame
rate, which defeats the goal of zero frame loss.  The concat *demuxer* is a
pure muxing operation — it never touches the encoded bitstream.

### Natural Sort

Files like `ch01_20240601120000.dav` … `ch01_20240601125500.dav` must be
ordered chronologically.  The `natural_sorted()` utility splits filenames on
digit boundaries and compares numeric substrings by value:

```
ch01_9.dav  < ch01_10.dav    (natural sort ✓)
ch01_9.dav  > ch01_10.dav    (lexicographic sort ✗)
```

---

## 7. Verifying the Output

```powershell
# Check total duration
ffprobe -v error -show_entries format=duration \
        -of default=noprint_wrappers=1 output.mp4

# Full stream info
ffprobe -v quiet -print_format json -show_streams output.mp4
```

The reported duration should equal `N × 300.0` seconds (for N five-minute
segments).  Differences of less than 1/fps (< 50 ms) are normal due to
inter-frame timing; larger discrepancies indicate a source file problem.

---

## 8. Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| "FFmpeg not found" | Not on PATH | Set FFmpeg Path in GUI |
| Segments out of order | Non-standard filename format | Rename files to include zero-padded sequence numbers |
| Audio/video desync in output | Source `.dav` has corrupted timestamps | Re-probe with `ffprobe -v error -show_entries stream=duration_ts -i file.dav` |
| Output shorter than expected | A segment failed to transcode | Check the log for `[ERROR]` lines; re-run with that segment isolated |
| UPX compression error during build | UPX not installed | Remove `upx=True` from the spec or install UPX |
