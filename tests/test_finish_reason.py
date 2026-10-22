"""验证 finish_reason 的处理链路是否完整。

三个环节都要对：
  1. 网关从上游 SSE 里正确读出 finish_reason
  2. Responder.finish() 据此标记 status
  3. 客户端收到 incomplete 时能感知
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wbcore import proto as wbproto

FAILS = []


def check(name, ok, detail=""):
    print("%-46s %s %s" % (name, "PASS" if ok else "FAIL", detail))
    if not ok:
        FAILS.append(name)


def parse_completed(fin: bytes):
    """从 finish() 输出里取出 response.completed 的 response 对象。"""
    text = fin.decode("utf-8")
    for block in text.split("\n\n"):
        if "response.completed" not in block:
            continue
        data = None
        for line in block.split("\n"):
            if line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if data:
            return data.get("response") or {}
    return {}


def main():
    print("=== 1. finish_reason=length 应标记 incomplete ===")
    r = wbproto.Responder("m")
    r.feed({"content": "我会完成首屏与移动端"}, "length")
    resp = parse_completed(r.finish())
    check("status 为 incomplete", resp.get("status") == "incomplete", str(resp.get("status")))
    check("incomplete_details.reason 正确",
          (resp.get("incomplete_details") or {}).get("reason") == "max_output_tokens",
          str(resp.get("incomplete_details")))

    print()
    print("=== 2. finish_reason=stop 仍为 completed ===")
    r2 = wbproto.Responder("m")
    r2.feed({"content": "正常回答"}, "stop")
    resp2 = parse_completed(r2.finish())
    check("status 为 completed", resp2.get("status") == "completed", str(resp2.get("status")))
    check("无 incomplete_details", "incomplete_details" not in resp2)

    print()
    print("=== 3. finish_reason=tool_calls 为 completed ===")
    r3 = wbproto.Responder("m")
    r3.feed({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                             "function": {"name": "f", "arguments": "{}"}}]}, "tool_calls")
    resp3 = parse_completed(r3.finish())
    check("status 为 completed", resp3.get("status") == "completed", str(resp3.get("status")))
    out_types = [it.get("type") for it in resp3.get("output") or []]
    check("含 function_call 项", "function_call" in out_types, str(out_types))

    print()
    print("=== 4. 解析上游 SSE 的 finish_reason ===")
    # 模拟上游 chunk
    chunk = {
        "choices": [{"index": 0, "delta": {"content": "abc"}, "finish_reason": "length"}],
    }
    raw = ("data: " + json.dumps(chunk) + "\n\n").encode()
    events, _ = wbproto.parse_sse_lines(raw)
    check("parse_sse_lines 读出事件", len(events) == 1)
    fr = events[0]["choices"][0].get("finish_reason")
    check("finish_reason 为 length", fr == "length", str(fr))

    print()
    print("=== 5. 空流不应被误判 ===")
    r5 = wbproto.Responder("m")
    resp5 = parse_completed(r5.finish())
    check("空流也是 completed", resp5.get("status") == "completed")

    print()
    print("ALL PASS" if not FAILS else "FAILED: %s" % FAILS)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
