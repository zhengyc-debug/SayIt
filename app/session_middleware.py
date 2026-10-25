"""签名 cookie 的 SessionMiddleware，但签名密钥是「用时才读」。

为什么需要这个子类：
  Starlette 的 SessionMiddleware 在 __init__ 里就把 secret_key 转成 signer，
  而它又只能在 import 期装配（lifespan 里 add_middleware 会抛
  "Cannot add middleware after an application has started"）。
  我们的签名密钥存在 sayit.db 里 —— 建表之后才存在。

  所以：先用一个空密钥装配，第一次处理请求时再把真实的 signer 建出来。

坑：不仅普通 HTTP 请求会穿过中间件栈，**lifespan 事件本身也会**
（TestClient 和 Starlette 都这么走）。所以不能假设「走到中间件时 lifespan
已经跑完了」。取密钥前先 ensure_schema()（幂等），保证表一定在。
"""
from __future__ import annotations

import itsdangerous
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from .store.sqlite_conn import ensure_schema


class LazySessionMiddleware(SessionMiddleware):
    def __init__(self, app: ASGIApp, secret_loader, **kwargs) -> None:
        # 占位密钥：构造时不会被用到，只为满足父类签名
        super().__init__(app, secret_key="lazy-placeholder", **kwargs)
        self._loader = secret_loader
        self._signer: itsdangerous.TimestampSigner | None = None

    def _signer_or_init(self) -> itsdangerous.TimestampSigner:
        if self._signer is None:
            ensure_schema()          # 幂等；保证 app_meta 表在
            self._signer = itsdangerous.TimestampSigner(str(self._loader()))
        return self._signer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            # 父类读写的是 self.signer 这个属性，这里换成惰性版本
            self.signer = self._signer_or_init()
        await super().__call__(scope, receive, send)
