$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$toolsRoot = Join-Path $projectRoot "tools\ffmpeg"
$ffmpegExe = Join-Path $toolsRoot "bin\ffmpeg.exe"

Write-Host "Limelight Training Recorder setup"
Write-Host "Project: $projectRoot"

function Find-UsablePython {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher -and $launcher.Source -notlike "*\WindowsApps\*") {
        return [pscustomobject]@{ Exe = $launcher.Source; Args = @("-3") }
    }
    $interpreter = Get-Command python -ErrorAction SilentlyContinue
    if ($interpreter -and $interpreter.Source -notlike "*\WindowsApps\*") {
        return [pscustomobject]@{ Exe = $interpreter.Source; Args = @() }
    }
    # Python.org's per-user installer commonly uses this location but does
    # not always install the py launcher or update PATH for existing shells.
    foreach ($knownPath in @(
        (Join-Path $env:LocalAppData "Programs\Python\Python313\python.exe"),
        (Join-Path $env:LocalAppData "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LocalAppData "Programs\Python\Python311\python.exe")
    )) {
        if (Test-Path -LiteralPath $knownPath) {
            return [pscustomobject]@{ Exe = $knownPath; Args = @() }
        }
    }
    return $null
}

$pythonInfo = Find-UsablePython
if (-not $pythonInfo) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "Python was not found. Installing Python 3.12 for the current user with winget..."
        & $winget.Source install --id Python.Python.3.12 --exact --scope user --accept-package-agreements --accept-source-agreements
        # winget updates the user PATH for future processes; refresh this
        # process too so the remainder of setup can use the newly installed
        # launcher immediately.
        $env:Path = @(
            [Environment]::GetEnvironmentVariable("Path", "Machine")
            [Environment]::GetEnvironmentVariable("Path", "User")
        ) -join ";"
        $pythonInfo = Find-UsablePython
    }
}

if (-not $pythonInfo) {
    throw "Python 3 was not found and winget could not install it. Install Python 3.10 or newer from https://www.python.org/downloads/windows/ and run setup again."
}
$pythonExe = $pythonInfo.Exe
$pythonArgs = @($pythonInfo.Args)

$versionText = (& $pythonExe @pythonArgs --version 2>&1 | Out-String).Trim()
Write-Host "Found $versionText"
$versionCheck = (& $pythonExe @pythonArgs -c "import sys; print(int(sys.version_info >= (3, 10)))").Trim()
if ($versionCheck -ne "1") {
    throw "Python 3.10 or newer is required. Found $versionText"
}

$pathFfmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ((Test-Path -LiteralPath $ffmpegExe) -or $pathFfmpeg) {
    if (Test-Path -LiteralPath $ffmpegExe) {
        Write-Host "FFmpeg is already installed locally."
    } else {
        Write-Host "FFmpeg was found on PATH: $($pathFfmpeg.Source)"
    }
} else {
    Write-Host "FFmpeg was not found. Downloading the current Windows essentials build..."
    $tempRoot = Join-Path $env:TEMP ("limelight_ffmpeg_" + [guid]::NewGuid().ToString("N"))
    $zipPath = Join-Path $tempRoot "ffmpeg.zip"
    New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
    try {
        Invoke-WebRequest -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $zipPath -UseBasicParsing
        Expand-Archive -LiteralPath $zipPath -DestinationPath $tempRoot -Force
        $downloadedExe = Get-ChildItem -LiteralPath $tempRoot -Filter "ffmpeg.exe" -Recurse -File | Select-Object -First 1
        if (-not $downloadedExe) {
            throw "The FFmpeg archive did not contain ffmpeg.exe."
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ffmpegExe) | Out-Null
        Copy-Item -LiteralPath $downloadedExe.FullName -Destination $ffmpegExe -Force
        Write-Host "Installed project-local FFmpeg at $ffmpegExe"
    } finally {
        if (Test-Path -LiteralPath $tempRoot) {
            Remove-Item -LiteralPath $tempRoot -Recurse -Force
        }
    }
}

Write-Host ""
Write-Host "Setup complete. Double-click windows\start_recorder.bat to begin recording."
