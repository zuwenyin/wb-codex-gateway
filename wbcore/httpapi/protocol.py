"""Responses API / Chat Completions 兼容协议的入口与转换。

从原 gateway.py 按职责拆出，逻辑未改动。
"""
from __future__ import annotations

import hmac
import os
import re
import threading
import time

from .. import context as ctx
from ..auth import _token_for
from ..codex_config import codex_set_mode
from ..faults import _explain_upstream, _fmt_tokens, _is_account_fault, _is_transient_network
from ..local_accounts import run_wbcreds, scan_local_accounts
from ..models import _public
from ..tokens import _estimate_tokens, _model_window
from ..upstream import call_upstream
from .. import meta as wbmeta
from .. import proto as wbproto


def _shutdown_server(server) -> None:
    """停止网关进程。

    给管理页的「停止网关」用：先让 200 响应发出去，再关监听、结束进程。
    进程结束后，本机其它客户端（dsh / Codex）与网关之间就不存在任何耦合了。
    """
    time.sleep(0.3)
    try:
        ctx.log("info", "收到停止指令，网关已退出")
    except Exception:
        pass
    try:
        server.shutdown()
    except Exception:
        pass
    os._exit(0)


class ProtocolMixin:
        def do_POST(self):
            clean = self.path.split("?")[0].rstrip("/") or "/"

            # 统一在这里把请求体读掉并缓存，任何分支（包括提前 return 的）
            # 都不会让字节残留在 keep-alive 连接里污染下一个请求。
            self._body = self._read_body()

            if clean in ("/v1/responses", "/responses"):
                self._handle_responses()
                return

            if clean == "/admin/api/login":
                body = self._body
                expected = self._admin_password()
                if not expected:
                    self._json(200, {"ok": True, "token": ""})
                    return
                if hmac.compare_digest(str(body.get("password") or ""), expected):
                    token = _token_for(expected)
                    self._json(
                        200,
                        {"ok": True, "token": token},
                        {"Set-Cookie": "wb_admin=%s; Path=/; HttpOnly; SameSite=Lax" % token},
                    )
                else:
                    self._json(401, {"ok": False, "error": "密码错误"})
                return

            if clean == "/admin/api/logout":
                self._json(200, {"ok": True}, {"Set-Cookie": "wb_admin=; Path=/; Max-Age=0"})
                return

            if not self._authed():
                self._json(401, {"error": "unauthorized"})
                return

            if clean == "/admin/api/scan-local":
                self._json(200, scan_local_accounts(manual=True))
                return

            if clean == "/admin/api/local-models":
                result = run_wbcreds("--models", timeout=120)
                self._json(200, result)
                return

            if clean == "/admin/api/accounts":
                body = self._body
                if not (str(body.get("name") or "").strip()):
                    self._json(400, {"error": "账号名称不能为空"})
                    return
                if not str(body.get("api_key") or "").strip():
                    self._json(400, {"error": "API Key 不能为空"})
                    return
                acc = ctx.STORE.add_account(body)
                self._json(200, {"ok": True, "account": _public(acc)})
                return

            m = re.match(r"^/admin/api/accounts/([A-Za-z0-9_-]+)/reset$", clean)
            if m:
                acc = ctx.STORE.reset_account(m.group(1))
                if acc:
                    ctx.log("info", "手动恢复账号：%s" % acc.get("name"))
                self._json(200 if acc else 404, {"ok": bool(acc), "account": _public(acc) if acc else None})
                return

            if clean == "/admin/api/credit-summary":
                self._json(200, self._credit_summary())
                return

            if clean == "/admin/api/shutdown":
                # 先把响应写回去，再关服务 —— 否则前端只会看到连接被重置
                self._json(200, {"ok": True, "message": "网关正在停止"})
                threading.Thread(target=_shutdown_server, args=(self.server,), daemon=True).start()
                return

            if clean == "/admin/api/codex":
                body = self._body
                mode = str(body.get("mode") or "").strip()
                result = codex_set_mode(mode, (body.get("model") or "").strip() or None)
                self._json(200 if result.get("ok") else 400, result)
                return

            if clean == "/admin/api/accounts/reset-all":
                result = ctx.STORE.reset_all()
                ctx.log("info", "手动恢复全部账号：%d 个" % result["count"])
                self._json(200, result)
                return

            if clean == "/admin/api/accounts/purge":
                n = ctx.STORE.clear_accounts()
                ctx.log("info", "清空账号池：删除 %d 个外来账号" % n)
                self._json(200, {"ok": True, "removed": n})
                return
                return

            m = re.match(r"^/admin/api/accounts/([A-Za-z0-9_-]+)/checkin$", clean)
            if m:
                self._json(200, self._do_checkin(m.group(1)))
                return

            if clean == "/admin/api/checkin-all":
                self._json(200, self._do_checkin_all())
                return

            m = re.match(r"^/admin/api/accounts/([A-Za-z0-9_-]+)/test$", clean)
            if m:
                self._json(200, self._test_account(m.group(1)))
                return

            self._json(404, {"error": "not found"})


        def _handle_responses(self) -> None:
            settings = ctx.STORE.get_settings()

            # 请求体已在 do_POST 入口读到 self._body；这里不再重复读。
            raw_body = getattr(self, "_body", None) or {}

            # 网关自身的准入校验：approval_key 一旦配置，Codex 必须带上同一个
            # experimental_bearer_token，否则本机任何进程都能白嫖账号池。
            need = ctx.CFG.get("approval_key") or ""
            if need:
                got = self._client_key()
                if not got or not hmac.compare_digest(got, need):
                    self._json(
                        401,
                        {
                            "error": {
                                "type": "unauthorized",
                                "message": "请求没有带对 approval_key。可以放在 "
                                "Authorization: Bearer <key>（Codex 用这个）、"
                                "x-api-key 或 api-key 头里，值必须等于 config.json 的 approval_key。",
                            }
                        },
                    )
                    return

            if not settings["proxy_enabled"]:
                self._json(
                    503,
                    {
                        "error": {
                            "type": "proxy_paused",
                            "message": "代理已暂停。请在管理页面 http://127.0.0.1:%s/ 开启。"
                            % ctx.CFG.get("listen", "").split(":")[-1],
                        }
                    },
                )
                return

            body = raw_body
            if not body:
                self._json(400, {"error": {"message": "empty body"}})
                return

            chat, name_map = wbproto.responses_to_chat(
                body,
                send_usage=settings["send_usage"],
                include_namespaces=settings["include_namespaces"],
                # WorkBuddy 云对 role:system 一律 400（code 11128），
                # 所以本机账号默认把 system 降级成 user。
                system_as_user=settings.get("system_as_user", True),
            )
            if not chat.get("messages"):
                self._json(400, {"error": {"message": "empty input"}})
                return

            # 请求体过大时提前拦掉：省一次无效上游调用，也让错误更可读。
            #
            # 注意：不能直接用「字节数 / 4」估算！
            # base64 图片体积巨大但 token 极少（1440x900 约 1100 tokens，
            # 而 1.4MB 的 base64 按字节估算会是 35 万）。早先正是这个错误估算，
            # 让「Codex 显示只用 16%」的会话被网关误判为 111 万 tokens 并拦下。
            est_tokens = _estimate_tokens(chat)
            limit = _model_window(chat.get("model") or "")

            # 输出上限也要跟模型对齐，否则上游会拒绝
            try:
                meta = wbmeta.get_meta()
                out_limit = (meta.get(chat.get("model") or "") or {}).get("max_output_tokens") or 0
                if out_limit:
                    wbproto.clamp_max_tokens(chat, int(out_limit))
            except Exception:
                pass

            if limit and est_tokens > limit:
                self._json(
                    400,
                    {
                        "error": {
                            "type": "context_too_long",
                            "message": (
                                "上下文太长，约 %s tokens，超过模型 %s 的上限"
                                "（%s tokens，约 %s）。请新建会话，或执行 /compact 压缩历史后重试。"
                                % (
                                    "{:,}".format(est_tokens),
                                    chat.get("model"),
                                    "{:,}".format(limit),
                                    _fmt_tokens(limit),
                                )
                            ),
                            "estimated_tokens": est_tokens,
                            "limit": limit,
                        }
                    },
                )
                return

            tried = []
            last_err = None
            # expire_first 需要知道各账号的积分过期时间
            if settings.get("policy") == "expire_first":
                self._refresh_credit_cache()

            while True:
                candidates = ctx.STORE.candidate_order(
                    exclude_ids=tried, model=chat.get("model") or ""
                )
                if not candidates:
                    if last_err:
                        msg = last_err
                    else:
                        msg = self._no_account_reason(chat.get("model") or "")
                    self._json(
                        502,
                        {
                            "error": {
                                "message": msg,
                                "type": "no_account",
                                "model": chat.get("model") or "",
                            }
                        },
                    )
                    return

                acc = candidates[0]
                tried.append(acc["id"])
                resp, err = call_upstream(acc, chat)

                if err:
                    status, detail = err
                    # 三类要分开处理：
                    #   1) 账号的锅（鉴权/限流/5xx）→ 记账进冷却，换号
                    #   2) 请求的锅（上下文超长/参数非法）→ 直接回给客户端
                    #   3) 链路抖动（SSL EOF/连接重置）→ **不记账，换号重试**
                    # 第 3 类之前被误判成第 2 类，直接抛给客户端，
                    # 表现就是 Codex 里频繁出现「上游返回 HTTP None」而中断。
                    if _is_account_fault(status, detail):
                        state = ctx.STORE.record_failure(
                            acc["id"], status, detail, model=chat.get("model") or ""
                        )
                    elif _is_transient_network(status, detail):
                        state = "retry"
                    else:
                        state = "request-error"

                    last_err = "账号 %s 失败(HTTP %s): %s" % (acc["name"], status, (detail or "")[:160])
                    ctx.log(
                        "warn",
                        "账号 %s -> HTTP %s，状态 %s | model=%s | 约 %s tokens | 上游=%s"
                        % (
                            acc["name"],
                            status,
                            state,
                            chat.get("model"),
                            "{:,}".format(est_tokens),
                            (detail or "").replace("\n", " ")[:240],
                        ),
                    )

                    if state == "retry":
                        # 链路抖动：换个账号再试，别让用户的会话中断。
                        time.sleep(0.3)
                        continue

                    if state == "request-error":
                        # 请求本身的错，重试其他账号没有意义，直接回给客户端。
                        self._json(
                            400,
                            {
                                "error": {
                                    "type": "upstream_bad_request",
                                    "message": _explain_upstream(status, detail, chat.get("model") or ""),
                                    "detail": (detail or "")[:600],
                                }
                            },
                        )
                        return
                    continue

                status = getattr(resp, "status", 200)
                if status >= 400:
                    detail = ""
                    try:
                        detail = resp.read().decode("utf-8", "replace")[:300]
                    except Exception:
                        pass

                    # 请求本身的错（超长/参数非法）不该记在账号头上
                    if _is_account_fault(status, detail):
                        ctx.STORE.record_failure(acc["id"], status, detail)
                        state = "account-error"
                    else:
                        state = "request-error"

                    last_err = "账号 %s 返回 HTTP %s" % (acc["name"], status)
                    # 带上上游原文，否则只看到 400 无法定位
                    ctx.log(
                        "warn",
                        "账号 %s HTTP %s，状态 %s | model=%s | 约 %s tokens | 上游=%s"
                        % (
                            acc["name"],
                            status,
                            state,
                            chat.get("model"),
                            "{:,}".format(est_tokens),
                            (detail or "").replace("\n", " ")[:220],
                        ),
                    )

                    if state == "request-error":
                        self._json(
                            400,
                            {
                                "error": {
                                    "type": "upstream_bad_request",
                                    "message": _explain_upstream(status, detail, chat.get("model") or ""),
                                    "detail": (detail or "")[:600],
                                }
                            },
                        )
                        return
                    continue

                ctx.STORE.record_success(acc["id"])
                ctx.log("info", "%s <- %s (%s)" % (chat.get("model"), acc["name"], "stream"))
                self._relay(resp, body.get("model") or chat.get("model") or "", name_map)
                return


        def _relay(self, resp, model: str, name_map: dict) -> None:
            responder = wbproto.Responder(model, name_map)

            # SSE 长度未知，必须用 chunked；否则 HTTP/1.1 客户端会一直等包体结束。
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def write(payload: bytes) -> None:
                if not payload:
                    return
                self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")

            def finish_body() -> None:
                try:
                    self.wfile.write(b"0\r\n\r\n")
                except Exception:
                    pass

            write(responder.begin())

            buf = b""
            chunks = 0
            try:
                while True:
                    piece = resp.read(2048)
                    if not piece:
                        break
                    chunks += 1
                    buf += piece
                    events, buf = wbproto.parse_sse_lines(buf)
                    for event in events:
                        if event.get("usage"):
                            responder.usage = wbproto.normalize_usage(event["usage"])
                        for choice in event.get("choices") or []:
                            delta = choice.get("delta") or {}
                            out = responder.feed(delta, choice.get("finish_reason"))
                            if out:
                                write(out)
                        # 上游偶尔把错误塞成 SSE
                        if event.get("error"):
                            ctx.log("warn", "上游 SSE 内错误: %s" % str(event["error"])[:200])
            except Exception as exc:
                ctx.log("error", "转发中断: %s" % exc)
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

            if chunks == 0:
                ctx.log("warn", "上游返回空流，model=%s" % model)
            write(responder.finish())
            finish_body()

