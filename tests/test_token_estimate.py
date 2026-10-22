"""验证 token 估算的正确性，重点是图片计费。

真实 bug：旧逻辑用 len(json)/4 估算，把 base64 图片当文本算 ——
一张 1440x900 截图 base64 约 1.4MB，会被算成 35 万 tokens，
于是「Codex 显示只用 16%」的会话被网关误判超限并拦下。

正确算法（OpenAI 官方）：token 数只取决于【图片尺寸】，与压缩后
的字节数无关。这里直接验证该属性，不依赖任何外部图片文件。
"""
import base64
import json
import math
import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from wbcore import tokens as g

FAILS = []


def check(name, ok, detail=""):
    print("%-52s %s %s" % (name, "PASS" if ok else "FAIL", detail))
    if not ok:
        FAILS.append(name)


def make_png(width: int, height: int, noise: bool = False) -> bytes:
    """生成一张合法 PNG。noise=True 填充随机数据，压缩后体积大很多。"""
    import random as _r

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = bytearray()
    for _ in range(height):
        raw.append(0)  # PNG filter type: None
        for _ in range(width):
            if noise:
                raw.extend(bytes([_r.randrange(256),
                                  _r.randrange(256),
                                  _r.randrange(256)]))
            else:
                raw.extend(b"\xc0\x40\x80")
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 1))
            + chunk(b"IEND", b""))


def img_msg(w: int, h: int, noise: bool = False) -> dict:
    raw = make_png(w, h, noise=noise)
    return wrap("data:image/png;base64," + base64.b64encode(raw).decode()) 


def wrap(url: str) -> dict:
    return {"messages": [{"role": "tool", "tool_call_id": "c1",
                          "content": [{"type": "image_url", "image_url": {"url": url}}]}]}


def main():
    print("=== 1. 图片 token 只取决于尺寸，与字节数无关 ===")
    # 同尺寸 1440x900，一份纯色（压缩后极小），一份噪声（压缩后很大）
    est_flat = g._estimate_tokens(img_msg(1440, 900, noise=False))
    est_noisy = g._estimate_tokens(img_msg(1440, 900, noise=True))
    print(f"  纯色图估算: {est_flat:,} tokens")
    print(f"  噪声图估算: {est_noisy:,} tokens")
    check("同尺寸图片估算一致", est_flat == est_noisy, f"{est_flat:,} vs {est_noisy:,}")

    print()
    print("=== 2. 公式 tiles*170+85 ===")
    for w, h in ((1440, 900), (800, 600), (256, 256), (4096, 2160)):
        ww, hh = w, h
        if ww > 2048 or hh > 2048:
            s = 2048 / max(ww, hh)
            ww, hh = round(ww * s), round(hh * s)
        short = min(ww, hh)
        if short > 768:
            s = 768 / short
            ww, hh = round(ww * s), round(hh * s)
        want = math.ceil(ww / 512) * math.ceil(hh / 512) * 170 + 85
        got = g._estimate_tokens(img_msg(w, h))
        check(f"{w}x{h} 公式正确", abs(got - want) <= 2, f"got={got:,} want={want:,}")

    print()
    print("=== 3. 纯文本 4 字节/token ===")
    plain = {"messages": [{"role": "user", "content": "y" * 40000}]}
    estp = g._estimate_tokens(plain)
    check("纯文本估算约 10K", 9000 < estp < 11000, f"实际 {estp:,}")

    print()
    print("=== 4. 坏图片保守值 ===")
    weird = {"messages": [{"role": "tool", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,ZZZZ"}}]}]}
    estw = g._estimate_tokens(weird)
    check("坏图片给保守值", estw <= 1200, f"实际 {estw:,}")

    print()
    print("ALL PASS" if not FAILS else "FAILED: %s" % FAILS)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
