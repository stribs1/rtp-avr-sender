@echo off
cd /d "C:\_Portables\_AVR Sender"
pyinstaller --onefile --windowed vban_sender.py
copy dist\vban_sender.exe vban_sender.exe
pause
