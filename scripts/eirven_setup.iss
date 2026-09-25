#define MyAppName "EIRVEN AI"
#define MyAppVersion "2.0.0"
#define MyAppPublisher "EIRVEN"
#define MyAppExeName "EIRVEN.exe"

[Setup]
AppId={{4E42B4D5-CA57-4A7B-8C12-61D9D86E1A92}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion} r67
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://eirven.foxyhosty.ru/
AppSupportURL=https://eirven.foxyhosty.ru/support
DefaultDirName={localappdata}\EIRVEN AI
DefaultGroupName=EIRVEN AI
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
MinVersion=10.0.17763
OutputDir=..
OutputBaseFilename=EIRVEN-Windows-r67
SetupIconFile=..\assets\eirven.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
LicenseFile=..\LICENSE
Compression=none
SolidCompression=no
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
VersionInfoVersion=2.0.0.67
VersionInfoCompany=EIRVEN
VersionInfoDescription=EIRVEN AI installer
VersionInfoProductName=EIRVEN AI
VersionInfoProductVersion=2.0.0.67
VersionInfoOriginalFileName=EIRVEN-Windows-r67.exe

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\build\native-launcher\payload\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\build\inno-launcher\EIRVEN.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\EIRVEN AI"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\EIRVEN AI"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить Эрви"; WorkingDir: "{app}"; Flags: nowait; Check: not MaterializeOnly

[Code]
function MaterializeOnly: Boolean;
var
  Index: Integer;
begin
  Result := False;
  for Index := 1 to ParamCount do
    if CompareText(ParamStr(Index), '/MATERIALIZEONLY') = 0 then
    begin
      Result := True;
      Exit;
    end;
end;
