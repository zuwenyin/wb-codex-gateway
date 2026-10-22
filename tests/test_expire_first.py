"""expire_first 策略排序逻辑的单元测试（不依赖网络与网关进程）。

用法：python test_expire_first.py
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
    print("%-30s %s %s" % (name, "PASS" if ok else "FAIL", detail if not ok else ""))
    if not ok:
        FAILS.append(name)


def fresh():
    tmp = os.path.join(HERE, "data-test-expire.json")
    if os.path.exists(tmp):
        os.remove(tmp)
    return wbstore.Store(tmp), tmp


def main():
    s, tmp = fresh()
    s.update_settings({"policy": "expire_first"})

    a_far = s.add_account({"name": "晚过期", "api_key": "k1"})
    a_near = s.add_account({"name": "早过期", "api_key": "k2"})
    a_unknown = s.add_account({"name": "无信息", "api_key": "k3"})
    a_empty = s.add_account({"name": "已耗尽", "api_key": "k4"})

    now = int(time.time() * 1000)
    day = 86400 * 1000
    s.set_credit_info(a_far["id"], now + 30 * day, 500)
    s.set_credit_info(a_near["id"], now + 3 * day, 100)
    s.set_credit_info(a_unknown["id"], 0, 0)
    s.set_credit_info(a_empty["id"], now + 5 * day, 0)

    order = [a["name"] for a in s.candidate_order()]
    print("排序:", order)

    check("最早过期排第一", order[0] == "早过期", order)
    check("次早排第二", order[1] == "晚过期", order)
    check("额度为 0 排后", order.index("已耗尽") > order.index("晚过期"), order)
    check("无信息排后", order.index("无信息") > order.index("晚过期"), order)

    # 只剩额度为 0 的账号时，不应崩溃，仍返回全部可选账号
    s.update_account(a_far["id"], {"enabled": False})
    s.update_account(a_near["id"], {"enabled": False})
    rest = [a["name"] for a in s.candidate_order()]
    check("仅剩无效账号仍可用", len(rest) == 2, rest)

    # 已过期的积分包不应排在有效包前面
    s.update_account(a_far["id"], {"enabled": True})
    s.update_account(a_near["id"], {"enabled": True})
    s.update_account(a_unknown["id"], {"enabled": True})
    s.update_account(a_empty["id"], {"enabled": True})
    s.set_credit_info(a_far["id"], now - day, 500)   # 已过期
    s.set_credit_info(a_near["id"], now + 3 * day, 100)
    order2 = [a["name"] for a in s.candidate_order()]
    print("含过期包时:", order2)
    check("过期包不优先", order2[0] == "早过期", order2)

    # 切回轮询应恢复轮换
    s.update_settings({"policy": "round_robin"})
    r1 = [a["name"] for a in s.candidate_order()]
    r2 = [a["name"] for a in s.candidate_order()]
    check("轮询仍然轮换", r1 != r2, "%s vs %s" % (r1, r2))

    os.remove(tmp)
    print()
    print("ALL PASS" if not FAILS else "FAILED: %s" % FAILS)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
