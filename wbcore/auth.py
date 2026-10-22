"""管理页登录口令。

从原 gateway.py 按职责拆出，逻辑未改动。
"""
from __future__ import annotations

import hashlib
from . import context as ctx

_TOKEN_SALT = b"wb-codex-gateway"


def _token_for(password: str) -> str:
    import hashlib

    return hashlib.sha256(_TOKEN_SALT + password.encode("utf-8")).hexdigest()[:32]

