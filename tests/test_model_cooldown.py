"""验证「按模型冷却」：一个模型额度用完，不该冻结整个账号。

真实 bug：上游 6004 是按模型计额的，
但旧逻辑让整个账号冷却 20 小时，导致 502「没有可用账号」，
而实际上换一个模型就能继续用。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from wbcore import store as wbstore

FAILS = []


def check(name, ok, detail=""):
    print("%-52s %s %s" % (name, "PASS" if ok else "FAIL", detail))
    if not ok:
        FAILS.append(name)


QUOTA_MSG = (
    '{"code":6004,"msg":"您的使用量已超出频率限制，'
    '将在 2026-10-21 09:42:19 UTC+8 重置，您也可以切换其他模型继续使用。"}'
)


def main():
    tmp = os.path.join(HERE, "data-test-modelcd.json")
    if os.path.exists(tmp):
        os.remove(tmp)
    s = wbstore.Store(tmp)
    s.update_settings({"policy": "round_robin", "cooldown_sec": 60,
                       "max_consecutive_failures": 100})

    a = s.add_account({"name": "A", "api_key": "k1"})
    b = s.add_account({"name": "B", "api_key": "k2"})

    print("=== 1. 配额耗尽只冷却该模型 ===")
    st = s.record_failure(a["id"], 429, QUOTA_MSG, model="deepseek-v4.1-flash")
    check("返回 model_cooling", st == "model_cooling", st)

    acc = s.get_account(a["id"])
    check("账号仍启用", acc["enabled"] is True)
    check("账号级 cooling_until 未设置", acc["cooling_until"] == 0,
          str(acc["cooling_until"]))
    cds = acc.get("model_cooldowns") or {}
    check("该模型进入冷却", cds.get("deepseek-v4.1-flash", 0) > time.time())
    check("连续失败被清零（不误禁用）", acc["consecutive_failures"] == 0)

    print()
    print("=== 2. 受限模型下单账号被排除 ===")
    cands = s.candidate_order(model="deepseek-v4.1-flash")
    names = [c["name"] for c in cands]
    check("A 被排除", "A" not in names, str(names))
    check("B 仍在", "B" in names, str(names))

    print()
    print("=== 3. 其它模型仍可用 A ===")
    cands2 = s.candidate_order(model="glm-5.3")
    names2 = [c["name"] for c in cands2]
    check("A 在 glm-5.3 上可用", "A" in names2, str(names2))
    check("B 也在", "B" in names2, str(names2))

    print()
    print("=== 4. 两个账号同一模型都耗尽 ===")
    s.record_failure(b["id"], 429, QUOTA_MSG, model="deepseek-v4.1-flash")
    cands3 = s.candidate_order(model="deepseek-v4.1-flash")
    check("该模型下无可用账号", len(cands3) == 0, str([c["name"] for c in cands3]))
    cands4 = s.candidate_order(model="kimi-k3-1")
    check("别的模型仍有两个账号", len(cands4) == 2, str([c["name"] for c in cands4]))

    print()
    print("=== 5. 普通 429 仍走账号冷却 ===")
    c = s.add_account({"name": "C", "api_key": "k3"})
    st2 = s.record_failure(c["id"], 429, '{"code":429,"msg":"rate limit"}',
                           model="deepseek-v4.1-flash")
    check("返回 cooling（非 model_cooling）", st2 == "cooling", st2)
    acc_c = s.get_account(c["id"])
    check("账号级冷却已设置", acc_c["cooling_until"] > time.time())

    print()
    print("=== 6. 模型冷却到期自动解除 ===")
    s.clear_model_cooldowns(a["id"])
    cands5 = s.candidate_order(model="deepseek-v4.1-flash")
    check("清除后 A 恢复", any(x["name"] == "A" for x in cands5),
          str([x["name"] for x in cands5]))

    print()
    print("=== 7. 冷却状态可查询 ===")
    state = s.model_cooldown_state()
    check("返回了状态", isinstance(state, dict))
    check("B 仍在冷却", b["id"] in state, str(list(state.keys())))

    os.remove(tmp)
    print()
    print("ALL PASS" if not FAILS else "FAILED: %s" % FAILS)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
