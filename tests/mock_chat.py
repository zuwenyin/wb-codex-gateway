"""A tiny OpenAI-compatible upstream used to test the gateway end to end."""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b"{}"
        req = json.loads(body)

        with open(os.path.join(HERE, "upstream-seen.log"), "ab") as f:
            f.write(b"\n=== " + self.path.encode() + b" ===\n")
            f.write(json.dumps(req, ensure_ascii=False)[:6000].encode("utf-8") + b"\n")

        # 如果最后一条是 tool 结果，就回一句总结；否则触发一次工具调用
        has_tool_result = any(m.get("role") == "tool" for m in req.get("messages", []))
        tools = req.get("tools") or []
        want_stream = req.get("stream") is not False

        # 非流式（账号连通性测试走这条路径）
        if not want_stream:
            payload = {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": req.get("model", "mock"),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "PONG"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            }
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def emit(obj):
            line = ("data: %s\n\n" % json.dumps(obj, ensure_ascii=False)).encode()
            self.wfile.write(b"%x\r\n" % len(line) + line + b"\r\n")
            self.wfile.flush()

        base = {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": req.get("model", "mock"),
        }

        if tools and not has_tool_result and req.get("tool_choice") == "auto":
            emit(dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                {"index": 0, "id": "call_mock_1", "type": "function",
                 "function": {"name": "exec_command", "arguments": ""}}]}, "finish_reason": None}]))
            emit(dict(base, choices=[{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '{"cmd":"echo GATEWAY_OK","yield_time_ms":10000}'}}]},
                "finish_reason": None}]))
            emit(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]))
        else:
            for word in ["GATE", "WAY_", "OK"]:
                emit(dict(base, choices=[{"index": 0, "delta": {"content": word}, "finish_reason": None}]))
            emit(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]))

        emit(dict(base, choices=[], usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}))
        tail = b"data: [DONE]\n\n"
        self.wfile.write(b"%x\r\n" % len(tail) + tail + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
