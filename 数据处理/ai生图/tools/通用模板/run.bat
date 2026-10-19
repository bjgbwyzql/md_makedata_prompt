@echo off
rem 分发通用模板给新人：名字读 config.yaml 的 name（或用 --name 覆盖）
cd /d "%~dp0"
python init.py %*
pause
