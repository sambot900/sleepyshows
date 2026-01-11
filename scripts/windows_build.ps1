[CmdletBinding()]
param(
    [string]$Python = "py",
    [string[]]$PythonArgs = @("-3"),
    [string]$VenvDir = ".venv",
    [string]$MpvArchive = "drivers/mpv-x86_64-v3-20260111-git-9483d6e.7z",
    [string]$DistDir = "dist/SleepyShows",
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'

function Get-SevenZip {
    $cmd = Get-Command "7z.exe" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $candidates = @(
        (Join-Path $env:ProgramFiles "7-Zip\7z.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "7-Zip\7z.exe")
    )

    foreach ($p in $candidates) {
        if ($p -and (Test-Path $p)) { return $p }
    }

    return $null
}

function Find-LibMpvCandidates {
    $candidates = New-Object System.Collections.Generic.List[string]

    # 1) Next to mpv.exe on PATH
    $mpvCmd = Get-Command mpv -ErrorAction SilentlyContinue
    if ($mpvCmd -and $mpvCmd.Source) {
        $mpvDir = Split-Path -Parent $mpvCmd.Source
        $candidates.Add((Join-Path $mpvDir 'libmpv-2.dll'))
        $candidates.Add((Join-Path $mpvDir 'mpv-2.dll'))
    }

    # 2) Common Scoop location
    $scoopDir = Join-Path $env:USERPROFILE 'scoop\apps\mpv\current'
    $candidates.Add((Join-Path $scoopDir 'libmpv-2.dll'))
    $candidates.Add((Join-Path $scoopDir 'mpv-2.dll'))

    # 3) Likely install folders
    $roots = @(
        $env:ProgramFiles,
        ${env:ProgramFiles(x86)},
        $env:LOCALAPPDATA
    ) | Where-Object { $_ -and (Test-Path $_) }

    foreach ($root in $roots) {
        foreach ($guess in @('mpv', 'MPV', 'mpv.net', 'MPV.net', 'Programs\\mpv', 'Programs\\MPV', 'Programs\\mpv.net', 'Programs\\MPV.net')) {
            $candidates.Add((Join-Path (Join-Path $root $guess) 'libmpv-2.dll'))
            $candidates.Add((Join-Path (Join-Path $root $guess) 'mpv-2.dll'))
        }
    }

    return $candidates | Where-Object { $_ -and $_.Length -gt 0 } | Select-Object -Unique
}

function Find-LibMpvOnSystem {
    foreach ($path in (Find-LibMpvCandidates)) {
        if (Test-Path $path) {
            return (Resolve-Path $path).Path
        }
    }
    return $null
}

function Invoke-Python([string[]]$PythonCmdArgs) {
    $display = @($Python) + $PythonArgs + $PythonCmdArgs
    Write-Host ($display -join ' ')

    & $Python @PythonArgs @PythonCmdArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE"
    }
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
    Invoke-Python @('-m', 'venv', $VenvDir)
}

$venvPython = Join-Path (Resolve-Path $VenvDir) "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Could not find venv python at $venvPython"
}

# 2) Install deps
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt

# 3) Build app (folder-based dist)
# Note: on Windows, PyInstaller --add-data uses a semicolon: source;dest
& $venvPython -m PyInstaller --name "SleepyShows" --windowed --noconsole --add-data "assets;assets" src/main.py

# 4) Extract MPV + copy libmpv-2.dll into dist
$extractDir = Join-Path $env:TEMP "sleepyshows-mpv"
if (Test-Path $extractDir) { Remove-Item -Recurse -Force $extractDir }
New-Item -ItemType Directory -Path $extractDir | Out-Null

$sevenZip = Get-SevenZip
if (-not $sevenZip) {
    throw "7-Zip (7z.exe) not found. Install 7-Zip or add 7z.exe to PATH, then re-run this script."
}

Write-Host "Extracting MPV archive with: $sevenZip"
& $sevenZip x $MpvArchive "-o$extractDir" -y | Out-Null

$srcDll = Get-ChildItem -Path $extractDir -Recurse -Filter "libmpv-2.dll" -File -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName

if (-not $srcDll) {
    $srcDll = Get-ChildItem -Path $extractDir -Recurse -Filter "mpv-2.dll" -File -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
}

if (-not $srcDll) {
    $srcDll = Find-LibMpvOnSystem
}

if (-not $srcDll) {
    $winget = Get-Command "winget.exe" -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "libmpv/mpv-2.dll not found; attempting to install mpv.net via winget..."
        & winget install --id mpv.net -e --accept-package-agreements --accept-source-agreements | Out-Null
        $srcDll = Find-LibMpvOnSystem
    }
}

if (-not $srcDll) {
    throw "Could not locate libmpv-2.dll. Install mpv (so libmpv-2.dll is present) or provide it manually, then re-run this script."
}

if (-not (Test-Path $DistDir)) {
    throw "Dist output not found: $DistDir (PyInstaller may have failed)"
}

$destDllName = "libmpv-2.dll"
if ((Split-Path -Leaf $srcDll) -ieq "mpv-2.dll") {
    $destDllName = "libmpv-2.dll"
}

Copy-Item -Force -Path $srcDll -Destination (Join-Path $DistDir $destDllName)

Write-Host "Done." -ForegroundColor Green
Write-Host "Built: $DistDir"
Write-Host "Copied: libmpv-2.dll"
