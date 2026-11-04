# PyInstaller spec for the macOS app bundle.  Build with packaging/build_mac.sh
import glob
import os

import imageio_ffmpeg
from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
VERSION = os.environ.get("APP_VERSION", "1.0.0")

datas = collect_data_files("yt_dlp_ejs", includes=["**/*.js"])

# ffmpeg binary shipped by imageio-ffmpeg. Collected as a *binary* so it ends
# up in Contents/Frameworks and gets code-signed with the rest of the app.
_ffdir = os.path.join(os.path.dirname(imageio_ffmpeg.__file__), "binaries")
binaries = [(f, "imageio_ffmpeg/binaries") for f in glob.glob(os.path.join(_ffdir, "ffmpeg-*"))]
assert binaries, "imageio-ffmpeg has no ffmpeg binary for this platform"
deno = os.path.join(SPECPATH, "vendor", "deno")
if os.path.exists(deno):
    binaries.append((deno, "bin"))                  # JS runtime for YouTube

a = Analysis(
    [os.path.join(ROOT, "run_audio_ripper.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=["yt_dlp", "imageio_ffmpeg", "imageio_ffmpeg.binaries"],
    excludes=["matplotlib", "scipy", "pandas", "PyQt5", "PySide6", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Audio Ripper",
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Audio Ripper")
app = BUNDLE(
    coll,
    name="Audio Ripper.app",
    icon=None,
    bundle_identifier="com.audioripper.app",
    version=VERSION,
    info_plist={
        "CFBundleName": "Audio Ripper",
        "CFBundleDisplayName": "Audio Ripper",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        "LSApplicationCategoryType": "public.app-category.music",
    },
)
