[CmdletBinding()]
param(
    [string]$Python = "py -3",
    [string]$VenvDir = ".venv",
    [string]$MpvArchive = "drivers/mpv-x86_64-v3-20260111-git-9483d6e.7z",
    [string]$DistDir = "dist/SleepyShows",
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'

function Invoke-Python([string]$Args) {
    $cmd = "$Python $Args"
    Write-Host $cmd
    Invoke-Expression $cmd
}

$repoRoot = (Resolve-Path "$PSScriptRoot/..").Path
Set-Location $repoRoot

if ($Clean) {
    if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
    if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }
}

if (-not (Test-Path $MpvArchive)) {
    throw "Missing MPV archive: $MpvArchive"
}

# 1) Create venv
if (-not (Test-Path $VenvDir)) {
    Invoke-Python "-m venv `"$VenvDir`""
}

$venvPython = Join-Path (Resolve-Path $VenvDir) "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Could not find venv python at $venvPython"
}

# 2) Install deps
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt
& $venvPython -m pip install py7zr

# 3) Build app (folder-based dist)
& $venvPython -m PyInstaller --name "SleepyShows" --windowed --noconsole src/main.py

# 4) Extract MPV + copy libmpv-2.dll into dist
$extractDir = Join-Path $env:TEMP "sleepyshows-mpv"
if (Test-Path $extractDir) { Remove-Item -Recurse -Force $extractDir }
New-Item -ItemType Directory -Path $extractDir | Out-Null

$extractPy = @'
import os
import sys
import shutil
from pathlib import Path

archive = Path(sys.argv[1]).resolve()
out_dir = Path(sys.argv[2]).resolve()

try:
    import py7zr
except Exception as e:
    raise SystemExit(f"py7zr is required but could not be imported: {e}")

if not archive.exists():
    raise SystemExit(f"Archive not found: {archive}")

out_dir.mkdir(parents=True, exist_ok=True)

with py7zr.SevenZipFile(str(archive), mode='r') as z:
    z.extractall(path=str(out_dir))

# Find libmpv-2.dll anywhere in the extracted tree
matches = list(out_dir.rglob('libmpv-2.dll'))
if not matches:
    raise SystemExit("libmpv-2.dll not found inside extracted archive")

# Prefer one closest to root if multiple
matches.sort(key=lambda p: len(p.parts))
src = matches[0]
print(str(src))
'@

$srcDll = & $venvPython -c $extractPy $MpvArchive $extractDir
$srcDll = $srcDll.Trim()

if (-not $srcDll) {
    throw "Failed to locate libmpv-2.dll in extracted archive"
}

if (-not (Test-Path $DistDir)) {
    throw "Dist output not found: $DistDir (PyInstaller may have failed)"
}

Copy-Item -Force -Path $srcDll -Destination (Join-Path $DistDir "libmpv-2.dll")

Write-Host "Done." -ForegroundColor Green
Write-Host "Built: $DistDir"
Write-Host "Copied: libmpv-2.dll"
