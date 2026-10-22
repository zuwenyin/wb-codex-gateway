"""验证 Codex 实际加载的 display_name 与 description。

codex.exe 的路径由环境变量 CODEX_BIN 指定，或自动查找标准安装位置。
找不到时跳过（不视为失败）。
"""
import glob
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def find_codex():
    for env_key in ("CODEX_BIN", "CODEX_EXE"):
        direct = os.environ.get(env_key)
        if direct and os.path.exists(direct):
            return direct
    local = os.environ.get("LOCALAPPDATA") or ""
    if not local:
        return None
    hits = glob.glob(os.path.join(local, "OpenAI", "Codex", "bin", "*", "codex.exe"))
    hits = [h for h in hits if os.path.exists(h)]
    if not hits:
        return None
    hits.sort(key=os.path.getmtime, reverse=True)
    return hits[0]


EXE = find_codex()
if not EXE:
    print("SKIP 未找到 codex.exe（可设 CODEX_BIN 指定路径）")
    raise SystemExit(0)

out = subprocess.run([EXE, "debug", "models"], capture_output=True, text=True,
                     encoding="utf-8", errors="replace", timeout=120)
raw = out.stdout or ""

# 输出可能包含多段 JSON（每个模型一段），逐个解析
decoder = json.JSONDecoder()
pos = raw.find("[")
models = []
while pos != -1 and pos < len(raw):
    try:
        obj, end = decoder.raw_decode(raw, pos)
        if isinstance(obj, list):
            models.extend(obj)
        pos = raw.find("[", end)
    except json.JSONDecodeError:
        pos = raw.find("[", pos + 1)

print("解析到模型数:", len(models))
print()
print("%-24s %-30s %s" % ("slug", "display_name", "description"))
print("-" * 100)

ok = True
for m in models:
    dn = m.get("display_name") or ""
    de = m.get("description") or ""
    print("%-24s %-34s %s" % (m.get("slug"), dn, de))

    # display_name 必须同时带窗口和倍率，形如 "(1M · x0.11)"
    if "(" not in dn or ")" not in dn:
        ok = False
        print("   !! display_name 缺少括号后缀")
    else:
        suffix = dn[dn.rfind("(") + 1:dn.rfind(")")]
        if "K" not in suffix and "M" not in suffix:
            ok = False
            print("   !! display_name 缺少上下文窗口")
        if "x" not in suffix:
            ok = False
            print("   !! display_name 缺少积分倍率")

    # description 保留完整信息
    if "credits" not in de:
        ok = False
        print("   !! description 缺少倍率")

print()
print("ALL PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
