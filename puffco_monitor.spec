# PyInstaller spec for "Puffco Monitor.exe"  -  run:  python -m PyInstaller --noconfirm --clean puffco_monitor.spec
# (or just double-click "Build Puffco Monitor.bat")
import os
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['add_rig'] + collect_submodules('bleak.backends.winrt')

datas = [('app.html', '.'), ('overlay.html', '.'), ('icon.png', '.'), ('icon.ico', '.')]
if os.path.isdir('rigs'):
    datas.append(('rigs', 'rigs'))      # default editions, copied next to the exe on first run

a = Analysis(
    ['puffco_monitor.py'],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=[
        'tkinter', 'bleak.backends.bluezdbus', 'bleak.backends.corebluetooth', 'bleak.backends.p4android',
        'PIL._avif', 'PIL.AvifImagePlugin', 'PIL._imagingft',          # codecs/fonts the app never uses
        'setuptools', 'pkg_resources', 'numpy.f2py', 'pydoc_data', 'lib2to3',
    ],
)
# Windows 10/11 always use their own Universal CRT; some Python builds (e.g. conda) drag a copy along
a.binaries = [b for b in a.binaries if not (b[0].lower().startswith('api-ms-win-') or b[0].lower() == 'ucrtbase.dll')]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Puffco Monitor',
    console=False,
    upx=False,
    version='version.txt',
    icon=['icon.ico'],
)
