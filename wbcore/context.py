"""配置、日志、状态文件与进程级共享状态（STORE）。

从原 gateway.py 按职责拆出，逻辑未改动。
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

from .paths import (
    CATALOG_PATH,
    DATA_PATH,
    ROOT,
    STATUS_PATH,
    WBCREDS_JS,
    WEB_DIR,
)

# 账号池实例，由 wbcore.httpapi.server 在启动时注入；
# 其它模块一律通过 ctx.STORE 晚绑定访问，不做模块级快照。
# 类型标成 Any 是因为它在导入时还是 None，静态检查会误报"None 没有该方法"。
STORE: Any = None
DEFAULTS = {
    "listen": "127.0.0.1:18080",
    "models": "deepseek-v4-pro,deepseek-v4-flash,glm-5.3,kimi-k3-1",
    "upstream_base": "https://copilot.tencent.com",
    "upstream_key": "",
    "approval_key": "",
    "admin_password": "",
    "timeout": 300,
    "node_path": "",
}


CFG = dict(DEFAULTS)


WB_CHAT_PATH = "/v2/chat/completions"


WB_BILLING_ORIGIN = "https://www.codebuddy.cn"


WB_CLIENT_UA = "CLI/2.63.2 CodeBuddy/2.63.2"


def log(level: str, msg: str) -> None:
    try:
        sys.stderr.write("[%s] %-5s %s\n" % (time.strftime("%H:%M:%S"), level.upper(), msg))
        sys.stderr.flush()
    except Exception:
        pass


def write_status(**extra) -> None:
    payload = {"pid": os.getpid(), "at": int(time.time()), "listen": CFG.get("listen")}
    payload.update(extra)
    try:
        with open(STATUS_PATH + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(STATUS_PATH + ".tmp", STATUS_PATH)
    except Exception:
        pass


def load_config() -> None:
    path = os.path.join(ROOT, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                CFG.update({k: v for k, v in json.load(fh).items() if v is not None})
        except Exception as exc:
            log("warn", "config.json 解析失败: %s" % exc)

    for env, key in (
        ("WB_LISTEN", "listen"),
        ("WB_MODELS", "models"),
        ("WB_UPSTREAM_BASE", "upstream_base"),
        ("WB_UPSTREAM_KEY", "upstream_key"),
        ("WB_APPROVAL_KEY", "approval_key"),
        ("WB_ADMIN_PASSWORD", "admin_password"),
        ("WB_TIMEOUT", "timeout"),
        ("WB_NODE_PATH", "node_path"),
    ):
        val = os.environ.get(env)
        if val:
            CFG[key] = val


START = time.time()

