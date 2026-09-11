; Dee.extension installer for pyRevit (Revit 2024+)
;
; This installer does NOT bundle a copy of the extension's code. Instead it
; drives pyRevit's own CLI ("pyrevit extend ui ...") to clone the extension
; straight from GitHub into pyRevit's default Extensions folder and register
; it with pyRevit's extension manager - the same mechanism pyRevit itself
; recommends for distributing a third-party extension, and what makes the
; extension's own "Update" button (pyrevit extensions update --all) work
; correctly afterward. Verified live against this machine's real pyrevit
; CLI (pyrevit extend ui <name> <url> --dest=<path> clones into
; <path>\<name>.extension and registers it; pyrevit extensions delete <name>
; unregisters AND removes the cloned folder) before writing this script.
;
; It DOES bundle one small file: acc_config.json (the ACC/APS app's
; client_id, redirect_uri and region - see lib/acc_auth.py). This is safe to
; ship inside a distributed binary specifically because of the PKCE OAuth
; migration: a client_id is a PUBLIC identifier by design (there is no
; client_secret anymore), the same way any desktop/mobile/SPA app ships its
; client_id in cleartext - it only lets someone start a login as THEMSELVES,
; never grants access on its own. The bundled file is only ever written to a
; fresh install with no existing acc_config.json, and only if the wizard's
; own "browse to a different one" page was left blank - it never overwrites
; a machine that already has its own config (see CurStepChanged below).
;
; Needs an internet connection at install time (it clones from GitHub) and
; pyRevit already installed - this installer only adds Dee.extension on top
; of an existing pyRevit install, it does not install pyRevit itself.
;
; Build with Inno Setup 6 (https://jrsoftware.org/isinfo.php):
;   ISCC.exe "Dee.extension.iss"
; Output lands in installer\Output\DeeExtensionSetup.exe

#define MyAppName "Dee.extension"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "ArchMKD"
#define MyAppURL "https://github.com/archMohk/Dee.extension"
#define RepoURL "https://github.com/archMohk/Dee.extension.git"
#define ExtensionCliName "Dee"

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
; No code payload - the extension's actual code comes from a live GitHub
; clone at install time, so this installer stays tiny and always fetches
; the current version rather than going stale. (It does embed one small
; JSON credentials file - see [Files] below.)
Uninstallable=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; Embedded in the compiled installer, NOT auto-extracted anywhere (dontcopy) -
; CurStepChanged below pulls it out on demand with ExtractTemporaryFile only
; when a fresh install has no acc_config.json of its own yet.
Source: "..\acc_config.json"; DestDir: "{tmp}"; Flags: dontcopy

[UninstallRun]
; pyrevit extensions delete both unregisters the extension AND deletes its
; cloned folder from disk (verified live) - one call is a complete removal.
Filename: "cmd.exe"; Parameters: "/c pyrevit extensions delete {#ExtensionCliName}"; Flags: runhidden; RunOnceId: "RemoveDeeExtension"

[Code]
var
  AccPage: TInputFileWizardPage;
  ExtensionsRoot, ExtDir: string;

procedure InitializeWizard;
begin
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

function PyRevitCliFound(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec('cmd.exe', '/c pyrevit --version', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  if CurPageID = wpReady then
  begin
    if not PyRevitCliFound() then
    begin
      if MsgBox(
        'pyRevit does not appear to be installed on this PC - the "pyrevit" ' +
        'command was not found.' + #13#10#13#10 +
        'Dee.extension is an add-on for pyRevit, so pyRevit itself needs to ' +
        'be installed first.' + #13#10#13#10 +
        'Open the pyRevit download page now?',
        mbError, MB_YESNO) = IDYES then
        ShellExec('open', 'https://github.com/pyrevitlabs/pyRevit/releases/latest',
          '', '', SW_SHOW, ewNoWait, ResultCode);
      Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Params: string;
begin
  if CurStep = ssPostInstall then
  begin
    ExtensionsRoot := ExpandConstant('{userappdata}\pyRevit\Extensions');
    ExtDir := ExtensionsRoot + '\Dee.extension';
    ForceDirectories(ExtensionsRoot);

    if DirExists(ExtDir) then
    begin
      if DirExists(ExtDir + '\.git') then
      begin
        WizardForm.StatusLabel.Caption := 'Dee.extension already installed - updating...';
        Params := '/c pyrevit extensions update ' + '{#ExtensionCliName}';
        if not Exec('cmd.exe', Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
           or (ResultCode <> 0) then
          MsgBox(
            'Dee.extension was already present but could not be auto-updated ' +
            '(it may not be registered with pyRevit''s extension manager). It ' +
            'has been left as-is - use the Update button inside Revit, or ' +
            'Reload pyRevit, to pick up the latest version.',
            mbInformation, MB_OK);
      end
      else
      begin
        MsgBox(
          'Dee.extension already exists at:' + #13#10 + ExtDir + #13#10#13#10 +
          'It is not managed by pyRevit''s own extension registry (normal if ' +
          'it was set up manually), so this installer is leaving it untouched ' +
          'rather than risk disturbing your existing setup.',
          mbInformation, MB_OK);
      end;
    end
    else
    begin
      WizardForm.StatusLabel.Caption := 'Cloning Dee.extension from GitHub...';
      Params := '/c pyrevit extend ui ' + '{#ExtensionCliName}' + ' ' + '{#RepoURL}' +
        ' --dest="' + ExtensionsRoot + '"';
      if not Exec('cmd.exe', Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
         or (ResultCode <> 0) then
      begin
        MsgBox(
          'Installing Dee.extension failed (pyrevit extend returned an error). ' +
          'Make sure you have an internet connection and that pyRevit is fully ' +
          'installed, then try running this installer again.',
          mbError, MB_OK);
        Exit;
      end;
    end;

    if DirExists(ExtDir) then
    begin
      if (AccPage.Values[0] <> '') and FileExists(AccPage.Values[0]) then
      begin
        // explicit browse always wins, even over an existing file
        if not CopyFile(AccPage.Values[0], ExtDir + '\acc_config.json', False) then
          MsgBox(
            'Could not copy acc_config.json into the extension folder. You ' +
            'can copy it there by hand:' + #13#10 + ExtDir,
            mbError, MB_OK);
      end
      else if not FileExists(ExtDir + '\acc_config.json') then
      begin
        // nothing browsed and no existing config (fresh install, or an
        // update that never had one) - fall back to the bundled default.
        // Never overwrites a config that's already there, so re-running
        // this installer on a machine with its own custom credentials
        // leaves that file untouched.
        ExtractTemporaryFile('acc_config.json');
        if FileExists(ExpandConstant('{tmp}\acc_config.json')) then
          CopyFile(ExpandConstant('{tmp}\acc_config.json'),
            ExtDir + '\acc_config.json', False);
      end;
    end;

    MsgBox(
      'Dee.extension is installed.' + #13#10#13#10 +
      'Start (or restart) Revit - the Dee ribbon tab will appear ' +
      'automatically once pyRevit loads it.',
      mbInformation, MB_OK);
  end;
end;
