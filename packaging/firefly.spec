# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Firefly AI Pet v1.0-rc1 (Windows, onedir).

Layout contract (PyInstaller 6.x onedir):
    dist/Firefly_AI_Pet/Firefly_AI_Pet.exe
    dist/Firefly_AI_Pet/_internal/          # python runtime + ALL bundled data

Data destinations below are relative to the contents directory (_internal).
That matches frozen `Path(__file__)`-based lookups used by app.py,
core/settings_manager.py, core/companion_config.py, providers/base.py and
voice_client/config.py (PROJECT_DIR resolves to _internal when frozen).

Config collection is a strict whitelist of TEMPLATES ONLY. Local user state
(hardware_devices.json / pet_preferences.json / ui_settings.json /
sessions.json) must never enter the bundle.
"""
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

SPEC_DIR = os.path.abspath(SPECPATH)
ROOT = os.path.dirname(SPEC_DIR)  # repository root


def _root(*parts):
    return os.path.join(ROOT, *parts)


datas = [
    (_root('assets', 'animations'), 'assets/animations'),
    (_root('assets', 'firefly.ico'), 'assets'),
    (_root('assets', 'paper_reader'), 'assets/paper_reader'),
    (_root('character', 'firefly'), 'character/firefly'),
    (_root('config', 'companion.json'), 'config'),
    (_root('config', 'voice_config.yaml'), 'config'),
    (_root('config', 'path_config.yaml'), 'config'),
    (_root('config', 'hardware_devices.example.json'), 'config'),
    (_root('.env.example'), '.'),
]

# RapidOCR ships its ONNX models inside the wheel -> offline OCR in the bundle.
datas += collect_data_files('rapidocr')
datas += collect_data_files('fastembed')

hiddenimports = [
    # persona package + root-level module
    'character',
    'character.character_loader',
    'state_broker',
    # Qt surface used beyond default essentials
    'PySide6.QtMultimedia',
    # declared-per-plan dependencies (mostly auto-tracked; explicit is stable)
    'yaml', 'requests', 'PIL', 'websockets',
    'pymupdf', 'fitz',
    'docx', 'pptx', 'openpyxl',
    'onnxruntime',
    # Provider Manager (rc2): lazy imports inside app.py / ui window
    'core.credential_store',
    'core.provider_manager',
    'ui.provider_manager_window',
]
# lazily imported at runtime (memory semantic index chain, OCR)
hiddenimports += collect_submodules('mem0')
hiddenimports += collect_submodules('qdrant_client')
hiddenimports += collect_submodules('fastembed')
hiddenimports += collect_submodules('rapidocr')

# Local-video optional chain (faster-whisper / ctranslate2 / scenedetect) is an
# Optional External capability per docs/Release_Scope_v1.md and stays OUT of
# the bundle; the code degrades through its existing try/except paths.
excludes = [
    'faster_whisper', 'ctranslate2', 'scenedetect',
    'torch', 'torchvision', 'torchaudio',
    'pytest', '_pytest',
    'tkinter',
    'serial', 'pyserial',
    'tools', 'tests',
]

a = Analysis(
    [_root('app.py')],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Firefly_AI_Pet',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=_root('assets', 'firefly.ico'),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Firefly_AI_Pet_rc2',
)
