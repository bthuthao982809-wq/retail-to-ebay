@echo off
title Retail to eBay CSV
cd /d "%~dp0"
python -c "import playwright, curl_cffi, PIL, boto3, openpyxl" 2>nul || pip install -r requirements.txt
python app.py
if errorlevel 1 pause
