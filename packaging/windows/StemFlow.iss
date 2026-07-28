#ifndef AppVersion
  #define AppVersion "1.4.0"
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
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist\installer
OutputBaseFilename=StemFlow-Setup-{#AppVersion}-x64
SetupIconFile=bundle\stemflow.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
DiskSpanning=yes
DiskSliceSize=1900000000
SlicesPerDisk=1
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "chinesesimp"; MessagesFile: "bundle\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Files]
Source: "..\..\dist\StemFlow\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "bundle\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Dirs]
Name: "{commonappdata}\StemFlow"; Permissions: users-modify
Name: "{commonappdata}\StemFlow\models"; Permissions: users-modify
Name: "{commonappdata}\StemFlow\logs"; Permissions: users-modify
Name: "{commonappdata}\StemFlow\work"; Permissions: users-modify

[Icons]
Name: "{autoprograms}\StemFlow"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\StemFlow"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "正在安装 Microsoft Visual C++ 运行库…"; Flags: waituntilterminated
Filename: "{app}\{#AppExeName}"; Description: "启动 StemFlow"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""StemFlow-Video-BGM-Removal"" /F"; Flags: runhidden; RunOnceId: "RemoveStemFlowTask"

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    ForceDirectories(ExpandConstant('{commonappdata}\StemFlow'));
end;
