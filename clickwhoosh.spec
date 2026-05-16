# PyInstaller spec for Whoosh Clicker.
#
# macOS:  pyinstaller --noconfirm clickwhoosh.spec
#         → dist/Whoosh Clicker.app
# Windows: same command, produces dist/Whoosh Clicker/Whoosh Clicker.exe
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
    datas=collect_data_files('customtkinter') + collect_data_files('bleak'),
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
    name='Whoosh Clicker',
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
    name='Whoosh Clicker',
)

app = BUNDLE(
    coll,
    name='Whoosh Clicker.app',
    icon=None,
    bundle_identifier='app.clickwhoosh.whooshclicker',
    info_plist={
        'CFBundleName': 'Whoosh Clicker',
        'CFBundleDisplayName': 'Whoosh Clicker',
        'CFBundleShortVersionString': '0.1.0',
        'CFBundleVersion': '0.1.0',
        'LSMinimumSystemVersion': '11.0',
        'NSHighResolutionCapable': True,
        # macOS shows this string in the Bluetooth permission prompt on first
        # BLE scan. Required by macOS 11+ for any app that uses CoreBluetooth.
        'NSBluetoothAlwaysUsageDescription':
            'Whoosh Clicker uses Bluetooth to talk to your Zwift Click controllers.',
        # Without this, macOS won't even prompt for Accessibility; the user
        # still has to grant it in System Settings, but at least the app has
        # a stable bundle ID the TCC system can key off.
        'NSAppleEventsUsageDescription':
            'Whoosh Clicker may send keystrokes to MyWhoosh.',
    },
)
