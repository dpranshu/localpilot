; LocalPilot installer (Inno Setup 6).  Build: build_installer.bat
#define AppName "LocalPilot"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{D70617FB-4585-483C-8AFE-7EB29CAE524B}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=LocalPilot
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=LocalPilot-Setup-{#AppVersion}
SetupIconFile=..\localpilot.ico
UninstallDisplayIcon={app}\LocalPilot.exe
LicenseFile=EULA.txt
InfoBeforeFile=..\THIRD_PARTY_NOTICES.md
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "Start LocalPilot in the system &tray when I sign in"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "..\dist\LocalPilot\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "EULA.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\LocalPilot.exe"
Name: "{group}\{#AppName} (tray)"; Filename: "{app}\LocalPilot.exe"; Parameters: "--tray"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\LocalPilot.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "LocalPilot"; ValueData: """{app}\LocalPilot.exe"" --tray"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\LocalPilot.exe"; Description: "Launch LocalPilot (it will help you set up the AI engine)"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /F /IM LocalPilot.exe"; Flags: runhidden; RunOnceId: "KillApp"

[Messages]
FinishedLabel=LocalPilot is installed. On first launch, click "Check setup": LocalPilot can install the free local AI engine (Ollama) and download the model (about 5 GB, one time). Your history and settings are kept in %APPDATA%\LocalPilot and are not removed on uninstall.
