#!/bin/bash

set -euo pipefail

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
pyinstaller --noconfirm --clean SleepyShows.spec

echo "Build complete. Executable is in dist/SleepyShows/"
