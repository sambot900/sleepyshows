from PyInstaller.utils.hooks.qt import add_qt6_dependencies

hiddenimports, binaries, datas = add_qt6_dependencies(__file__)

# Exclude Qt TIFF image plugin on Linux builds to avoid optional libtiff
# dependency warnings; Sleepy Shows does not rely on TIFF assets.
binaries = [entry for entry in binaries if 'libqtiff' not in entry[0]]
