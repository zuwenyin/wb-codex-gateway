"""本机 WorkBuddy 账号的扫描与调度（含自动扫描）。

从原 gateway.py 按职责拆出，逻辑未改动。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time

from . import context as ctx

def node_candidates() -> list:
    """node.exe 的所有候选位置（按可靠性排序）。

    为什么不能只靠 PATH：用户可能装在 PowerShell 配置文件里（fnm/volta/nvm
    只改了 PS 的 PATH），而网关是 CMD 起的；也可能装完 node 没有重开窗口。
    这两种情况下 `node -v` 在终端里正常，网关却报 WinError 2。
    """
    out = []
    for env_key in ("WB_NODE_PATH", "NODE_PATH_BIN"):
        direct = (os.environ.get(env_key) or "").strip()
        if direct:
            out.append(direct)

    configured = (ctx.CFG.get("node_path") or "").strip()
    if configured:
        out.append(configured)

    local = os.environ.get("LOCALAPPDATA") or ""
    roaming = os.environ.get("APPDATA") or ""
    program_files = os.environ.get("ProgramFiles") or r"C:\Program Files"
    program_files_x86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"

    import glob as _glob

    # 官方 MSI 安装位置
    out.append(os.path.join(program_files, "nodejs", "node.exe"))
    out.append(os.path.join(program_files_x86, "nodejs", "node.exe"))
    out.append(os.path.join(local, "Programs", "nodejs", "node.exe"))
    # 项目旁边的便携版（作者本机布局，存在才命中）
    out.append(os.path.join(ctx.ROOT, "..", "..", "node", "node.exe"))

    # 版本管理器
    if roaming:
        out += _glob.glob(os.path.join(roaming, "nvm", "v*", "node.exe"))
    if local:
        out += _glob.glob(os.path.join(local, "fnm_multishells", "*", "node.exe"))
        out += _glob.glob(os.path.join(local, "Volta", "bin", "node.exe"))
        out += _glob.glob(os.path.join(local, "mise", "installs", "node", "*", "node.exe"))
        # 常见 WorkBuddy 自带的 node
        out += _glob.glob(
            os.path.join(local, ".workbuddy", "binaries", "node", "versions", "*", "node.exe"))
    home = os.path.expanduser("~")
    out += _glob.glob(os.path.join(home, "scoop", "apps", "nodejs", "*", "node.exe"))
    out += _glob.glob(os.path.join(home, ".volta", "bin", "node.exe"))

    # PATH（Windows 上 which("node") 会自动补 .exe）
    found = shutil.which("node") or shutil.which("node.exe")
    if found:
        out.append(found)

    # 去重且保持顺序（nvm 的新版本在前）
    seen = set()
    result = []
    for path in out:
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def node_bin() -> str:
    """返回可用的 node 路径；实在找不到时返回 "node"（交给 PATH 再试一次）。"""
    for cand in node_candidates():
        try:
            if os.path.isfile(cand):
                return cand
        except OSError:
            continue
    return "node"


def run_wbcreds(*args, timeout: int = 60) -> dict:
    """运行 wbcreds.js 并解析 JSON 输出。"""
    script = ctx.WBCREDS_JS
    if not os.path.exists(script):
        return {"ok": False, "code": "NO_SCRIPT", "error": "缺少 wbcreds.js"}

    node = node_bin()
    # 连 "node" 都不在 PATH 时直接给出可执行的提示，别让 WinError 2 糊一脸
    if not os.path.isabs(node) and shutil.which(node) is None:
        return {
            "ok": False,
            "code": "NO_NODE",
            "error": (
                "找不到 Node.js（node.exe）：当前进程的 PATH 里没有 node，"
                "常见安装目录也没找到。若终端里 node -v 正常，多半是网关在装 node 之前就启动了"
            ),
            "hint": "关掉窗口重新运行 start-gateway.cmd；或用 set WB_NODE_PATH=<node.exe 完整路径> 指定",
            "tried_node": node_candidates()[:6],
        }

    cmd = [node, script, *args]
    if node.lower().endswith((".cmd", ".bat")):
        # 某些版本管理器给的是批处理 shim，CreateProcess 直接跑会失败
        cmd = ["cmd", "/c", node, script, *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            cwd=os.path.dirname(ctx.WBCREDS_JS),
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "code": "NO_NODE",
            "error": "找不到 Node.js（node.exe）：扫描本机账号需要它，但系统解析不到该文件",
            "hint": "安装 Node.js LTS 后重开窗口；或 set WB_NODE_PATH=<node.exe 的完整路径>",
            "tried_node": [node],
        }
    except Exception as exc:
        return {"ok": False, "code": "EXEC_FAIL", "error": str(exc)}

    raw = (proc.stdout or "").strip()
    if not raw:
        return {
            "ok": False,
            "code": "EMPTY",
            "error": (proc.stderr or "wbcreds 无输出").strip()[:500],
        }
    try:
        return json.loads(raw)
    except Exception:
        return {"ok": False, "code": "BAD_JSON", "error": raw[:500]}


def scan_preflight() -> dict:
    """扫描前置条件自检：Node 在不在、登录态文件找到没、桌面端 exe 找到没。

    这三件事任何一件不成立，扫描都拿不到账号 —— 而用户看到的往往只是
    "扫描完成：无变化（共 0 个）"，远程根本没法定位。启动时先查一遍并写日志。
    """
    info = {
        "node": node_bin(),
        "node_ok": False,
        "auth_files": [],
        "auth_dirs": [],
        "app_candidates": [],
        "error": "",
    }
    result = run_wbcreds("--doctor", timeout=30)
    if not result.get("ok"):
        info["error"] = result.get("error") or result.get("code") or "检测失败"
        # Node 起不来时，错误信息里通常带 [WinError 2] 之类
        info["node_ok"] = False
        return info
    info["node_ok"] = True
    info["auth_files"] = result.get("auth_files") or []
    info["auth_dirs"] = result.get("auth_dirs") or []
    info["app_candidates"] = result.get("app_candidates") or []
    # 身份信息很重要：用"另一个管理员账户"提权运行时，这里的目录会变成别人的
    info["user"] = result.get("user") or ""
    info["home"] = result.get("home") or ""
    info["localappdata"] = result.get("localappdata") or ""
    return info


def log_scan_preflight() -> None:
    """把自检结果翻译成人能看懂的告警。"""
    info = scan_preflight()
    if not info["node_ok"]:
        ctx.log("warn", "找不到 Node.js，无法扫描本机账号（%s）" % (info["error"] or "node 不可用"))
        ctx.log("warn", "  安装 Node.js，或用 WB_NODE_PATH 指定 node.exe 路径")
        return
    if not info["auth_files"]:
        ctx.log("warn", "没有找到 WorkBuddy 登录态文件（workbuddy-desktop.info）")
        ctx.log("warn", "  当前身份：%s（%%LOCALAPPDATA%%=%s）"
                % (info.get("user") or "?", info.get("localappdata") or "?"))
        ctx.log("warn", "  请确认：① WorkBuddy 桌面端已登录 ② 用同一个 Windows 账号运行网关")
        ctx.log("warn", "  （用「以其他用户身份运行」提权会切到别人的用户目录，登录态就读不到了）")
        tried = [d for d in info["auth_dirs"] if d.get("files")]
        if not tried:
            ctx.log("warn", "  已探测目录：%s" % "、".join(d["dir"] for d in info["auth_dirs"][:3]))
        ctx.log("warn", "  仍找不到时用 WORKBUDDY_AUTH_FILE=<目录> 指定")
        return
    if not info["app_candidates"]:
        ctx.log("warn", "找到登录态，但没找到 WorkBuddy 桌面端 exe（解密密钥需要它）")
        ctx.log("warn", "  用 WORKBUDDY_APP_EXECUTABLE=<WorkBuddy.exe 完整路径> 指定")
        return
    ctx.log("info", "扫描自检通过：登录态 %d 个文件，桌面端 %s"
            % (len(info["auth_files"]), os.path.basename(info["app_candidates"][0])))


def scan_local_accounts(manual: bool = False) -> dict:
    """扫描本机 WorkBuddy 账号并合并进池子。

    manual=False 表示后台自动扫描：跳过用户手动删过的账号。
    manual=True 是用户点了扫描按钮 —— 清空忽略名单后重新扫。
    """
    result = run_wbcreds("--dump", timeout=90)
    if not result.get("ok"):
        return result

    if manual:
        ctx.STORE.clear_scan_ignored()
    ignored = ctx.STORE.scan_ignored()

    discovered = []
    for item in result.get("accounts") or []:
        discovered.append(
            {
                "uin": item.get("uin", ""),
                "uid": item.get("uid", ""),
                "nickname": item.get("nickname", ""),
                "access_token": item.get("access_token", ""),
                "refresh_token": item.get("refresh_token", ""),
                "expires_at": item.get("expires_at", 0),
                "domain": item.get("domain", ""),
                "enterprise_id": item.get("enterprise_id", ""),
                "base_url": ctx.CFG.get("upstream_base") or "https://copilot.tencent.com",
            }
        )
    if not discovered:
        return {
            "ok": False,
            "code": "NO_ACCOUNT",
            "error": "找到 WorkBuddy 安装，但没有可用的登录账号，请先在 WorkBuddy 里登录。",
            "app": result.get("app", ""),
        }

    merged = ctx.STORE.sync_local_accounts(discovered, ignored=ignored)
    return {
        "ok": True,
        "app": result.get("app", ""),
        "added": merged["added"],
        "updated": merged["updated"],
        "skipped_ignored": merged["skipped_ignored"],
        "count": len(discovered),
    }


AUTO_SCAN_MIN_GAP = 300       # 两次自动扫描的最小间隔（秒）


_last_auto_scan = 0.0         # 上次自动扫描时刻


_auto_scan_lock = threading.Lock()


def has_usable_local_account() -> bool:
    """池子里是否还有一个能用的本机账号。"""
    for acc in ctx.STORE.list_accounts():
        if acc.get("kind") != "local" or not acc.get("enabled", True):
            continue
        if acc.get("access_token"):
            return True
    return False


def auto_scan_if_needed(reason: str) -> bool:
    """没有可用的本机账号时才扫一次。返回是否真的扫了。

    扫描要启动 WorkBuddy 桌面端取密钥（几秒），所以：
    * 有可用账号就完全不动；
    * 两次扫描之间至少间隔 AUTO_SCAN_MIN_GAP；
    * 只在后台线程里跑，不阻塞请求。
    """
    global _last_auto_scan

    # 自测 / 无人值守场景必须能关掉：否则会把本机真实账号扫进隔离的测试数据里
    if (os.environ.get("WB_NO_AUTO_SCAN") or "").strip() not in ("", "0"):
        return False
    if has_usable_local_account():
        return False
    now = time.time()
    with _auto_scan_lock:
        if now - _last_auto_scan < AUTO_SCAN_MIN_GAP:
            return False
        _last_auto_scan = now

    ctx.log("info", "%s，自动扫描本机 WorkBuddy 账号…" % reason)
    try:
        result = scan_local_accounts()
    except Exception as exc:
        ctx.log("warn", "自动扫描失败：%s" % exc)
        return True
    if result.get("ok"):
        ctx.log("info", "自动扫描完成：新增 %s 个、刷新 %s 个"
            % (result.get("added"), result.get("updated")))
    elif result.get("code") == "NO_NODE":
        ctx.log("warn", "自动扫描不可用：%s" % result.get("error"))
        ctx.log("warn", "  %s" % (result.get("hint") or "安装 Node.js 或设置 WB_NODE_PATH"))
    else:
        ctx.log("warn", "自动扫描未发现账号：%s"
            % (result.get("error") or result.get("code") or "未知原因"))
    return True

