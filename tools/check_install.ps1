<#
.SYNOPSIS
Check a Face Unlock installation and say what, if anything, still needs doing.

.DESCRIPTION
Read-only: it changes nothing. Run it as the person who uses the PC:

    powershell -NoProfile -ExecutionPolicy Bypass -File check_install.ps1

It looks at the background tasks and processes, the lock-screen tile, a Windows
setting that can hide the tile, and asks the running service for its status
(the "status" request only; it never asks for the stored password).
Exit code 0 means face sign-in is ready.
#>
$ErrorActionPreference = 'Continue'
$todo = New-Object System.Collections.Generic.List[string]

function Show([string] $Name, [bool] $Ok, [string] $Detail) {
    $mark = if ($Ok) { 'OK  ' } else { 'TODO' }
    Write-Host ("[{0}] {1}: {2}" -f $mark, $Name, $Detail)
}

# ---- background tasks and processes ---------------------------------------------------
foreach ($name in 'FaceUnlock-Service', 'FaceUnlock-Presence') {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    $runKey = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction SilentlyContinue
    $inRunKey = $runKey -and (($name -eq 'FaceUnlock-Service' -and $runKey.FaceUnlockService) -or
                              ($name -eq 'FaceUnlock-Presence' -and $runKey.FaceUnlock))
    if ($task) {
        Show "Starts at sign-in ($name)" $true "scheduled task, state $($task.State)"
    } elseif ($inRunKey) {
        Show "Starts at sign-in ($name)" $true 'Run key'
    } else {
        Show "Starts at sign-in ($name)" $false 'not set up'
        $todo.Add('Reinstall, or let first-run setup turn on "start when you sign in".')
    }
}
$processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -in 'face_service.exe', 'face_unlock_tray.exe' -or
    ($_.Name -in 'python.exe', 'pythonw.exe' -and $_.CommandLine -match 'face_service|presence_monitor') })
$serviceRunning = @($processes | Where-Object { $_.Name -eq 'face_service.exe' -or $_.CommandLine -match 'face_service' }).Count -gt 0
$trayRunning = @($processes | Where-Object { $_.Name -eq 'face_unlock_tray.exe' -or $_.CommandLine -match 'presence_monitor' }).Count -gt 0
Show 'Background service' $serviceRunning ($(if ($serviceRunning) { 'running' } else { 'not running' }))
Show 'Tray app' $trayRunning ($(if ($trayRunning) { 'running' } else { 'not running' }))
if (-not $serviceRunning -or -not $trayRunning) {
    $todo.Add('Start them: Start-ScheduledTask FaceUnlock-Service; Start-ScheduledTask FaceUnlock-Presence')
}

# ---- lock-screen tile ---------------------------------------------------------------------
$clsid = '{F8A0B4D9-3C7F-4B0A-9E21-8C1B1E2B7C10}'
$provider = Test-Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\Credential Providers\$clsid"
$dll = (Get-ItemProperty "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Classes\CLSID\$clsid\InprocServer32" -ErrorAction SilentlyContinue).'(default)'
$tileOk = $provider -and $dll -and (Test-Path ([Environment]::ExpandEnvironmentVariables($dll)))
Show 'Lock-screen tile' $tileOk ($(if ($tileOk) { $dll } elseif ($provider) { 'registered, but its file is missing' } else { 'not installed' }))
if (-not $tileOk) {
    $todo.Add('Add the tile: in Face Unlock, Overview > "Add to lock screen" (Windows asks for permission once).')
}
$helloOnly = $false
try {
    $helloOnly = (Get-ItemPropertyValue 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device' `
        -Name DevicePasswordLessBuildVersion) -eq 2
} catch { }
Show 'Windows Hello-only sign-in' (-not $helloOnly) ($(if ($helloOnly) { 'on: it hides the Face Unlock tile' } else { 'off' }))
if ($helloOnly) {
    $todo.Add('Settings > Accounts > Sign-in options: turn off "For improved security, only allow Windows Hello sign-in" (the person decides; it is a security setting).')
}

# ---- the service's own view -------------------------------------------------------------------
$status = $null
try {
    $pipe = New-Object System.IO.Pipes.NamedPipeClientStream('.', 'FaceUnlock', [System.IO.Pipes.PipeDirection]::InOut)
    $pipe.Connect(5000)
    $pipe.ReadMode = [System.IO.Pipes.PipeTransmissionMode]::Message
    $request = [Text.Encoding]::UTF8.GetBytes('{"cmd":"status"}')
    $pipe.Write($request, 0, $request.Length)
    $buffer = New-Object byte[] 65536
    $count = $pipe.Read($buffer, 0, $buffer.Length)
    $pipe.Dispose()
    $status = [Text.Encoding]::UTF8.GetString($buffer, 0, $count) | ConvertFrom-Json
} catch {
    Show 'Service status' $false "no answer ($($_.Exception.Message))"
    $todo.Add('Start the service (see above), wait about 20 seconds, then check again.')
}
$explain = @{
    'not-enrolled'              = 'Capture your face: Face Unlock > Overview > "Set up" (or first-run setup).'
    'no-credentials'            = 'Store your Windows account password in Face Unlock (the person types it into the app).'
    'password-rejected'         = 'Windows turned the stored password down: store the current one in Face Unlock.'
    'camera-missing'            = 'The chosen camera is not connected. Plug it in (Face Unlock never switches cameras by itself).'
    'camera-moved'              = 'The camera is on a different USB port: confirm it on the Overview.'
    'camera-ambiguous'          = 'Several matching cameras: choose one in Settings.'
    'camera-unpinned'           = 'No camera chosen: choose one in Settings or first-run setup.'
    'anti-spoofing-unavailable' = 'Photo protection could not load its model: connect to the internet, or turn photo protection off in Settings.'
    'model-unavailable'         = 'The face model could not be prepared (it downloads once, about 130 MB): connect to the internet, then Overview > "Try again".'
}
if ($status) {
    Show 'Service status' $true "version $($status.version), $(if ($status.warming -ne 'ready') { 'still starting' } else { 'started' })"
    Show 'Face profile' ($status.profile.anchors -gt 0) "$($status.profile.anchors) reference photos, $($status.profile.learned) learned"
    Show 'Stored password' ([bool] $status.credentials) ($(if ($status.credentials) { 'stored (encrypted)' } else { 'not stored' }))
    Show 'Camera' ($status.camera.device.state -eq 'ready') "$($status.camera.device.name) ($($status.camera.device.state))"
    Show 'Recognition model' ([bool] $status.recognition.model_present) ($(if ($status.recognition.model_present) { 'downloaded' } else { 'not downloaded yet (about 130 MB, needs the internet)' }))
    foreach ($reason in @($status.not_ready)) {
        if ($explain.ContainsKey($reason)) { $todo.Add($explain[$reason]) } else { $todo.Add("Service reports: $reason") }
    }
    if (@($status.config_problems).Count -gt 0) {
        $todo.Add('Some settings were reset to safe values: ' + (@($status.config_problems) -join '; '))
    }
}

Write-Host ''
if ($todo.Count -eq 0 -and $status -and $status.ready) {
    Write-Host 'Face sign-in is ready. To try it: press Windows+L, step away for a moment, come back and look at the camera.'
    exit 0
}
Write-Host 'Still to do:'
$todo | Select-Object -Unique | ForEach-Object { Write-Host " - $_" }
Write-Host ("Logs (no images or passwords): $env:USERPROFILE\.face-unlock\service.log and presence.log")
exit 1
