; Inno Setup script for tide-setup.exe. Built by the windows workflow:
;   iscc /DAppVersion=2.3.0 windows\tide.iss
; Installs for the current user only, so there's no admin prompt. The
; wizard is just: desktop icon checkbox, install, "launch tide". tide's
; Windows build also runs it silently to update itself (updates.py).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; never change AppId: Windows uses it to match upgrades and the uninstaller
AppId={{33905D0B-91A5-4272-BBD6-5DE46BAC871A}
AppName=tide
AppVersion={#AppVersion}
AppVerName=tide {#AppVersion}
AppPublisher=tide
AppPublisherURL=https://github.com/captiencelovesarch/tide
AppSupportURL=https://github.com/captiencelovesarch/tide/issues
DefaultDirName={localappdata}\Programs\tide
DefaultGroupName=tide
PrivilegesRequired=lowest
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\build\windows\installer
OutputBaseFilename=tide-setup
SetupIconFile=tide.ico
UninstallDisplayIcon={app}\tide.exe
UninstallDisplayName=tide
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; an upgrade closes a running tide first instead of failing on locked files
CloseApplications=force

[Tasks]
Name: "desktopicon"; Description: "Put a tide icon on the desktop"

[InstallDelete]
; clear the old version's files so an upgrade never mixes two builds
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\build\windows\dist\tide\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\tide"; Filename: "{app}\tide.exe"
Name: "{autodesktop}\tide"; Filename: "{app}\tide.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\tide.exe"; Description: "Open tide now"; Flags: nowait postinstall skipifsilent
; tide's own updater runs this silently as it quits; /RELAUNCH=1 (the
; toast's restart button) opens it again when the files are in place
Filename: "{app}\tide.exe"; Flags: nowait; Check: Relaunch

[Code]
function Relaunch: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;
