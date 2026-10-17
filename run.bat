@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM 为什么要清 PYTHONPATH：这台机器上 Hermes Agent 会把 PYTHONPATH 指向它自己的内置
REM 环境，导致任何 python 都先 import 到 Hermes 的包（还是 3.14 编的，和本机 3.12 不兼容）。
REM 清空后 python 只会看到项目自己的 .venv。
set PYTHONPATH=
set PYTHONHOME=

if not exist ".venv\Scripts\python.exe" (
  echo [*] 首次运行：创建项目虚拟环境 .venv ...
  py -3.12 -m venv .venv 2>nul
  if not exist ".venv\Scripts\python.exe" python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo [!] 建 venv 失败，请先安装 Python 3.12
    pause
    exit /b 1
  )
  echo [*] 安装依赖，约 1-2 分钟 ...
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
)

if not exist ".env" (
  echo [!] 找不到 .env，先复制 .env.example 为 .env 并填 DOUBAO_API_KEY
  pause
  exit /b 1
)

echo ============================================
echo  SayIt  http://127.0.0.1:8787   （仅本机，开发用）
echo  线上部署见 DEPLOY.md（nginx + systemd）
echo ============================================
".venv\Scripts\python.exe" tools\serve.py
if errorlevel 1 echo [i] 服务已退出 (exit %errorlevel%)
pause
