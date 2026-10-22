"""服务装配与生命周期（端口探测、线程模型、启动、心跳）。

从原 gateway.py 按职责拆出，逻辑未改动。
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import context as ctx
from .. import store as wbstore
from ..local_accounts import auto_scan_if_needed, log_scan_preflight
from .admin import AdminApiMixin
from .base import HttpBaseMixin
from .protocol import ProtocolMixin


class Handler(HttpBaseMixin, ProtocolMixin, AdminApiMixin, BaseHTTPRequestHandler):
    """真正的请求处理器：把三个职责 mixin 合成一个类。

    为什么要拆：路由 + 协议转换 + 管理 REST 合在一起有 900 行，
    改一处要在几百行里翻找。合成一个类只是为了交给 HTTPServer 调用。
    """


def _port_in_use(host: str, port: int) -> bool:
    """Windows 上 SO_REUSEADDR 会让两个进程同时监听同一端口而不报错，
    表现为请求随机打到旧实例，极难排查。启动前主动探测一次。"""
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            probe.close()
        except Exception:
            pass


def main() -> None:

    ctx.load_config()
    ctx.STORE = wbstore.Store()

    host, _, port = (ctx.CFG.get("listen") or "127.0.0.1:18080").rpartition(":")
    host = host or "127.0.0.1"
    port = int(port)

    if _port_in_use(host, port):
        ctx.log("error", "端口 %d 已被占用 —— 很可能网关已经在运行。" % port)
        ctx.log("error", "请先关闭已有的网关窗口，或改用其他端口：")
        ctx.log("error", '  set WB_LISTEN=127.0.0.1:18081 && start-gateway.cmd')
        ctx.log("error", "已有实例的管理页： http://%s:%d/" % (host, port))
        sys.exit(1)

    ctx.log("info", "管理页面 http://%s:%d/" % (host, port))
    ctx.log("info", "Codex 端点 http://%s:%d/v1" % (host, port))
    settings = ctx.STORE.get_settings()
    ctx.log("info", "代理状态 %s | 策略 %s | 账号 %d 个"
        % ("开启" if settings["proxy_enabled"] else "暂停", settings["policy"], len(ctx.STORE.list_accounts())))
    if not (ctx.CFG.get("admin_password") or ""):
        ctx.log("warn", "未设置管理密码（config.json 的 admin_password），本机任何人可打开管理页")

    _warn_imported_data()

    ctx.write_status(state="starting")
    server = ThreadingHTTPServer((host, port), Handler)
    ctx.write_status(state="listening")

    # 启动时先自检扫描前置条件（Node / 登录态 / 桌面端），缺什么直接写进日志；
    # 池子里没有可用的本机账号时再自动扫一次。都放后台线程，避免拖慢启动。
    threading.Thread(target=_startup_local_accounts, daemon=True).start()

    t = threading.Thread(target=_heartbeat, daemon=True)
    t.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ctx.write_status(state="stopped")


def _startup_local_accounts() -> None:
    """后台启动任务：先自检扫描条件，再按需自动扫描。"""
    for step in (log_scan_preflight, lambda: auto_scan_if_needed("启动时账号池为空")):
        try:
            step()
        except Exception as exc:  # 自检失败不该影响网关启动
            ctx.log("warn", "启动自检异常：%s" % exc)


def _warn_imported_data() -> None:
    """data.json 不是本机生成的 —— 多半是别人打包分享时带过来的。

    这种数据里带着对方的账号和有效 token：看着能用，但既不安全
    也不属于你。启动时明确提示，并在管理页给出一键清空。
    """
    warn = ctx.STORE.owner_warning()
    if not warn:
        return
    origin = warn.get("origin") or {}
    n = int(warn.get("accounts") or 0)
    where = (origin.get("user") or "?") + "@" + (origin.get("host") or "?")
    ctx.log("warn", "data.json 来自另一台机器（%s），里面的 %d 个账号不属于本机" % (where, n))
    ctx.log("warn", "  通常是别人打包分享项目时误带了 data.json 所致。")
    if n:
        ctx.log("warn", "  这些账号带着对方的有效 token，请勿使用；管理页可一键清空后重新扫描本机账号。")
        ctx.log("warn", "  或手动：关掉网关 → 删除 data.json → 重新运行 start-gateway.cmd")


def _heartbeat() -> None:
    ticks = 0
    while True:
        time.sleep(30)
        ticks += 1
        try:
            s = ctx.STORE.get_settings()
            ctx.write_status(
                state="listening" if s["proxy_enabled"] else "paused",
                accounts=len(ctx.STORE.list_accounts()),
                policy=s["policy"],
            )
        except Exception:
            pass

        # 每 5 分钟（心跳 30s × 10）看一眼：池里没有可用本机账号就自动扫，
        # 这样在 WorkBuddy 里重新登录后不用手动点扫描。
        if ticks % 10 == 0:
            try:
                auto_scan_if_needed("后台巡检发现没有可用的本机账号")
            except Exception:
                pass
