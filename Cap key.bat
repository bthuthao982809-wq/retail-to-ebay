@echo off
setlocal
title Cap key ban quyen - RetailToEbay
cd /d "%~dp0"

rem Uu tien python tren PATH, khong co thi dung ban da cai san
set "PY=python"
where python >nul 2>nul || set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"

if not exist "keys\private.key" (
    echo.
    echo  [!] Chua co khoa bi mat ^(keys\private.key^).
    echo      Mo terminal chay: python keygen.py --init
    echo.
    pause
    exit /b 1
)

rem Dung nhanh:  Cap key.bat ABCD-EF12-3456-7890 [so_ngay] [ten]
set "QUICK="
if not "%~1"=="" set "QUICK=1"

:again
if defined QUICK (
    set "MID=%~1"
    set "NGAY=%~2"
    set "TEN=%~3"
) else (
    cls
    echo ============================================================
    echo   CAP KEY BAN QUYEN - RetailToEbay
    echo ============================================================
    echo.
    echo  Dan MA MAY nguoi dung gui cho ban ^(dang ABCD-EF12-3456-7890^)
    echo.
    set "MID="
    set /p "MID=  Ma may : "
    echo.
    set "TEN="
    set /p "TEN=  Cap cho ai (de trong cung duoc) : "
    echo.
    echo  So ngay hieu luc. Bo trong = VINH VIEN.
    set "NGAY="
    set /p "NGAY=  So ngay : "
)
if "%MID%"=="" goto :thoat

set "THEM="
if not "%NGAY%"=="" set "THEM=--days %NGAY%"

echo.
echo ------------------------------------------------------------
rem Ghi ra file tam roi doc lai (khong dung 'for /f' vi dau nhay long nhau)
set "TMPK=%TEMP%\retail_key_%RANDOM%.txt"
"%PY%" keygen.py "%MID%" --ten "%TEN%" %THEM% --chi-key > "%TMPK%" 2>&1
set "KEY="
set /p "KEY=" <"%TMPK%"
del "%TMPK%" >nul 2>nul

if "%KEY%"=="" (
    echo  [!] Khong tao duoc key. Kiem tra lai ma may.
    goto :hoilai
)
echo %KEY%| findstr /b /c:"LOI:" >nul
if not errorlevel 1 (
    echo  [!] %KEY%
    goto :hoilai
)

echo  KEY ^(da chep vao clipboard, chi can dan cho nguoi dung^):
echo.
echo  %KEY%
echo.
echo ------------------------------------------------------------
<nul set /p "=%KEY%" | clip
echo  Da chep vao clipboard. Da ghi so vao keys\da_cap.csv

:hoilai
if defined QUICK goto :xong
echo.
set "TIEP="
set /p "TIEP=  Cap key cho may khac nua? (c/K) : "
if /i "%TIEP%"=="c" goto :again

:thoat
if defined QUICK goto :xong
echo.
echo  Xong. Nhan phim bat ky de dong.
pause >nul
:xong
endlocal
