# One-line installer for Windows:
#   irm https://raw.githubusercontent.com/ConnorSawaya/limelight-training-recorder/main/install/install.ps1 | iex
$ErrorActionPreference = "Stop"

$repo = "ConnorSawaya/limelight-training-recorder"
$branch = "main"
$dest = Join-Path (Get-Location) "limelight-training-recorder"
$git = Get-Command git -ErrorAction SilentlyContinue

if (Test-Path -LiteralPath (Join-Path $dest ".git")) {
    if (-not $git) {
        throw "The existing project is a Git checkout, but Git was not found. Install Git or download the latest project folder manually."
    }
    Write-Host "Updating the existing checkout in $dest..."
    & $git.Source -C $dest pull --ff-only
    if ($LASTEXITCODE -ne 0) {
        throw "Could not update the existing checkout. Commit or stash local changes, then run the installer again."
    }
} elseif (-not (Test-Path -LiteralPath (Join-Path $dest "limelight_recorder.py"))) {
    Write-Host "Downloading limelight-training-recorder to $dest..."
    $tempRoot = Join-Path $env:TEMP ("limelight_setup_" + [guid]::NewGuid().ToString("N"))
    $zipPath = Join-Path $tempRoot "repo.zip"
    New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
    try {
        Invoke-WebRequest -Uri "https://github.com/$repo/archive/refs/heads/$branch.zip" -OutFile $zipPath -UseBasicParsing
        Expand-Archive -LiteralPath $zipPath -DestinationPath $tempRoot -Force
        $src = Join-Path $tempRoot "limelight-training-recorder-$branch"
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        Get-ChildItem -LiteralPath $src -Force | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $dest -Recurse -Force
        }
    } finally {
        if (Test-Path -LiteralPath $tempRoot) {
            Remove-Item -LiteralPath $tempRoot -Recurse -Force
        }
    }
} else {
    Write-Host "Using the existing folder $dest. Re-run from a Git checkout to enable automatic updates."
}

& (Join-Path $dest "windows\setup_windows.ps1")
