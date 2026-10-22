"""验证失败分类：账号问题 / 请求问题 / 链路抖动 三类互不混淆。

真实 bug：SSL EOF 被误判成"请求错误"直接抛给客户端，
导致 Codex 里频繁显示「上游返回 HTTP None」并中断会话。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from wbcore import faults as g

FAILS = []


def check(name, ok, detail=""):
    print("%-46s %s %s" % (name, "PASS" if ok else "FAIL", detail))
    if not ok:
        FAILS.append(name)


SSL_EOF = (
    "<urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] "
    "EOF occurred in violation of protocol (_ssl.c:1010)>"
)

CASES = [
    # (status, detail, 期望是账号问题, 期望是链路抖动, 说明)
    (None, SSL_EOF, False, True, "SSL EOF"),
    (None, "ConnectionResetError: [WinError 10054]", False, True, "连接重置"),
    (None, "ConnectionAbortedError: [WinError 10053]", False, True, "连接中止"),
    (None, "<urlopen error timed out>", True, False, "超时算账号问题"),
    (400, '{"code":11115,"msg":"prompt is too long"}', False, False, "上下文超长=请求问题"),
    (400, '{"code":11150}', False, False, "参数非法=请求问题"),
    (429, "rate limited", True, False, "限流=账号问题"),
    (401, "unauthorized", True, False, "鉴权=账号问题"),
    (500, "server error", True, False, "5xx=账号问题"),
]


def main():
    for st, detail, want_acct, want_trans, label in CASES:
        acct = g._is_account_fault(st, detail)
        trans = g._is_transient_network(st, detail)

        check("%s -> 账号问题=%s" % (label, want_acct), acct == want_acct, "实际 %s" % acct)
        check("%s -> 链路抖动=%s" % (label, want_trans), trans == want_trans, "实际 %s" % trans)

        # 关键不变量：不能同时被判定为两类
        check("%s -> 分类互斥" % label, not (acct and trans))

    # 核心回归：SSL EOF 必须走"重试"路径，而不是"直接返回错误"
    check("SSL EOF 不会被当成请求错误",
          not g._is_account_fault(None, SSL_EOF) and g._is_transient_network(None, SSL_EOF))

    # 重试次数至少 3 次
    src = open(os.path.join(ROOT, "wbcore", "upstream.py"), encoding="utf-8").read()
    check("call_upstream 重试 >= 3 次", "attempts = 3" in src)

    print()
    print("ALL PASS" if not FAILS else "FAILED: %s" % FAILS)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
