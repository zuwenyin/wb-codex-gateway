"""
wbbilling — WorkBuddy 签到与积分

上游接口（与 dsh-workbuddy-xdpool 插件一致，billing base 为 www.codebuddy.cn）：

    POST /v2/billing/meter/checkin-activity-status   查询签到状态（只读）
    POST /v2/billing/meter/daily-checkin             领取今日签到积分（写）
    POST /v2/billing/meter/get-user-resource         查询积分包明细

说明
----
`checkin-activity-status` 同时返回了累签天数、今日积分、累计积分、
本周签到进度、历史签到日期，足够页面直接展示，且**不消耗额度**。

`get-user-resource` 在本机实测恒返回 403 / code 10085，需要额外授权，
因此积分包明细作为「可选增强」：拿不到就把错误如实回传，不影响主流程。

所有接口都要求带 `X-User-Id`；跨域来源是 www.codebuddy.cn。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from urllib import error as urlerror
from urllib import request as urlrequest

BILLING_BASE = "https://www.codebuddy.cn"
CLIENT_UA = "CLI/2.63.2 CodeBuddy/2.63.2"
ORIGIN = "https://www.codebuddy.cn"
TIMEOUT = 30


class BillingError(Exception):
    def __init__(self, message: str, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def _headers(acc: dict) -> dict:
    """billing 接口的请求头。

    注意：`get-user-resource` 对请求头非常敏感 —— 只带 Authorization 会返回
    403 / code 10085「请求不合法」。必须同时带上 X-Requested-With / Origin /
    Referer / User-Agent，缺一个都会被拒。签到类接口则宽松一些，
    统一带上即可。
    """
    h = {
        "Authorization": "Bearer %s" % (acc.get("access_token") or ""),
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": ORIGIN,
        "Referer": ORIGIN + "/",
        "User-Agent": CLIENT_UA,
    }
    if acc.get("uid"):
        h["X-User-Id"] = acc["uid"]
    if acc.get("domain"):
        h["X-Domain"] = acc["domain"]
    if acc.get("enterprise_id"):
        h["X-Enterprise-Id"] = acc["enterprise_id"]
        h["X-Tenant-Id"] = acc["enterprise_id"]
    return h


def _post(acc: dict, path: str, body: dict | None = None, retries: int = 2) -> dict:
    """POST 到 billing 接口。对 SSL/连接类瞬时错误重试，业务错误不重试。"""
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    last_exc = None

    for attempt in range(retries + 1):
        req = urlrequest.Request(
            BILLING_BASE + path, data=data, headers=_headers(acc), method="POST"
        )
        try:
            with urlrequest.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
            break
        except urlerror.HTTPError as exc:
            # 4xx 是业务拒绝，重试无意义；5xx 可以再试
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            code = None
            try:
                code = json.loads(detail).get("code")
            except Exception:
                pass
            err = BillingError(_human(detail) or ("HTTP %s" % exc.code), exc.code, code)
            if exc.code < 500 or attempt >= retries:
                raise err
            last_exc = err
            time.sleep(0.6 * (attempt + 1))
            continue
        except Exception as exc:
            # SSL EOF / 连接重置这类瞬时问题，退避后重试
            last_exc = BillingError(str(exc))
            if attempt >= retries:
                raise last_exc
            time.sleep(0.6 * (attempt + 1))
            continue
    else:
        raise last_exc or BillingError("请求失败")

    try:
        envelope = json.loads(raw)
    except Exception:
        raise BillingError("上游返回不是 JSON：%s" % raw[:200])

    if envelope.get("code") not in (0, None):
        raise BillingError(
            _human(raw) or ("code %s" % envelope.get("code")),
            None,
            envelope.get("code"),
        )
    data_out = envelope.get("data")
    return data_out if isinstance(data_out, dict) else {}


def _human(raw: str) -> str:
    try:
        j = json.loads(raw)
    except Exception:
        return raw.strip()[:200]
    msg = j.get("msg") or j.get("message") or ""
    code = j.get("code")
    if code:
        return "%s (code %s)" % (msg, code) if msg else "code %s" % code
    return msg


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def checkin_status(acc: dict) -> dict:
    """只读。返回结构化的签到信息。"""
    d = _post(acc, "/v2/billing/meter/checkin-activity-status")

    def num(key):
        v = d.get(key)
        return v if isinstance(v, (int, float)) else 0

    def boolean(key):
        return d.get(key) is True

    return {
        "active": boolean("active"),
        "today_checked_in": boolean("today_checked_in"),
        "streak_days": num("streak_days"),
        "daily_credit": num("daily_credit"),
        "today_credit": num("today_credit"),
        "total_credits": num("total_credits"),
        "week_checkin_days": num("week_checkin_days"),
        "week_progress": d.get("week_progress") if isinstance(d.get("week_progress"), list) else [],
        "checkin_dates": d.get("checkin_dates") if isinstance(d.get("checkin_dates"), list) else [],
        "season": num("season"),
        "activity_name": d.get("activity_name") or "",
        "theme_name": d.get("theme_name") or "",
        "is_streak_day": boolean("is_streak_day"),
        "next_streak_day": num("next_streak_day"),
        "streak_bonus_credit": num("streak_bonus_credit"),
        "claim_button_text": d.get("claim_button_text") or "",
        "start_time": d.get("start_time") or "",
        "end_time": d.get("end_time") or "",
    }


def claim_checkin(acc: dict) -> dict:
    """执行签到（有副作用）。已签到时上游会返回特定错误，这里翻译成友好结果。"""
    d = _post(acc, "/v2/billing/meter/daily-checkin")

    def num(key):
        v = d.get(key)
        return v if isinstance(v, (int, float)) else 0

    return {
        "credit": num("credit") or num("today_credit"),
        "streak_days": num("streak_days"),
        "is_streak_day": d.get("is_streak_day") is True,
    }


def is_already_checked_in(exc: BillingError) -> bool:
    text = str(exc).lower()
    return any(
        k in text
        for k in ("already", "已签到", "已经签到", "重复签到", "repeat")
    )


# ---------------------------------------------------------------------------
# 积分包明细（可选增强）
# ---------------------------------------------------------------------------
# 套餐额度（不是积分包）：官网「剩余积分」不统计它
PLAN_CAPACITY_TYPE = 4
PLAN_SUB_PRODUCT = "sp_tcaca_codebuddy_ide"


def _to_float(raw):
    """CapacityRemainPrecise 之类的字段是字符串，可能带小数（27.76000183）。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            return float(raw.strip())
        except ValueError:
            return None
    return None


def credit_packages(acc: dict) -> dict:
    """查询积分包明细。

    两类资源要分开看，否则数字对不上官网（实测差 500）：

    * **积分包**（`CapacityType=1` / `sp_tcaca_codebuddyide_bonus_pack`）：
      官网「可用 N 个 · 剩余 X 积分」统计的就是这些。
    * **套餐额度**（`CapacityType=4` / `sp_tcaca_codebuddy_ide`）：
      个人版/体验版每月的基础额度，单独展示，不计入「剩余积分」。

    数值优先取 `*Precise` 字段（字符串，带小数），整数版字段会把
    27.76 截成 27 —— 这就是之前积分「不太准」的第二个原因。

    `Status`：0 = 有效，3 = 已耗尽/失效。只有有效且剩余 > 0 的参与统计与排序。
    """
    now = datetime.now()

    def fmt(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    body = {
        "PageNumber": 1,
        "PageSize": 100,
        "ProductCode": "p_tcaca",
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": fmt(now),
        "PackageEndTimeRangeEnd": fmt(now + timedelta(days=3650)),
    }
    d = _post(acc, "/v2/billing/meter/get-user-resource", body)

    wrapper = d.get("Response") if isinstance(d.get("Response"), dict) else d
    inner = wrapper.get("Data") if isinstance(wrapper.get("Data"), dict) else {}
    rows = inner.get("Accounts") if isinstance(inner.get("Accounts"), list) else []

    packages = []
    total_remain = 0.0
    usable = []          # 有效且有剩余的（积分包 + 套餐额度），用于调度排序
    pack_credits = 0.0   # 与官网「剩余积分」口径一致
    plan_credits = 0.0   # 套餐额度，单独展示

    for row in rows:
        if not isinstance(row, dict):
            continue

        def num(precise_key, *fallbacks):
            value = _to_float(row.get(precise_key))
            if value is not None:
                return value
            for key in fallbacks:
                value = _to_float(row.get(key))
                if value is not None:
                    return value
            return 0.0

        remain = num("CapacityRemainPrecise", "CapacityRemain", "Remain")
        size = num("CapacitySizePrecise", "CapacitySize", "TotalDosage", "Size")
        used = num("CapacityUsedPrecise", "CapacityUsed", "Used")
        cycle_remain = num("CycleCapacityRemainPrecise", "CycleCapacityRemain")
        status = int(num("Status"))
        end_raw = row.get("CycleEndTime") or row.get("PackageEndTime") or ""

        end_ms = _parse_time_ms(end_raw)
        name = row.get("PackageName") or row.get("Name") or "(未命名)"
        capacity_type = int(num("CapacityType"))
        is_plan = capacity_type == PLAN_CAPACITY_TYPE or row.get("SubProductCode") == PLAN_SUB_PRODUCT

        packages.append(
            {
                "name": name,
                "kind": "plan" if is_plan else "pack",
                "remain": round(remain, 2),
                "size": round(size, 2),
                "used": round(used, 2),
                "cycle_remain": round(cycle_remain, 2),
                "capacity_type": capacity_type,
                "status": status,
                "end_time": end_raw,
                "end_ms": end_ms,
                "valid": status == 0,
                "usable": status == 0 and remain > 0,
            }
        )
        total_remain += max(0.0, remain)
        if status == 0 and remain > 0:
            usable.append(
                {
                    "remain": remain,
                    "end_ms": end_ms,
                    "end_time": end_raw,
                    "name": name,
                    "kind": "plan" if is_plan else "pack",
                }
            )
            if is_plan:
                plan_credits += remain
            else:
                pack_credits += remain

    # 调度用：最早过期的优先（含套餐额度 —— 它通常最先到期，不烧掉就浪费了）
    usable.sort(key=lambda x: (x["end_ms"] or 9e18))
    nearest = usable[0] if usable else None

    pack_count = sum(1 for u in usable if u["kind"] == "pack")
    plan_count = sum(1 for u in usable if u["kind"] == "plan")

    return {
        "total": round(total_remain, 2),
        "packages": packages,
        "count": len(packages),
        # 官网「可用 N 个」= 有剩余的积分包数量
        "usable_count": pack_count,
        "plan_count": plan_count,
        # 官网「剩余 X 积分」= 积分包剩余合计
        "pack_credits": round(pack_credits, 2),
        "plan_credits": round(plan_credits, 2),
        "nearest": nearest,
        # 调度口径：这个账号总共还能花多少（积分包 + 套餐额度）
        "available_credits": round(pack_credits + plan_credits, 2),
    }


def _parse_time_ms(raw) -> int:
    """把上游的时间解析成毫秒时间戳。支持毫秒/秒数值与 'YYYY-mm-dd HH:MM:SS'。"""
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw if raw > 1e11 else raw * 1000)
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return int(datetime.strptime(text, fmt).timestamp() * 1000)
            except ValueError:
                continue
        try:
            return int(datetime.fromisoformat(text).timestamp() * 1000)
        except Exception:
            return 0
    return 0
