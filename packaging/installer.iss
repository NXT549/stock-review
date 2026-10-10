; installer.iss - build "Stock Review Setup.exe" with Inno Setup 6.
;
; Compile from the project root via the build helper (recommended):
;     powershell -ExecutionPolicy Bypass -File packaging/build_exe.ps1
; ...or directly, after a PyInstaller build has populated dist/Stock Review/:
;     "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
;
; Output: packaging/installer_output/Stock Review Setup.exe
;
; Per-user install ({autopf} + PrivilegesRequired=lowest) => lands in
; %LocalAppData%\Programs\Stock Review with NO admin/UAC prompt.

[Setup]
; A fixed AppId keeps upgrades/uninstall tied to the same entry across versions.
AppId={{A7D3F1E2-9C4B-4A6E-8F21-3B5C7D9E0A11}
AppName=Stock Review
AppVersion=2.3
AppPublisher=NXT
DefaultDirName={autopf}\Stock Review
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=Stock Review Setup
SetupIconFile=stock_review.ico
UninstallDisplayIcon={app}\Stock Review.exe
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes

[Messages]
; A friendlier finish page: this is a hobby project, so it says thanks like one.
FinishedHeadingLabel=Yay - you made it!
FinishedLabelNoIcons=Stock Review is installed and ready to fetch some numbers.%n%nThanks for installing this silly lil vibe-coded project. It was made for fun, and now it's yours to have fun with too.
FinishedLabel=Stock Review is installed and ready to fetch some numbers.%n%nThanks for installing this silly lil vibe-coded project. It was made for fun, and now it's yours to have fun with too.
ClickFinish=Click Finish and go review some stocks!

[Files]
Source: "..\dist\Stock Review\*"; DestDir: "{app}"; \
    Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autodesktop}\Stock Review";        Filename: "{app}\Stock Review.exe";          Tasks: desktopicon
Name: "{group}\Stock Review";              Filename: "{app}\Stock Review.exe"
Name: "{group}\Stock Review (Terminal)";   Filename: "{app}\Stock Review Terminal.exe"
Name: "{group}\Uninstall Stock Review";    Filename: "{uninstallexe}"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
Filename: "{app}\Stock Review.exe"; Description: "Launch Stock Review now"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The app writes into its own folder when Documents isn't writable (cache,
; selftest output). Sweep the whole install dir so nothing is left behind.
Type: filesandordirs; Name: "{app}"

[Code]
{ The app stores reports/cache/settings in "<Documents>\Stock Review". Documents
  can be redirected to OneDrive (see gui._documents_candidates), so mirror that
  candidate list here and remove every one that exists. }
procedure DeleteAppData();
var
  Roots: array of string;
  P: string;
  I: Integer;
begin
  SetArrayLength(Roots, 3);
  Roots[0] := ExpandConstant('{userdocs}') + '\Stock Review';
  P := GetEnv('OneDrive');
  if P <> '' then
    Roots[1] := P + '\Documents\Stock Review';
  P := GetEnv('USERPROFILE');
  if P <> '' then
    Roots[2] := P + '\Documents\Stock Review';
  for I := 0 to GetArrayLength(Roots) - 1 do
    if (Roots[I] <> '') and DirExists(Roots[I]) then
      DelTree(Roots[I], True, True, True);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    { Interactive uninstall asks before touching the user's reports; a silent
      uninstall (/SILENT) removes everything, no questions asked. }
    if UninstallSilent() or
       (MsgBox('Also delete your saved reports, settings and cache?'#13#10#13#10
               + ExpandConstant('{userdocs}') + '\Stock Review',
               mbConfirmation, MB_YESNO) = IDYES) then
      DeleteAppData();
  end;
end;
