"""前端页面 + 静态资源。

静态资源用「文件名白名单」而不是挂载整个 web/：
/static/ 在登录门禁的公开前缀里，整目录挂上去会让本应登录后才能看的文件绕过门禁。
settings.js 这类只有登录后才用的脚本走独立的受保护路由。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from .. import auth
from ..paths import WEB_DIR

router = APIRouter(tags=["pages"])

# 登录页本身和公共页面真正需要的文件
PUBLIC_STATIC = {
    "styles.css",        # 所有页面（含登录页）的样式
    "i18n.js",           # 登录页和全站都用
    "nav.js",            # 底部标签栏
    "app.js",            # 通话页逻辑
    "audio-worklet.js",  # AudioWorklet，必须固定路径
    "icon.svg",          # favicon / PWA 图标
}


def _page(name: str) -> FileResponse:
    return FileResponse(WEB_DIR / name)


@router.get("/")
async def index() -> FileResponse:
    return _page("index.html")


@router.get("/login")
async def login_page(request: Request):
    """已登录的人再访问登录页就直接送回去，省得误点。"""
    if auth.current_user(request):
        return RedirectResponse(url="/", status_code=303)
    return _page("login.html")


@router.get("/settings")
async def settings_page() -> FileResponse:
    return _page("settings.html")


@router.get("/history")
async def history_page() -> FileResponse:
    return _page("history.html")


@router.get("/stats")
async def stats_page() -> FileResponse:
    return _page("stats.html")


@router.get("/me")
async def me_page() -> FileResponse:
    return _page("me.html")


@router.get("/settings.js")
async def settings_script() -> FileResponse:
    """设置页脚本。

    刻意不做成 /static/settings.js：/static/ 在登录门禁的公开前缀里，
    放进去等于匿名可读。这里走受 require_login 保护的路由，
    和页面本身的门禁保持一致。
    """
    return FileResponse(WEB_DIR / "settings.js", media_type="application/javascript")


@router.get("/sw.js")
async def service_worker() -> FileResponse:
    return FileResponse(WEB_DIR / "sw.js", media_type="application/javascript")


@router.get("/manifest.webmanifest")
async def manifest() -> FileResponse:
    return FileResponse(WEB_DIR / "manifest.webmanifest",
                        media_type="application/manifest+json")


@router.get("/static/{filename:path}", include_in_schema=False)
async def static_file(filename: str) -> FileResponse:
    # 只认平铺文件名，带斜杠或 .. 一律 404（防穿越）
    if filename not in PUBLIC_STATIC:
        raise HTTPException(404)
    target = (WEB_DIR / filename).resolve()
    if not target.is_file():
        raise HTTPException(404)
    return FileResponse(target)
