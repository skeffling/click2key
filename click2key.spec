# PyInstaller spec for Click2Key.
#
# macOS:  pyinstaller --noconfirm click2key.spec
#         → dist/Click2Key.app
# Windows: same command, produces dist/Click2Key/Click2Key.exe
#
# The BUNDLE block at the bottom is macOS-only; PyInstaller silently ignores
# it on Windows, where COLLECT's output is the artifact.

from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

a = Analysis(
    ['launch.py'],
    pathex=[],
    binaries=[],
    # CustomTkinter ships JSON theme files; bleak has platform backends loaded
    # dynamically. Both need their data files explicitly collected.
    datas=collect_data_files('customtkinter')
        + collect_data_files('bleak')
        + [('assets/title_icon.png', 'assets'),
           ('assets/title_icon_dark.png', 'assets')],
    hiddenimports=['pynput.keyboard._darwin', 'pynput.mouse._darwin',
                   'pynput.keyboard._win32', 'pynput.mouse._win32'],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Click2Key',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Click2Key',
)

app = BUNDLE(
    coll,
    name='Click2Key.app',
    icon='assets/icon.icns',
    bundle_identifier='app.click2key',
    info_plist={
        'CFBundleName': 'Click2Key',
        'CFBundleDisplayName': 'Click2Key',
        'CFBundleShortVersionString': '0.1.2',
        'CFBundleVersion': '0.1.2',
        'LSMinimumSystemVersion': '11.0',
        'NSHighResolutionCapable': True,
        # macOS shows this string in the Bluetooth permission prompt on first
        # BLE scan. Required by macOS 11+ for any app that uses CoreBluetooth.
        'NSBluetoothAlwaysUsageDescription':
            'Click2Key uses Bluetooth to talk to your Zwift Click controllers.',
        # Click2Key synthesizes keystrokes via the Accessibility API; this
        # string surfaces in macOS prompts where appropriate.
        'NSAppleEventsUsageDescription':
            'Click2Key sends keystrokes to the focused app.',
    },
)
