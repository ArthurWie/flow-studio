; Flow Studio installer — per-user, no admin. Compile with ISCC.exe (Inno Setup 6).
; Expects staged files under .\stage\ (produced by package.ps1).
#define AppName "Flow Studio"
#define AppVer "1.0.0"

[Setup]
AppName={#AppName}
AppVersion={#AppVer}
DefaultDirName={localappdata}\Programs\FlowStudio
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=FlowStudioSetup
OutputDir=.
Compression=lzma2
SolidCompression=yes
SetupIconFile=flow.ico
UninstallDisplayIcon={app}\bootstrap.exe
WizardStyle=modern

[Files]
Source: "stage\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Flow Studio"; Filename: "{app}\bootstrap.exe"; IconFilename: "{app}\flow.ico"
Name: "{userdesktop}\Flow Studio"; Filename: "{app}\bootstrap.exe"; IconFilename: "{app}\flow.ico"
Name: "{group}\Uninstall Flow Studio"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\bootstrap.exe"; Description: "Launch Flow Studio (runs one-time setup)"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\env"
