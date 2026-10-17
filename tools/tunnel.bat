@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

REM ============================================================
REM  手机免证书方案 B：临时公网隧道（iPhone / Android 通用）
REM
REM  cloudflared 会给你一个 https://xxx.trycloudflare.com —— 真证书，
REM  手机直接信任，什么都不用装，4G 也能用。
REM  代价：语音要绕到 Cloudflare 的边缘节点，延迟会明显变高，
REM        实时对话体验不如局域网直连。适合临时试、不适合天天练。
REM
REM  用法：先 run.bat 起服务（纯 HTTP），再跑本脚本
REM ============================================================

where cloudflared >nul 2>nul
if errorlevel 1 (
  echo [!] 没找到 cloudflared。先装：
  echo     winget install --id Cloudflare.cloudflared -e
  echo     （国内网络下载可能很慢，可以先手动下 exe 放到 PATH 里）
  pause
  exit /b 1
)

echo [*] 确认另一个窗口里 run.bat 已经起来了（http://127.0.0.1:8787）
echo [*] 正在建立隧道，几秒后会打印一个 https://xxxx.trycloudflare.com 地址
echo     在手机浏览器打开那个地址即可，不需要装任何证书
echo     用完 Ctrl+C 关掉
echo.
cloudflared tunnel --url http://127.0.0.1:8787
pause
