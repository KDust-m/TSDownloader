@echo off
cd /d "%~dp0"
start "" pyw ts_downloader_gui.py || start "" pythonw ts_downloader_gui.py
