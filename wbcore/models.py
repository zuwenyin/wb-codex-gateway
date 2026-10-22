"""模型清单与账号脱敏。

从原 gateway.py 按职责拆出，逻辑未改动。
"""
from __future__ import annotations

from . import context as ctx
from . import store as wbstore

def account_models(acc: dict) -> list:
    raw = (acc.get("models") or "").strip()
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()]
    return [m.strip() for m in (ctx.CFG.get("models") or "").split(",") if m.strip()]


def all_models() -> list:
    seen, out = set(), []
    for acc in ctx.STORE.list_accounts():
        if not acc.get("enabled", True) or acc.get("reserved"):
            continue
        for m in account_models(acc):
            if m not in seen:
                seen.add(m)
                out.append(m)
    if not out:
        out = [m.strip() for m in (ctx.CFG.get("models") or "").split(",") if m.strip()]
    return out


def _public(acc: dict | None) -> dict | None:
    if not acc:
        return None
    out = dict(acc)
    out["api_key"] = wbstore._mask(acc.get("api_key", ""))
    return out

