from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs


project_root = Path(SPECPATH).parents[1]
backend_root = project_root / "backend"
bundle_root = project_root / "packaging" / "windows" / "bundle"
icon_path = bundle_root / "stemflow.ico"

datas = []
binaries = []
hiddenimports = [
    "app.gui",
    "app.cli.main",
    "audio_separator.separator",
    "audio_separator.separator.architectures",
    "onnx2torch",
    "soundfile",
    "samplerate",
]

for package in (
    "audio_separator",
    "imageio_ffmpeg",
    "librosa",
    "ml_collections",
    "onnx2torch",
    "samplerate",
    "soundfile",
    "torchvision",
):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

datas += collect_data_files("onnx")
binaries += collect_dynamic_libs("onnxruntime")

models_dir = bundle_root / "models"
if not (models_dir / "UVR-MDX-NET-Inst_HQ_3.onnx").is_file():
    raise SystemExit(
        "Bundled MDX model is missing. Run scripts/windows/download-bundle-assets.ps1 first."
    )
datas.append((str(models_dir), "models"))

gpu_bootstrap_dir = bundle_root / "gpu-bootstrap"
if not (gpu_bootstrap_dir / "python-3.12.10-embed-amd64.zip").is_file():
    raise SystemExit(
        "GPU bootstrap assets are missing. Run "
        "scripts/windows/download-bundle-assets.ps1 first."
    )
datas.append((str(gpu_bootstrap_dir), "gpu-bootstrap"))
datas.append((str(backend_root / "app"), "gpu-bootstrap/stemflow/app"))

a = Analysis(
    [str(backend_root / "app" / "gui.py")],
    pathex=[str(backend_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["fastapi", "uvicorn", "starlette", "httpx"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="StemFlow",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    icon=str(icon_path) if icon_path.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="StemFlow",
)
