"""上下文/token 计算相关的完整回归。

覆盖已修复的 4 个问题：
  1. 预检把 base64 图片当文本 -> 误报超限
  2. tool 结果里的图片被 json.dumps 成字符串 -> 上游按文本计费
  3. 消息里的图片被降级成 "[image]" -> 模型看不到图
  4. max_output_tokens 超出模型能力 -> 上游拒绝
"""
import base64
import json
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wbcore import proto as wbproto
from wbcore import tokens as g

FAILS = []


def check(name, ok, detail=""):
    print("%-52s %s %s" % (name, "PASS" if ok else "FAIL", detail))
    if not ok:
        FAILS.append(name)


def make_test_png(width: int, height: int) -> bytes:
    """生成一张最小的合法 PNG（纯色），用于测试图片尺寸解析。

    不依赖任何外部文件或本机截图，保证克隆仓库后测试可直接运行。
    """
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b""
    for _ in range(height):
        raw += b"\x00" + b"\xc0\x40\x80" * width
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def main():
    raw = make_test_png(1440, 900)
    b64 = base64.b64encode(raw).decode()
    data_url = "data:image/png;base64," + b64

    print("=== 1. 预检估算 ===")
    chat_img = {
        "model": "deepseek-v4.1-flash",
        "messages": [
            {"role": "user", "content": "看图"},
            {"role": "tool", "tool_call_id": "c1",
             "content": [{"type": "image_url", "image_url": {"url": data_url}}]},
        ],
    }
    est = g._estimate_tokens(chat_img)
    check("图片按尺寸估算而非 base64", est < 3000, f"{est:,} tokens")

    plain = {"messages": [{"role": "user", "content": "z" * 40000}]}
    check("纯文本仍是 4 字节/token", 9000 < g._estimate_tokens(plain) < 11000)

    print()
    print("=== 2. tool 结果里的图片保留结构 ===")
    req = {
        "model": "m",
        "input": [
            # 必须先声明 function_call，否则孤立的 output 会被有意丢弃
            {"type": "function_call", "call_id": "c1", "name": "view_image",
             "arguments": '{"path":"x.png"}'},
            {"type": "function_call_output", "call_id": "c1",
             "output": [{"type": "input_image", "image_url": data_url}]},
        ],
    }
    chat, _ = wbproto.responses_to_chat(req)
    roles = [m.get("role") for m in chat["messages"]]
    check("首条是 assistant.tool_calls", roles[0] == "assistant", str(roles))
    tool_msg = chat["messages"][1]
    check("第二条是 tool", tool_msg.get("role") == "tool", str(roles))
    check("content 是数组而非字符串", isinstance(tool_msg["content"], list))
    if isinstance(tool_msg["content"], list):
        kinds = [c.get("type") for c in tool_msg["content"]]
        check("含 image_url 结构", "image_url" in kinds, str(kinds))
        url = tool_msg["content"][0].get("image_url", {}).get("url", "")
        check("data URI 完整保留", url.startswith("data:image/png;base64,"))

    print()
    print("=== 3. 消息里的图片不再降级 ===")
    req2 = {
        "model": "m",
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "这是什么"},
                {"type": "input_image", "image_url": data_url},
            ]}
        ],
    }
    chat2, _ = wbproto.responses_to_chat(req2)
    msg = chat2["messages"][0]
    check("用户消息 content 是多模态数组", isinstance(msg["content"], list))
    if isinstance(msg["content"], list):
        kinds = [c.get("type") for c in msg["content"]]
        check("保留 image_url", "image_url" in kinds, str(kinds))
        check("没有 [image] 占位符",
              not any("[image]" in str(c.get("text") or "") for c in msg["content"]))

    print()
    print("=== 4. max_tokens 截断 ===")
    c4 = wbproto.clamp_max_tokens({"max_tokens": 999999}, 64000)
    check("超出上限被截断", c4["max_tokens"] == 64000, str(c4["max_tokens"]))
    c5 = wbproto.clamp_max_tokens({"max_tokens": 1000}, 64000)
    check("未超上限不变", c5["max_tokens"] == 1000)
    c6 = wbproto.clamp_max_tokens({"max_tokens": 999999}, 0)
    check("上限未知时不截断", c6["max_tokens"] == 999999)

    print()
    print("=== 5. 纯文本仍走字符串（兼容性）===")
    req3 = {"model": "m", "input": [
        {"type": "message", "role": "user", "content": "纯文本"}]}
    chat3, _ = wbproto.responses_to_chat(req3)
    check("纯文本 content 是 str", isinstance(chat3["messages"][0]["content"], str))

    print()
    print("=== 6. system_as_user 与多模态并存 ===")
    req4 = {
        "model": "m",
        "input": [
            {"type": "message", "role": "developer", "content": [
                {"type": "input_text", "text": "你是助手"}]},
            {"type": "message", "role": "user", "content": [
                {"type": "input_image", "image_url": data_url}]},
        ],
    }
    chat4, _ = wbproto.responses_to_chat(req4, system_as_user=True)
    roles = [m["role"] for m in chat4["messages"]]
    check("developer 降级为 user", roles[0] == "user", str(roles))
    check("图片消息独立保留", isinstance(chat4["messages"][1]["content"], list))

    print()
    print("ALL PASS" if not FAILS else "FAILED: %s" % FAILS)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
