; Inno Setup script for the ImageSL Windows installer.
;
; Wraps the PyInstaller onedir build (dist\ImageSL\) into a single
; ImageSL-Setup-Windows.exe — the exact asset name the landing page links to.
;
; Built by .github/workflows/build-desktop.yml:
;   ISCC /DMyAppVersion=2.0.1 /DMyAppVersionNum=2.0.1 desktop\installer.iss
;
; Installs per-user (no admin prompt, no UAC shield) because the app is not code
; signed — asking an unsigned installer for administrator rights is exactly the
; prompt users are told to refuse. Per-user install needs no elevation at all.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif
#ifndef MyAppVersionNum
  #define MyAppVersionNum "0.0.0"
#endif

#define MyAppName    "ImageSL"
#define MyAppExeName "ImageSL.exe"
#define MyAppPublisher "ImageSL"
; The site, NOT the GitHub repository. The repo is private, so a repo URL is a
; 404 for every user who has this installed - and these three end up in Add or
; Remove Programs as the publisher, support and updates links.
#define MyAppURL     "https://imagesl.com"

[Setup]
; Stable AppId — never change it, or upgrades install alongside instead of over.
AppId={{8F3C1D2A-6B47-4E9A-9C05-2A7D4E61B3F8}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersionNum}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}

; Paths below are relative to the repo root, not to desktop\.
SourceDir=..
OutputDir=.
OutputBaseFilename=ImageSL-Setup-Windows

; Per-user install: {autopf} becomes {localappdata}\Programs with lowest privileges.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
AllowNoIcons=yes

SetupIconFile=desktop\ImageSL.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

; Setup's own artwork. Without these Inno shows its stock graphics, so the
; installer - the first thing anyone sees of the product - looked like a generic
; setup wizard rather than like ImageSL. The extra files are the higher-DPI
; variants Inno picks on a scaled display; supplying only the 1x makes Setup
; upscale it and look soft.
WizardImageFile=desktop\wizard-large.bmp,desktop\wizard-large@2x.bmp,desktop\wizard-large@4x.bmp
WizardSmallImageFile=desktop\wizard-small.bmp,desktop\wizard-small@2x.bmp,desktop\wizard-small@3x.bmp
WizardImageStretch=no
UninstallDisplayName={#MyAppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; The bundle is ~24 MB of engine plus its scientific dependency stack.
DiskSpanning=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; The whole PyInstaller onedir tree: ImageSL.exe plus _internal\.
Source: "dist\ImageSL\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; PyInstaller writes nothing here, but the app caches decoded tiles under
; %LOCALAPPDATA%\ImageSL\cache — leave the user's data, drop only the cache.
Type: filesandordirs; Name: "{localappdata}\{#MyAppName}\cache"
