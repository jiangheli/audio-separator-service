#ifndef AppVersion
  #define AppVersion "1.4.1"
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
OutputDir=..\..\dist\repair
OutputBaseFilename=StemFlow-Repair-{#AppVersion}-x64
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
Source: "..\..\dist-repair\StemFlow\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\StemFlow"; Filename: "{app}\{#AppExeName}"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动 StemFlow"; Flags: nowait postinstall skipifsilent

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ManifestPath: String;
begin
  ManifestPath := ExpandConstant(
    '{app}\_internal\gpu-bootstrap\wheelhouse\wheelhouse-manifest.json'
  );
  if not FileExists(ManifestPath) then
    Result :=
      '没有检测到 StemFlow v1.4.0 完整离线 CUDA 资源。' + #13#10 +
      '请先安装完整离线套件，再运行此修复包。'
  else
    Result := '';
end;
