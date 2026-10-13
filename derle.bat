@echo off
rem ======================================================================================
rem  Bench Monitor - Windows derleme:  main.py -> dist\BenchMonitor\  -> Output\kur.exe
rem  Gerekenler: Python 3.11 (PATH'te), Inno Setup 6, internet (ilk seferde nssm indirmek icin)
rem  Kullanim  : derle.bat [surum]      ornek: derle.bat 1.2.0
rem ======================================================================================
setlocal
cd /d "%~dp0"

set "VERSION=%~1"
if "%VERSION%"=="" set "VERSION=1.0.0"
set "NSSM_URL=https://nssm.cc/ci/nssm-2.24-101-g897c7ad.zip"
set "NSSM_URL2=https://nssm.cc/release/nssm-2.24.zip"

echo.
echo [1/4] Python bagimliliklari...
python -m pip install --upgrade pip || goto :hata
python -m pip install -r requirements.txt pyinstaller==6.11.1 || goto :hata

echo.
echo [2/4] PyInstaller (onedir, konsolsuz)...
python -m PyInstaller --noconfirm --clean --onedir --noconsole --name BenchMonitor ^
    --collect-submodules uvicorn ^
    --add-data "vendor;vendor" ^
    main.py || goto :hata

echo.
echo [3/4] NSSM (servis sarmalayici)...
if exist "packaging\tools\nssm.exe" goto :nssm_tamam
if not exist "packaging\tools" mkdir "packaging\tools"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop'; $z=Join-Path $env:TEMP 'nssm.zip'; $d=Join-Path $env:TEMP 'nssm_x'; $ok=$false;" ^
  "foreach ($u in @('%NSSM_URL%','%NSSM_URL2%')) { for ($i=1; $i -le 3 -and -not $ok; $i++) {" ^
  "  try { Invoke-WebRequest -Uri $u -OutFile $z -UseBasicParsing; $ok=$true } catch { Write-Host ('    ' + $u + ' deneme ' + $i + ': ' + $_); Start-Sleep 5 } } };" ^
  "if (-not $ok) { throw 'nssm indirilemedi; nssm.exe (win64) dosyasini elle packaging\tools\ altina koyun' };" ^
  "if (Test-Path $d) { Remove-Item $d -Recurse -Force }; Expand-Archive $z $d;" ^
  "Copy-Item (Get-ChildItem $d -Recurse -Filter nssm.exe | Where-Object { $_.FullName -match 'win64' } | Select-Object -First 1).FullName 'packaging\tools\nssm.exe'" || goto :hata
:nssm_tamam
echo     packaging\tools\nssm.exe hazir.

echo.
echo [4/4] Inno Setup...
set "ISCC="
for %%I in (ISCC.exe) do set "ISCC=%%~$PATH:I"
if "%ISCC%"=="" if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if "%ISCC%"=="" if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if "%ISCC%"=="" if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if "%ISCC%"=="" (
    echo HATA: ISCC.exe bulunamadi. Inno Setup 6 kurun: https://jrsoftware.org/isdl.php
    goto :hata
)
"%ISCC%" /DAppVersion=%VERSION% "packaging\BenchMonitor.iss" || goto :hata

echo.
echo ==== TAMAM: Output\kur.exe  (surum %VERSION%) ====
endlocal
exit /b 0

:hata
echo.
echo ==== DERLEME BASARISIZ ====
endlocal
exit /b 1
