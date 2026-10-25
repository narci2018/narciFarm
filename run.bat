@echo off
rem narciFarm 启动脚本（使用 py 启动器找到的默认 Python 3）
cd /d %~dp0
py -3 -m uvicorn narci.main:app --host 127.0.0.1 --port 8555
