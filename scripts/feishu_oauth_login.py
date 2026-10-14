"""飞书用户授权小工具：本机回调换取 user_access_token 并缓存。

前置（开放平台）：
1. 权限管理 → 用户身份：日历读/写；并开通 offline_access（才能 refresh）
2. 安全设置 → 重定向 URL 添加：http://127.0.0.1:8765/callback
3. 版本发布；可用性包含你自己

用法（在 cyber_lobster_api_fusion 目录）：
  ..\\venv311\\Scripts\\python.exe scripts\\feishu_oauth_login.py
"""
from __future__ import annotations

import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_PKG = Path(__file__).resolve().parents[1]
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from apitools.feishu.oauth import (  # noqa: E402
    DEFAULT_PORT,
    build_authorize_url,
    exchange_code_for_token,
    redirect_uri,
    token_cache_path,
)


def _parse_port(uri: str) -> int:
    u = urllib.parse.urlparse(uri)
    if u.port:
        return int(u.port)
    return DEFAULT_PORT


def main() -> int:
    redir = redirect_uri()
    parsed = urllib.parse.urlparse(redir)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        print(f"Redirect URI must be local for this tool, got: {redir}")
        return 1

    port = _parse_port(redir)
    auth_url, expected_state = build_authorize_url()
    result: dict = {"done": False, "error": None, "bundle": None}
    done_event = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            return

        def do_GET(self) -> None:  # noqa: N802
            u = urllib.parse.urlparse(self.path)
            if u.path.rstrip("/") != "/callback":
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not Found")
                return
            qs = urllib.parse.parse_qs(u.query)
            err = (qs.get("error") or [None])[0]
            code = (qs.get("code") or [None])[0]
            state = (qs.get("state") or [None])[0]
            if err:
                result["error"] = err
                body = f"<h3>授权失败</h3><pre>{err}</pre>".encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                done_event.set()
                return
            if not code:
                result["error"] = "missing code"
                body = b"<h3>missing code</h3>"
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                done_event.set()
                return
            if state != expected_state:
                result["error"] = "state mismatch"
                body = b"<h3>state mismatch</h3>"
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                done_event.set()
                return
            try:
                bundle = exchange_code_for_token(code)
                result["bundle"] = bundle
                result["done"] = True
                body = (
                    "<h3>飞书授权成功</h3>"
                    "<p>可以关闭本页，回到终端。</p>"
                    f"<p>token 已写入：<code>{token_cache_path()}</code></p>"
                ).encode("utf-8")
                self.send_response(200)
            except Exception as e:
                result["error"] = str(e)
                body = f"<h3>换 token 失败</h3><pre>{e}</pre>".encode("utf-8")
                self.send_response(500)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done_event.set()

    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("=== 飞书用户授权（写个人主日历）===")
    print(f"回调地址: {redir}")
    print("请确认开放平台「安全设置 → 重定向 URL」已添加上述地址。")
    print("请确认已开通用户身份：日历读/写 + 多维表格(bitable:app / base:app:create) + offline_access，并已发布。")
    print("改过权限后必须重新跑本脚本；旧 token 不会自动带上新权限。")
    print()
    print("正在打开浏览器授权页…")
    print(auth_url)
    print()
    try:
        webbrowser.open(auth_url)
    except Exception:
        print("无法自动打开浏览器，请手动复制上面的链接。")

    if not done_event.wait(timeout=300):
        print("超时：5 分钟内未完成授权。")
        server.shutdown()
        return 1

    # 给浏览器一点时间收完响应
    time.sleep(0.3)
    server.shutdown()

    if result.get("error"):
        print("失败:", result["error"])
        return 1

    bundle = result.get("bundle") or {}
    print("成功。")
    print("access_token 前缀:", str(bundle.get("access_token") or "")[:16] + "…")
    print("expires_in:", bundle.get("expires_in"))
    print("refresh_token:", "有" if bundle.get("refresh_token") else "无（请开通 offline_access）")
    print("已保存:", token_cache_path())
    print()
    print("下一步：重启 app.py，在 Dash 里试「明天下午加一个日程」。")
    print("无需再手填 FEISHU_USER_ACCESS_TOKEN（脚本缓存优先；过期会自动 refresh）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
