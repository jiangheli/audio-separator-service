#ifndef AppVersion
  #define AppVersion "1.7.1"
#endif

#define AppName "StemFlow 视频纯人声工具"
#define AppPublisher "StemFlow"
#define AppExeName "StemFlow.exe"

[Setup]
AppId={{9D7E125B-1604-47DC-9C89-C61C69EB75D7}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\StemFlow
DefaultGroupName=StemFlow
DisableProgramGroupPage=yes
OutputDir=..\..\dist\update
OutputBaseFilename=StemFlow-Update-{#AppVersion}-x64
SetupIconFile=bundle\stemflow.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "chinesesimp"; MessagesFile: "bundle\ChineseSimplified.isl"

[Files]
Source: "..\..\dist-update\StemFlow\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\StemFlow"; Filename: "{app}\{#AppExeName}"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动 StemFlow"; Flags: nowait postinstall
