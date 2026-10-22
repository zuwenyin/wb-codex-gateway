"""
wbproto — Responses API <-> Chat Completions 协议翻译层

Codex（ChatGPT 桌面 / CLI 0.163+）只会说 Responses 协议；WorkBuddy 云只说
Chat Completions。本模块负责两个方向的语义转换，与 HTTP / 账号池彻底解耦，
方便单独测试。
"""

from __future__ import annotations

import json
import time
import uuid

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def sse(event: str, payload: dict) -> bytes:
    """编码一个 Responses SSE 事件。"""
    return (
        "event: %s\ndata: %s\n\n"
        % (event, json.dumps(payload, ensure_ascii=False))
    ).encode("utf-8")


def content_to_parts(content):
    """Responses 的 content -> Chat 的 content。

    纯文本时返回字符串（兼容性最好）；含图片时返回多模态数组。
    早先所有图片都被压成 "[image]" 占位符，导致用户贴图会丢失内容。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("input_text", "output_text", "text"):
            text = part.get("text") or ""
            if text:
                parts.append({"type": "text", "text": text})
        elif ptype in ("input_image", "image_url", "image"):
            url = part.get("image_url") or part.get("url")
            if isinstance(url, dict):
                url = url.get("url")
            if isinstance(url, str) and url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
        elif ptype == "refusal":
            ref = part.get("refusal") or ""
            if ref:
                parts.append({"type": "text", "text": ref})

    if not parts:
        return ""
    if all(p.get("type") == "text" for p in parts):
        return "".join(p.get("text") or "" for p in parts)
    return parts


def content_to_text(content) -> str:
    """Responses 的 content 提取为纯文本（图片降级为占位符）。

    仅在确实需要字符串的场景使用；能保留结构的地方请用 content_to_parts。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("input_text", "output_text", "text"):
            chunks.append(part.get("text") or "")
        elif ptype == "input_image":
            chunks.append("[image]")
        elif ptype == "refusal":
            chunks.append(part.get("refusal") or "")
    return "".join(chunks)


def _safe_name(name: str) -> str:
    """Chat Completions 的函数名限制较严，统一净化。"""
    return "".join(c if (c.isalnum() or c in "_-") else "_" for c in name)[:64]


def _ensure_params(params) -> dict:
    if not isinstance(params, dict) or not params:
        params = {"type": "object", "properties": {}}
    params.setdefault("type", "object")
    params.setdefault("properties", {})
    return params


# ---------------------------------------------------------------------------
# 工具定义转换
# ---------------------------------------------------------------------------


def _tool_output_to_content(out):
    """把 Responses 的 function_call_output 转成 Chat 的 tool content。

    关键：Codex 的截图是 `input_image` 结构，嵌着 data:image/png;base64。
    如果直接 json.dumps 成字符串，上游会把它当**纯文本**算 token ——
    一张 1.4MB 的截图能算出几十万 tokens，直接把上下文撑爆。
    必须还原成 `image_url` 多模态结构，上游才会按图片规则（约 1000 tokens）计。

    纯文本/纯 JSON 的输出保持字符串，不额外包装。
    """
    # 情形 1：纯字符串，直接用
    if isinstance(out, str):
        return out

    if out is None:
        return ""

    # 情形 2：数组（Codex 的多模态输出）
    if isinstance(out, list):
        parts = []
        for item in out:
            if not isinstance(item, dict):
                parts.append({"type": "text", "text": str(item)})
                continue

            itype = item.get("type")
            if itype in ("input_image", "image_url", "image"):
                url = item.get("image_url") or item.get("url") or ""
                if isinstance(url, dict):
                    url = url.get("url") or ""
                if isinstance(url, str) and url:
                    parts.append({"type": "image_url", "image_url": {"url": url}})
            elif itype in ("input_text", "text", "output_text"):
                parts.append({"type": "text", "text": item.get("text") or ""})
            else:
                # 未知结构：保底转文本，但去掉可能的巨量 base64
                parts.append({"type": "text", "text": json.dumps(item, ensure_ascii=False)})

        if not parts:
            return ""
        # 全是文本时退回纯字符串，兼容性最好
        if all(p.get("type") == "text" for p in parts):
            return "".join(p.get("text") or "" for p in parts)
        return parts

    # 情形 3：单个对象
    if isinstance(out, dict):
        itype = out.get("type")
        if itype in ("input_image", "image_url", "image"):
            url = out.get("image_url") or out.get("url") or ""
            if isinstance(url, dict):
                url = url.get("url") or ""
            if isinstance(url, str) and url:
                return [{"type": "image_url", "image_url": {"url": url}}]
        if itype in ("input_text", "text", "output_text"):
            return out.get("text") or ""

    return json.dumps(out, ensure_ascii=False)


def flatten_tools(tools, include_namespaces: bool = False):
    """把 Codex 的 tools 转成 Chat Completions 格式。

    返回 (chat_tools, name_map)：
      chat_tools —— 可直接塞进请求体的工具数组
      name_map   —— 发出去的函数名 -> 原始 (namespace, 子工具名)，用于回程还原

    Codex 会发两种：`function`（顶层）与 `namespace`（内嵌若干子工具）。
    namespace 默认**不展开**：展开后模型若调用，Codex 未必认得子工具名，
    属于有风险的优化，交给设置项 include_namespaces 显式开启。
    """
    out = []
    name_map = {}

    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        ttype = tool.get("type")

        if ttype == "function" and tool.get("name"):
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": (tool.get("description") or "")[:4000],
                        "parameters": _ensure_params(tool.get("parameters")),
                    },
                }
            )

        elif ttype == "namespace" and tool.get("name") and include_namespaces:
            prefix = tool["name"]
            for sub in tool.get("tools") or []:
                if not isinstance(sub, dict):
                    continue
                if sub.get("type") != "function" or not sub.get("name"):
                    continue
                emitted = _safe_name("%s__%s" % (prefix, sub["name"]))
                name_map[emitted] = (prefix, sub["name"])
                out.append(
                    {
                        "type": "function",
                        "function": {
                            "name": emitted,
                            "description": (sub.get("description") or "")[:4000],
                            "parameters": _ensure_params(sub.get("parameters")),
                        },
                    }
                )

    return out, name_map


# ---------------------------------------------------------------------------
# 请求转换
# ---------------------------------------------------------------------------


def responses_to_chat(
    body: dict,
    send_usage: bool = True,
    include_namespaces: bool = False,
    system_as_user: bool = False,
):
    """Responses 请求体 -> Chat Completions 请求体。

    system_as_user
        把 system 角色降级为 user。WorkBuddy 云网关对 `role: system` 一律返回
        400 / code 11128「Illegal API invocation from an unapproved channel」，
        与长度无关（500 字符也照样被拒）；改成 user 后同样的 prompt 可正常通过。
        Codex 的整段系统提示因此会被折叠进用户消息。
    """
    messages = []

    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        role = "user" if system_as_user else "system"
        messages.append({"role": role, "content": instructions})

    pending_calls = []

    def flush_calls():
        if pending_calls:
            messages.append(
                {"role": "assistant", "content": None, "tool_calls": list(pending_calls)}
            )
            pending_calls.clear()

    raw_input = body.get("input")
    if isinstance(raw_input, str):
        raw_input = [{"type": "message", "role": "user", "content": raw_input}]

    # ------------------------------------------------------------------
    # 两阶段：先把 function_call / function_call_output 按 call_id 收齐，
    # 再按正确顺序落成 Chat 消息。
    #
    # 为什么要这么做：Codex 的并行调用序列是
    #     CALL A, CALL B, CALL C, OUT A, OUT B, OUT C
    # 而 Chat 协议要求
    #     assistant{tool_calls:[A,B,C]}, tool A, tool B, tool C
    # 如果一边遍历一边 flush，遇到插入的 user 消息就会把 CALL 提前发出去，
    # 后续 OUT 变成孤儿 —— 上游直接报 11148
    # "tool calls and tool results do not match"。
    # ------------------------------------------------------------------
    pending_calls = []          # 尚未落盘的 tool_call
    call_ids = set()            # 已声明过的 call_id
    deferred_outputs = {}       # call_id -> content，先缓存，等 flush 时按序输出

    def flush_calls():
        """把累积的 tool_call 落成 assistant 消息，并紧跟对应的 tool 结果。"""
        if not pending_calls:
            return
        batch = list(pending_calls)
        pending_calls.clear()
        messages.append({"role": "assistant", "content": None, "tool_calls": batch})

        # 按 tool_calls 的声明顺序补结果，保证一一对应
        for tc in batch:
            cid = tc["id"]
            if cid in deferred_outputs:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": cid,
                        "content": deferred_outputs.pop(cid),
                    }
                )

    for item in raw_input or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")

        if itype == "message" or (itype is None and item.get("role")):
            # 只在没有挂起调用时才落盘；否则保持顺序（见上方说明）
            if not pending_calls:
                flush_calls()

            role = item.get("role") or "user"
            if role in ("developer", "system"):
                role = "system"
            if role == "system" and system_as_user:
                role = "user"

            # 用 content_to_parts：用户粘贴的图片必须保留结构，
            # 否则会退化成 "[image]" 占位符，模型看不到图。
            content = content_to_parts(item.get("content"))
            if not content:
                continue

            # system 文本可以追加合并；含图片的多模态内容不能合并
            if (
                isinstance(content, str)
                and messages
                and messages[-1]["role"] == role == "system"
                and isinstance(messages[-1]["content"], str)
            ):
                messages[-1]["content"] += "\n\n" + content
            else:
                messages.append({"role": role, "content": content})

        elif itype == "function_call":
            cid = item.get("call_id") or item.get("id") or ("call_" + uuid.uuid4().hex)
            args = item.get("arguments")
            if not isinstance(args, str):
                args = json.dumps(args or {}, ensure_ascii=False)

            # 同一个 call_id 只声明一次
            if cid in call_ids:
                continue
            call_ids.add(cid)

            pending_calls.append(
                {
                    "id": cid,
                    "type": "function",
                    "function": {"name": item.get("name") or "", "arguments": args},
                }
            )

        elif itype == "function_call_output":
            cid = item.get("call_id") or ""
            content = _tool_output_to_content(item.get("output"))

            if cid in call_ids:
                # 该 call 已声明过：缓存结果，等 flush 时按顺序落盘
                deferred_outputs[cid] = content
            # 没有对应声明的孤立结果直接忽略，避免污染消息序列

        # reasoning 回灌：上游普遍不接受，直接丢弃

    flush_calls()

    chat = {"model": body.get("model") or "", "messages": messages, "stream": True}
    if send_usage:
        chat["stream_options"] = {"include_usage": True}

    tools, name_map = flatten_tools(body.get("tools"), include_namespaces)
    if tools:
        chat["tools"] = tools
        chat["tool_choice"] = body.get("tool_choice") or "auto"
        if body.get("parallel_tool_calls") is not None:
            chat["parallel_tool_calls"] = body["parallel_tool_calls"]

    if body.get("max_output_tokens"):
        chat["max_tokens"] = body["max_output_tokens"]
    if body.get("temperature") is not None:
        chat["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        chat["top_p"] = body["top_p"]

    return chat, name_map


def clamp_max_tokens(chat: dict, model_output_limit: int) -> dict:
    """把 max_tokens 压到模型允许的输出上限内。

    Responses 的 max_output_tokens 可能比上游模型的实际能力大
    （例如 Codex 默认给 32768，而某些模型只支持 8192），
    上游会直接报 invalid_request_error。超了就按上限截断。
    """
    if not model_output_limit:
        return chat
    cur = chat.get("max_tokens")
    if isinstance(cur, int) and cur > model_output_limit:
        chat["max_tokens"] = model_output_limit
    return chat


# ---------------------------------------------------------------------------
# 用量
# ---------------------------------------------------------------------------


def normalize_usage(usage: dict) -> dict:
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    return {
        "input_tokens": prompt,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": completion,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": usage.get("total_tokens") or (prompt + completion),
    }


# ---------------------------------------------------------------------------
# 响应流转换
# ---------------------------------------------------------------------------


class Responder:
    """把 Chat Completions 的增量 chunk 累积成 Responses 事件序列。

    feed() 在流式过程中逐块产出字节；finish() 收尾并给出 response.completed。
    """

    def __init__(self, model: str, name_map: dict | None = None):
        self.response_id = "resp_" + uuid.uuid4().hex
        self.created = int(time.time())
        self.model = model
        self.name_map = name_map or {}

        self.msg_item_id = None
        self.msg_index = 0
        self.text_parts = []
        self.reasoning_parts = []
        self.tool_calls = {}       # index -> {"id","name","arguments"}
        self.finished_items = []
        self.usage = None
        self.finish_reason = None

    # -- 消息 -------------------------------------------------------------
    def _start_message(self) -> bytes:
        if self.msg_item_id:
            return b""
        self.msg_item_id = "msg_" + uuid.uuid4().hex
        self.msg_index = len(self.finished_items)
        return sse(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": self.msg_index,
                "item": {
                    "type": "message",
                    "id": self.msg_item_id,
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                },
            },
        )

    def _close_message(self) -> bytes:
        if not self.msg_item_id:
            return b""
        text = "".join(self.text_parts) or "".join(self.reasoning_parts)
        item = {
            "type": "message",
            "id": self.msg_item_id,
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
        self.finished_items.append(item)
        self.msg_item_id = None
        return sse(
            "response.output_item.done",
            {"type": "response.output_item.done", "output_index": self.msg_index, "item": item},
        )

    # -- 流式 -------------------------------------------------------------
    def begin(self) -> bytes:
        return sse(
            "response.created",
            {
                "type": "response.created",
                "response": {
                    "id": self.response_id,
                    "object": "response",
                    "created_at": self.created,
                    "model": self.model,
                    "status": "in_progress",
                    "output": [],
                },
            },
        )

    def feed(self, delta: dict, finish_reason: str | None = None) -> bytes:
        if finish_reason:
            self.finish_reason = finish_reason

        out = b""

        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            slot = self.tool_calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] += fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]

        if delta.get("content"):
            out += self._start_message()
            piece = delta["content"]
            self.text_parts.append(piece)
            out += sse(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": self.msg_item_id,
                    "output_index": self.msg_index,
                    "content_index": 0,
                    "delta": piece,
                },
            )

        if delta.get("reasoning_content"):
            self.reasoning_parts.append(delta["reasoning_content"])

        return out

    def finish(self) -> bytes:
        out = self._close_message()

        for idx in sorted(self.tool_calls):
            slot = self.tool_calls[idx]
            if not slot["name"]:
                continue
            name = slot["name"]
            if name in self.name_map:
                name = self.name_map[name][1]
            call_id = slot["id"] or ("call_" + uuid.uuid4().hex)
            pos = len(self.finished_items)
            args = slot["arguments"] or "{}"
            item_id = "fc_" + uuid.uuid4().hex

            out += sse(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": pos,
                    "item": {
                        "type": "function_call",
                        "id": item_id,
                        "call_id": call_id,
                        "name": name,
                        "arguments": "",
                        "status": "in_progress",
                    },
                },
            )
            out += sse(
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": item_id,
                    "output_index": pos,
                    "delta": args,
                },
            )
            done = {
                "type": "function_call",
                "id": item_id,
                "call_id": call_id,
                "name": name,
                "arguments": args,
                "status": "completed",
            }
            out += sse(
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": pos, "item": done},
            )
            self.finished_items.append(done)

        resp = {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created,
            "model": self.model,
            "status": "completed",
            "output": self.finished_items,
        }

        # 上游因 max_tokens 用尽被截断（finish_reason=length）时，
        # 绝不能报 completed —— 否则 Codex 会把半句话当成完整回复，
        # 接着开下一轮，表现就是「任务没干完就结束」。
        if self.finish_reason == "length":
            resp["status"] = "incomplete"
            resp["incomplete_details"] = {"reason": "max_output_tokens"}

        if self.usage:
            resp["usage"] = self.usage
        out += sse("response.completed", {"type": "response.completed", "response": resp})
        return out

    # -- 非流式 -----------------------------------------------------------
    def feed_message(self, message: dict) -> bytes:
        out = b""
        if message.get("content"):
            out += self._start_message()
            self.text_parts.append(message["content"])
        for i, tc in enumerate(message.get("tool_calls") or []):
            enriched = dict(tc)
            enriched["index"] = i
            out += self.feed({"tool_calls": [enriched]})
        return out


def parse_sse_lines(buffer: bytes):
    """从缓冲区里切出完整的 SSE data 行，返回 (事件列表, 剩余缓冲)。"""
    events = []
    while b"\n" in buffer:
        line, buffer = buffer.split(b"\n", 1)
        line = line.strip()
        if not line or not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if data == b"[DONE]":
            continue
        try:
            events.append(json.loads(data.decode("utf-8", "replace")))
        except Exception:
            continue
    return events, buffer
