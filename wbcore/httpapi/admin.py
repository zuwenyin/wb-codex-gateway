"""管理面板 REST 接口（账号增删改查、签到、积分、Codex 模式）。

从原 gateway.py 按职责拆出，逻辑未改动。
"""
from __future__ import annotations

import time
import re
from .. import context as ctx
from ..models import _public, account_models
from ..upstream import call_upstream_once
from .. import billing as wbbilling


class AdminApiMixin:
        def do_PATCH(self):
            clean = self.path.split("?")[0].rstrip("/")
            self._body = self._read_body()
            if not self._authed():
                self._json(401, {"error": "unauthorized"})
                return

            if clean == "/admin/api/settings":
                patch = self._body
                if patch.get("proxy_enabled") is False:
                    ctx.log("info", "代理已暂停")
                elif patch.get("proxy_enabled") is True:
                    ctx.log("info", "代理已开启")
                self._json(200, {"ok": True, "settings": ctx.STORE.update_settings(patch)})
                return

            m = re.match(r"^/admin/api/accounts/([A-Za-z0-9_-]+)$", clean)
            if m:
                acc = ctx.STORE.update_account(m.group(1), self._body)
                self._json(200 if acc else 404, {"ok": bool(acc), "account": _public(acc) if acc else None})
                return

            self._json(404, {"error": "not found"})


        def do_DELETE(self):
            clean = self.path.split("?")[0].rstrip("/")
            self._body = self._read_body()
            if not self._authed():
                self._json(401, {"error": "unauthorized"})
                return
            m = re.match(r"^/admin/api/accounts/([A-Za-z0-9_-]+)$", clean)
            if m:
                ok = ctx.STORE.delete_account(m.group(1))
                self._json(200 if ok else 404, {"ok": ok})
                return
            self._json(404, {"error": "not found"})


        def _credit_summary(self) -> dict:
            """汇总每个账号的签到状态与积分。只读，不触发签到。"""
            accounts = []
            for acc in ctx.STORE.list_accounts():
                entry = {
                    "id": acc["id"],
                    "name": acc.get("name"),
                    "uin": acc.get("uin"),
                    "kind": acc.get("kind"),
                    "enabled": acc.get("enabled", True),
                }
                if not acc.get("access_token"):
                    entry["ok"] = False
                    entry["error"] = "没有可用 token"
                    accounts.append(entry)
                    continue
                try:
                    st = wbbilling.checkin_status(acc)
                    entry["ok"] = True
                    entry.update(
                        {
                            "today_checked_in": st["today_checked_in"],
                            "streak_days": st["streak_days"],
                            "daily_credit": st["daily_credit"],
                            "today_credit": st["today_credit"],
                            "total_credits": st["total_credits"],
                            "week_checkin_days": st["week_checkin_days"],
                            "week_progress": st["week_progress"],
                            "checkin_dates": st["checkin_dates"],
                            "season": st["season"],
                            "activity_name": st["activity_name"],
                            "end_time": st["end_time"],
                        }
                    )
                except wbbilling.BillingError as exc:
                    entry["ok"] = False
                    entry["error"] = str(exc)

                # 积分包过期信息（用于 expire_first 展示）
                try:
                    info = wbbilling.credit_packages(acc)
                    # 调度用：该账号还能花多少（积分包 + 套餐额度）
                    ctx.STORE.set_credit_info(
                        acc["id"],
                        (info.get("nearest") or {}).get("end_ms") or 0,
                        info.get("available_credits") or 0,
                    )
                    nearest = info.get("nearest") or {}
                    # 展示用：与官网口径一致 —— 只剩积分包，套餐额度单列
                    entry["available_credits"] = info.get("pack_credits", 0)
                    entry["plan_credits"] = info.get("plan_credits", 0)
                    entry["total_spendable"] = info.get("available_credits", 0)
                    entry["nearest_expire"] = nearest.get("end_time", "")
                    entry["nearest_expire_remain"] = round(nearest.get("remain", 0) or 0, 2)
                    entry["nearest_expire_kind"] = nearest.get("kind", "")
                    entry["usable_packages"] = info.get("usable_count", 0)
                    entry["plan_packages"] = info.get("plan_count", 0)
                    entry["package_count"] = info.get("count", 0)
                    entry["packages"] = info.get("packages", [])[:50]
                except wbbilling.BillingError as exc:
                    entry["packages_error"] = str(exc)

                accounts.append(entry)

            ok_list = [a for a in accounts if a.get("ok")]
            return {
                "ok": True,
                "accounts": accounts,
                # 累计获得（历史签到总和），不是余额
                "total_credits": sum(a.get("total_credits", 0) for a in ok_list),
                # 当前可用（积分包口径，与官网一致）
                "available_credits": round(sum(a.get("available_credits", 0) for a in ok_list), 2),
                "plan_credits": round(sum(a.get("plan_credits", 0) for a in ok_list), 2),
                "checked_in_today": sum(1 for a in ok_list if a.get("today_checked_in")),
                "account_count": len(ok_list),
            }


        def _refresh_credit_cache(self) -> None:
            """为 expire_first 策略准备数据：查各账号最近过期的积分。

            有 5 分钟 TTL，且只查缓存过期或缺失的账号，避免每个请求都打上游。
            """
            for acc in ctx.STORE.list_accounts():
                if not acc.get("access_token"):
                    continue
                # 让给外部客户端的账号：连积分都不查，彻底不打扰
                if acc.get("reserved"):
                    continue
                if not ctx.STORE.needs_credit_refresh(acc["id"]):
                    continue
                try:
                    info = wbbilling.credit_packages(acc)
                    nearest = info.get("nearest") or {}
                    ctx.STORE.set_credit_info(
                        acc["id"],
                        nearest.get("end_ms") or 0,
                        info.get("available_credits") or 0,
                    )
                except wbbilling.BillingError as exc:
                    ctx.log("warn", "查询 %s 积分过期时间失败：%s" % (acc.get("name"), exc))


        def _no_account_reason(self, model: str) -> str:
            """没有可用账号时，给一句能行动的说明，而不是干巴巴的 502。"""
            accounts = ctx.STORE.list_accounts()
            if not accounts:
                return "账号池为空。请打开控制台 http://127.0.0.1:18080/ 扫描本机账号。"

            cooled_for_model = 0
            cooling = 0
            disabled = 0
            no_cred = 0
            now = int(time.time())
            state = ctx.STORE.model_cooldown_state()

            for a in accounts:
                if not a.get("enabled", True):
                    disabled += 1
                    continue
                has = a.get("access_token") if a.get("kind") == "local" else a.get("api_key")
                if not has:
                    no_cred += 1
                    continue
                if now < int(a.get("cooling_until", 0)):
                    cooling += 1
                    continue
                cds = state.get(a["id"]) or {}
                if model and cds.get(model, 0) > 0:
                    cooled_for_model += 1

            if cooled_for_model:
                # 最常见的情况：模型额度用完，但账号本身没问题
                left = []
                for a in accounts:
                    cds = (state.get(a["id"]) or {}).get(model)
                    if cds:
                        left.append("%d 分钟" % max(1, cds // 60))
                hint = ("（还有 %s 后重置；也可以换用其它模型）" % "、".join(left[:2])) if left else ""
                return (
                    "模型 %s 在所有账号上的当日额度都已用完%s。"
                    "请在 Codex 里切换到其它模型，或等待额度重置。" % (model, hint)
                )
            if cooling:
                return "所有账号都在冷却中（%d 个），请稍等片刻或到控制台点「恢复全部」。" % cooling
            if disabled:
                return "所有账号都被禁用了（%d 个），请到控制台点「恢复全部」。" % disabled
            if no_cred:
                return "账号缺少凭证（%d 个），请到控制台重新扫描本机账号。" % no_cred
            return "没有可用账号，请到控制台 http://127.0.0.1:18080/ 检查。"


        def _do_checkin(self, acc_id: str) -> dict:
            acc = ctx.STORE.get_account(acc_id)
            if not acc:
                return {"ok": False, "error": "账号不存在"}
            if not acc.get("access_token"):
                return {"ok": False, "error": "该账号没有可用 token，请先扫描本机账号"}

            try:
                status = wbbilling.checkin_status(acc)
            except wbbilling.BillingError as exc:
                return {"ok": False, "error": str(exc), "stage": "status"}

            if status.get("today_checked_in"):
                return {
                    "ok": True,
                    "already": True,
                    "message": "今天已经签到过了",
                    "credit": status.get("today_credit", 0),
                    "streak_days": status.get("streak_days", 0),
                    "total_credits": status.get("total_credits", 0),
                }

            try:
                claim = wbbilling.claim_checkin(acc)
            except wbbilling.BillingError as exc:
                if wbbilling.is_already_checked_in(exc):
                    return {"ok": True, "already": True, "message": "今天已经签到过了"}
                return {"ok": False, "error": str(exc), "stage": "claim"}

            ctx.log("info", "签到成功：%s +%s 积分" % (acc.get("name"), claim.get("credit")))
            return {
                "ok": True,
                "already": False,
                "message": "签到成功",
                "credit": claim.get("credit", 0),
                "streak_days": claim.get("streak_days", 0),
            }


        def _do_checkin_all(self) -> dict:
            results = []
            total_credit = 0
            for acc in ctx.STORE.list_accounts():
                if acc.get("kind") != "local" and not acc.get("access_token"):
                    continue
                r = self._do_checkin(acc["id"])
                r["name"] = acc.get("name")
                r["uin"] = acc.get("uin")
                results.append(r)
                if r.get("ok") and not r.get("already"):
                    total_credit += int(r.get("credit") or 0)

            done = sum(1 for r in results if r.get("ok") and not r.get("already"))
            already = sum(1 for r in results if r.get("already"))
            failed = sum(1 for r in results if not r.get("ok"))
            return {
                "ok": failed == 0,
                "checked": done,
                "already": already,
                "failed": failed,
                "total_credit": total_credit,
                "results": results,
            }


        def _test_account(self, acc_id: str) -> dict:
            acc = ctx.STORE.get_account(acc_id)
            if not acc:
                return {"ok": False, "error": "账号不存在"}
            models = account_models(acc)
            model = models[0] if models else "deepseek-v4-flash"

            started = time.time()
            text, err = call_upstream_once(
                acc,
                {
                    "model": model,
                    "messages": [{"role": "user", "content": "请只回复两个字：正常"}],
                    "max_tokens": 16,
                },
                timeout=45,
            )
            elapsed = int((time.time() - started) * 1000)

            if err:
                status, detail = err
                ctx.STORE.record_failure(acc_id, status, detail)
                return {"ok": False, "status": status, "error": detail or "连接失败", "ms": elapsed}
            if text is None:
                ctx.STORE.record_failure(acc_id, None, "空响应")
                return {"ok": False, "error": "上游没有返回内容", "ms": elapsed}

            ctx.STORE.record_success(acc_id)
            return {"ok": True, "ms": elapsed, "model": model, "sample": (text or "")[:60]}

