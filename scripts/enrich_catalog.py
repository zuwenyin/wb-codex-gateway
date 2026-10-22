"""把上下文窗口/积分倍率写进 models.json，让 Codex 两处都能看到。

背景（实测结论，别再猜）
-----------------------
* `codex debug models` 证明 description 被正确加载；
* 但 Composer 底部那个模型下拉**只渲染 display_name**，description 不显示；
* `/model` 斜杠命令的搜索列表则会渲染 description。

所以采用双轨：
  display_name → "DeepSeek V4.1 Flash (1M)"     简短窗口，给下拉看
  description  → "WorkBuddy · 上下文 1M · x0.11"  完整信息，给 /model 搜索看

display_name 只加窗口不加倍率：底部输入框那行也会显示模型名，
倍率塞进去会太长。倍率留给 description。

幂等：重复运行不会叠加后缀（会先剥掉旧的 "(...)"）。
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
MODELS = os.path.join(ROOT, "models.json")
META = os.path.join(ROOT, "models-meta.json")

# 模型展示名（不含后缀，这是"干净"的名字）
BASE_NAMES = {
    "deepseek-v4.1-flash": "DeepSeek V4.1 Flash",
    "deepseek-v4-flash": "DeepSeek V4 Flash",
    "deepseek-v4-pro": "DeepSeek V4 Pro",
    "glm-5.3": "GLM-5.3",
    "glm-5.3-flash": "GLM-5.3 Flash",
    "kimi-k3-1": "Kimi K3.1",
    "kimi-k2.8-preview": "Kimi K2.8",
    "kimi-k2.7": "Kimi K2.7",
    "kimi-k2.5": "Kimi K2.5",
    "minimax-m3": "MiniMax M3",
    "minimax-m2.7": "MiniMax M2.7",
    "hy3": "Hunyuan 3",
    "step-5-preview": "Step 5",
    "mimo-v2.6-pro": "MiMo V2.6 Pro",
    "deepseek-v3-2-volc": "DeepSeek V3.2",
}

SUFFIX_RE = re.compile(r"\s*\([^()]*\)\s*$")
# 带倍率的新后缀形如 "(1M · x0.11)"，同样被 SUFFIX_RE 剥掉（括号内不含嵌套括号）


def fmt_tokens(n):
    if not n:
        return ""
    if n >= 1000000:
        v = n / 1000000
        return ("%dM" % v) if abs(v - round(v)) < 0.05 else ("%.1fM" % v)
    if n >= 1000:
        return "%dK" % round(n / 1000)
    return str(n)


def fmt_credit(raw: str) -> str:
    """把 'x0.11 credits' 压成紧凑的 'x0.11'，给 display_name 用。

    名字里再带 'credits' 太长，底部输入框那行会挤。
    """
    if not raw:
        return ""
    text = str(raw).strip()
    # 去掉 credits / 空格，只留 x0.11 这样的主体
    text = re.sub(r"\s*credits?\s*", "", text, flags=re.I).strip()
    return text


def clean_name(model):
    """剥掉旧的 "(...)" 后缀，得到干净名字。"""
    base = BASE_NAMES.get(model.get("slug"))
    if base:
        return base
    name = (model.get("display_name") or model.get("slug") or "").strip()
    return SUFFIX_RE.sub("", name).strip()


def main():
    if not os.path.exists(META):
        print("缺少 models-meta.json，先运行 wbmeta.py")
        return 1

    meta = json.load(open(META, encoding="utf-8"))["models"]
    catalog = json.load(open(MODELS, encoding="utf-8"))

    for m in catalog["models"]:
        info = meta.get(m["slug"]) or {}
        win = info.get("context_window") or m.get("context_window") or 0
        credit = info.get("credits_raw") or ""

        base = clean_name(m)
        win_label = fmt_tokens(win)
        credit_label = fmt_credit(credit)

        # display_name：名字 + (窗口 · 倍率)，缺哪项就少哪项
        tags = [t for t in (win_label, credit_label) if t]
        m["display_name"] = "%s (%s)" % (base, " · ".join(tags)) if tags else base

        # description：完整信息，供 /model 搜索列表显示
        bits = []
        if win_label:
            bits.append("上下文 %s" % win_label)
        if credit:
            bits.append(credit)
        m["description"] = "WorkBuddy · " + " · ".join(bits) if bits else "WorkBuddy 云模型"

        print("%-22s %-34s %s" % (m["slug"], m["display_name"], m["description"]))

    json.dump(catalog, open(MODELS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\n已更新 %d 个模型" % len(catalog["models"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
