#!/bin/bash
# This script downloads the missing library LOCALLY into the project folder
# It does NOT install anything to your system.

mkdir -p local_lib
cd local_lib

# Download the package file (.deb) from the repository to this folder
echo "Fetching libxcb-cursor0..."
apt download libxcb-cursor0 2>/dev/null

# Clean previous extraction
rm -rf libxcb-cursor0_extract
mkdir -p libxcb-cursor0_extract

# Handle potential tarball wrapper from apt
if [ -f "libxcb-cursor0.tar.gz" ]; then
    echo "Unpacking tarball..."
    tar -xf libxcb-cursor0.tar.gz -C libxcb-cursor0_extract
else
    # Maybe it downloaded as .deb directly
    cp libxcb-cursor0*.deb libxcb-cursor0_extract/ 2>/dev/null
fi

cd libxcb-cursor0_extract

# Find the .deb file recursively (in case it's in a subdir)
DEB_FILE=$(find . -name "*.deb" | head -n 1)

if [ -z "$DEB_FILE" ]; then
    echo "Error: Could not find .deb file."
    exit 1
fi

echo "Extracting $DEB_FILE..."
ar x "$DEB_FILE"
tar -xf data.tar.zst

# Move the library file to the project root libs/ folder
mkdir -p ../libs
find . -name "libxcb-cursor.so*" -exec cp {} ../libs/ \;

echo "Cleaning up..."
cd ..
rm -rf libxcb-cursor0_extract
rm -f libxcb-cursor0.tar.gz libxcb-cursor0*.deb
rm -rf local_lib 

echo "Library ready in libs/"
ls -l libs/

# Move the library file to the root of local_lib for easy access
find usr/lib -name "libxcb-cursor.so*" -exec cp {} . \;

# Cleanup extra files
rm -rf usr
rm -f *.deb *.tar.zst debian-binary control.tar.zst
cd ..

echo "Library ready in local_lib/"
