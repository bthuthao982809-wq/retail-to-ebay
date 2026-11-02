@echo off
rem Dong goi tool: dist\RetailToEbay.exe + dist\_internal (ban dung tren may nay, du lieu cung nam trong dist\)
rem + goi chia se RetailToEbay-chiase.zip
rem Kieu "thu muc" (onedir): mo app nhanh, khong phai giai nen ~80 MB moi lan mo nhu kieu 1 file
cd /d "%~dp0"
python -c "import webview, PyInstaller" 2>nul || pip install pywebview pyinstaller
if not exist license_pub.py (
    echo [!] Chua co license_pub.py - chay "python keygen.py --init" truoc.
    pause
    exit /b 1
)
python make_default_config.py
if not exist app.ico python make_icon.py
rem Module site nap dong (importlib) nen PyInstaller khong tu thay -> khai bao tung site trong sites.ORDER
for /f "delims=" %%a in ('python check_exe.py --hidden-imports') do set SITE_IMPORTS=%%a
rem Build ra build\dist truoc (app dang mo o thu muc tool khong lam hong lan build)
pyinstaller --noconfirm --onedir --windowed --name RetailToEbay --icon "%~dp0app.ico" ^
  --distpath build\dist --workpath build --specpath build --paths "%~dp0." ^
  --add-data "%~dp0web;web" --add-data "%~dp0build\config.default.json;." ^
  --collect-data playwright --collect-all curl_cffi --collect-data camoufox --copy-metadata camoufox --collect-all fpgen --collect-data language_tags --collect-data ua_parser_builtins --collect-data botocore --collect-submodules openpyxl ^
  --hidden-import sites.base %SITE_IMPORTS% --hidden-import license_pub ^
  desktop.py || (echo [!] Build loi & pause & exit /b 1)
set OUT=build\dist\RetailToEbay
python check_exe.py %OUT% || (pause & exit /b 1)

rem Goi gui cho may khac: exe + _internal + file mau + huong dan, KHONG co keys\ hay config cua ban
if not exist chia-se mkdir chia-se
copy /y %OUT%\RetailToEbay.exe "chia-se\RetailToEbay.exe" >nul || (echo [!] Dong RetailToEbay.exe trong chia-se roi chay lai & pause & exit /b 1)
robocopy %OUT%\_internal chia-se\_internal /MIR /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (echo [!] Khong chep duoc _internal vao chia-se & pause & exit /b 1)
copy /y "HUONG DAN.txt" "chia-se\HUONG DAN.txt" >nul
python -c "import core, pathlib; core.make_template(None, pathlib.Path('chia-se') / 'file mau' / core.TEMPLATE_NAME)"
python -c "import shutil; shutil.make_archive('RetailToEbay-chiase', 'zip', 'chia-se')" || (pause & exit /b 1)
echo.

rem Ban dung o may nay: dist\ (dung chung config / phien / output voi ban chay bang python app.py)
if not exist dist mkdir dist
copy /y %OUT%\RetailToEbay.exe dist\RetailToEbay.exe >nul 2>nul || goto busy
robocopy %OUT%\_internal dist\_internal /MIR /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto busy
copy /y "HUONG DAN.txt" "dist\HUONG DAN.txt" >nul
echo Xong: dist\RetailToEbay.exe va goi chia se RetailToEbay-chiase.zip
pause
exit /b 0
:busy
echo Xong goi chia se RetailToEbay-chiase.zip. [!] dist\RetailToEbay.exe dang mo nen chua thay ban moi - tat app roi chay lai file nay
pause
