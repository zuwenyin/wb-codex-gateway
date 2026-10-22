"""Codex 侧 config.toml 的模式切换（官方模型 <-> WorkBuddy 网关）。

从原 gateway.py 按职责拆出，逻辑未改动。
"""
from __future__ import annotations

import os
import re
import shutil

from . import context as ctx
from .models import all_models

CODEX_PROVIDER = "workbuddy"


CODEX_FALLBACK_MODEL = "gpt-5.6-terra"


def codex_config_path() -> str:
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(home, "config.toml")


def _codex_lines():
    path = codex_config_path()
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().splitlines()


def _section_start(lines) -> int:
    """第一个 [section] 的位置 —— 顶层 key 都在它之前。"""
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            return i
    return len(lines)


def _split_comment(text: str) -> tuple:
    """按引号外的 # 切成 (值, 行尾注释)。路径里不会有 #，注释里可能有引号。"""
    quote = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return text[:i], text[i:]
    return text, ""


def _codex_key_value(lines, key: str):
    """取顶层 key 的值；被注释掉或不存在返回 None。

    Codex 的 config.toml 里这些行往往带行尾注释
    （model_provider = "workbuddy"  # 【后来加】...），必须只取引号内的值。
    """
    # 注意：文件顶部注释块里有 "#     model = ..." 这种留档示例，
    # 遇到注释行要继续往下找，不能当成"该项被注释掉了"。
    limit = _section_start(lines)
    pat = re.compile(r"^\s*(?P<off>#\s*)?" + re.escape(key) + r"\s*=\s*(?P<val>.+?)\s*$")
    for i in range(limit):
        m = pat.match(lines[i])
        if m and not m.group("off"):
            return _split_comment(m.group("val"))[0].strip().strip("'\"")
    return None


def _codex_put(lines, key: str, value: str | None) -> int:
    """设置顶层 key。value 为 None 表示注释掉该行。返回是否改动。

    改写时保留原本的行尾注释，用户手写的说明不会被抹掉。
    """
    limit = _section_start(lines)
    pat = re.compile(r"^\s*(?P<off>#\s*)?" + re.escape(key) + r"\s*=(?P<rest>.*)$")

    # 第一遍只认未注释的行：顶部注释块里的同名示例行不能被改写
    for i in range(limit):
        m = pat.match(lines[i])
        if not (m and not m.group("off")):
            continue
        if value is None:
            lines[i] = "# " + lines[i].lstrip()
            return 1
        lines[i] = _render_assign(key, value, m.group("rest"))
        return 1

    if value is None:
        return 0

    # 第二遍：之前被注释掉过（比如我们上一轮切到官方模式）就在原行解开
    for i in range(limit):
        m = pat.match(lines[i])
        if m:
            lines[i] = _render_assign(key, value, m.group("rest"))
            return 1

    lines.insert(limit, "%s = %s" % (key, value))
    return 1


def _render_assign(key: str, value: str, rest: str) -> str:
    """拼接一行赋值，保留原本的行尾注释。"""
    comment = _split_comment(rest)[1].strip()
    return "%s = %s" % (key, value) + ("   " + comment if comment else "")


def _codex_ensure_provider(lines) -> int:
    """保证末尾有 [model_providers.workbuddy] 段（没有才追加）。"""
    header = "[model_providers.%s]" % CODEX_PROVIDER
    if any(ln.strip() == header for ln in lines):
        return 0
    lines.extend([
        "",
        header,
        'name = "WorkBuddy"',
        'base_url = "http://%s/v1"' % ctx.CFG.get("listen", "127.0.0.1:18080"),
        'wire_api = "responses"',
        'experimental_bearer_token = "%s"' % (ctx.CFG.get("approval_key") or ""),
    ])
    return 1


def _default_gateway_model() -> str:
    models = all_models()
    return models[0] if models else CODEX_FALLBACK_MODEL


def codex_state() -> dict:
    path = codex_config_path()
    lines = _codex_lines()
    if lines is None:
        return {"ok": False, "path": path, "exists": False,
                "error": "未找到 Codex 配置文件"}
    conf = ctx.STORE.get_codex_conf()
    provider = _codex_key_value(lines, "model_provider")
    catalog = _codex_key_value(lines, "model_catalog_json")
    return {
        "ok": True,
        "path": path,
        "exists": True,
        "mode": "gateway" if provider == CODEX_PROVIDER else "official",
        "model": _codex_key_value(lines, "model"),
        "provider": provider,
        "catalog": catalog,
        "catalog_exists": bool(catalog) and os.path.exists(catalog),
        "original_model": conf.get("original_model") or "",
        "gateway_model": conf.get("gateway_model") or "",
        "models": all_models(),
    }


def codex_set_mode(mode: str, model: str | None = None) -> dict:
    """切换 Codex 的接入模式。会先备份原文件。"""
    path = codex_config_path()
    lines = _codex_lines()
    if lines is None:
        return {"ok": False, "error": "未找到 Codex 配置文件：%s" % path}

    conf = ctx.STORE.get_codex_conf()
    patch: dict = {}

    if mode == "gateway":
        # 只在当前 model 不是网关模型时记录，避免把网关模型记成"官方原值"
        cur = _codex_key_value(lines, "model")
        if cur and cur not in all_models():
            patch["original_model"] = cur
        gw_model = model or conf.get("gateway_model") or _default_gateway_model()
        _codex_put(lines, "model", '"%s"' % gw_model)
        _codex_put(lines, "model_provider", '"%s"' % CODEX_PROVIDER)
        _codex_put(lines, "model_catalog_json", "'%s'" % ctx.CATALOG_PATH)
        _codex_ensure_provider(lines)
        patch["gateway_model"] = gw_model
    elif mode == "official":
        _codex_put(lines, "model", '"%s"' % (conf.get("original_model") or CODEX_FALLBACK_MODEL))
        _codex_put(lines, "model_provider", None)
        _codex_put(lines, "model_catalog_json", None)
    else:
        return {"ok": False, "error": "未知模式：%s" % mode}

    try:
        shutil.copy2(path, path + ".wb-bak")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    except Exception as exc:
        return {"ok": False, "error": "写入失败（可能被 Codex 占用）：%s" % exc}

    if patch:
        ctx.STORE.set_codex_conf(patch)
    ctx.log("info", "Codex 配置已切换为 %s 模式（备份 config.toml.wb-bak）"
        % ("网关" if mode == "gateway" else "官方"))
    return {"ok": True, "mode": mode, **codex_state()}

