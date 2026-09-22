; ============================================================
;  凌日光影棚 —— Inno Setup 安装程序脚本（可选）
;  生成方式：安装 Inno Setup 6 后运行
;      ISCC.exe packaging\make_installer_windows.iss
;  或直接运行 packaging\make_installer_windows.bat
;  前置：已执行 packaging\build_windows.bat，存在 dist\HorizonLightStudio.exe
; ============================================================
#define AppName "凌日光影棚"
#define AppNameEn "Horizon Light Studio"
#define AppVersion "1.0.0"
#define AppExe "HorizonLightStudio.exe"

[Setup]
AppId={{8E4C1D62-3B7A-4F19-9E52-6D0C7A21B4E3}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Horizon Light Studio
DefaultDirName={autopf}\{#AppNameEn}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=HorizonLightStudio-{#AppVersion}-Setup
SetupIconFile=icons\appicon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 中文安装向导
ShowLanguageDialog=auto

[Languages]
; Inno Setup 6 ships 简体中文 as Languages\ChineseSimplified.isl in some builds and
; as an unofficial translation in others. To avoid a hard compile error, the
; installer defaults to English; add the Chinese line below (with a real .isl
; path) if you want a fully localized wizard:
;   Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\*"; DestDir: "{app}\docs"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\server\*"; DestDir: "{app}\server"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "__pycache__"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\用户手册"; Filename: "{app}\docs\用户手册.md"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent
