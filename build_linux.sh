#!/bin/bash

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install requirements
echo "Installing dependencies..."
pip install -r requirements.txt


# Create executable
# Note: Ensure libmpv is installed on the system (e.g. sudo apt install libmpv2)
echo "Building executable..."
# We explicitly add the binary (libxcb-cursor.so.0) to the build
# Format: src:dest (dest=. means root of app)
pyinstaller --name "SleepyShows" --windowed --noconsole --clean \
    --add-binary "libs/libxcb-cursor.so.0:." \
    --add-data "assets:assets" \
    src/main.py

echo "Build complete. Executable is in dist/SleepyShows/"
