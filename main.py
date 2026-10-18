"""SayIt 启动器（本机开发用）。

线上是 nginx 终结 TLS、uvicorn 只监听 127.0.0.1（见 deploy/），
自签证书那一套是为「手机直连本机局域网 IP」服务的，已经不需要，全部移除。
浏览器要求「非 localhost 页面必须 HTTPS 才能用麦克风」这条限制依然存在，
但现在由 nginx 的真证书满足，不需要本地证书。

用法：
    python main.py                # http://127.0.0.1:8787
    python main.py --port 9000
    python main.py --host 0.0.0.0 # 让同一局域网的其它机器访问，默认只绑回环
    python main.py --reload       # 改代码自动重启
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from app.main import app

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1",
                    help="默认只绑回环；线上由 nginx 反代，不需要直接暴露")
    ap.add_argument("--reload", action="store_true", help="改代码自动重启")
    args = ap.parse_args()

    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print()
    print("=" * 60)
    print(f"  SayIt  http://{shown}:{args.port}")
    print("=" * 60)
    if args.host in ("0.0.0.0", "::"):
        print("  已绑定 0.0.0.0（容器/局域网场景正常）。")
        print("  注意：手机要用麦克风必须走 HTTPS —— 在容器前面放 nginx 终结 TLS。")
    print()

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
