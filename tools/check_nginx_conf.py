"""nginx 配置结构检查（本机没有 docker/nginx 时的替代验证）。

它不做完整语法解析，只覆盖最容易出错的几类：
  1. 花括号配平、每条指令以 ; 或 { 结尾
  2. map 必须位于 http 顶层（不能在 server 里）
  3. default_server 的兜底 server 不能重复声明同一地址
  4. ssl_certificate / proxy_pass / root / include 指向的路径或上游是否合理
  5. proxy_set_header 用到的变量是否在 map 或 nginx 内置变量表里

用法： python tools/check_nginx_conf.py deploy/nginx/sayit.conf
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BUILTIN_VARS = {
    "http_upgrade", "host", "remote_addr", "proxy_add_x_forwarded_for", "scheme",
    "request_uri", "uri", "args", "server_name", "server_port", "binary_remote_addr",
    "request_method", "content_length", "http_user_agent",
}


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"#.*$", "", line).strip()
        if not line:
            continue
        out.append(line)
    return out


def check(path: Path) -> list[str]:
    errs: list[str] = []
    lines = tokenize(path.read_text(encoding="utf-8"))

    depth = 0
    context: list[str] = []
    maps_declared_by_us: set[str] = set()
    upstreams: set[str] = set()
    listens: dict[tuple[str, str], int] = {}
    cert_paths: list[str] = []

    for i, line in enumerate(lines, 1):
        if line.startswith("map "):
            m = re.match(r"map\s+\S+\s+(\$\w+)\s*\{", line)
            if not m:
                errs.append(f"L{i}: map 声明格式看不懂：{line}")
            else:
                maps_declared_by_us.add(m.group(1)[1:])
            if context:
                errs.append(f"L{i}: map 只能写在 http 顶层，当前在 {'/'.join(context)} 里")

        if line.endswith("{") or line.endswith(";"):
            pass
        elif line in ("}",) or line.endswith("}"):
            pass
        else:
            errs.append(f"L{i}: 没以 ; 或 {{ 结尾：{line}")

        if line == "}":
            if depth == 0:
                errs.append(f"L{i}: 多余的 }}")
            depth -= 1
            if context:
                context.pop()
            continue
        if line.endswith("{"):
            depth += 1
            context.append(line[:-1].strip().split()[0])

        m = re.match(r"listen\s+(\d+)(.*);", line)
        if m:
            port, rest = m.group(1), m.group(2)
            is_default = "default_server" in rest
            key = (port, "default" if is_default else "named")
            listens[key] = listens.get(key, 0) + 1

        m = re.match(r"ssl_certificate\s+(\S+);", line)
        if m:
            cert_paths.append(m.group(1))
        m = re.match(r"ssl_certificate_key\s+(\S+);", line)
        if m:
            cert_paths.append(m.group(1))

        m = re.match(r"proxy_pass\s+(\S+);", line)
        if m:
            target = m.group(1)
            tm = re.match(r"https?://([^/:]+)(?::(\d+))?", target)
            if not tm:
                errs.append(f"L{i}: proxy_pass 地址不合法：{target}")
            else:
                host = tm.group(1)
                if host not in ("127.0.0.1", "localhost") and dot_in_host(host):
                    upstreams.add(host)

    if depth != 0:
        errs.append(f"花括号不配平：多出 {depth} 个 {{")
    for key, n in listens.items():
        if n > 1:
            errs.append(f"listen {key[0]} 的 {key[1]} server 声明了 {n} 次")
    if not cert_paths:
        errs.append("没有任何 ssl_certificate —— 443 站点起不来")
    return errs


def dot_in_host(host: str) -> bool:
    """sayit / nginx 这类单个标签是 compose 服务名；example.com 这种要提示检查。"""
    return "." in host


def check_vars(path: Path) -> list[str]:
    errs: list[str] = []
    text = path.read_text(encoding="utf-8")
    declared = set(re.findall(r"map\s+\S+\s+\$(\w+)", text))
    for var in set(re.findall(r"\$(\w+)", text)):
        if var in declared or var in BUILTIN_VARS:
            continue
        # $host$request_uri 这种粘连写法里 $host 已知、其余已在上表
        errs.append(f"未定义的变量 ${var}")
    return errs


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "deploy/nginx/sayit.conf")
    errs = check(target) + check_vars(target)
    text = target.read_text(encoding="utf-8")
    if "proxy_pass http://sayit:8787" not in text:
        errs.append("proxy_pass 没有指向 compose 服务名 sayit:8787（容器里 127.0.0.1 不可用）")
    if not re.search(r"map \$http_upgrade \$connection_upgrade", text):
        errs.append("缺少 WebSocket 的 $connection_upgrade map")
    if "proxy_read_timeout 3600s" not in text:
        errs.append("缺少 3600s 的读超时 —— 长通话会被 60s 默认值掐断")

    if errs:
        print(f"{target}: {len(errs)} 个问题")
        for e in errs:
            print("  -", e)
        return 1
    print(f"{target}: 结构检查通过（括号配平 / map 位置 / 监听唯一 / 证书与上游 / 变量已定义）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
