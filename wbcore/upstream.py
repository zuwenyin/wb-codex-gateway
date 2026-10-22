"""上游（WorkBuddy 云）HTTP 调用：地址、请求头、重试。

从原 gateway.py 按职责拆出，逻辑未改动。
"""
from __future__ import annotations

import json
import os
import time
from urllib import error as urlerror
from urllib import request as urlrequest

from . import context as ctx
from . import proto as wbproto
from .faults import _is_transient_network

def is_local(acc: dict) -> bool:
    return acc.get("kind") == "local"


def chat_url(acc: dict) -> str:
    base = (acc.get("base_url") or ctx.CFG.get("upstream_base") or "").rstrip("/")
    if not base:
        return ""
    if is_local(acc):
        # WorkBuddy 云的真实对话端点，且只支持流式
        return base + ctx.WB_CHAT_PATH
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def upstream_headers(acc: dict) -> dict:
    if is_local(acc):
        domain = acc.get("domain") or "www.codebuddy.cn"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": ctx.WB_BILLING_ORIGIN,
            "Referer": ctx.WB_BILLING_ORIGIN + "/",
            "User-Agent": ctx.WB_CLIENT_UA,
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % (acc.get("access_token") or ""),
            "X-Product": "SaaS",
            "X-Domain": domain,
        }
        if acc.get("uid"):
            headers["X-User-Id"] = acc["uid"]
        else:
            headers["X-No-User-Id"] = "1"
        if acc.get("enterprise_id"):
            headers["X-Enterprise-Id"] = acc["enterprise_id"]
        else:
            headers["X-No-Enterprise-Id"] = "1"
        return headers

    key = acc.get("api_key") or ""
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "CLI/2.156.0 WorkBuddy/2.156.0",
        "x-requested-with": "XMLHttpRequest",
        "x-codebuddy-request": "1",
        "x-stainless-arch": "x64",
        "x-stainless-lang": "js",
        "x-stainless-os": "Windows",
        "x-stainless-package-version": "6.25.0",
        "x-stainless-runtime": "node",
        "X-Product": "SaaS",
        "X-IDE-Type": "CLI",
        "X-IDE-Name": "CLI",
        "X-IDE-Version": "2.156.0",
    }
    if key:
        headers["Authorization"] = "Bearer %s" % key
        headers["X-API-Key"] = key
    return headers


def call_upstream(acc: dict, chat_payload: dict, timeout: float | None = None):
    payload = dict(chat_payload)
    if is_local(acc):
        # WorkBuddy 云明确拒绝非流式请求（code 11101），强制走流
        payload["stream"] = True
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if os.environ.get("WB_DEBUG_UPSTREAM"):
        with open(os.path.join(ctx.ROOT, "upstream-debug.jsonl"), "ab") as fh:
            fh.write(data + b"\n")

    # SSL EOF / 连接重置属于链路抖动，同一个请求多试几次通常就好。
    # 从 2 提到 3 并加退避：实测长请求（100KB+）更容易被中间设备掐断，
    # 只重试 1 次的话仍会把错误抛给用户，表现为 Codex 里"总是暂停"。
    attempts = 3
    last = None
    for i in range(attempts):
        req = urlrequest.Request(chat_url(acc), data=data, headers=upstream_headers(acc), method="POST")
        try:
            return urlrequest.urlopen(req, timeout=timeout or float(ctx.CFG.get("timeout") or 300)), None
        except urlerror.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            return None, (exc.code, detail)
        except Exception as exc:
            msg = str(exc)
            last = (None, msg)
            if _is_transient_network(None, msg) and i + 1 < attempts:
                time.sleep(0.5 * (i + 1))
                continue
            return None, last
    return None, last


def call_upstream_once(acc: dict, chat_payload: dict, timeout: float | None = None):
    """发一次请求并把 SSE 读成纯文本，用于账号连通性测试与非流式调用方。"""
    resp, err = call_upstream(acc, chat_payload, timeout=timeout)
    if err:
        return None, err
    text_parts = []
    buf = b""
    try:
        while True:
            chunk = resp.read(2048)
            if not chunk:
                break
            buf += chunk
            events, buf = wbproto.parse_sse_lines(buf)
            for event in events:
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        text_parts.append(delta["content"])
                    if choice.get("finish_reason"):
                        break
    except Exception as exc:
        return None, (None, str(exc))
    finally:
        try:
            resp.close()
        except Exception:
            pass
    return "".join(text_parts), None

