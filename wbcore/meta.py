"""模型元数据：从上游 /v3/config 拉取窗口大小与积分倍率，供 UI 展示。

缓存到 models-meta.json，避免每次打开页面都请求上游。
"""
import json
import os
import sys
import time


from .paths import ROOT

META_PATH = os.path.join(ROOT, "models-meta.json")
TTL = 1800  # 30 分钟


def _fetch():
    """向上游拉取原始模型列表。"""
    import urllib.request

    from .store import Store

    s = Store()
    accs = [a for a in s.list_accounts() if a.get("access_token")]
    if not accs:
        raise RuntimeError("没有可用账号")

    acc = accs[0]
    h = {
        "Authorization": "Bearer " + acc["access_token"],
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://www.codebuddy.cn",
        "Referer": "https://www.codebuddy.cn/",
        "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2",
        "X-User-Id": acc.get("uid", ""),
    }
    req = urllib.request.Request("https://copilot.tencent.com/v3/config", headers=h)
    data = json.loads(urllib.request.urlopen(req, timeout=40).read().decode("utf-8", "replace"))["data"]
    return data.get("models") or []


def _parse_credits(raw):
    """把 'x0.51 credits' 解析成 0.51 这样的数字。"""
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str):
        return None
    text = raw.lower().replace("credits", "").replace("credit", "").strip()
    text = text.lstrip("x").strip()
    try:
        return float(text)
    except ValueError:
        return None


def build_meta(models=None):
    if models is None:
        models = _fetch()

    out = {}
    for m in models:
        mid = m.get("id")
        if not mid:
            continue
        win = m.get("maxInputTokens") or m.get("maxAllowedSize")
        out[mid] = {
            "id": mid,
            "name": m.get("name") or mid,
            "context_window": int(win) if isinstance(win, (int, float)) and win else 0,
            "max_output_tokens": int(m.get("maxOutputTokens") or 0),
            "credits": _parse_credits(m.get("credits")),
            "credits_raw": m.get("credits") or "",
            "supports_images": m.get("supportsImages") is True,
            "only_reasoning": m.get("onlyReasoning") is True,
            "reasoning": m.get("reasoning") or {},
        }
    return out


def get_meta(force=False) -> dict:
    """读取缓存；过期或强制刷新时重新拉取。失败则返回上次的缓存。"""
    cached = None
    if os.path.exists(META_PATH):
        try:
            with open(META_PATH, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
        except Exception:
            cached = None

    fresh = cached and (time.time() - cached.get("fetched_at", 0) < TTL)
    if fresh and not force:
        return cached.get("models") or {}

    try:
        models = build_meta()
        payload = {"fetched_at": int(time.time()), "models": models}
        tmp = META_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, META_PATH)
        return models
    except Exception:
        return (cached or {}).get("models") or {}


if __name__ == "__main__":
    m = get_meta(force=True)
    print("模型数:", len(m))
    for k, v in list(m.items())[:8]:
        print(
            "  %-24s 窗口=%-9s 倍率=%s"
            % (k, "{:,}".format(v["context_window"]), v["credits_raw"] or "-")
        )
