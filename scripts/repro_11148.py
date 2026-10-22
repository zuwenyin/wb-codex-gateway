"""复现 11148：用真实会话的序列走一遍转换，检查生成的 Chat 消息是否合法。

上游规则（Chat Completions）：
  - assistant.tool_calls 里的每个 id，后面必须紧跟一条 role=tool、tool_call_id 相同的消息
  - tool 消息不能出现在对应 assistant 之前
  - tool 消息的数量/顺序必须能对应上

Codex 的实际序列是"并行调用"：CALL A, CALL B, CALL C, OUT A, OUT B, OUT C
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wbcore import proto as wbproto

d = os.path.expanduser(r"~\.codex\sessions")
files = []
for root, dirs, fs in os.walk(d):
    for f in fs:
        if f.endswith(".jsonl"):
            p = os.path.join(root, f)
            files.append((os.path.getsize(p), p))
files.sort(reverse=True)
path = files[0][1] if files[0][0] > 5 * 1024 * 1024 else next(
    p for sz, p in files if sz > 5 * 1024 * 1024
)

print("用真实会话构造 Responses 请求:", os.path.basename(path)[:50])

# 把会话还原成 Codex 会发的那种 input 数组
inputs = []
for line in open(path, encoding="utf-8", errors="replace"):
    try:
        obj = json.loads(line)
    except Exception:
        continue
    pay = obj.get("payload") or {}
    t = pay.get("type")
    if t == "message":
        inputs.append({"type": "message", "role": pay.get("role"),
                       "content": pay.get("content") or ""})
    elif t == "function_call":
        inputs.append({"type": "function_call", "call_id": pay.get("call_id"),
                       "name": pay.get("name"), "arguments": pay.get("arguments")})
    elif t == "function_call_output":
        inputs.append({"type": "function_call_output", "call_id": pay.get("call_id"),
                       "output": pay.get("output")})

print("input 条数:", len(inputs))

req = {"model": "deepseek-v4.1-flash", "input": inputs}
chat, _ = wbproto.responses_to_chat(req, system_as_user=True)

msgs = chat["messages"]
print("转换后消息数:", len(msgs))
print()

# 校验：每个 tool 消息必须紧跟在声明它的 assistant 之后
problems = []
open_calls = []          # 当前 assistant 声明、还没被消费的 call_id
tool_count = 0
for i, m in enumerate(msgs):
    role = m.get("role")
    if role == "assistant" and m.get("tool_calls"):
        if open_calls:
            problems.append(f"msg[{i}] 新的 assistant.tool_calls 出现时，上一批还有 {len(open_calls)} 个未配对: {open_calls[:3]}")
        open_calls = [tc["id"] for tc in m["tool_calls"]]
    elif role == "tool":
        tool_count += 1
        cid = m.get("tool_call_id")
        if not open_calls:
            problems.append(f"msg[{i}] tool 消息 {cid} 前面没有 assistant.tool_calls")
        elif cid not in open_calls:
            problems.append(f"msg[{i}] tool_call_id={cid} 不在待配对列表 {open_calls[:3]}")
        else:
            open_calls.remove(cid)
    elif role in ("user", "system"):
        if open_calls:
            problems.append(f"msg[{i}] role={role} 打断配对，仍有 {len(open_calls)} 个未配对: {open_calls[:3]}")
            open_calls = []

if open_calls:
    problems.append(f"结尾仍有 {len(open_calls)} 个 tool_call 没有结果: {open_calls[:3]}")

print(f"tool 消息数: {tool_count}")
print(f"问题数: {len(problems)}")
for p in problems[:10]:
    print("  !!", p)

print()
print("ALL PASS" if not problems else "FAILED —— 这就会触发上游 11148")
