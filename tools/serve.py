"""SayIt 启动器（支持局域网 HTTPS）。

为什么需要它：
  浏览器规定——页面不在 localhost 上时，必须是 HTTPS 才允许使用麦克风。
  手机访问 http://192.168.x.x:8787 会被直接拒绝录音。所以要自签证书 + 绑 0.0.0.0。

它会自动做四件事：
  1. 找出本机所有 IPv4（含虚拟网卡），生成带 SAN 的自签证书（缺才生成）
  2. 绑 0.0.0.0 启动，让局域网能连
  3. 尝试加一条防火墙放行规则（需要管理员，失败会提示）
  4. 打印手机上该访问的地址

用法：
    python tools/serve.py                # HTTPS + 局域网（手机用这个）
    python tools/serve.py --http         # 纯 HTTP，仅本机
    python tools/serve.py --port 9000
    python tools/serve.py --regen-cert   # 强制重签证书（换了网段/IP 变了就重签）
"""
from __future__ import annotations

import argparse
import datetime
import ipaddress
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CERT_DIR = ROOT / "certs"
CERT = CERT_DIR / "cert.pem"
KEY = CERT_DIR / "key.pem"


def local_ips() -> list[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    # 出口 IP（最可能的那个）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    # 过滤掉 169.254 链路本地地址
    return sorted(i for i in ips if not i.startswith("169.254."))


def make_cert(ips: list[str]) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    CERT_DIR.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "SayIt Local"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SayIt"),
    ])
    san: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.DNSName(socket.gethostname() + ".local"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    for ip in ips:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    # 同一张证书导出 DER 版，供手机「安装 CA 证书」用（这样就不会再有警告）
    (CERT_DIR / "sayit-ca.cer").write_bytes(
        cert.public_bytes(serialization.Encoding.DER))
    KEY.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    print(f"[cert] 已生成自签证书 → {CERT}")
    print(f"[cert] 包含: localhost, 127.0.0.1, {', '.join(ips)}")


def cert_covers(ips: list[str]) -> bool:
    """证书里的 SAN 是否覆盖当前所有 IP？IP 变了要重签，否则手机会报证书错误。"""
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(CERT.read_bytes())
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            have = {str(i) for i in san.get_values_for_type(x509.IPAddress)}
        except Exception:
            have = set()
        return set(ips).issubset(have) and cert.not_valid_after_utc.replace(tzinfo=None) > \
            datetime.datetime.utcnow()
    except Exception:
        return False


def ensure_firewall(port: int) -> None:
    """放行入站端口。需要管理员；失败只提示，不中断。"""
    rule = f"SayIt {port}"
    try:
        q = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule}"],
                           capture_output=True, text=True, encoding="gbk", errors="replace")
        if q.returncode == 0 and "SayIt" in (q.stdout or ""):
            print(f"[fw] 防火墙规则已存在：{rule}")
            return
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "add", "rule", f"name={rule}",
             "dir=in", "action=allow", "protocol=TCP", f"localport={port}", "profile=any"],
            capture_output=True, text=True, encoding="gbk", errors="replace")
        if r.returncode == 0:
            print(f"[fw] 已放行入站 TCP {port}")
        else:
            print(f"[fw] 加规则失败（可能要管理员）：{(r.stdout or r.stderr).strip()[:200]}")
            print(f"     手动执行：netsh advfirewall firewall add rule name=\"{rule}\" "
                  f"dir=in action=allow protocol=TCP localport={port}")
    except FileNotFoundError:
        print("[fw] 找不到 netsh，跳过防火墙配置")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true", help="纯 HTTP（仅本机可用麦克风）")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default=None)
    ap.add_argument("--regen-cert", action="store_true")
    ap.add_argument("--reload", action="store_true", help="改代码自动重启（仅 --http 模式）")
    ap.add_argument("--no-firewall", action="store_true")
    ap.add_argument("--cert", default=None,
                    help="用自己签发的证书（真实域名用，手机不需要装任何证书）")
    ap.add_argument("--key", default=None, help="配合 --cert 的私钥文件")
    ap.add_argument("--domain", default=None,
                    help="域名，如 sayit.example.com（解析到你内网 IP），只用于打印地址")
    args = ap.parse_args()

    ips = local_ips()
    use_https = not args.http

    # 用自己的证书（真域名）时，不再生成/续签自签证书，也不提示装 CA
    cert_file, key_file = CERT, KEY
    custom_cert = bool(args.cert)
    if custom_cert:
        cert_file = Path(args.cert).expanduser()
        key_file = Path(args.key).expanduser() if args.key else cert_file
        if not cert_file.exists() or not key_file.exists():
            print(f"[cert] 找不到证书：{cert_file} / {key_file}")
            return 2

    if use_https and custom_cert:
        # 自有证书：不生成、不续签，也不导出 CA（手机本来就信任它）
        if not args.no_firewall:
            ensure_firewall(args.port)
        host, scheme = (args.host or "0.0.0.0"), "https"
    elif use_https:
        if args.regen_cert or not CERT.exists() or not cert_covers(ips):
            if CERT.exists():
                print("[cert] 现有证书不覆盖当前 IP（换网段了？），重新签发")
            make_cert(ips)
        # 证书已存在也要保证 .cer 在（给手机安装用）
        der = CERT_DIR / "sayit-ca.cer"
        if not der.exists():
            try:
                from cryptography import x509
                from cryptography.hazmat.primitives import serialization
                c = x509.load_pem_x509_certificate(CERT.read_bytes())
                der.write_bytes(c.public_bytes(serialization.Encoding.DER))
                print(f"[cert] 已导出 {der}")
            except Exception as e:
                print(f"[cert] 导出 .cer 失败: {e}")
        if not args.no_firewall:
            ensure_firewall(args.port)
        host = args.host or "0.0.0.0"
        scheme = "https"
    else:
        host = args.host or "127.0.0.1"
        scheme = "http"

    print()
    print("=" * 60)
    print(f"  SayIt  {scheme}://{host}:{args.port}")
    print("=" * 60)
    if use_https and custom_cert:
        host_name = args.domain or "你的域名"
        print(f"  手机：  https://{host_name}:{args.port}      <<< 用这个")
        print(f"  证书：  {cert_file}")
        print()
        print("  用的是真实证书 —— 手机浏览器直接受信任，不用装 CA、不用点「继续前往」。")
        print(f"  前提：把 {host_name} 的 A 记录指向本机内网 IP（{'/'.join(ips) or '127.0.0.1'}）。")
        print("  证书过期前记得续（阿里云/腾讯云免费 DV 一年一续，或 acme.sh --dns 自动续）。")
        print()
    elif use_https:
        primary = ips[0] if ips else "127.0.0.1"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            primary = s.getsockname()[0]
            s.close()
        except Exception:
            pass
        print(f"  电脑：  https://127.0.0.1:{args.port}")
        print(f"  手机：  https://{primary}:{args.port}      <<< 用这个")
        others = [i for i in ips if i != primary]
        if others:
            print(f"          （其它网卡，多半是虚拟网卡，可忽略：{', '.join(others)}）")
        print()
        print(f"  ⚠ 手机必须装证书，否则 WebSocket 会连不上（页面能进，WS 会被拒）：")
        print(f"     在手机浏览器打开  https://{primary}:{args.port}/ca.cer  直接下载安装")
        print(f"  想彻底去掉证书警告：把 certs\\sayit-ca.cer 传到手机安装为「CA 证书」")
        print(f"    · Android：设置 → 安全 → 加密与凭据 → 安装证书 → CA 证书")
        print(f"    · iOS：把 .cer 发给自己打开 → 设置 → 已下载描述文件 → 安装")
        print(f"           再到 设置 → 通用 → 关于本机 → 证书信任设置 → 打开完全信任")
        print()
        print("  手机第一次打开会提示证书不受信任 —— 这是自签证书的正常现象：")
        print("    · Android Chrome：高级 → 继续前往")
        print("    · iPhone Safari：显示详细信息 → 访问此网站")
        print("  进去之后再点『添加到主屏幕』，就是全屏 PWA。")
    else:
        print("  纯 HTTP 模式：手机无法使用麦克风（浏览器安全限制），只能本机用。")
    print()

    import uvicorn

    if not use_https:
        uvicorn.run("app.main:app", host=host, port=args.port, reload=args.reload)
        return 0

    # HTTPS 给手机（局域网），同时开一个只绑回环的 HTTP 给电脑自己用 ——
    # 这样电脑上不用忍受自签证书警告，手机也不受影响。
    import asyncio

    local_port = args.port + 1
    print(f"  电脑（免证书）：http://127.0.0.1:{local_port}")
    print()

    async def serve_both() -> None:
        https_srv = uvicorn.Server(uvicorn.Config(
            "app.main:app", host="0.0.0.0", port=args.port, log_level="info",
            ssl_certfile=str(cert_file), ssl_keyfile=str(key_file)))
        http_srv = uvicorn.Server(uvicorn.Config(
            "app.main:app", host="127.0.0.1", port=local_port, log_level="warning"))
        await asyncio.gather(https_srv.serve(), http_srv.serve())

    try:
        asyncio.run(serve_both())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
