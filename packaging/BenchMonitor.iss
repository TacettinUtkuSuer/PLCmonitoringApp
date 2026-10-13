; ==========================================================================================
;  Bench Monitor - Inno Setup kurulum betigi  ->  Output\kur.exe
;  Derleme: derle.bat (once PyInstaller ciktisini ve nssm.exe'yi hazirlar)
;
;  Program : C:\Program Files\BenchMonitor      (normal kullanici silemez/degistiremez)
;  Veri    : C:\ProgramData\BenchMonitor        (config.json, presets.json, logs\) - kaldirmada KORUNUR
;  Servis  : "BenchMonitor" (NSSM), LocalSystem, otomatik baslar, coker ise yeniden baslar
;  Ag      : Guvenlik duvarinda gelen TCP 8989 izni
; ==========================================================================================

#define AppName      "Bench Monitor"
#define SvcName      "BenchMonitor"
#define AppPort      "8989"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6E0B7C1A-4F2D-4C8E-9B1A-BE7C4D0A8989}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Bench Monitor
DefaultDirName={autopf}\BenchMonitor
DisableDirPage=yes
DisableProgramGroupPage=yes
DefaultGroupName={#AppName}
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\Output
OutputBaseFilename=kur
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\BenchMonitor.exe
CloseApplications=no

[Languages]
Name: "tr"; MessagesFile: "compiler:Languages\Turkish.isl"

[Dirs]
; Veri klasoru: SYSTEM + Yoneticiler tam yetki, Kullanicilar sadece okur.
Name: "{commonappdata}\BenchMonitor"; Flags: uninsneveruninstall
Name: "{commonappdata}\BenchMonitor\logs"; Flags: uninsneveruninstall

[Files]
Source: "..\dist\BenchMonitor\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "tools\nssm.exe"; DestDir: "{app}"; Flags: ignoreversion

[INI]
; Masaustu + Baslat menusu: tarayicida panel
Filename: "{commondesktop}\{#AppName}.url"; Section: "InternetShortcut"; Key: "URL"; String: "http://localhost:{#AppPort}"
Filename: "{group}\{#AppName}.url"; Section: "InternetShortcut"; Key: "URL"; String: "http://localhost:{#AppPort}"

[Icons]
Name: "{group}\Veri ve log klasoru"; Filename: "{commonappdata}\BenchMonitor"

[Run]
; --- veri klasoru izinleri (devralmayi kes; SYSTEM/Yoneticiler F, Kullanicilar RX)
Filename: "{sys}\icacls.exe"; Parameters: """{commonappdata}\BenchMonitor"" /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F *S-1-5-32-545:(OI)(CI)RX"; Flags: runhidden waituntilterminated; StatusMsg: "Klasor izinleri ayarlaniyor..."
; --- servis (guncellemede once eskisini kaldir, sonra temiz kur)
Filename: "{app}\nssm.exe"; Parameters: "remove {#SvcName} confirm"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "install {#SvcName} ""{app}\BenchMonitor.exe"""; Flags: runhidden waituntilterminated; StatusMsg: "Servis kuruluyor..."
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} DisplayName ""{#AppName}"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} Description ""Test tezgahi canli izleme paneli (http://localhost:{#AppPort})"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} AppDirectory ""{app}"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} AppEnvironmentExtra BENCHMONITOR_DATA={commonappdata}\BenchMonitor"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} Start SERVICE_AUTO_START"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} AppNoConsole 1"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} AppExit Default Restart"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} AppRestartDelay 5000"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} AppStdout ""{commonappdata}\BenchMonitor\logs\servis.log"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} AppStderr ""{commonappdata}\BenchMonitor\logs\servis.log"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} AppRotateFiles 1"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} AppRotateBytes 5242880"; Flags: runhidden waituntilterminated
; nssm.exe'nin kendisi coker ise Windows SCM yeniden baslatsin
Filename: "{sys}\sc.exe"; Parameters: "failure {#SvcName} reset= 86400 actions= restart/5000/restart/5000/restart/30000"; Flags: runhidden waituntilterminated
; --- guvenlik duvari
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#AppName} (TCP {#AppPort})"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#AppName} (TCP {#AppPort})"" dir=in action=allow protocol=TCP localport={#AppPort} profile=any"; Flags: runhidden waituntilterminated; StatusMsg: "Guvenlik duvari ayarlaniyor..."
; --- baslat
Filename: "{app}\nssm.exe"; Parameters: "start {#SvcName}"; Flags: runhidden waituntilterminated; StatusMsg: "Servis baslatiliyor..."
Filename: "http://localhost:{#AppPort}"; Description: "Paneli tarayicida ac"; Flags: postinstall shellexec nowait skipifsilent

[UninstallRun]
Filename: "{app}\nssm.exe"; Parameters: "stop {#SvcName}"; Flags: runhidden waituntilterminated; RunOnceId: "SvcStop"
Filename: "{app}\nssm.exe"; Parameters: "remove {#SvcName} confirm"; Flags: runhidden waituntilterminated; RunOnceId: "SvcRemove"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#AppName} (TCP {#AppPort})"""; Flags: runhidden waituntilterminated; RunOnceId: "FwDelete"

[UninstallDelete]
Type: files; Name: "{commondesktop}\{#AppName}.url"
Type: files; Name: "{group}\{#AppName}.url"

[Code]
// Guncelleme: dosyalar kopyalanmadan once calisan servisi durdur (exe kilitli kalmasin).
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Rc: Integer;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\nssm.exe')) then
    Exec(ExpandConstant('{app}\nssm.exe'), 'stop {#SvcName}', '', SW_HIDE, ewWaitUntilTerminated, Rc)
  else
    Exec(ExpandConstant('{sys}\net.exe'), 'stop {#SvcName}', '', SW_HIDE, ewWaitUntilTerminated, Rc);
end;
