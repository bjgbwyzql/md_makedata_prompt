@echo off
rem 分发 AI 标注通用模板：名字读 config.yaml 的 name（或用 --name 覆盖），为空时会提示输入
cd /d "%~dp0"
python init.py %*
pause
