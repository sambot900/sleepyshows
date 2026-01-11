import os
import sys

def get_asset_path(filename):
    if getattr(sys, 'frozen', False):
        base_dir = sys._MEIPASS
    else:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    path = os.path.join(base_dir, 'assets', filename)
    print(f"Calculated path for {filename}: {path}")
    print(f"Exists: {os.path.exists(path)}")
    return path

get_asset_path("stars.png")
