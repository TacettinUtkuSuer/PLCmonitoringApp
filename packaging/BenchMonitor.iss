; ==========================================================================================
;  Bench Monitor - Inno Setup kurulum betigi  ->  Output\kur.exe
;  Derleme: derle.bat (once PyInstaller ciktisini ve nssm.exe'yi hazirlar)
;
;  Program : C:\Program Files\BenchMonitor      (normal kullanici silemez/degistiremez)
;  Veri    : C:\ProgramData\BenchMonitor        (config.json, presets.json, logs\) - kaldirmada KORUNUR
;  Servis  : "BenchMonitor" (NSSM), LocalSystem, otomatik baslar, coker ise yeniden baslar
;  Port    : kurulumda bir kez sorulur (varsayilan 8989, guncellemede onceki deger);
;            servise BENCHMONITOR_PORT olarak verilir. Sessiz kurulum: kur.exe /SILENT /PORT=9000
;  Ag      : Guvenlik duvarinda gelen TCP <port> izni
; ==========================================================================================

#define AppName      "Bench Monitor"
#define SvcName      "BenchMonitor"
#define DefaultPort  "8989"
#define FwRule       "Bench Monitor"
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

[Messages]
; Standart "...ve tum bilesenlerini kaldirmak istiyor musunuz?" metni veri de silinecek
; gibi anlasiliyordu. Veri klasoru ([Dirs] uninsneveruninstall) HICBIR ZAMAN silinmez.
ConfirmUninstall=%1 programi kaldirilacak.%n%nAyarlar, presetler ve malzeme tablolari (C:\ProgramData\BenchMonitor) SILINMEZ, oldugu gibi kalir; yeniden kurulunca aynen kullanilir.%n%nDevam edilsin mi?

[Dirs]
; Veri klasoru: SYSTEM + Yoneticiler tam yetki, Kullanicilar sadece okur.
Name: "{commonappdata}\BenchMonitor"; Flags: uninsneveruninstall
Name: "{commonappdata}\BenchMonitor\logs"; Flags: uninsneveruninstall

[Files]
Source: "..\dist\BenchMonitor\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "tools\nssm.exe"; DestDir: "{app}"; Flags: ignoreversion

[INI]
; Masaustu + Baslat menusu: tarayicida panel
Filename: "{commondesktop}\{#AppName}.url"; Section: "InternetShortcut"; Key: "URL"; String: "http://localhost:{code:GetPort}"
Filename: "{group}\{#AppName}.url"; Section: "InternetShortcut"; Key: "URL"; String: "http://localhost:{code:GetPort}"

[Icons]
Name: "{group}\Veri ve log klasoru"; Filename: "{commonappdata}\BenchMonitor"

[Run]
; --- veri klasoru izinleri (devralmayi kes; SYSTEM/Yoneticiler F, Kullanicilar RX)
Filename: "{sys}\icacls.exe"; Parameters: """{commonappdata}\BenchMonitor"" /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F *S-1-5-32-545:(OI)(CI)RX"; Flags: runhidden waituntilterminated; StatusMsg: "Klasor izinleri ayarlaniyor..."
; --- servis (guncellemede once eskisini kaldir, sonra temiz kur)
Filename: "{app}\nssm.exe"; Parameters: "remove {#SvcName} confirm"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "install {#SvcName} ""{app}\BenchMonitor.exe"""; Flags: runhidden waituntilterminated; StatusMsg: "Servis kuruluyor..."
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} DisplayName ""{#AppName}"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} Description ""Test tezgahi canli izleme paneli (http://localhost:{code:GetPort})"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} AppDirectory ""{app}"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} AppEnvironmentExtra BENCHMONITOR_DATA={commonappdata}\BenchMonitor BENCHMONITOR_PORT={code:GetPort}"; Flags: runhidden waituntilterminated
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
; (port degistiyse eski kural da gitsin: ayni adli tum kurallar silinir)
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FwRule}"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#FwRule}"" dir=in action=allow protocol=TCP localport={code:GetPort} profile=any"; Flags: runhidden waituntilterminated; StatusMsg: "Guvenlik duvari ayarlaniyor..."
; --- baslat
Filename: "{app}\nssm.exe"; Parameters: "start {#SvcName}"; Flags: runhidden waituntilterminated; StatusMsg: "Servis baslatiliyor..."
Filename: "http://localhost:{code:GetPort}"; Description: "Paneli tarayicida ac"; Flags: postinstall shellexec nowait skipifsilent

[UninstallRun]
Filename: "{app}\nssm.exe"; Parameters: "stop {#SvcName}"; Flags: runhidden waituntilterminated; RunOnceId: "SvcStop"
Filename: "{app}\nssm.exe"; Parameters: "remove {#SvcName} confirm"; Flags: runhidden waituntilterminated; RunOnceId: "SvcRemove"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FwRule}"""; Flags: runhidden waituntilterminated; RunOnceId: "FwDelete"

[UninstallDelete]
Type: files; Name: "{commondesktop}\{#AppName}.url"
Type: files; Name: "{group}\{#AppName}.url"

[Code]
var
  PortPage: TInputQueryWizardPage;

// Gecerli port (1-65535) ya da -1
function ParsePort(const S: String): Integer;
begin
  Result := StrToIntDef(Trim(S), -1);
  if (Result < 1) or (Result > 65535) then
    Result := -1;
end;

// {code:GetPort}: sihirbazdaki deger; sessiz kurulumda /PORT=, onceki kurulum ya da varsayilan
function GetPort(Param: String): String;
var
  P: Integer;
begin
  P := ParsePort(PortPage.Values[0]);
  if P < 0 then
    P := {#DefaultPort};
  Result := IntToStr(P);
end;

procedure InitializeWizard;
begin
  PortPage := CreateInputQueryPage(wpWelcome,
    'Port', 'Panel hangi portta calissin?',
    'Panel tarayicida http://localhost:<port> adresinden acilir; agdaki diger PC''ler ' +
    'http://<bu-pc-ip>:<port> ile baglanir. Emin degilseniz 8989 olarak birakin.');
  PortPage.Add('Port (1-65535):', False);
  PortPage.Values[0] := ExpandConstant('{param:PORT|' + GetPreviousData('Port', '{#DefaultPort}') + '}');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = PortPage.ID then
  begin
    if ParsePort(PortPage.Values[0]) < 0 then
    begin
      MsgBox('Gecersiz port. 1 ile 65535 arasinda bir sayi girin (varsayilan 8989).', mbError, MB_OK);
      Result := False;
    end else
      PortPage.Values[0] := IntToStr(ParsePort(PortPage.Values[0]));
  end;
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'Port', GetPort(''));
end;

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
