# PyInstaller spec — Sort Memories (macOS .app)
# Build : .venv/bin/pyinstaller build/SortMemories.spec --clean --noconfirm
# Output : dist/Sort Memories.app

import os
import re
from PyInstaller.utils.hooks import (
    collect_data_files, collect_submodules, collect_dynamic_libs,
)

block_cipher = None

# Résolution absolue des chemins relatifs au spec (évite les soucis de cwd).
_SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
_PROJECT_ROOT = os.path.dirname(_SPEC_DIR)
_ENTITLEMENTS = os.path.join(_SPEC_DIR, "entitlements.plist")
_APP_ENTRY = os.path.join(_PROJECT_ROOT, "app.py")

# Version lue depuis sort_memories/__init__.py (source unique de vérité, évite
# de figer une version obsolète dans le bundle macOS — cf. règle versioning).
with open(os.path.join(_PROJECT_ROOT, "sort_memories", "__init__.py"), encoding="utf-8") as _f:
    _APP_VERSION = re.search(r'__version__\s*=\s*"([^"]+)"', _f.read()).group(1)

hiddenimports = []
hiddenimports += collect_submodules("flask")
hiddenimports += collect_submodules("webview")
hiddenimports += collect_submodules("sort_memories")
hiddenimports += collect_submodules("send2trash")
hiddenimports += collect_submodules("pillow_heif")   # HEIC/HEIF/AVIF iPhone
hiddenimports += ["PIL._tkinter_finder", "imagehash", "send2trash",
                  "pillow_heif", "rawpy", "exifread", "sort_memories.updater",
                  "certifi"]   # bundle CA pour l'updater HTTPS (sinon CERTIFICATE_VERIFY_FAILED)

datas = []
datas += collect_data_files("webview")
datas += collect_data_files("certifi")   # cacert.pem embarqué (certifi.where())

# Bibliothèques natives : libheif (pillow_heif) + libraw (rawpy). Sans elles,
# le décodage HEIC/RAW échoue dans le .app bundlé.
binaries = []
binaries += collect_dynamic_libs("pillow_heif")
binaries += collect_dynamic_libs("rawpy")

# torch + open_clip exclus du bundle v0.1.0 (CLIP optionnel, gain ~2 GB).
# La détection runtime via try/except dans core.py désactive proprement la vue CLIP.
excludes = [
    "torch",
    "torchvision",
    "open_clip",
    "open_clip_torch",
    "tensorflow",
    "matplotlib",
    "pandas",
    "scipy",
    "tkinter",
    "test",
    "unittest",
]

a = Analysis(
    [_APP_ENTRY],
    pathex=[_PROJECT_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SortMemories",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=_ENTITLEMENTS,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SortMemories",
)

app = BUNDLE(
    coll,
    name="Sort Memories.app",
    icon=None,
    bundle_identifier="fr.eliottbouquerel.sortmemories",
    version=_APP_VERSION,
    info_plist={
        "CFBundleName": "Sort Memories",
        "CFBundleDisplayName": "Sort Memories",
        "CFBundleVersion": _APP_VERSION,
        "CFBundleShortVersionString": _APP_VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        "NSHumanReadableCopyright": "© 2026 Eliott Bouquerel. Tous droits réservés.",
        "NSAppTransportSecurity": {
            "NSAllowsLocalNetworking": True,
        },
    },
)
