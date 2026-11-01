#define AppName "WB Profit Desktop"
#define AppVersion "0.1.0"
[Setup]
AppId={{42F5C285-3537-49A9-AD8E-0F138E10A3A7}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
OutputDir=..\dist\installer
OutputBaseFilename=WB-Profit-Desktop-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\WB Profit Desktop.exe
[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
[Files]
Source: "..\dist\WB Profit Desktop\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\WB Profit Desktop.exe"
Name: "{group}\{#AppName}"; Filename: "{app}\WB Profit Desktop.exe"
[Run]
Filename: "{app}\WB Profit Desktop.exe"; Description: "Открыть WB Profit Desktop"; Flags: nowait postinstall skipifsilent
