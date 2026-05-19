#!/bin/bash
# On Linux/Wayland, mpv embedding needs an X11 window handle (winId).
# Force Qt to render through XWayland so `QWidget.winId()` returns a
# valid X11 WId that python-mpv can use.
export QT_QPA_PLATFORM=xcb
export LD_LIBRARY_PATH=$PWD/libs:$LD_LIBRARY_PATH
./venv/bin/python src/main.py