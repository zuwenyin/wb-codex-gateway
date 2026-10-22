"""按上游真实的 maxInputTokens 重建 models.json。

之前所有模型都被写成 272000（GPT 模板遗留），导致 Codex 的自动压缩阈值
远低于真实上限：1M 的模型到 272K 就该压缩却没压，最终撞到 1M 硬墙。
"""
import json
import os
import sys


from wbcore import store as wbstore

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
CATALOG = os.path.join(ROOT, "models.json")
CACHE = os.path.join(os.path.expanduser("~"), ".codex", "models_cache.json")

# slug -> 展示名（顺序即模型下拉框里的排序依据）
DISPLAY = [
    ("deepseek-v4.1-flash", "DeepSeek V4.1 Flash"),
    ("deepseek-v4-flash", "DeepSeek V4 Flash"),
    ("deepseek-v4-pro", "DeepSeek V4 Pro"),
    ("glm-5.3", "GLM-5.3"),
    ("glm-5.3-flash", "GLM-5.3 Flash"),
    ("kimi-k3-1", "Kimi K3.1"),
    ("kimi-k2.8-preview", "Kimi K2.8"),
    ("kimi-k2.7", "Kimi K2.7"),
    ("kimi-k2.5", "Kimi K2.5"),
    ("minimax-m3", "MiniMax M3"),
    ("minimax-m2.7", "MiniMax M2.7"),
    ("hy3", "Hunyuan 3"),
    ("step-5-preview", "Step 5"),
    ("mimo-v2.6-pro", "MiMo V2.6 Pro"),
    ("deepseek-v3-2-volc", "DeepSeek V3.2"),
]


def fetch_context_windows():
    """从上游 /v3/config 拉每个模型的 maxInputTokens。"""
    import urllib.request

    s = wbstore.Store()
    acc = [a for a in s.list_accounts() if a.get("access_token")][0]
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

    out = {}
    for m in data.get("models", []):
        mid = m.get("id")
        if not mid:
            continue
        win = m.get("maxInputTokens") or m.get("maxAllowedSize")
        if isinstance(win, (int, float)) and win > 0:
            out[mid] = int(win)
    return out


def main():
    windows = fetch_context_windows()
    print("上游返回 %d 个模型的窗口大小" % len(windows))

    base = json.load(open(CACHE, encoding="utf-8"))["models"][0]
    tmpl = (base.get("model_messages") or {}).get("instructions_template") or ""

    models = []
    for prio, (slug, name) in enumerate(DISPLAY, start=1):
        win = windows.get(slug)
        if not win:
            print("  跳过（上游无数据）：%s" % slug)
            continue

        m = json.loads(json.dumps(base))
        m["slug"] = slug
        m["display_name"] = name
        m["description"] = "WorkBuddy 云模型（经本地网关）"
        m["priority"] = prio

        # === 关键：用上游真实窗口，别再沿用 GPT 模板的值 ===
        m["context_window"] = win
        m["max_context_window"] = win
        m["effective_context_window_percent"] = 95

        m["shell_type"] = "shell_command"
        m["tool_mode"] = "default"
        m["use_responses_lite"] = False
        m["apply_patch_tool_type"] = "freeform"
        m["supports_search_tool"] = False
        m["web_search_tool_type"] = "text"
        m["experimental_supported_tools"] = []
        m["multi_agent_version"] = None
        m["node_repl_disabled"] = True
        m["node_repl_auto_review_required"] = False
        m.pop("available_access_programs", None)
        m["model_messages"] = {"instructions_template": tmpl} if tmpl else {}
        if not tmpl:
            m["base_instructions"] = "You are Codex, a coding agent."

        models.append(m)
        print("  %-22s context_window=%d" % (slug, win))

    json.dump({"models": models}, open(CATALOG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\n已写入 %d 个模型 -> %s" % (len(models), CATALOG))


if __name__ == "__main__":
    main()
