"""验证：账号配额耗尽（429/6004）时，网关的行为是否会让 Codex 反复重试。

真实场景：任务跑到一半，当前账号额度耗尽，网关换号；
如果两个号都耗尽，用户看到的就是「任务没完成就结束」——
因为每轮请求都被 429 打断，而模型刚输出半句话就断了。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from wbcore import store as wbstore

FAILS = []


def _fake_reset_time(hours: int) -> str:
    """按 UTC+8 时钟生成 hours 小时后的重置时间，与线上 429 文案格式一致。"""
    from datetime import datetime, timedelta, timezone

    later = datetime.now(timezone.utc) + timedelta(hours=hours + 8)
    return later.strftime("%Y-%m-%d %H:%M:%S")


def check(name, ok, detail=""):
    print("%-50s %s %s" % (name, "PASS" if ok else "FAIL", detail))
    if not ok:
        FAILS.append(name)


def main():
    import os
    tmp = os.path.join(HERE, "data-test-429.json")
    if os.path.exists(tmp):
        os.remove(tmp)
    s = wbstore.Store(tmp)
    s.update_settings({"policy": "round_robin", "cooldown_sec": 60,
                       "max_consecutive_failures": 100})

    a = s.add_account({"name": "A", "api_key": "k1"})
    b = s.add_account({"name": "B", "api_key": "k2"})

    print("=== 429 应让账号进入冷却 ===")
    st = s.record_failure(a["id"], 429, '{"code":6004,"msg":"quota exceeded"}')
    check("返回 cooling", st == "cooling", st)
    info = s.get_account(a["id"])
    check("cooling_until 被设置", info["cooling_until"] > 0)
    check("last_error 是短句", len(info.get("last_error") or "") < 120,
          str(info.get("last_error")))

    print()
    print("=== 冷却期内候选列表应只剩 B ===")
    cands = s.candidate_order()
    names = [c["name"] for c in cands]
    check("A 被排除", "A" not in names, str(names))
    check("B 仍在", "B" in names, str(names))

    print()
    print("=== 两个号都 429 -> 无可用账号 ===")
    s.record_failure(b["id"], 429, '{"code":6004}')
    cands2 = s.candidate_order()
    check("候选为空", len(cands2) == 0, str([c["name"] for c in cands2]))
    broken = s.count_broken()
    check("broken 计数反映问题", broken["total"] == 2, str(broken))

    print()
    print("=== 配额耗尽：按模型冷却，不冻结整个账号 ===")
    # 重置时间必须是"未来"：写死日期的用例过了那天就会假失败
    reset_at = _fake_reset_time(hours=2)
    msg = ('{"code":6004,"msg":"您的使用量已超出频率限制，'
           '将在 %s UTC+8 重置"}' % reset_at)
    parse = wbstore._quota_reset_delay(msg)
    check("能解析出重置时间", parse > 3600, f"{parse}s")

    c = s.add_account({"name": "C", "api_key": "k3"})
    st = s.record_failure(c["id"], 429, msg, model="deepseek-v4.1-flash")
    check("返回 model_cooling", st == "model_cooling", st)

    info_c = s.get_account(c["id"])
    cds = info_c.get("model_cooldowns") or {}
    left = int(cds.get("deepseek-v4.1-flash", 0)) - int(__import__("time").time())
    check("该模型冷却跟随重置时间", left > 3600, f"{left}s")
    check("账号本身未被冷却", info_c["cooling_until"] == 0, str(info_c["cooling_until"]))

    print()
    print("=== 普通 429 仍用短冷却 ===")
    d = s.add_account({"name": "D", "api_key": "k4"})
    s.record_failure(d["id"], 429, '{"code":429,"msg":"rate limit"}')
    info_d = s.get_account(d["id"])
    left_d = info_d["cooling_until"] - int(__import__("time").time())
    check("短冷却 <= 61s", left_d <= 61, f"{left_d}s")

    os.remove(tmp)
    print()
    print("ALL PASS" if not FAILS else "FAILED: %s" % FAILS)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
