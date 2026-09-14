; ---------------------------------------------------------------------
; Dee.extension installer
; ---------------------------------------------------------------------
; Installs the extension for pyRevit from EITHER of two sources, chosen
; by the person installing:
;
;   GitHub  - drives pyRevit's own CLI ("pyrevit extend ui ...") to clone
;             the repo. The result is a real git clone, so pyRevit's
;             built-in updater and the extension's Update button can
;             git-pull it forever after. This is the normal route.
;
;   Mirror  - downloads one zip from Supabase Storage, extracts it, and
;             registers the folder with "pyrevit extensions paths add".
;             Needs no git and no access to github.com, which is the
;             whole point: on a network that blocks GitHub the first
;             route cannot work at all.
;
; Both carry identical files - the mirror is published from `git ls-files`
; at a commit that is already on GitHub, and the publisher refuses to run
; otherwise (tools/publish_supabase.py).
;
; THE ONE DIFFERENCE WORTH KNOWING, AND THE INSTALLER SAYS IT OUT LOUD:
; a mirror install is NOT a git clone, so "pyrevit extensions update"
; will never touch it. Those users update through the extension's own
; Update button on the mirror setting. It is a one-way choice per install,
; so it is stated on the page where the choice is made rather than buried.
;
; Needs an internet connection at install time (both routes fetch the
; code) and pyRevit already installed (checked before anything happens).
;
; Build with Inno Setup 6 (https://jrsoftware.org/isinfo.php):
;   ISCC.exe "Dee.extension.iss"
; Output lands in installer\Output\DeeExtensionSetup.exe

#define MyAppName "Dee.extension"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "ArchMKD"
#define MyAppURL "https://github.com/archMohk/Dee.extension"
#define RepoURL "https://github.com/archMohk/Dee.extension.git"
#define ExtensionCliName "Dee"
#define MirrorBase "https://gmuvmeolvkgqkmwvfbjj.supabase.co/storage/v1/object/public/dee-extension"
#define MirrorZip "Dee.extension.zip"

[Setup]
AppId={{E70C9D9F-CF93-45B9-9E64-88FC2BD8F608}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
; Installs entirely under the current user's own AppData (where pyRevit
; keeps its Extensions folder) - no admin rights needed, which matters for
; "any PC", including locked-down office machines.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\DeeExtensionInstaller
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=no
OutputDir=Output
OutputBaseFilename=DeeExtensionSetup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; No code payload - the extension's actual code is fetched at install
; time from whichever source was chosen, so this installer stays tiny and
; always delivers the current version rather than going stale. (It does
; embed one small JSON credentials file - see [Files] below.)
Uninstallable=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; Embedded in the compiled installer, NOT auto-extracted anywhere (dontcopy) -
; CurStepChanged below pulls it out on demand with ExtractTemporaryFile only
; when a fresh install has no acc_config.json of its own yet.
Source: "..\acc_config.json"; DestDir: "{tmp}"; Flags: dontcopy

[Code]
var
  SourcePage: TInputOptionWizardPage;
  AccPage: TInputFileWizardPage;
  ExtensionsRoot, ExtDir: string;
  InstalledViaMirror: Boolean;

const
  SRC_GITHUB = 0;
  SRC_MIRROR = 1;

function UseMirror: Boolean;
begin
  Result := SourcePage.Values[SRC_MIRROR];
end;

procedure InitializeWizard;
begin
  SourcePage := CreateInputOptionPage(wpWelcome,
    'Where should the extension come from?',
    'Both sources carry exactly the same files',
    'Choose GitHub unless your network blocks it.' + #13#10#13#10 +
    'GitHub gives you a git clone, which pyRevit can update by itself ' +
    'forever after - that is the normal route.' + #13#10#13#10 +
    'The mirror is a direct download that needs no git and no access to ' +
    'github.com. Worth knowing before you pick it: a mirror install is ' +
    'NOT a git clone, so "pyrevit extensions update" will never update ' +
    'it. You would update it from the extension''s own Update button, ' +
    'set to the mirror. That choice cannot be changed later without ' +
    'reinstalling.',
    True, False);
  SourcePage.Add('GitHub  (recommended - a git clone pyRevit can update itself)');
  SourcePage.Add('Supabase mirror  (direct download - use if GitHub is blocked)');
  SourcePage.Values[SRC_GITHUB] := True;

  AccPage := CreateInputFilePage(wpSelectDir,
    'ACC Credentials (optional)',
    'A default is already included - only browse if you need a different one',
    'This file holds Autodesk Construction Cloud app credentials for the ' +
    'cloud tools (DeeS.Publish, DeeSuperLINK, DeeMAPLink, and others). A ' +
    'default one is already bundled with this installer and will be used ' +
    'automatically - each person still signs in with their OWN Autodesk ' +
    'account, this file only identifies the app itself, not you.' + #13#10#13#10 +
    'Only browse to a file here if you specifically need a DIFFERENT ' +
    'Autodesk app registration than the bundled default. Leave this blank ' +
    'in every normal case.');
  AccPage.Add('acc_config.json:', 'JSON files|*.json|All files|*.*', '.json');
end;

function PyRevitInstalled: Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec('cmd.exe', '/c pyrevit --version', '', SW_HIDE,
                 ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ErrorCode: Integer;
begin
  Result := '';
  if not PyRevitInstalled then
  begin
    Result :=
      'pyRevit does not appear to be installed on this PC - the "pyrevit" ' +
      'command could not be run.' + #13#10#13#10 +
      'Install pyRevit first, then run this installer again.';
    ShellExec('open', 'https://github.com/pyrevitlabs/pyRevit/releases/latest',
              '', '', SW_SHOW, ewNoWait, ErrorCode);
  end;
end;

{ Runs a command, waits, and reports whether it succeeded. }
function RunHidden(const Cmd: string; var Code: Integer): Boolean;
begin
  Result := Exec('cmd.exe', '/c ' + Cmd, '', SW_HIDE,
                 ewWaitUntilTerminated, Code) and (Code = 0);
end;

{ Downloads the mirror zip and unpacks it into the extensions folder.

  PowerShell does both halves: Invoke-WebRequest for the download and
  Expand-Archive for the unpack. Both ship with Windows 10 and later, so
  this needs nothing installed that a target PC does not already have -
  and notably not git, which is the entire reason this route exists. }
function InstallFromMirror(var Detail: string): Boolean;
var
  Code: Integer;
  ZipPath, Cmd: string;
begin
  ZipPath := ExpandConstant('{tmp}\{#MirrorZip}');
  Cmd :=
    'powershell -NoProfile -ExecutionPolicy Bypass -Command "' +
    '$ErrorActionPreference=''Stop''; ' +
    '[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; ' +
    'Invoke-WebRequest -UseBasicParsing -Uri ''{#MirrorBase}/{#MirrorZip}'' ' +
    '-OutFile ''' + ZipPath + '''; ' +
    'Expand-Archive -Path ''' + ZipPath + ''' -DestinationPath ''' +
    ExtensionsRoot + ''' -Force"';

  if not RunHidden(Cmd, Code) then
  begin
    Detail :=
      'The download from the mirror failed (code ' + IntToStr(Code) + ').' +
      #13#10#13#10 +
      'Either the mirror has not been published yet, or this PC cannot ' +
      'reach it. Try the GitHub option instead.';
    Result := False;
    Exit;
  end;

  { Registering the FOLDER is what makes pyRevit find a non-cloned
    extension - "pyrevit extend" would clone from GitHub, which is
    exactly what this route is avoiding. }
  if not RunHidden('pyrevit extensions paths add "' + ExtensionsRoot + '"', Code) then
  begin
    Detail :=
      'The files downloaded, but pyRevit would not register the folder ' +
      '(code ' + IntToStr(Code) + '):' + #13#10 + ExtensionsRoot;
    Result := False;
    Exit;
  end;

  Detail := '';
  Result := True;
end;

function InstallFromGitHub(var Detail: string): Boolean;
var
  Code: Integer;
  Params: string;
begin
  if DirExists(ExtDir + '\.git') then
    Params := 'pyrevit extensions update ' + '{#ExtensionCliName}'
  else
    Params := 'pyrevit extend ui ' + '{#ExtensionCliName}' + ' ' +
              '{#RepoURL}' + ' --dest="' + ExtensionsRoot + '"';

  if not RunHidden(Params, Code) then
  begin
    Detail :=
      'pyRevit could not fetch the extension from GitHub (code ' +
      IntToStr(Code) + ').' + #13#10#13#10 +
      'If this PC blocks github.com, run the installer again and choose ' +
      'the Supabase mirror instead.';
    Result := False;
    Exit;
  end;
  Detail := '';
  Result := True;
end;

procedure PlaceAccConfig;
var
  Chosen, Target: string;
begin
  Target := ExtDir + '\acc_config.json';

  { An explicit browse wins over everything, including a file that is
    already there. The wizard page says "only browse if you need a
    different one", so someone who browsed has said precisely what they
    want - quietly discarding it is the single outcome they did not ask
    for. }
  Chosen := AccPage.Values[0];
  if (Chosen <> '') and FileExists(Chosen) then
  begin
    if not FileCopy(Chosen, Target, False) then
      MsgBox('Could not copy the acc_config.json you chose into:' + #13#10 +
             ExtDir + #13#10#13#10 + 'Copy it there by hand to finish.',
             mbError, MB_OK);
    Exit;
  end;

  { Nothing was browsed. An existing config is the user's own app
    registration - overwriting it would sign their whole team into
    somebody else's Autodesk app, so the bundled default only ever
    fills a gap, never replaces. }
  if FileExists(Target) then
    Exit;

  ExtractTemporaryFile('acc_config.json');
  FileCopy(ExpandConstant('{tmp}\acc_config.json'), Target, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Detail: string;
  Ok: Boolean;
begin
  if CurStep <> ssPostInstall then
    Exit;

  ExtensionsRoot := ExpandConstant('{userappdata}\pyRevit\Extensions');
  ExtDir := ExtensionsRoot + '\{#MyAppName}';
  ForceDirectories(ExtensionsRoot);

  InstalledViaMirror := UseMirror;
  if InstalledViaMirror then
    Ok := InstallFromMirror(Detail)
  else
    Ok := InstallFromGitHub(Detail);

  if not Ok then
  begin
    MsgBox(Detail, mbError, MB_OK);
    Exit;
  end;

  if not DirExists(ExtDir) then
  begin
    MsgBox('The install reported success but ' + ExtDir + ' is not there. ' +
           'Nothing further was changed.', mbError, MB_OK);
    Exit;
  end;

  PlaceAccConfig;

  if InstalledViaMirror then
    MsgBox('Installed from the Supabase mirror.' + #13#10#13#10 +
           'This copy is not a git clone, so "pyrevit extensions update" ' +
           'will not update it. Use the extension''s own Update button ' +
           'with "Supabase mirror" selected - it defaults to that ' +
           'automatically here.' + #13#10#13#10 +
           'Start Revit to finish loading the extension.',
           mbInformation, MB_OK)
  else
    MsgBox('Installed from GitHub.' + #13#10#13#10 +
           'Start Revit to finish loading the extension.',
           mbInformation, MB_OK);
end;

{ Uninstall has to undo whichever route was used. "pyrevit extensions
  delete" only knows about clones, so it silently does nothing for a
  mirror install - that folder has to be unregistered and removed by
  hand instead. Trying both, in that order, covers either case without
  the uninstaller needing to remember which one happened. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
  Root, Dir: string;
begin
  if CurUninstallStep <> usUninstall then
    Exit;

  Root := ExpandConstant('{userappdata}\pyRevit\Extensions');
  Dir := Root + '\{#MyAppName}';

  Exec('cmd.exe', '/c pyrevit extensions delete {#ExtensionCliName}', '',
       SW_HIDE, ewWaitUntilTerminated, Code);

  if DirExists(Dir) then
  begin
    Exec('cmd.exe', '/c pyrevit extensions paths forget "' + Root + '"', '',
         SW_HIDE, ewWaitUntilTerminated, Code);
    DelTree(Dir, True, True, True);
  end;
end;
