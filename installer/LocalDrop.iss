; LocalDrop - Inno Setup script (Windows installer, the only Windows distribution)
; Build:  ISCC.exe /DAppVersion=0.2.2 /DAppExe=localdrop.exe installer\LocalDrop.iss
; Expects the `flet build windows` output in ..\build\windows, writes ..\dist\LocalDrop-Setup-<version>.exe

#ifndef AppVersion
  #define AppVersion "0.2.2"
#endif
#ifndef AppExe
  #define AppExe "localdrop.exe"
#endif
#define AppName "LocalDrop"
#define AppPublisher "RonovaDev"
#define AppURL "https://github.com/RonovaMain"

[Setup]
AppId={{B6D2A7A3-5C41-4E0B-9A0E-7F3A1C2D4E55}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=LocalDrop-Setup-{#AppVersion}
SetupIconFile=localdrop.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoProductName={#AppName}

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
; Windows Firewall blocks incoming connections by default - without a rule other devices can't open the sites.
Name: "firewall"; Description: "Allow LocalDrop in Windows Firewall (needed to open sites from phones on your Wi-Fi) / Разрешить LocalDrop в брандмауэре Windows (нужно, чтобы открывать сайты с телефонов в вашей Wi-Fi сети)"; GroupDescription: "Network / Сеть:"; Check: IsAdmin

[Files]
Source: "..\build\windows\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#AppName}"""; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#AppName}"" dir=in action=allow program=""{app}\{#AppExe}"" enable=yes profile=private,domain"; Flags: runhidden; Tasks: firewall
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#AppName}"""; Flags: runhidden; RunOnceId: "DelFirewallRule"
