# Optional: switch the Face Unlock sign-in tile to the newer provider build,
# or roll back to the previous one. Run in an elevated PowerShell.
#
#   .\update_provider.ps1            # use build-next\Release\FaceCredentialProvider.dll
#   .\update_provider.ps1 -Rollback  # go back to build\Release\FaceCredentialProvider.dll
#
# The new DLL is copied to its own folder first, so no file that the sign-in
# screen may have loaded is ever overwritten. Your PIN and password keep
# working either way; if the tile misbehaves, sign in with your PIN and run
# -Rollback.
param([switch]$Rollback)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated (administrator) PowerShell.'
}

$previous = Join-Path $PSScriptRoot 'build\Release\FaceCredentialProvider.dll'
$built = Join-Path $PSScriptRoot 'build-next\Release\FaceCredentialProvider.dll'

if ($Rollback) {
    if (-not (Test-Path $previous)) { throw "Previous provider not found: $previous" }
    & regsvr32.exe /s $previous
    Write-Host "Sign-in tile now uses: $previous"
    return
}

if (-not (Test-Path $built)) { throw "Build the provider first: cmake --build build-next --config Release" }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$target = Join-Path $PSScriptRoot "installed\$stamp"
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item $built $target
$dll = Join-Path $target 'FaceCredentialProvider.dll'
& regsvr32.exe /s $dll
if ($LASTEXITCODE -ne 0) { throw "Registration failed (exit $LASTEXITCODE); nothing was changed." }
Write-Host "Sign-in tile now uses: $dll"
Write-Host "To undo: .\update_provider.ps1 -Rollback"
