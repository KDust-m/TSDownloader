@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/2] 필요한 패키지 설치 중...
py -m pip install --upgrade pyinstaller pycryptodome || python -m pip install --upgrade pyinstaller pycryptodome
echo [2/2] exe 빌드 중...
py -m PyInstaller --noconfirm --onefile --windowed --name TSDownloader ts_downloader_gui.py || python -m PyInstaller --noconfirm --onefile --windowed --name TSDownloader ts_downloader_gui.py
echo.
echo 완료: dist\TSDownloader.exe
echo MP4 저장을 쓰려면 ffmpeg.exe 를 TSDownloader.exe 와 같은 폴더에 두세요.
pause
