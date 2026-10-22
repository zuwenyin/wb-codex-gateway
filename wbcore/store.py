"""
wbstore — 账号池与网关状态持久化

设计要点
--------
* 多账号：每个账号有独立的 base_url / api_key / 模型白名单。
* 池策略：
    round_robin —— 依次轮换（默认）
    failover    —— 固定用第一个可用账号，挂了才切下一个
    random      —— 随机
* 失败处理：账号出现 401/403/429/5xx 时记录失败次数并进入冷却，
  冷却期内不参与选择；连续失败达到阈值自动禁用（可在 UI 一键恢复）。
* 全部状态落盘到 data.json，进程重启不丢。
* 并发安全：用可重入锁包住所有读写，落盘走临时文件 + 原子替换。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import socket
import threading
import time
import uuid

# 数据文件路径统一由 wbcore.paths 定义：允许通过环境变量覆盖，
# 便于测试时与真实数据隔离（selftest 早先因为共用 data.json，
# 把 mock 账号写进了真实池子）。
from .paths import DATA_PATH  # noqa: F401

# 上游错误里视为"这个账号该歇一会儿"的 HTTP 状态
RETRYABLE_STATUS = {401, 402, 403, 408, 409, 429, 500, 502, 503, 504}

POLICIES = ("round_robin", "failover", "random", "expire_first")

# 积分过期时间的缓存：account_id -> {"nearest_end_ms", "available_credits", "at"}
# 查一次上游要几百毫秒，不能每个请求都查。
_credit_cache: dict = {}
CREDIT_CACHE_TTL = 300  # 秒


def _now() -> int:
    return int(time.time())


def _machine_fingerprint() -> dict:
    """生成"这台机器"的指纹，用于识别 data.json 是不是从别处拷来的。

    背景：有人把整个项目目录打包分享时，连 data.json 一起带走了，
    对方打开管理页看到的却是分享者本人的账号（还带着有效的 token）。
    所以在 data.json 里记下它诞生于哪台机器，启动时比对一下即可发现。

    只用「用户名 + 主机名」，不落任何密钥信息，也不参与鉴权。
    """
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    try:
        host = socket.gethostname()
    except Exception:
        host = ""
    raw = ("%s@%s" % (user, host)).strip().lower() or "unknown"
    return {
        "user": user,
        "host": host,
        "sig": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
    }


def _blank_account(name: str = "") -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "name": name or "未命名账号",
        "kind": "manual",        # manual = 手填 key；local = 从本机 WorkBuddy 读取
        "enabled": True,
        # reserved = 让给外部客户端（如 dsh）：网关不拿它发请求、也不查它的积分。
        # 用于两个程序共用同一个 WorkBuddy 登录态时彻底解耦。
        "reserved": False,
        "base_url": "",
        "api_key": "",
        "access_token": "",      # 仅 local：运行时由 wbcreds 注入
        "refresh_token": "",
        "token_expires_at": 0,
        "uid": "",
        "uin": "",
        "nickname": "",
        "models": "",            # 逗号分隔；留空表示不限
        "note": "",
        # 运行期统计
        "requests": 0,
        "successes": 0,
        "failures": 0,
        "consecutive_failures": 0,
        "cooling_until": 0,
        "model_cooldowns": {},
        "auto_disabled": False,
        "auto_disabled_at": 0,
        "last_error": "",
        "last_used_at": 0,
        "created_at": _now(),
    }


class Store:
    def __init__(self, path: str = DATA_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._data = self._load()
        self._owner_warning: dict | None = None
        self._check_owner()

    def _check_owner(self) -> None:
        """比对 data.json 的归属机器与当前机器。

        * 老数据没有归属信息 —— 静默认领为本机，不打扰用户。
        * 归属与当前机器不符 —— 说明这份数据是别人拷过来的，
          记下告警，由上层在日志与管理页提示。
        """
        current = _machine_fingerprint()
        owner = self._data.get("owner")
        with self._lock:
            if not isinstance(owner, dict) or not owner.get("sig"):
                self._data["owner"] = current
                self._save()
                return
            if owner.get("sig") != current["sig"]:
                self._owner_warning = {
                    "origin": owner,
                    "current": current,
                    "accounts": len(self._data.get("accounts") or []),
                }

    def owner_warning(self) -> dict | None:
        """data.json 归属异常时返回详情，正常返回 None。"""
        if not self._owner_warning:
            return None
        return json.loads(json.dumps(self._owner_warning))

    def clear_accounts(self) -> int:
        """清空账号池（用于"这份 data.json 是别人拷来的"场景）。

        只删账号与游标，归属信息会顺带更新为本机，避免告警反复出现。
        返回被清空的账号数。
        """
        with self._lock:
            n = len(self._data.get("accounts") or [])
            self._data["accounts"] = []
            self._data["pool_cursor"] = 0
            self._data["scan_ignored"] = []
            self._data["owner"] = _machine_fingerprint()
            self._save()
            self._owner_warning = None
        return n

    # -- 持久化 -----------------------------------------------------------
    def _load(self) -> dict:
        base = {
            "version": 1,
            "proxy_enabled": True,
            "policy": "round_robin",
            "cooldown_sec": 60,
            "max_consecutive_failures": 5,
            "send_usage": True,
            "include_namespaces": False,
            "system_as_user": True,
            "admin_password": "",
            "accounts": [],
            "pool_cursor": 0,
        }
        if not os.path.exists(self.path):
            return base
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
        except Exception:
            return base
        if isinstance(loaded, dict):
            base.update(loaded)
        if not isinstance(base.get("accounts"), list):
            base["accounts"] = []
        for acc in base["accounts"]:
            for key, val in _blank_account(acc.get("name", "")).items():
                acc.setdefault(key, val)
        return base

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, self.path)

    # -- 通用读写 ---------------------------------------------------------
    @property
    def lock(self):
        return self._lock

    def snapshot(self, reveal: bool = False) -> dict:
        # 先从"自动禁用"里恢复已过冷却期的，再取快照，避免界面显示陈旧的"已禁用"
        self.revive_auto_disabled()
        with self._lock:
            data = json.loads(json.dumps(self._data))
        for acc in data["accounts"]:
            if not reveal:
                acc["api_key"] = _mask(acc.get("api_key", ""))
            # 老数据里的 last_error 是整段上游 JSON，读出来时顺手压成一句话，
            # 免得界面继续显示一大坨原文。
            raw_err = acc.get("last_error") or ""
            if len(raw_err) > 120 or raw_err.lstrip().startswith("{"):
                acc["last_error"] = _reformat_legacy_error(raw_err)
            # token 永不外泄，只告诉前端"有没有"。注意：可用性必须在抹掉
            # token **之前**算，否则 _available_now 看到的是空串，恒为不可用。
            # 已让出的账号对网关而言不可用（否则统计卡和"当前可用"会虚高）
            acc["available"] = self._available_now(acc) and not acc.get("reserved")
            acc["reserved"] = bool(acc.get("reserved"))
            acc["cooling_in"] = max(0, int(acc.get("cooling_until", 0)) - _now())
            acc["has_token"] = bool(acc.get("access_token"))
            acc.pop("access_token", None)
            acc.pop("refresh_token", None)
        return data

    # -- 设置 -------------------------------------------------------------
    def get_settings(self) -> dict:
        with self._lock:
            return {
                "proxy_enabled": bool(self._data.get("proxy_enabled", True)),
                "policy": self._data.get("policy", "round_robin"),
                "cooldown_sec": int(self._data.get("cooldown_sec", 60)),
                "max_consecutive_failures": int(self._data.get("max_consecutive_failures", 5)),
                "send_usage": bool(self._data.get("send_usage", True)),
                "include_namespaces": bool(self._data.get("include_namespaces", False)),
                "system_as_user": bool(self._data.get("system_as_user", True)),
                "admin_password_set": bool(self._data.get("admin_password")),
            }

    def update_settings(self, patch: dict) -> dict:
        allowed = {
            "proxy_enabled": bool,
            "policy": str,
            "cooldown_sec": int,
            "max_consecutive_failures": int,
            "send_usage": bool,
            "include_namespaces": bool,
            "system_as_user": bool,
            "admin_password": str,
        }
        with self._lock:
            for key, caster in allowed.items():
                if key not in patch:
                    continue
                try:
                    val = caster(patch[key])
                except (TypeError, ValueError):
                    continue
                if key == "policy" and val not in POLICIES:
                    continue
                if key == "cooldown_sec":
                    val = max(1, min(3600, val))
                if key == "max_consecutive_failures":
                    val = max(1, min(100, val))
                self._data[key] = val
            self._save()
            return self.get_settings()

    def get_raw_password(self) -> str:
        """管理密码（config.json 未配置时回退到 data.json）。"""
        with self._lock:
            return str(self._data.get("admin_password") or "")

    # -- 账号 CRUD --------------------------------------------------------
    def list_accounts(self) -> list:
        with self._lock:
            return json.loads(json.dumps(self._data["accounts"]))

    def get_account(self, acc_id: str):
        with self._lock:
            for acc in self._data["accounts"]:
                if acc["id"] == acc_id:
                    return json.loads(json.dumps(acc))
        return None

    def add_account(self, payload: dict) -> dict:
        acc = _blank_account((payload.get("name") or "").strip())
        for field in ("base_url", "api_key", "models", "note"):
            if payload.get(field) is not None:
                acc[field] = str(payload[field]).strip()
        acc["enabled"] = bool(payload.get("enabled", True))
        with self._lock:
            self._data["accounts"].append(acc)
            self._save()
        return acc

    def update_account(self, acc_id: str, payload: dict) -> dict | None:
        with self._lock:
            for acc in self._data["accounts"]:
                if acc["id"] != acc_id:
                    continue
                if "name" in payload:
                    acc["name"] = (str(payload["name"]).strip() or acc["name"])
                for field in ("base_url", "models", "note"):
                    if field in payload:
                        acc[field] = str(payload[field]).strip()
                if "api_key" in payload:
                    key = str(payload["api_key"]).strip()
                    # UI 里显示的是掩码，回传掩码表示"不修改"
                    if key and not key.startswith("••••"):
                        acc["api_key"] = key
                if "enabled" in payload:
                    acc["enabled"] = bool(payload["enabled"])
                if "reserved" in payload:
                    acc["reserved"] = bool(payload["reserved"])
                self._save()
                return json.loads(json.dumps(acc))
        return None

    def delete_account(self, acc_id: str) -> bool:
        with self._lock:
            before = len(self._data["accounts"])
            victim = next((a for a in self._data["accounts"] if a["id"] == acc_id), None)
            self._data["accounts"] = [a for a in self._data["accounts"] if a["id"] != acc_id]
            changed = len(self._data["accounts"]) != before
            # 删掉的本机账号记一笔，自动扫描不会把它再扫回来
            #（真想加回来的话，手动点一次扫描按钮即可）
            if changed and victim and victim.get("kind") == "local":
                self._add_scan_ignored(victim.get("uin") or victim.get("uid"))
            if changed:
                self._save()
            return changed

    def reset_account(self, acc_id: str) -> dict | None:
        """清空失败计数 / 解除冷却 / 重新启用。"""
        with self._lock:
            for acc in self._data["accounts"]:
                if acc["id"] != acc_id:
                    continue
                acc.update(
                    {
                        "enabled": True,
                        "consecutive_failures": 0,
                        "cooling_until": 0,
                        "model_cooldowns": {},
                        "auto_disabled": False,
                        "auto_disabled_at": 0,
                        "last_error": "",
                    }
                )
                self._save()
                return json.loads(json.dumps(acc))
        return None

    # -- Codex 侧配置 -----------------------------------------------------
    def get_codex_conf(self) -> dict:
        """切换 Codex 接入模式时记下的值（官方原模型 / 上次用的网关模型）。"""
        with self._lock:
            return dict(self._data.get("codex_conf") or {})

    def set_codex_conf(self, patch: dict) -> dict:
        with self._lock:
            cur = dict(self._data.get("codex_conf") or {})
            cur.update({k: v for k, v in (patch or {}).items() if v is not None})
            self._data["codex_conf"] = cur
            self._save()
            return dict(cur)

    # -- 本机账号同步 -----------------------------------------------------
    def reset_all(self) -> dict:
        """一键恢复全部账号：清冷却、清失败计数、解除自动禁用。

        注意：这里只恢复账号的"故障状态"，不会去改 api_key / token，
        也不会删除账号。手动禁用过的账号同样会被重新启用 —— 这是
        "我想让整个池子立刻可用"的语义。
        """
        changed = 0
        with self._lock:
            for acc in self._data["accounts"]:
                before = (
                    acc.get("enabled", True),
                    int(acc.get("consecutive_failures", 0)),
                    int(acc.get("cooling_until", 0)),
                )
                acc.update(
                    {
                        "enabled": True,
                        "consecutive_failures": 0,
                        "cooling_until": 0,
                        "model_cooldowns": {},
                        "auto_disabled": False,
                        "auto_disabled_at": 0,
                        "last_error": "",
                    }
                )
                after = (True, 0, 0)
                if before != after:
                    changed += 1
            self._save()
            total = len(self._data["accounts"])
        return {"ok": True, "count": total, "changed": changed}

    def count_broken(self) -> dict:
        """统计当前"不可用"的账号数，用于界面提示。"""
        now = _now()
        disabled = cooling = notoken = 0
        with self._lock:
            for acc in self._data["accounts"]:
                if not acc.get("enabled", True):
                    disabled += 1
                    continue
                if int(acc.get("cooling_until", 0)) > now:
                    cooling += 1
                    continue
                has = acc.get("access_token") if acc.get("kind") == "local" else acc.get("api_key")
                if not has:
                    notoken += 1
        return {"disabled": disabled, "cooling": cooling, "no_credential": notoken,
                "total": disabled + cooling + notoken}


    # -- 自动扫描的忽略名单 ----------------------------------------------
    def scan_ignored(self) -> list:
        """被用户手动删掉的本机账号：自动扫描不再把它们加回来。

        手动点「扫描」按钮属于明确意图，会把名单清空后重新扫描。
        """
        with self._lock:
            return list(self._data.get("scan_ignored") or [])

    def clear_scan_ignored(self) -> None:
        with self._lock:
            if self._data.get("scan_ignored"):
                self._data["scan_ignored"] = []
                self._save()

    def _add_scan_ignored(self, identity: str) -> None:
        if not identity:
            return
        ignored = set(self._data.get("scan_ignored") or [])
        ignored.add(str(identity))
        self._data["scan_ignored"] = sorted(ignored)

    def sync_local_accounts(self, discovered: list, ignored=None) -> dict:
        """把 wbcreds 扫到的本机账号并入池子。

        按 uin（其次 uid）去重：已存在则原地更新 token，不存在才新建。
        这样重复扫描不会产生重复条目，重新登录也会就地刷新。
        ignored 里的身份会被跳过（用户手动删过的账号）。
        返回 {'added': n, 'updated': n, 'skipped_ignored': n, 'accounts': [...]}
        """
        added = updated = skipped_ignored = 0
        ignored = set(ignored or [])
        with self._lock:
            index = {}
            for acc in self._data["accounts"]:
                key = acc.get("uin") or acc.get("uid")
                if acc.get("kind") == "local" and key:
                    index[key] = acc

            for item in discovered:
                key = item.get("uin") or item.get("uid")
                if not key:
                    continue
                if key in ignored:
                    skipped_ignored += 1
                    continue

                fields = {
                    "access_token": item.get("access_token", ""),
                    "refresh_token": item.get("refresh_token", ""),
                    "token_expires_at": int(item.get("expires_at") or 0),
                    "uid": item.get("uid", ""),
                    "uin": item.get("uin", ""),
                    "nickname": item.get("nickname", ""),
                    "domain": item.get("domain", ""),
                    "enterprise_id": item.get("enterprise_id", ""),
                }

                existing = index.get(key)
                if existing:
                    existing.update(fields)
                    existing["kind"] = "local"
                    existing["base_url"] = item.get("base_url") or existing.get("base_url") or ""
                    # token 变了说明重新登录过，清掉失败状态
                    existing["consecutive_failures"] = 0
                    existing["cooling_until"] = 0
                    existing["last_error"] = ""
                    updated += 1
                    continue

                acc = _blank_account(item.get("nickname") or ("本机账号 " + str(key)[-6:]))
                acc["kind"] = "local"
                acc["base_url"] = item.get("base_url", "")
                acc["note"] = "从本机 WorkBuddy 读取"
                acc.update(fields)
                self._data["accounts"].append(acc)
                index[key] = acc
                added += 1

            if added or updated:
                self._save()

        return {
            "added": added,
            "updated": updated,
            "skipped_ignored": skipped_ignored,
            "accounts": self.list_accounts(),
        }

    # -- 选择逻辑 ---------------------------------------------------------
    @staticmethod
    def _available_now(acc: dict) -> bool:
        if not acc.get("enabled", True):
            return False
        if acc.get("kind") == "local":
            has_cred = bool(acc.get("access_token"))
        else:
            has_cred = bool(acc.get("api_key"))
        if not has_cred:
            return False
        return int(acc.get("cooling_until", 0)) <= _now()

    def set_credit_info(self, acc_id: str, nearest_end_ms: int, available_credits: float) -> None:
        """由上层把查到的积分过期信息写入缓存（expire_first 策略要用）。

        额度保留两位小数：上游是 72.24 这种带小数的，取整会越算越偏。
        """
        with self._lock:
            _credit_cache[acc_id] = {
                "nearest_end_ms": int(nearest_end_ms or 0),
                "available_credits": round(float(available_credits or 0), 2),
                "at": _now(),
            }

    def get_credit_info(self, acc_id: str) -> dict:
        with self._lock:
            return dict(_credit_cache.get(acc_id) or {})

    def needs_credit_refresh(self, acc_id: str) -> bool:
        with self._lock:
            info = _credit_cache.get(acc_id)
            if not info:
                return True
            return _now() - int(info.get("at", 0)) > CREDIT_CACHE_TTL

    def credit_snapshot(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in _credit_cache.items()}

    # -- 按模型冷却 -----------------------------------------------------
    @staticmethod
    def _model_cooled(acc: dict, model: str) -> bool:
        """该账号在指定模型上是否处于冷却期。

        上游 6004（配额类）是按【模型】计的：
        同一账号在 deepseek-v4.1-flash 上耗尽，换 glm-5.3 依然可用。
        所以不能因为一个模型限流就把整个账号冷却掉。
        """
        if not model:
            return False
        cds = acc.get("model_cooldowns") or {}
        until = int(cds.get(model) or 0)
        if not until:
            return False
        if until <= _now():
            # 已过期，顺手清掉
            cds.pop(model, None)
            acc["model_cooldowns"] = cds
            return False
        return True

    def record_model_cooldown(self, acc_id: str, model: str, seconds: int) -> int:
        """给某账号的某个模型设置冷却，返回到期时间戳。"""
        if not model:
            return 0
        seconds = max(1, min(int(seconds), 24 * 3600))
        with self._lock:
            for acc in self._data["accounts"]:
                if acc["id"] != acc_id:
                    continue
                cds = acc.setdefault("model_cooldowns", {})
                until = _now() + seconds
                cds[model] = until
                self._save()
                return until
        return 0

    def model_cooldown_state(self) -> dict:
        """返回 {account_id: {model: 剩余秒数}}，供界面展示。"""
        now = _now()
        out = {}
        with self._lock:
            for acc in self._data["accounts"]:
                cds = acc.get("model_cooldowns") or {}
                live = {m: int(u) - now for m, u in cds.items() if int(u) > now}
                if live:
                    out[acc["id"]] = live
        return out

    def clear_model_cooldowns(self, acc_id: str | None = None) -> None:
        with self._lock:
            for acc in self._data["accounts"]:
                if acc_id and acc["id"] != acc_id:
                    continue
                acc["model_cooldowns"] = {}
            self._save()

    def candidate_order(self, exclude_ids=(), model: str = "") -> list:
        """按策略给出本次请求的账号尝试顺序（只含可用账号）。

        model 非空时，会排除"该模型被限流"的账号 —— 上游的 6004
        是按模型计额的，账号在别的模型上可能完全正常。
        """
        exclude_ids = set(exclude_ids or ())
        # 先兜底恢复冷却期已过的自动禁用账号
        self.revive_auto_disabled()
        with self._lock:
            accounts = [
                a for a in self._data["accounts"]
                if a["id"] not in exclude_ids
                # 让给外部客户端的账号：网关一律不碰，避免同一个 token 被两边抢
                and not a.get("reserved")
                and self._available_now(a)
                and not self._model_cooled(a, model)
            ]
            if not accounts:
                return []
            policy = self._data.get("policy", "round_robin")

            if policy == "failover":
                return accounts

            if policy == "random":
                shuffled = list(accounts)
                random.shuffle(shuffled)
                return shuffled

            if policy == "expire_first":
                # 按"最近要过期的积分"排前面：先烧掉快过期的，减少浪费。
                # 没有积分信息的账号排在最后（避免它们抢占），
                # 其中又按可用积分从多到少排，保证仍能从有额度的账号拿到服务。
                now_ms = int(time.time() * 1000)

                def sort_key(a):
                    info = _credit_cache.get(a["id"]) or {}
                    end_ms = int(info.get("nearest_end_ms") or 0)
                    credits = float(info.get("available_credits") or 0)
                    # 没有信息 / 额度为 0 / 已过期，都归入"不可优先"组（组号 1），
                    # 组内按可用额度降序，保证仍有额度的账号排前面。
                    # 否则"已过期但额度非 0"会排到"无信息"前面，反而不对。
                    usable = credits > 0 and (end_ms == 0 or end_ms > now_ms)
                    if not usable:
                        return (1, -credits, 0)
                    return (0, end_ms, -credits)

                return sorted(accounts, key=sort_key)

            # round_robin：从游标位置开始环形展开
            cursor = int(self._data.get("pool_cursor", 0)) % len(accounts)
            ordered = accounts[cursor:] + accounts[:cursor]
            self._data["pool_cursor"] = (cursor + 1) % max(1, len(accounts))
            self._save()
            return ordered

    # -- 结果记账 ---------------------------------------------------------
    def record_success(self, acc_id: str) -> None:
        with self._lock:
            for acc in self._data["accounts"]:
                if acc["id"] != acc_id:
                    continue
                acc["requests"] = int(acc.get("requests", 0)) + 1
                acc["successes"] = int(acc.get("successes", 0)) + 1
                acc["consecutive_failures"] = 0
                acc["last_error"] = ""
                acc["last_used_at"] = _now()
                self._save()
                return

    def record_failure(self, acc_id: str, status: int | None, message: str,
                       model: str = "") -> str:
        """记录一次失败，返回 'ok' / 'cooling' / 'model_cooling' / 'disabled'。

        注意：`message` 可能是上游的整段 JSON，界面上直接显示会把表格撑爆。
        这里统一压成一句人能看懂的话，原文只保留很短的一截备查。

        model 非空且错误是"该模型额度耗尽"时，只冷却这个模型，
        不牵连整个账号（上游的 6004 是按模型计的）。
        """
        with self._lock:
            for acc in self._data["accounts"]:
                if acc["id"] != acc_id:
                    continue
                acc["requests"] = int(acc.get("requests", 0)) + 1
                acc["failures"] = int(acc.get("failures", 0)) + 1
                acc["consecutive_failures"] = int(acc.get("consecutive_failures", 0)) + 1
                acc["last_error"] = _summary_error(status, message)
                acc["last_error_detail"] = (message or "")[:600]
                acc["last_used_at"] = _now()
                # 刷新凭证后旧的失败状态就失去意义了
                acc["last_error_at"] = _now()

                # 模型级配额耗尽：只冻结该模型，账号其它模型照常可用
                quota_delay = _quota_reset_delay(message)
                if model and quota_delay:
                    cds = acc.setdefault("model_cooldowns", {})
                    cds[model] = _now() + quota_delay
                    # 配额类失败不算"账号连续失败"，避免误禁用
                    acc["consecutive_failures"] = 0
                    self._save()
                    return "model_cooling"

                threshold = int(self._data.get("max_consecutive_failures", 5))
                if acc["consecutive_failures"] >= threshold:
                    acc["enabled"] = False
                    # 标记为"自动禁用"，才能在恢复时自动放开。
                    # 手动禁用的账号不能被自动复活。
                    acc["auto_disabled"] = True
                    acc["auto_disabled_at"] = _now()
                    state = "disabled"
                elif status is None or status in RETRYABLE_STATUS:
                    acc["cooling_until"] = _now() + int(self._data.get("cooldown_sec", 60))
                    state = "cooling"
                else:
                    state = "ok"
                self._save()
                return state
        return "ok"

    def revive_auto_disabled(self) -> int:
        """把"自动禁用"且已过冷却期的账号重新启用。

        背景：账号因连续失败被自动禁用后，只要 max_consecutive_failures 被调大
        或过了冷却期，它本该恢复，但 enabled=False 是个死标记，不会自愈，
        于是界面上永远显示"已禁用"。这里在每次取账号前兜底恢复一次。
        手动禁用的账号（auto_disabled 为假）不受影响。
        """
        changed = 0
        now = _now()
        with self._lock:
            for acc in self._data["accounts"]:
                if not acc.get("auto_disabled"):
                    continue
                if acc.get("enabled", True):
                    acc["auto_disabled"] = False
                    continue
                cooldown = int(self._data.get("cooldown_sec", 60))
                disabled_at = int(acc.get("auto_disabled_at", 0))
                if now - disabled_at >= cooldown:
                    acc["enabled"] = True
                    acc["auto_disabled"] = False
                    acc["consecutive_failures"] = 0
                    acc["cooling_until"] = 0
                    acc["last_error"] = ""
                    changed += 1
            if changed:
                self._save()
        return changed


def _quota_reset_delay(message: str) -> int:
    """从上游的配额耗尽提示里解析出"还要等多久"，返回秒数。

    上游原文形如：
        "您的使用量已超出频率限制，将在 2026-10-21 09:42:19 UTC+8 重置，..."
    解析成功返回剩余秒数（上限 24 小时）；解析不了返回 0，由调用方用默认值。
    """
    if not message:
        return 0
    text = str(message)

    # 只对配额类错误生效；普通的 429 限流仍是短冷却
    if "6004" not in text and "超出频率限制" not in text and "quota" not in text.lower():
        return 0

    import re
    from datetime import datetime, timedelta, timezone

    # 抓形如 2026-10-21 09:42:19
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})", text)
    if not m:
        return 0

    try:
        y, mo, d, hh, mm, ss = (int(x) for x in m.groups())
        # 上游明确写了 UTC+8
        tz = timezone(timedelta(hours=8)) if "UTC+8" in text else None
        target = datetime(y, mo, d, hh, mm, ss, tzinfo=tz)
        now = datetime.now(tz) if tz else datetime.now()
        delay = int((target - now).total_seconds())
        if delay <= 0:
            return 60
        return min(delay, 24 * 3600)
    except Exception:
        return 0


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "••••"
    return "••••" + key[-4:]


# 上游业务码 -> 人话。按顺序匹配，先命中的优先。
_ERROR_HINTS = (
    ("11115", "上下文超长（超过模型上限）", "prompt_too_long"),
    ("prompt is too long", "上下文超长（超过模型上限）", "prompt_too_long"),
    ("11150", "模型不支持该思考强度", "bad_request"),
    ("11128", "请求被上游渠道拒绝", "blocked"),
    ("11101", "上游不支持非流式请求", "bad_request"),
    ("11125", "请求参数不合法", "bad_request"),
)


def _reformat_legacy_error(raw: str) -> str:
    """把老版本存下来的整段错误原文压成一句话。"""
    text = (raw or "").strip()
    if not text:
        return ""
    status = None
    if text.startswith("HTTP "):
        head, _, rest = text.partition(":")
        try:
            status = int(head.replace("HTTP", "").strip())
        except ValueError:
            status = None
        text = rest.strip()
    return _summary_error(status, text)


def _summary_error(status, message: str) -> str:
    """把上游返回压成一句话，供界面显示。

    返回形如：「400 · 上下文超长（超过模型上限）」
    以及 SSL 断连这类网络错误：「网络中断（连接被重置）」
    """
    detail = message or ""
    lowered = detail.lower()

    # 网络层：没有 HTTP 状态码
    if status is None:
        if "ssl" in lowered or "unexpected_eof" in lowered:
            return "网络中断（SSL 连接被重置）"
        if "timed out" in lowered or "timeout" in lowered:
            return "网络超时"
        if "reset" in lowered or "aborted" in lowered or "refused" in lowered:
            return "网络中断（连接被重置）"
        if "getaddrinfo" in lowered or "name or service" in lowered:
            return "DNS 解析失败"
        return "网络错误"

    for token, label, _kind in _ERROR_HINTS:
        if token in detail or token in lowered:
            return "%s · %s" % (status, label)

    if status == 429:
        return "429 · 触发限流"
    if status in (401, 403):
        return "%s · 凭证失效或无权限" % status
    if status == 402:
        return "402 · 额度不足"
    if status >= 500:
        return "%s · 上游服务异常" % status

    # 其它情况：截取上游 msg 字段（如果有），否则只给状态码
    try:
        import json as _json

        obj = _json.loads(detail)
        msg = obj.get("msg") or obj.get("message") or ""
        if msg:
            return "%s · %s" % (status, str(msg)[:80])
    except Exception:
        pass

    if detail:
        return "%s · %s" % (status, detail.replace("\n", " ")[:80])
    return "%s · 请求失败" % status
