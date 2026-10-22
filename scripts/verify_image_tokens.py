"""端到端验证：修复后的网关是否让图片按"图片"计费，而不是按文本。

之前：input_image 被 json.dumps 成字符串 -> 上游当文本算 -> 撑爆上下文
之后：保留 image_url 结构 -> 上游按图片算 -> 约 1000 tokens/张
"""
import base64
import json
import os
import struct
import sys
import urllib.error
import urllib.request
import zlib

GW = "http://127.0.0.1:18080/v1/responses"
TOKEN = "wb-local-8f3a2c"


def call(body: dict) -> str:
    """把 Responses 请求发给本机网关，返回原始响应体（流式文本）。"""
    req = urllib.request.Request(
        GW,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + TOKEN,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read().decode("utf-8", "replace")


def make_test_png(width: int, height: int) -> bytes:
    """生成一张最小的合法 PNG（纯色），用于测试图片尺寸解析。

    不依赖任何外部文件或本机截图，保证克隆仓库后测试可直接运行。
    """
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
    print(f"测试图片: {len(raw)/1024:.0f} KB -> base64 {len(b64)/1024:.0f} KB")

    # 模拟 Codex 的 view_image 工具返回
    body = {
        "model": "deepseek-v4.1-flash",
        "input": [
            {"type": "message", "role": "user", "content": "这张截图里有什么？一句话"},
            {"type": "function_call", "call_id": "c1", "name": "view_image",
             "arguments": '{"path":"desktop.png"}'},
            {"type": "function_call_output", "call_id": "c1", "output": [
                {"type": "input_image", "image_url": "data:image/png;base64," + b64}
            ]},
        ],
    }

    size = len(json.dumps(body).encode())
    print(f"Responses 请求体: {size/1048576:.2f} MB")
    print()

    try:
        text = call(body)
        print("网关响应长度:", len(text))
        # 找 response.completed 里的 usage
        for line in text.split("\n"):
            if '"usage"' in line and "input_tokens" in line:
                i = line.find("{")
                obj = json.loads(line[i:])
                resp = obj.get("response") or {}
                u = resp.get("usage") or {}
                print("usage:", json.dumps(u, ensure_ascii=False))
                it = u.get("input_tokens") or 0
                print()
                if it < 5000:
                    print(f"PASS  input_tokens={it:,}（图片按图片计费）")
                else:
                    print(f"FAIL  input_tokens={it:,}（仍按文本算，体积爆了）")
                return
        print("未找到 usage，片段:", text[:300])
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read().decode("utf-8", "replace")[:400])
    except Exception as e:
        print("ERR", str(e)[:200])


if __name__ == "__main__":
    main()
