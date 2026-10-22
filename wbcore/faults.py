"""上游错误判定与人话解释。

从原 gateway.py 按职责拆出，逻辑未改动。
"""
from __future__ import annotations

import json
import re
from . import context as ctx

def _is_account_fault(status, detail: str) -> bool:
    """判断这次失败该不该记在账号头上。

    账号自身的毛病：401/403 鉴权失效、402 欠费、429 限流、5xx 服务端故障。
    请求自身的毛病：上下文超长（11115）、参数非法（11150 等）——换号也一样，
    记账只会污染账号健康度。
    网络抖动：SSL EOF / 连接重置这类，属于链路问题，也不该算账号的锅。

    注意：返回 False 只表示"不是账号的锅"，调用方还要区分它是
    「请求本身的错」还是「偶发的链路抖动」——后者应该换号重试，
    不能直接把错误丢给用户。
    """
    detail = detail or ""
    lowered = detail.lower()

    # 上游业务码：明确是"请求太长/格式不对"
    for code in ("11115", "11150", "11128", "11125"):
        if code in detail:
            return False
    if "prompt is too long" in detail or "too long" in detail:
        return False

    # 11148 = 工具调用序列不匹配。这是网关转换层的问题，
    # 不该记在账号头上；但也不该直接抛给用户 —— 换号重试通常能过。
    if "11148" in detail:
        return False

    # 网络层瞬时抖动：SSL EOF、连接被重置/中止。
    # 这类必须和 _is_transient_network 保持一致，否则会出现
    # "既算账号问题、又算链路抖动"的矛盾分类，调用方就不知道该换号还是直接返回。
    if _is_transient_network(status, detail):
        return False

    if status is None:
        # 其余网络错误（超时、DNS）仍算账号侧问题，让它进冷却，避免反复打同一个。
        return True
    return status in (401, 402, 403, 408, 409, 429) or status >= 500


def _is_transient_network(status, detail: str) -> bool:
    """是否是"重试一下/换个账号就大概率能好"的链路抖动。

    SSL EOF、连接被重置、连接中止这类，跟具体账号无关，通常是
    长连接被中间设备掐断、或上游某台节点抖了一下。遇到这些必须
    换号重试，而不是把错误直接抛给用户（那会让会话看起来"总是暂停"）。

    另外把 11148（工具调用序列不匹配）也算进来：它同样不是账号问题，
    换号重试往往能过，比直接把错误甩给用户好。
    """
    if status is not None and "11148" in (detail or ""):
        return True
    if status is not None:
        return False
    lowered = (detail or "").lower()
    markers = (
        "unexpected_eof",
        "ssl",
        "connection reset",
        "remote end closed",
        "connection aborted",
        "connectionbroken",
        "remote host forced",
        "10053",
        "10054",
        "eof occurred",
    )
    return any(m in lowered for m in markers)


def _fmt_tokens(n: int) -> str:
    if n >= 1000000:
        v = n / 1000000
        return ("%dM" % v) if abs(v - round(v)) < 0.05 else ("%.1fM" % v)
    if n >= 1000:
        return "%dK" % round(n / 1000)
    return str(n)


def _explain_upstream(status, detail: str, model: str = "") -> str:
    """把上游错误翻译成用户能看懂的一句话。

    上限数字必须从上游错误里解析，不能写死 —— 不同模型窗口差别很大
    （96K ~ 1M），写死 100K 会误导用户。
    """
    detail = detail or ""

    if "11115" in detail or "prompt is too long" in detail:
        # 上游形如：prompt is too long: 1216514 tokens > 1048576 maximum
        used = limit = 0
        m = re.search(r"(\d+)\s*tokens?\s*>\s*(\d+)", detail)
        if m:
            used, limit = int(m.group(1)), int(m.group(2))
        else:
            m2 = re.search(r"(\d+)\s*maximum", detail)
            if m2:
                limit = int(m2.group(1))

        bits = ["上下文太长"]
        if used and limit:
            bits.append("，已达 %s tokens，" % "{:,}".format(used))
        else:
            bits.append("，")
        if model:
            bits.append("超过模型 %s 的上限" % model)
        else:
            bits.append("超过模型上限")
        if limit:
            bits.append("（%s tokens，约 %s）" % ("{:,}".format(limit), _fmt_tokens(limit)))

        return "".join(bits) + "。请新建一个会话，或执行 /compact 压缩历史后重试。"

    if "11150" in detail:
        return "该模型不支持当前的思考强度（reasoning effort），请在设置里调低或更换模型。"
    if "11148" in detail:
        return (
            "工具调用与结果对不上（协议错误）。这是网关转换的问题，"
            "通常换个账号重试即可恢复；若持续出现请反馈。"
        )
    if "11128" in detail:
        return "上游拒绝了该请求（渠道限制）。"
    if status == 429:
        return "该账号被限流（429），请稍后重试或换一个账号。"
    if status == 402:
        return "账号额度不足（402），请更换账号或充值。"
    if status in (401, 403):
        return "账号凭证失效或无权限（HTTP %s），请重新扫描本机账号。" % status
    if status and status >= 500:
        return "上游服务异常（HTTP %s），请稍后重试。" % status

    # 兜底：尽量给出上游的 msg，而不是让用户去翻 detail
    try:
        obj = json.loads(detail)
        msg = obj.get("msg") or obj.get("message") or ""
        if msg:
            return "上游错误（HTTP %s）：%s" % (status, str(msg)[:120])
    except Exception:
        pass
    if detail:
        return "上游错误（HTTP %s）：%s" % (status, detail.replace("\n", " ")[:120])
    return "上游错误（HTTP %s），请稍后重试。" % status

