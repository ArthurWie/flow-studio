; Flow Studio installer — per-user, no admin. Compile with ISCC.exe (Inno Setup 6) via package.ps1.
; Wraps the frozen app in dist\FlowStudio\ (bundled models included) and the WebView2 bootstrapper.
#define AppName "Flow Studio"
#ifndef AppVer
  #define AppVer "1.0.0"
#endif

[Setup]
; Same AppId as the old bootstrap installer (which left it at its default, AppName),
; so installing over it upgrades in place: same uninstall entry, same folder.
AppId={#AppName}
AppName={#AppName}
AppVersion={#AppVer}
DefaultDirName={localappdata}\Programs\FlowStudio
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=FlowStudioSetup
OutputDir=.
Compression=lzma2
SolidCompression=yes
SetupIconFile=flow.ico
UninstallDisplayIcon={app}\FlowStudio.exe
WizardStyle=modern

[InstallDelete]
; Leftovers of the old bootstrap install: its Python env and the loose files it shipped.
; User data in %LOCALAPPDATA%\FlowStudio (history, pronunciations, settings) is not touched.
Type: filesandordirs; Name: "{app}\env"
Type: files; Name: "{app}\bootstrap.exe"
Type: files; Name: "{app}\uv.exe"
Type: files; Name: "{app}\*.py"
Type: files; Name: "{app}\requirements.txt"
; Libraries of the previous frozen build, so no stale DLL survives an upgrade.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "dist\FlowStudio\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "build\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedsWebView2

[Icons]
Name: "{group}\Flow Studio"; Filename: "{app}\FlowStudio.exe"
Name: "{userdesktop}\Flow Studio"; Filename: "{app}\FlowStudio.exe"
Name: "{group}\Uninstall Flow Studio"; Filename: "{uninstallexe}"

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installing Microsoft Edge WebView2 Runtime..."; Check: NeedsWebView2; Flags: waituntilterminated
Filename: "{app}\FlowStudio.exe"; Description: "Launch Flow Studio"; Flags: nowait postinstall skipifsilent
; The in-app updater installs with /VERYSILENT /RELAUNCH=1 and wants the new version started.
Filename: "{app}\FlowStudio.exe"; Flags: nowait; Check: Relaunch

[UninstallDelete]
; Models downloaded in-app share the bundled cache folder.
Type: filesandordirs; Name: "{app}\models"

[Code]
function Relaunch: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

// WebView2 Runtime detection, per Microsoft's distribution docs: a non-empty "pv" other
// than 0.0.0.0 under the machine-wide or per-user EdgeUpdate client key.
function HasWebView2(Root: Integer; Key: String): Boolean;
var
  pv: String;
begin
  Result := RegQueryStringValue(Root, Key, 'pv', pv) and (pv <> '') and (pv <> '0.0.0.0');
end;

function NeedsWebView2: Boolean;
begin
  Result := not (HasWebView2(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}')
              or HasWebView2(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'));
end;
