<#
.SYNOPSIS
Download, verify and install Face Unlock for Windows, then open its first-run setup.

.DESCRIPTION
Run this as yourself, in an ordinary (not "Run as administrator") PowerShell window:

    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1

The script checks the PC, downloads the newest release from GitHub, checks the
file against its published SHA-256, and runs Setup. Windows then asks for
permission once (a User Account Control prompt): Setup needs it to add the face
sign-in tile to the lock screen. Afterwards the background service and tray app
start, and first-run setup opens for the password, face capture and preferences.

Nothing here reads or asks for your Windows password.

.PARAMETER Version
A release tag such as v0.3.0-beta.1. Default: the newest release (pre-releases included).

.PARAMETER DownloadOnly
Download and verify the installer, then stop.

.PARAMETER SkipChecks
Install even if the PC checks find a problem.
#>
param(
    [string] $Version,
    [switch] $DownloadOnly,
    [switch] $SkipChecks
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with a progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo = 'manager-Ryx/face-sign-in'
$Headers = @{ 'User-Agent' = 'face-unlock-install-script' }

function Say([string] $Text) { Write-Host "[face-unlock] $Text" }

# ---- 1. Is this PC suitable? ------------------------------------------------------------
$problems = @()
if (-not [Environment]::Is64BitOperatingSystem) {
    $problems += 'Face Unlock needs 64-bit Windows 10 or 11.'
}
if ([Environment]::OSVersion.Version.Major -lt 10) {
    $problems += 'Face Unlock needs Windows 10 or 11.'
}
$cameras = @(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
    Where-Object { $_.Class -in 'Camera', 'Image' -and $_.Status -eq 'OK' })
if ($cameras.Count -eq 0) {
    $problems += 'No working camera was found. Connect a webcam and try again.'
} else {
    Say ('Cameras: ' + (($cameras | ForEach-Object { $_.FriendlyName }) -join ', '))
}
$helloOnly = $false
try {
    $value = Get-ItemPropertyValue 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device' `
        -Name DevicePasswordLessBuildVersion
    $helloOnly = ($value -eq 2)
} catch { }
if ($helloOnly) {
    Say ('Note: "For improved security, only allow Windows Hello sign-in" is on. It hides ' +
         'Face Unlock on the lock screen. Turn it off in Settings > Accounts > Sign-in options ' +
         '(you can do that after installing).')
}
$drive = Get-PSDrive -Name ($env:ProgramFiles.Substring(0, 1))
if ($drive.Free -lt 2GB) {
    $problems += ('Less than 2 GB free on drive ' + $drive.Name + ': (the app needs about 1.5 GB).')
}
if ($problems.Count -gt 0) {
    $problems | ForEach-Object { Say "Problem: $_" }
    if (-not $SkipChecks) { throw 'The PC checks found a problem (use -SkipChecks to install anyway).' }
}

# ---- 2. Which release? ------------------------------------------------------------------
if ($Version) {
    $release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/tags/$Version" -Headers $Headers
} else {
    $release = @(Invoke-RestMethod "https://api.github.com/repos/$Repo/releases?per_page=20" -Headers $Headers |
        Where-Object { -not $_.draft })[0]
}
if (-not $release) { throw "No release found in $Repo." }
$exe = @($release.assets | Where-Object { $_.name -like 'WindowsFaceUnlock-Setup-*.exe' })[0]
$sum = @($release.assets | Where-Object { $exe -and $_.name -eq ($exe.name + '.sha256') })[0]
if (-not $exe -or -not $sum) { throw "Release $($release.tag_name) has no installer and checksum." }
Say "Release $($release.tag_name): $($exe.name) ($([math]::Round($exe.size / 1MB)) MB)"

# ---- 3. Download and verify -------------------------------------------------------------
$folder = Join-Path $env:TEMP 'FaceUnlockSetup'
New-Item -ItemType Directory -Path $folder -Force | Out-Null
$exePath = Join-Path $folder $exe.name
$sumPath = Join-Path $folder $sum.name
Invoke-WebRequest $sum.browser_download_url -OutFile $sumPath -UseBasicParsing -Headers $Headers
$expected = ((Get-Content $sumPath -Raw).Trim() -split '\s+')[0].ToLower()
$haveIt = (Test-Path $exePath) -and ((Get-FileHash $exePath -Algorithm SHA256).Hash.ToLower() -eq $expected)
if (-not $haveIt) {
    Say 'Downloading (this can take a few minutes)...'
    Invoke-WebRequest $exe.browser_download_url -OutFile $exePath -UseBasicParsing -Headers $Headers
}
$actual = (Get-FileHash $exePath -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $expected) {
    Remove-Item $exePath -Force
    throw "The download doesn't match its published checksum (got $actual, expected $expected). Deleted it."
}
Say "Verified SHA-256 $actual"
if ($DownloadOnly) { Say "Installer saved at $exePath"; return }

# ---- 4. Install -------------------------------------------------------------------------
Say 'Starting Setup. Windows will ask for permission (User Account Control): choose Yes.'
# Not elevated on purpose: Setup asks for permission itself and then registers the
# background tasks for the person who started it, not for an administrator account.
$setup = Start-Process -FilePath $exePath -ArgumentList '/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-' `
    -PassThru -Wait
if ($setup.ExitCode -ne 0) {
    throw ("Setup did not finish (exit code $($setup.ExitCode)). If you chose No at the permission " +
           'prompt, run this script again and choose Yes.')
}
$installDir = (Get-ItemProperty 'HKLM:\SOFTWARE\WindowsFaceUnlock' -ErrorAction SilentlyContinue).InstallLocation
if (-not $installDir) { $installDir = Join-Path $env:ProgramFiles 'WindowsFaceUnlock' }
Say "Installed in $installDir"

# ---- 5. Start it and open first-run setup -----------------------------------------------
# A silent install skips Setup's own "start now" steps.
foreach ($task in 'FaceUnlock-Service', 'FaceUnlock-Presence') {
    try { Start-ScheduledTask -TaskName $task } catch { Say "Could not start $task : $($_.Exception.Message)" }
}
Start-Sleep -Seconds 5
Start-Process -FilePath (Join-Path $installDir 'face_unlock_tray.exe') -ArgumentList '--welcome'

# ---- 6. Did it really start? ---------------------------------------------------------------
# Installing the files is not the same as the app working: check both programs answer.
function Get-ServiceStatus {
    try {
        $pipe = New-Object System.IO.Pipes.NamedPipeClientStream('.', 'FaceUnlock', [System.IO.Pipes.PipeDirection]::InOut)
        $pipe.Connect(2000)
        $pipe.ReadMode = [System.IO.Pipes.PipeTransmissionMode]::Message
        $request = [Text.Encoding]::UTF8.GetBytes('{"cmd":"status"}')
        $pipe.Write($request, 0, $request.Length)
        $buffer = New-Object byte[] 65536
        $count = $pipe.Read($buffer, 0, $buffer.Length)
        $pipe.Dispose()
        return [Text.Encoding]::UTF8.GetString($buffer, 0, $count) | ConvertFrom-Json
    } catch { return $null }
}
$trayPipe = "FaceUnlockTray-$env:USERNAME"
$status = $null
$trayUp = $false
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline -and -not ($status -and $trayUp)) {
    Start-Sleep -Seconds 3
    if (-not $status) { $status = Get-ServiceStatus }
    $trayUp = @([IO.Directory]::GetFiles('\\.\pipe\') | Where-Object { $_ -like "*\$trayPipe" }).Count -gt 0
}
$logs = Join-Path $env:USERPROFILE '.face-unlock'
$healthy = $true
if (-not $trayUp) {
    $healthy = $false
    Say 'Problem: the Face Unlock app did not start (it may be showing an error window).'
}
if (-not $status) {
    $healthy = $false
    Say "Problem: the background service is not answering. See $logs\service.log"
} else {
    Say "Background service $($status.version) is running."
    $deadline = (Get-Date).AddMinutes(5)
    while ((Get-Date) -lt $deadline -and -not $status.recognition.model_loaded -and
           -not $status.recognition.model_error) {
        Say 'Preparing the face model (it downloads once, about 130 MB)...'
        Start-Sleep -Seconds 15
        $next = Get-ServiceStatus
        if ($next) { $status = $next }
    }
    if ($status.recognition.model_loaded) {
        Say 'Face model ready.'
    } elseif ($status.recognition.model_error) {
        $healthy = $false
        Say "Problem: the face model could not be prepared: $($status.recognition.model_error)"
    } else {
        Say 'The face model is still downloading; setup shows its progress.'
    }
}
if (-not $healthy) {
    Say "Installed, but not working yet. Logs (no images or passwords): $logs\service.log, $logs\presence.log"
    Say 'Please report it at https://github.com/manager-Ryx/face-sign-in/issues with those logs.'
    exit 1
}
Say 'First-run setup is open. It needs you for three things: your Windows account password'
Say '(typed into the setup window, never anywhere else), about a minute in front of the camera,'
Say 'and a few preferences.'
