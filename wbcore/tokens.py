"""上下文窗口与 token 估算。

从原 gateway.py 按职责拆出，逻辑未改动。
"""
from __future__ import annotations

import os
import json
import base64
from . import context as ctx

_window_cache: dict = {"mtime": 0, "map": {}}


def _image_tokens_from_data_url(url: str) -> int:
    """从 data:image/...;base64, 里读出图片尺寸，按 OpenAI 规则估算 token。

    规则（官方）：缩放到 2048 以内 -> 最短边压到 768 -> 按 512 切块
                  token = 块数 * 170 + 85
    读不出尺寸时返回 1100（1440x900 的典型值），绝不按 base64 长度算。
    """
    try:
        import base64
        import struct

        head, _, payload = url.partition(",")
        if "base64" not in head or not payload:
            return 1100
        raw = base64.b64decode(payload[:65536] + "=" * (-len(payload[:65536]) % 4))
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
            w, h = struct.unpack(">II", raw[16:24])
        elif raw[:2] == b"\xff\xd8":  # JPEG：扫 SOF
            i, w, h = 2, 0, 0
            while i + 9 < len(raw):
                if raw[i] != 0xFF:
                    i += 1
                    continue
                marker = raw[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
                    h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                    break
                seg = struct.unpack(">H", raw[i + 2:i + 4])[0]
                i += 2 + seg
        else:
            return 1100
        if not w or not h:
            return 1100
        if w > 2048 or h > 2048:
            s = 2048 / max(w, h)
            w, h = round(w * s), round(h * s)
        short = min(w, h)
        if short > 768:
            s = 768 / short
            w, h = round(w * s), round(h * s)
        tiles = __import__("math").ceil(w / 512) * __import__("math").ceil(h / 512)
        return tiles * 170 + 85
    except Exception:
        return 1100


def _estimate_tokens(chat: dict) -> int:
    """估算请求的 token 数，图片按图片规则、文本按 4 字节/token。

    这个函数存在的意义：早先用 len(json)/4 估算，把 base64 图片当文本，
    导致「实际只用 16%」的会话被误判成 100%+ 而拦下。
    """
    text_bytes = 0
    image_tokens = 0

    def walk(node):
        nonlocal text_bytes, image_tokens
        if isinstance(node, str):
            # data:image 的 base64 绝不按字节计
            if node.startswith("data:image"):
                image_tokens += _image_tokens_from_data_url(node)
                return
            text_bytes += len(node.encode("utf-8"))
            return
        if isinstance(node, dict):
            # 图片结构：{"type":"image_url","image_url":{"url":"data:..."}}
            if node.get("type") in ("image_url", "input_image", "image"):
                url = node.get("image_url") or node.get("url")
                if isinstance(url, dict):
                    url = url.get("url")
                if isinstance(url, str):
                    image_tokens += _image_tokens_from_data_url(url)
                return
            for v in node.values():
                walk(v)
            return
        if isinstance(node, list):
            for v in node:
                walk(v)

    walk(chat)
    return text_bytes // 4 + image_tokens


def _model_window(model: str) -> int:
    """从 models.json 里查该模型的上下文窗口；查不到返回 0（表示不校验）。

    按文件 mtime 缓存，避免每个请求都读一遍 JSON。
    """
    if not model:
        return 0
    path = ctx.CATALOG_PATH
    try:
        mtime = os.path.getmtime(path)
        if _window_cache["mtime"] != mtime:
            with open(path, "r", encoding="utf-8") as fh:
                catalog = json.load(fh)
            mapping = {}
            for m in catalog.get("models") or []:
                win = m.get("context_window") or m.get("max_context_window")
                if m.get("slug") and win:
                    mapping[m["slug"]] = int(win)
            _window_cache["mtime"] = mtime
            _window_cache["map"] = mapping
        return int(_window_cache["map"].get(model, 0))
    except Exception:
        return 0

