@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

REM ============================================================
REM  手机免证书方案 A：USB 反向端口转发（Android）
REM
REM  原理：浏览器的"安全上下文"包括 localhost —— 手机自己访问
REM  http://localhost:8787 时，麦克风照常可用，跟 https 一样。
REM  所以不需要证书、不需要同一个 WiFi、也不用管内网 IP 变了。
REM
REM  前提：① Android 手机 ② 开发者选项里打开"USB 调试"
REM        ③ 电脑装了 platform-tools（有 adb 命令）
REM  用法：先 run.bat 起服务（纯 HTTP、只绑 127.0.0.1），再跑本脚本
REM ============================================================

where adb >nul 2>nul
if errorlevel 1 (
  echo [!] 没找到 adb。先装：
  echo     winget install --id Google.PlatformTools -e
  echo     装完关掉这个窗口重开。
  pause
  exit /b 1
)

echo [*] 已连接的设备：
adb devices

adb reverse --remove-all >nul 2>nul
adb reverse tcp:8787 tcp:8787
if errorlevel 1 (
  echo.
  echo [!] 端口转发失败。检查：
  echo     · 手机插好了、USB 调试开着
  echo     · 手机上弹的"允许 USB 调试"点了允许
  pause
  exit /b 1
)

echo.
echo ============================================================
echo   手机浏览器打开：  http://localhost:8787
echo   保持插着 USB；拔线后重插，再跑一次本脚本即可
echo   （这个地址是手机自己的 localhost，所以电脑上 8787 也要在跑）
echo ============================================================
pause
