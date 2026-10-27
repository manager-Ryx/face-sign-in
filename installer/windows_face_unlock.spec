# PyInstaller spec — builds face_service.exe and face_unlock_tray.exe into a
# single shared dist folder. Run from the repo root:
#     pyinstaller installer/windows_face_unlock.spec --noconfirm --clean
#
# Both programs go into one COLLECT, which lays shared files (Python, numpy,
# OpenCV) down once. Each keeps all of its own Python modules: an earlier
# MERGE() moved modules both use into one program only, and the tray then
# couldn't import numpy.

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata
from PyInstaller.building.api import PYZ, EXE, COLLECT
from PyInstaller.building.build_main import Analysis

REPO_ROOT = Path(SPECPATH).resolve().parent
BLOCK_CIPHER = None

# ---------------------------------------------------------------------------
# Hidden imports + data collection — TF / DeepFace / torch are hostile to
# static analysis, so we help PyInstaller find everything.
# ---------------------------------------------------------------------------
WIN32 = ["win32api", "win32security", "win32file", "win32pipe", "win32event",
         "win32process", "win32gui", "win32ts", "win32crypt", "winerror", "pywintypes",
         "cv2_enumerate_cameras"]

# The service runs the recognition model; the tray app never imports it.
SERVICE_HIDDEN = []
SERVICE_HIDDEN += collect_submodules("tensorflow")
SERVICE_HIDDEN += collect_submodules("tf_keras")
SERVICE_HIDDEN += collect_submodules("keras")
SERVICE_HIDDEN += collect_submodules("deepface")
SERVICE_HIDDEN += collect_submodules("torch")
SERVICE_HIDDEN += collect_submodules("torchvision")
SERVICE_HIDDEN += WIN32 + collect_submodules("face_service")

TRAY_HIDDEN = WIN32 + collect_submodules("presence_monitor") + [
    "face_service.config", "face_service.pipe", "face_service.metrics",
    "face_service.win_events", "face_service.camera", "face_service.detector",
    "face_service.quality", "face_service.credentials", "face_service.logs",
    "face_service._version", "face_service.i18n",
]

MODELS = [(str(REPO_ROOT / "models" / "face_detection_yunet_2023mar.onnx"), "models")]

SERVICE_DATAS = []
SERVICE_DATAS += collect_data_files("tensorflow")
SERVICE_DATAS += collect_data_files("tf_keras")
SERVICE_DATAS += collect_data_files("keras")
SERVICE_DATAS += collect_data_files("deepface")
SERVICE_DATAS += collect_data_files("torch")
SERVICE_DATAS += collect_data_files("cv2")
SERVICE_DATAS += MODELS
weights_dir = REPO_ROOT / "models" / "weights"
if weights_dir.exists():
    for p in weights_dir.iterdir():
        if p.is_file():
            SERVICE_DATAS.append((str(p), "models/weights"))

TRAY_DATAS = collect_data_files("cv2") + MODELS
# The how-to guides' animations (presence_monitor/ui/assets/guides).
TRAY_DATAS += [(str(REPO_ROOT / "presence_monitor" / "ui" / "assets"), "presence_monitor/ui/assets")]


def metadata(*names):
    """Package metadata some libraries read at run time (gdown reads its own
    version on import, for example); missing packages are skipped."""
    found = []
    for name in names:
        try:
            found += copy_metadata(name)
        except Exception:
            pass
    return found


SERVICE_DATAS += metadata("gdown", "deepface", "tensorflow", "tf_keras", "keras", "numpy",
                          "tqdm", "requests", "torch", "torchvision", "opencv-python",
                          "h5py", "protobuf", "filelock", "pillow")
TRAY_DATAS += metadata("numpy", "opencv-python", "PySide6_Essentials", "shiboken6")

COMMON_EXCLUDES = [
    "matplotlib", "PyQt5", "PyQt6", "PySide2", "tkinter",
    "jupyter", "ipykernel", "notebook",
    # Not tensorflow.lite.experimental.microfrontend: TensorFlow 2.21 imports it.
    "scipy.spatial.cKDTree",  # huge, unused
]
SERVICE_EXCLUDES = COMMON_EXCLUDES + ["PySide6", "shiboken6"]
TRAY_EXCLUDES = COMMON_EXCLUDES + ["tensorflow", "tf_keras", "keras", "deepface", "torch",
                                   "torchvision"]

# ---------------------------------------------------------------------------
# Two entry points.
# ---------------------------------------------------------------------------
service_analysis = Analysis(
    [str(REPO_ROOT / "face_service" / "__main__.py")],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=SERVICE_DATAS,
    hiddenimports=SERVICE_HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    excludes=SERVICE_EXCLUDES,
    cipher=BLOCK_CIPHER,
    noarchive=False,
)

tray_analysis = Analysis(
    [str(REPO_ROOT / "presence_monitor" / "__main__.py")],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=TRAY_DATAS,
    hiddenimports=TRAY_HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    excludes=TRAY_EXCLUDES,
    cipher=BLOCK_CIPHER,
    noarchive=False,
)

service_pyz = PYZ(service_analysis.pure, service_analysis.zipped_data, cipher=BLOCK_CIPHER)
tray_pyz = PYZ(tray_analysis.pure, tray_analysis.zipped_data, cipher=BLOCK_CIPHER)

service_exe = EXE(
    service_pyz,
    service_analysis.scripts,
    [],
    exclude_binaries=True,
    name="face_service",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # service has no UI; log to file
    windowed=True,
    disable_windowed_traceback=False,
    icon=None,
)

tray_exe = EXE(
    tray_pyz,
    tray_analysis.scripts,
    [],
    exclude_binaries=True,
    name="face_unlock_tray",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    windowed=True,
    disable_windowed_traceback=False,
    icon=None,
)

coll = COLLECT(
    service_exe,
    service_analysis.binaries,
    service_analysis.zipfiles,
    service_analysis.datas,
    tray_exe,
    tray_analysis.binaries,
    tray_analysis.zipfiles,
    tray_analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WindowsFaceUnlock",
)
