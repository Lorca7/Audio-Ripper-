# Audio Ripper

A small macOS app that rips the audio from a YouTube video. You choose the part
you want on a zoomable waveform and export it as a **WAV** file.

The finished app is standalone. Python, ffmpeg and the JavaScript runtime that
yt-dlp needs for YouTube are all bundled inside `Audio Ripper.app`, so nothing
else needs to be installed. It runs on macOS 11 and later, including
**macOS Sonoma 14.6**, on both Apple silicon and Intel Macs.

## Using it

1. Paste a YouTube link into **YouTube URL** and press **Load** (or press Return).
   You can also use **Open File…** to load a local audio or video file.
2. The **whole-video strip** at the top shows the entire soundtrack. The
   **yellow window** on it marks the part shown in the large zoomed view:
   * drag the window to move it,
   * drag its left or right edge to **expand** it (up to the whole video) or
     **narrow** it down to a tiny section,
   * click anywhere on the strip to jump there.
3. In the **zoomed view**:
   * drag across the waveform to select the part to rip,
   * drag the white edges to fine-tune the start and end,
   * drag inside the selection to move it,
   * click to place the play cursor,
   * scroll up or down to zoom around the mouse, and swipe sideways (or
     Shift + scroll) to pan.

   You can also type exact **Start** and **End** times (`1:23.5`, `83.5` or
   `1:02:03.250`).
4. Press **Space** (or ▶ Play) to preview the selection.
5. Click **Export WAV…** (⌘E). The default is 16-bit PCM at the source's
   sample rate and channel count. You can change the sample rate (44.1 / 48 /
   96 / 22.05 kHz) and choose stereo or mono, and you can add a 10 ms fade at
   each end to avoid clicks.

| Key | Action |
| --- | --- |
| Space | Play / pause |
| `+` / `−` | Zoom in / out |
| `Z` | Zoom to selection |
| `0` | Show whole video |
| `[` / `]` (or `I` / `O`) | Set selection start / end at the cursor |
| `A` | Select all |
| Esc | Clear selection |
| ← / → | Pan |
| ⌘E | Export WAV |

## Getting the app

### Option A: download a pre-built app

Every push to this repository runs the **Build macOS app** GitHub Actions
workflow, which builds the app on real Macs:

1. Open the repository's **Actions** tab, then the latest *Build macOS app* run.
2. Download **Audio-Ripper-macOS-arm64** if your Mac has Apple silicon (M1, M2,
   M3 or M4). Download **Audio-Ripper-macOS-x86_64** if it has an Intel
   processor. To check, open  → About This Mac.
3. Unzip the file and drag **Audio Ripper.app** into **Applications**.

If you push a tag such as `v1.0.0`, the zips are also attached to a GitHub
Release.

### Option B: build it yourself on your Mac

You need Python 3.11–3.13 from <https://www.python.org/downloads/macos/>. The
python.org installer includes the Tk GUI toolkit.

```bash
git clone <this repo>
cd Audio-Ripper-
./packaging/build_mac.sh
```

The build creates `dist/Audio Ripper.app` and a zip of it. Drag the app into
**Applications**.

### First launch (Gatekeeper)

The app is ad-hoc signed, not notarized by Apple, so macOS asks for
confirmation the first time you open it:

* **Right-click** (or Control-click) **Audio Ripper.app**, choose **Open**, then
  click **Open** again. You only need to do this once.
* If macOS says the app "is damaged", run the following once in Terminal:
  ```bash
  xattr -dr com.apple.quarantine "/Applications/Audio Ripper.app"
  ```

## Running from source (for development)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run_audio_ripper.py            # optionally: python run_audio_ripper.py <url-or-file>
```

When you run from source, YouTube downloads need a JavaScript runtime:
`brew install deno` (or have `node` installed).

## How it works

* [yt-dlp](https://github.com/yt-dlp/yt-dlp) downloads the best audio-only
  stream. The bundled [deno](https://deno.com) solves YouTube's JavaScript
  challenges.
* ffmpeg (bundled through `imageio-ffmpeg`) decodes it to 16-bit PCM. The PCM is
  memory-mapped, so even long videos stay responsive. A min/max envelope is
  precomputed so the waveform redraws instantly at any zoom level, right down
  to individual samples.
* Preview uses macOS's built-in `afplay`.
* Export writes the exact sample range with Python's `wave` module. It uses
  ffmpeg only when you change the sample rate or channel count.

### Updating when YouTube changes

YouTube changes things often, and old yt-dlp versions eventually stop working.
If downloads start failing, rebuild the app to pick up the latest yt-dlp. You
can push any commit and download the new build from Actions, or re-run
`./packaging/build_mac.sh`.

## Notes

Only download content that you have the right to use. Respect copyright and
YouTube's Terms of Service.
