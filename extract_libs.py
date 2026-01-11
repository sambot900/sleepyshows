import os
import shutil
import subprocess
from pathlib import Path

def extract_libs():
    # Define paths
    root_dir = Path.cwd()
    local_lib_dir = root_dir / 'local_lib'
    libs_dir = root_dir / 'libs'
    
    # Create libs dir
    libs_dir.mkdir(exist_ok=True)
    
    # Find the deb file
    deb_files = list(local_lib_dir.rglob("*.deb"))
    if not deb_files:
        print("No .deb file found in local_lib. Please ensure the download succeeded.")
        return

    deb_path = deb_files[0]
    print(f"Found deb: {deb_path}")
    
    # Create temp extraction dir
    extract_dir = local_lib_dir / 'temp_extract'
    extract_dir.mkdir(exist_ok=True)
    
    try:
        # Extract deb (ar x)
        subprocess.run(['ar', 'x', str(deb_path)], cwd=extract_dir, check=True)
        
        # Extract data.tar.zst (tar -xf)
        # It might be data.tar.xz or data.tar.gz or data.tar.zst
        data_tar = list(extract_dir.glob("data.tar*"))
        if not data_tar:
             print("Could not find data.tar archive inside deb.")
             return
             
        subprocess.run(['tar', '-xf', data_tar[0].name], cwd=extract_dir, check=True)
        
        # Find the .so file
        so_files = list(extract_dir.rglob("libxcb-cursor.so*"))
        if not so_files:
            print("No libxcb-cursor.so found in extracted files.")
            return
            
        for so in so_files:
            # Copy to libs
            dest = libs_dir / so.name
            shutil.copy2(so, dest)
            print(f"Copied {so.name} to {libs_dir}")
            
    except Exception as e:
        print(f"Error during extraction: {e}")
    finally:
        # Cleanup temp
        shutil.rmtree(extract_dir, ignore_errors=True)

if __name__ == "__main__":
    extract_libs()
