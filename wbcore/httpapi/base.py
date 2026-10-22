"""HTTP 传输层：请求/响应、静态资源、鉴权、GET 路由。

从原 gateway.py 按职责拆出，逻辑未改动。
"""
from __future__ import annotations

import hmac
import json
import os
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler

from .. import context as ctx
from ..auth import _token_for
from ..codex_config import codex_state
from ..models import all_models
from .. import meta as wbmeta


class HttpBaseMixin:
        def log_message(self, fmt, *args):
            # 临时：记录所有到达的请求，便于定位 Codex 的探针
            if os.environ.get("WB_LOG_REQUESTS"):
                try:
                    with open(os.path.join(ctx.ROOT, "requests.ctx.log"), "a", encoding="utf-8") as fh:
                        fh.write("%s %s %s\n" % (time.strftime("%H:%M:%S"), self.command, self.path))
                except Exception:
                    pass
            pass


        def end_headers(self):
            # 记下"响应头已发出"：之后若再抛异常，就只能断开连接，不能再补 500
            self._headers_sent = True
            super().end_headers()


        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            extra = dict(extra or {})
            # 统一默认不缓存；调用方若要覆盖（比如静态资源加更严格的头），
            # 用大小写不敏感的方式去重，避免出现两个 Cache-Control 头。
            if not any(k.lower() == "cache-control" for k in extra):
                extra["Cache-Control"] = "no-store"

            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for key, val in extra.items():
                self.send_header(key, val)
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass


        def _json(self, status: int, payload, extra: dict | None = None) -> None:
            self._send(
                status,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                extra,
            )


        def handle_one_request(self):
            """在这里显式处理两次：读请求行/头，回应 Expect: 100-continue，然后分发。"""
            try:
                self._handle_one_request()
            except (ConnectionResetError, ConnectionAbortedError):
                # 客户端提前断开（浏览器刷新、探活超时）不该在日志里刷一屏 traceback
                self.close_connection = True


        def _handle_one_request(self):
            self._headers_sent = False
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(414)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return

            # 关键：客户端可能在等 100 才发 body
            if "100-continue" in (self.headers.get("Expect") or "").lower():
                try:
                    self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                    self.wfile.flush()
                except Exception:
                    pass

            mname = "do_" + self.command
            if not hasattr(self, mname):
                self.send_error(501, "Unsupported method (%r)" % self.command)
                return
            try:
                getattr(self, mname)()
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True
            except Exception as exc:  # noqa: BLE001
                # 未捕获异常以前会让连接直接断掉，前端只看到 "Failed to fetch"，
                # 完全不知道错在哪。这里至少把原因回给调用方（能回的话）。
                ctx.log("error", "%s %s 处理失败：%s: %s"
                        % (self.command, self.path, type(exc).__name__, exc))
                traceback.print_exc()
                if not getattr(self, "_headers_sent", False):
                    try:
                        self._json(500, {
                            "ok": False,
                            "error": "网关内部错误：%s: %s" % (type(exc).__name__, exc),
                        })
                    except Exception:
                        pass
                self.close_connection = True


        def _read_body(self) -> dict:
            """
            读请求体。必须同时支持 Content-Length 与 chunked。

            为什么不能只认 Content-Length：某些客户端（PowerShell 的
            Invoke-WebRequest、部分 JS fetch 场景）对 POST 用 chunked 传输。
            若此时不把 chunked 帧读干净，残留的字节会留在 keep-alive 连接里，
            被当成下一个请求的起始行，报 "Unsupported method ('{}GET')" 之类的怪错。
            """
            te = (self.headers.get("Transfer-Encoding") or "").lower()
            raw = b""

            if "chunked" in te:
                while True:
                    line = self.rfile.readline(1024)
                    if not line:
                        break
                    size_token = line.split(b";", 1)[0].strip()
                    try:
                        size = int(size_token, 16)
                    except ValueError:
                        break
                    if size == 0:
                        # 吃掉 trailer 直到空行
                        while True:
                            trailer = self.rfile.readline(1024)
                            if trailer in (b"\r\n", b"\n", b""):
                                break
                        break
                    raw += self.rfile.read(size)
                    self.rfile.read(2)  # 去掉 chunk 末尾的 CRLF
            else:
                length = int(self.headers.get("content-length") or 0)
                if length > 0:
                    raw = self.rfile.read(length)

            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8", "replace")) or {}
            except Exception:
                return {}


        def _cookie_token(self) -> str:
            raw = self.headers.get("Cookie") or ""
            try:
                cookie = SimpleCookie()
                cookie.load(raw)
                morsel = cookie.get("wb_admin")
                return morsel.value if morsel else ""
            except Exception:
                return ""


        def _client_key(self) -> str:
            """取客户端带来的 API key（用于 /v1 准入校验）。

            第三方客户端带 key 的方式五花八门：Codex 用
            `Authorization: Bearer X`，不少管理器只会发 `x-api-key: X` 或
            `Authorization: X`（没有 Bearer 前缀）。多认几种，比让人对着
            401 猜"我明明填了 key"强。
            """
            auth = (self.headers.get("Authorization") or "").strip()
            if auth:
                if auth.lower().startswith("bearer "):
                    return auth[7:].strip()
                return auth
            for name in ("x-api-key", "api-key", "x-auth-token", "apikey"):
                value = (self.headers.get(name) or "").strip()
                if value:
                    return value
            return ""


        def _admin_password(self) -> str:
            return ctx.CFG.get("admin_password") or ctx.STORE.get_raw_password() or ""


        def _authed(self) -> bool:
            expected = self._admin_password()
            if not expected:
                return True  # 未设密码则本机免登录
            token = self.headers.get("X-Admin-Token") or self._cookie_token()
            return bool(token) and hmac.compare_digest(token, _token_for(expected))


        def do_HEAD(self):
            """健康探针可能用 HEAD 探测端点。

            BaseHTTPRequestHandler 默认对未实现的方法返回 501，
            而 Codex 会把 501 当成"端点不可用"，于是拒绝发正式请求、
            对外报 502 Bad Gateway（网关日志里完全看不到流量，极难排查）。
            HEAD 只需回状态码与头，无需正文。
            """
            path = self.path.split("?")[0]
            clean = path.rstrip("/") or "/"
            if clean in ("/health", "/v1/health", "/v1/models", "/models",
                         "/v1/responses", "/responses", "/"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


        def do_OPTIONS(self):
            """CORS 预检 / 能力探测。"""
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()


        def do_GET(self):
            path = self.path.split("?")[0]
            clean = path.rstrip("/") or "/"

            if clean in ("/health", "/v1/health"):
                settings = ctx.STORE.get_settings()
                self._json(
                    200,
                    {
                        "status": "ok" if settings["proxy_enabled"] else "paused",
                        "proxy_enabled": settings["proxy_enabled"],
                        "policy": settings["policy"],
                        "accounts": len(ctx.STORE.list_accounts()),
                        "uptime": int(time.time() - ctx.START),
                    },
                )
                return

            if clean in ("/v1/models", "/models"):
                now = int(time.time())
                self._json(
                    200,
                    {
                        "object": "list",
                        "data": [
                            {"id": m, "object": "model", "created": now, "owned_by": "workbuddy"}
                            for m in all_models()
                        ],
                    },
                )
                return

            if clean == "/admin/api/state":
                if not self._authed():
                    self._json(401, {"error": "unauthorized"})
                    return
                data = ctx.STORE.snapshot()
                model_cd = ctx.STORE.model_cooldown_state()
                for acc in data["accounts"]:
                    live = model_cd.get(acc["id"]) or {}
                    acc["model_cooldowns"] = {
                        m: {"seconds": s, "minutes": max(1, s // 60)} for m, s in live.items()
                    }
                self._json(
                    200,
                    {
                        "settings": ctx.STORE.get_settings(),
                        "accounts": data["accounts"],
                        "models": all_models(),
                        "model_meta": wbmeta.get_meta(),
                        "authed": bool(self._admin_password()),
                        "broken": ctx.STORE.count_broken(),
                        "data_owner_warning": ctx.STORE.owner_warning(),
                        "codex": codex_state(),
                        "version": "2.4",
                    },
                )
                return

            # 静态资源
            if clean in ("/", "/index.html"):
                self._serve_file(os.path.join(ctx.WEB_DIR, "index.html"), "text/html; charset=utf-8")
                return
            if clean.startswith("/assets/"):
                name = os.path.basename(clean)
                ctype = "text/css; charset=utf-8" if name.endswith(".css") else "application/javascript; charset=utf-8"
                self._serve_file(os.path.join(ctx.WEB_DIR, name), ctype, force_reload=True)
                return

            self._json(404, {"error": "not found"})


        def _serve_file(self, path: str, ctype: str, force_reload: bool = False) -> None:
            if not os.path.exists(path):
                self._json(404, {"error": "missing asset"})
                return
            with open(path, "rb") as fh:
                body = fh.read()

            extra = {}
            if force_reload:
                # 开发期频繁改前端：强制浏览器每次回源，避免用户看到旧页面
                # （曾经因此出现过"策略下拉框空白""错误还是长文本"的假象）。
                extra["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                extra["Pragma"] = "no-cache"
                extra["Expires"] = "0"
            self._send(200, body, ctype, extra)

