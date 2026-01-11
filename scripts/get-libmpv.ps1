[CmdletBinding()]
param(
    [string]$Destination = $PSScriptRoot
)

$ErrorActionPreference = 'Stop'

function Resolve-DestinationPath([string]$path) {
    if ([string]::IsNullOrWhiteSpace($path)) {
        throw "Destination path is empty."
    }
    $full = [System.IO.Path]::GetFullPath($path)
    if (-not (Test-Path $full)) {
        New-Item -ItemType Directory -Path $full | Out-Null
    }
    return $full
}

function Find-LibMpvCandidates {
    $candidates = New-Object System.Collections.Generic.List[string]

    # 1) Next to mpv.exe on PATH
    $mpvCmd = Get-Command mpv -ErrorAction SilentlyContinue
    if ($mpvCmd -and $mpvCmd.Source) {
        $mpvDir = Split-Path -Parent $mpvCmd.Source
        $candidates.Add((Join-Path $mpvDir 'libmpv-2.dll'))
    }

    # 2) Common Scoop location
    $scoopDir = Join-Path $env:USERPROFILE 'scoop\apps\mpv\current'
    $candidates.Add((Join-Path $scoopDir 'libmpv-2.dll'))

    # 3) Common install roots (shallow scan)
    $roots = @(
        $env:ProgramFiles,
        $env:'ProgramFiles(x86)',
        $env:LOCALAPPDATA
    ) | Where-Object { $_ -and (Test-Path $_) }

    foreach ($root in $roots) {
        # Try a couple likely folders without crawling the whole drive
        foreach ($guess in @('mpv', 'MPV', 'mpv.net', 'MPV.net')) {
            $candidates.Add((Join-Path (Join-Path $root $guess) 'libmpv-2.dll'))
        }
    }

    return $candidates | Where-Object { $_ -and $_.Length -gt 0 } | Select-Object -Unique
}

try {
    $destDir = Resolve-DestinationPath $Destination
    $destFile = Join-Path $destDir 'libmpv-2.dll'

    $found = $null
    foreach ($path in (Find-LibMpvCandidates)) {
        if (Test-Path $path) {
            $found = $path
            break
        }
    }

    if (-not $found) {
        Write-Host "Could not find libmpv-2.dll on this system." -ForegroundColor Yellow
        Write-Host "" 
        Write-Host "Recommended (fastest): install mpv, then re-run this script:" -ForegroundColor Cyan
        Write-Host "  winget install --id=mpv.mpv -e" 
        Write-Host "  # or" 
        Write-Host "  scoop install mpv" 
        Write-Host "" 
        Write-Host "After installing, re-run:" -ForegroundColor Cyan
        Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\\get-libmpv.ps1 -Destination ." 
        exit 1
    }

    Copy-Item -Force -Path $found -Destination $destFile
    Write-Host "Copied libmpv-2.dll" -ForegroundColor Green
    Write-Host "  From: $found"
    Write-Host "  To:   $destFile"
    exit 0
}
catch {
    Write-Host "Error: $($_.Exception.Message)" -ForegroundColor Red
    exit 2
}
