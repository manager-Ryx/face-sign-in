; Inno Setup script for Windows Face Unlock.
; Produces installer_output\WindowsFaceUnlock-Setup-<ver>.exe
;
; Build with:   ISCC.exe installer\installer.iss
; Or via:       python installer\build.py

#define MyAppName "Windows Face Unlock"
#define MyAppShortName "WindowsFaceUnlock"
#define MyAppVersion "0.3.0"
#define MyAppPublisher "Ryan van Zyl"
#define MyAppURL "https://github.com/manager-Ryx/face-sign-in"
#define MyAppExeName "face_unlock_tray.exe"
#define MyServiceExe "face_service.exe"
#define BuildRoot "..\dist\WindowsFaceUnlock"
; Decided when the installer is built: Setup's own file checks run before any
; file is installed, so they can't see the tile it is about to install.
#define HaveTile FileExists(AddBackslash(SourcePath) + BuildRoot + "\credential_provider\FaceCredentialProvider.dll")

[Setup]
; This fork's own ID (the original project used {2F7A9B14-...}), so the two
; never upgrade or uninstall each other.
AppId={{6795F92E-E1C6-4D2A-9A26-CA169621F440}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppShortName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\installer_output
OutputBaseFilename=WindowsFaceUnlock-Setup-{#MyAppVersion}
SetupIconFile=
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64
PrivilegesRequired=admin
CloseApplications=yes
RestartApplications=yes
UsePreviousAppDir=yes
UsePreviousTasks=yes

[Languages]
; Inno Setup 6 ships only Default.isl (English) out of the box. The app
; itself is fully translated into 12 languages at runtime — the installer
; wizard stays English for simplicity.
Name: "english";  MessagesFile: "compiler:Default.isl"

[Tasks]
#if HaveTile
Name: "cp"; Description: "Add face sign-in to the Windows lock screen (recommended)"; GroupDescription: "Windows integration"
#endif
Name: "startuptray"; Description: "Start the Face Unlock tray at sign-in"; GroupDescription: "Windows integration"

[Files]
; The whole PyInstaller output. Inno Setup will recurse.
Source: "{#BuildRoot}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; Post-install scripts (task registration etc.) invoked from [Run].
Source: "postinstall\register_tasks.ps1"; DestDir: "{app}\postinstall"; Flags: ignoreversion

; The per-user data folder (%USERPROFILE%\.face-unlock) is created by the app
; itself on first run, inside the right user's profile with that user's
; default permissions. (An earlier [Dirs] entry created a stray folder under
; AppData with "users-modify" rights, which would let other local users edit
; the face profile and settings.)

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName} — Uninstall"; Filename: "{uninstallexe}"
Name: "{autostartmenu}\Programs\{#MyAppName}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Registry]
; Used by the auto-updater fallback + uninstaller UI.
Root: HKLM; Subkey: "Software\{#MyAppShortName}"; ValueType: string; ValueName: "InstallLocation"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKLM; Subkey: "Software\{#MyAppShortName}"; ValueType: string; ValueName: "Version";         ValueData: "{#MyAppVersion}"

[Run]
; 1. Register the Credential Provider DLL (only if the user ticked the task)
#if HaveTile
Filename: "regsvr32.exe"; Parameters: "/s ""{app}\credential_provider\FaceCredentialProvider.dll"""; \
  Tasks: cp; StatusMsg: "Registering Credential Provider…"; Flags: runhidden
#endif

; 2. Create the per-user sign-in tasks as the user who started Setup (not the
;    elevated account), so they run in that user's session and profile.
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\postinstall\register_tasks.ps1"" -Service ""{app}\{#MyServiceExe}"" -Tray ""{app}\{#MyAppExeName}"""; \
  StatusMsg: "Registering scheduled tasks…"; Flags: runhidden runasoriginaluser

; 3. Kick off the service and tray right away.
Filename: "schtasks.exe"; Parameters: "/Run /TN ""\FaceUnlock-Service"""; Flags: runhidden skipifsilent runasoriginaluser
Filename: "schtasks.exe"; Parameters: "/Run /TN ""\FaceUnlock-Presence"""; Flags: runhidden skipifsilent runasoriginaluser

; 4. Open first-run setup (password, face, preferences). If the tray is already
;    running it opens setup itself; on an upgrade that was set up before, the
;    window just opens.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--welcome"; Description: "Set up {#MyAppName} now"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop and delete scheduled tasks first so files aren't held open.
Filename: "schtasks.exe"; Parameters: "/End    /TN ""\FaceUnlock-Presence"""; Flags: runhidden
Filename: "schtasks.exe"; Parameters: "/End    /TN ""\FaceUnlock-Service"""; Flags: runhidden
Filename: "schtasks.exe"; Parameters: "/Delete /TN ""\FaceUnlock-Presence"" /F"; Flags: runhidden
Filename: "schtasks.exe"; Parameters: "/Delete /TN ""\FaceUnlock-Service""  /F"; Flags: runhidden
; Unregister the Credential Provider if it was installed.
Filename: "regsvr32.exe"; Parameters: "/u /s ""{app}\credential_provider\FaceCredentialProvider.dll"""; \
  Flags: runhidden; Check: FileExists(ExpandConstant('{app}\credential_provider\FaceCredentialProvider.dll'))

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
function InitializeUninstall(): Boolean;
begin
  // Nothing fancy — UninstallRun handles task teardown.
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
  ResultCode: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{%USERPROFILE}\.face-unlock');
    if DirExists(DataDir) then
    begin
      if MsgBox('Also remove your saved enrollment data at ' + DataDir + '?', mbConfirmation, MB_YESNO) = IDYES then
      begin
        Exec(ExpandConstant('{cmd}'), '/C rmdir /S /Q "' + DataDir + '"',
             '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      end;
    end;
  end;
end;
