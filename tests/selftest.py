"""端到端自测：账号池 / 代理开关 / 管理 API / Codex 工具闭环。

用法：
    python selftest.py
退出码 0 表示全部通过。
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))          # tests/
ROOT = os.path.dirname(HERE)                               # 项目根
PY = sys.executable


def find_codex():
    """找到可用的 codex.exe。应用每次更新会换哈希目录，不能写死。

    优先级：
      1) 环境变量 CODEX_BIN / CODEX_EXE 直接指定
      2) %LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe（标准安装位置）
    """
    import glob

    for env_key in ("CODEX_BIN", "CODEX_EXE"):
        direct = os.environ.get(env_key)
        if direct and os.path.exists(direct):
            return direct

    local = os.environ.get("LOCALAPPDATA") or ""
    pinned = os.path.join(local, "OpenAI", "Codex", "bin") if local else ""
    patterns = [os.path.join(pinned, "*", "codex.exe")] if pinned else []
    hits = []
    for pat in patterns:
        hits.extend(glob.glob(pat))
    hits = [h for h in hits if os.path.exists(h)]
    if not hits:
        return None
    # 取最新的
    hits.sort(key=os.path.getmtime, reverse=True)
    return hits[0]


CODEX = find_codex()

GW_PORT = 18090
MOCK_A = 18791
MOCK_B = 18792
GATE_TOKEN = "test-gateway-token"

FAILS = []


def check(name, ok, detail=""):
    print("%-34s %s %s" % (name, "PASS" if ok else "FAIL", detail if not ok else ""))
    if not ok:
        FAILS.append(name)


def wait(url, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def req(path, method="GET", body=None, timeout=60, token=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    r = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (GW_PORT, path),
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def responses_payload():
    return {
        "model": "mock-model",
        "input": [{"type": "message", "role": "user", "content": "hi"}],
    }


def main():
    # 干净起步：只清理自测专用文件，绝不碰真实 data.json
    for name in ("status.json", "selftest.log"):
        p = os.path.join(HERE, name)
        if os.path.exists(p):
            os.remove(p)

    env = dict(os.environ)
    SELF_DATA = os.path.join(HERE, "data-selftest.json")
    if os.path.exists(SELF_DATA):
        os.remove(SELF_DATA)
    env.update({
        "WB_LISTEN": "127.0.0.1:%d" % GW_PORT,
        "WB_MODELS": "mock-model",
        "WB_APPROVAL_KEY": GATE_TOKEN,
        # 关键：用独立数据文件，绝不碰真实账号池。
        # 早先共用 data.json，把 mock 账号写进了真实池子，
        # 导致真实请求被路由到 127.0.0.1 的假服务器上。
        "WB_DATA_PATH": SELF_DATA,
        # 关掉启动自动扫描：否则会把本机真实账号扫进自测用的隔离数据文件，
        # 测试就从"纯 mock"变成"看这台机器装了谁的 WorkBuddy"。
        "WB_NO_AUTO_SCAN": "1",
    })

    procs = []
    log = open(os.path.join(HERE, "selftest.log"), "wb")
    try:
        for port in (MOCK_A, MOCK_B):
            procs.append(subprocess.Popen([PY, os.path.join(HERE, "mock_chat.py"), str(port)],
                                          stdout=log, stderr=log))
        procs.append(subprocess.Popen([PY, os.path.join(ROOT, "gateway.py")], env=env,
                                      stdout=log, stderr=log))

        check("网关启动", wait("http://127.0.0.1:%d/health" % GW_PORT))

        # --- UI 资源 ---
        for asset in ("/", "/assets/app.css", "/assets/app.js"):
            st, body = req(asset)
            check("静态资源 %s" % asset, st == 200 and len(body) > 100, "status=%s" % st)

        st, a1 = req("/admin/api/accounts", "POST",
                     {"name": "A", "base_url": "http://127.0.0.1:%d" % MOCK_A,
                      "api_key": "sk-aaaa1111", "models": "mock-model"})
        check("新增账号 A", st == 200)
        st, a2 = req("/admin/api/accounts", "POST",
                     {"name": "B", "base_url": "http://127.0.0.1:%d" % MOCK_B,
                      "api_key": "sk-bbbb2222", "models": "mock-model"})
        check("新增账号 B", st == 200)
        aid = a1["account"]["id"] if isinstance(a1, dict) else ""

        # --- 签到与积分接口 ---
        st, credit = req("/admin/api/credit-summary", "POST", {})
        check("积分汇总接口", st == 200 and credit.get("ok") is True,
              "status=%s" % st)
        # 无 token 的测试账号应优雅降级，而不是 500
        bad_entries = [a for a in credit.get("accounts", []) if not a.get("ok")]
        check("无 token 账号优雅降级",
              all(a.get("error") for a in bad_entries),
              "bad=%s" % bad_entries)

        st, ci = req("/admin/api/checkin-all", "POST", {})
        check("一键签到接口", st == 200 and "checked" in ci,
              "status=%s" % st)

        st, one = req("/admin/api/accounts/%s/checkin" % aid, "POST", {})
        check("单账号签到接口", st == 200 and "ok" in one,
              "status=%s" % st)

        st, state = req("/admin/api/state")
        check("Key 默认掩码", state["accounts"][0]["api_key"].startswith("••••"))

        # --- 网关准入（approval_key）---
        st, _ = req("/v1/responses", "POST", responses_payload())
        check("缺 approval_key 被拒(401)", st == 401, "status=%s" % st)
        st, _ = req("/v1/responses", "POST", responses_payload(), token="wrong")
        check("错 approval_key 被拒(401)", st == 401, "status=%s" % st)

        # --- 代理开关 ---
        req("/admin/api/settings", "PATCH", {"proxy_enabled": False})
        st, _ = req("/v1/responses", "POST", responses_payload(), token=GATE_TOKEN)
        check("暂停时代理拒流(503)", st == 503, "status=%s" % st)

        req("/admin/api/settings", "PATCH", {"proxy_enabled": True})
        st, body = req("/v1/responses", "POST", responses_payload(), token=GATE_TOKEN)
        check("恢复后正常响应", st == 200 and "response.completed" in body)

        # --- 轮换 ---
        req("/admin/api/settings", "PATCH", {"policy": "round_robin"})
        for _ in range(2):
            req("/v1/responses", "POST", responses_payload(), token=GATE_TOKEN)
        st, state = req("/admin/api/state")
        used = [a for a in state["accounts"] if a["requests"] > 0]
        check("轮询覆盖多个账号", len(used) >= 2, "used=%d" % len(used))

        # --- 故障转移 ---
        st, bad = req("/admin/api/accounts", "POST",
                      {"name": "坏号", "base_url": "http://127.0.0.1:9", "api_key": "sk-bad"})
        bid = bad["account"]["id"]
        req("/admin/api/settings", "PATCH", {"policy": "failover", "max_consecutive_failures": 1,
                                             "cooldown_sec": 1})
        st, _ = req("/v1/responses", "POST", responses_payload(), token=GATE_TOKEN)
        check("坏账号不阻断请求", st == 200, "status=%s" % st)

        # 让坏号成为首选：禁用 A/B，只留坏号可用，连打两次触发自动禁用。
        req("/admin/api/settings", "PATCH", {"cooldown_sec": 60})
        for acc in req("/admin/api/state")[1]["accounts"]:
            if acc["id"] != bid:
                req("/admin/api/accounts/%s" % acc["id"], "PATCH", {"enabled": False})
        for _ in range(2):
            req("/v1/responses", "POST", responses_payload(), timeout=30, token=GATE_TOKEN)
        st, state = req("/admin/api/state")
        bad_acc = [a for a in state["accounts"] if a["id"] == bid][0]
        check("连续失败自动禁用", bad_acc["enabled"] is False,
              "enabled=%s consec=%s" % (bad_acc["enabled"], bad_acc["consecutive_failures"]))

        # --- 重置 ---
        req("/admin/api/accounts/%s/reset" % bid, "POST")
        st, state = req("/admin/api/state")
        bad_acc = [a for a in state["accounts"] if a["id"] == bid][0]
        check("重置恢复启用", bad_acc["enabled"] is True and bad_acc["cooling_in"] == 0)

        # --- 积分过期优先策略（此处只验证 API 层；排序逻辑见 test_expire_first）---
        check("expire_first 策略可设置",
              req("/admin/api/settings", "PATCH", {"policy": "expire_first"})[0] == 200)
        st, state = req("/admin/api/state")
        check("策略已生效", state["settings"]["policy"] == "expire_first")
        req("/admin/api/settings", "PATCH", {"policy": "round_robin"})


        st, state = req("/admin/api/state")
        check("broken 统计存在", "broken" in state and "total" in state["broken"])
        # 人为制造两个异常：禁用 A、给 B 加冷却
        req("/admin/api/accounts/%s" % aid, "PATCH", {"enabled": False})
        st, state = req("/admin/api/state")
        check("异常计数上升", state["broken"]["total"] >= 1,
              "broken=%s" % state["broken"])
        st, res = req("/admin/api/accounts/reset-all", "POST")
        st, state = req("/admin/api/state")
        check("一键恢复全部", res.get("ok") is True and state["broken"]["total"] == 0,
              "changed=%s broken=%s" % (res.get("changed"), state["broken"]))

        # --- 删除 ---
        st, _ = req("/admin/api/accounts/%s" % bid, "DELETE")
        st, state = req("/admin/api/state")
        check("删除账号", all(a["id"] != bid for a in state["accounts"]))

        # --- 测试按钮 ---
        st, res = req("/admin/api/accounts/%s/test" % aid, "POST")
        check("账号连通性测试", res.get("ok") is True, str(res)[:80])

        # --- 复原池子（前面的禁用测试会把 A/B 关掉）---
        req("/admin/api/settings", "PATCH", {"policy": "round_robin", "cooldown_sec": 60})
        for acc in req("/admin/api/state")[1]["accounts"]:
            req("/admin/api/accounts/%s" % acc["id"], "PATCH", {"enabled": True})
        st, state = req("/admin/api/state")
        check("池子恢复可用", any(a["available"] for a in state["accounts"]))
        # 诊断：Codex 步骤前打印真实状态，便于定位 502
        if os.environ.get("SELFTEST_VERBOSE"):
            for a in state["accounts"]:
                print("   [dbg] %-6s enabled=%s available=%s model_cd=%s" % (
                    a.get("name"), a.get("enabled"), a.get("available"),
                    a.get("model_cooldowns")))

        # --- Codex 真实客户端 + 工具闭环 ---
        if not CODEX:
            print("%-34s SKIP 未找到 codex.exe" % "Codex 走网关")
            return 0 if not FAILS else 1
        # 生成一个只含 mock-model 的临时模型目录。
        # 不能直接沿用用户的 models.json（里面没有 mock-model），
        # 也不能用空字符串覆盖（Codex 会当作当前目录并报「拒绝访问」）。
        test_catalog = os.path.join(HERE, "models-selftest.json")
        base_catalog = json.load(open(os.path.join(ROOT, "models.json"), encoding="utf-8"))
        tmpl = json.loads(json.dumps(base_catalog["models"][0]))
        tmpl["slug"] = "mock-model"
        tmpl["display_name"] = "Mock Model"
        tmpl["context_window"] = 1000000
        tmpl["max_context_window"] = 1000000
        json.dump({"models": [tmpl]}, open(test_catalog, "w", encoding="utf-8"),
                  ensure_ascii=False)

        cmd = [
            CODEX,
            # name 必须显式给出，否则 Codex 报 "provider name must not be empty"
            "-c", 'model_providers.wb.name="Selftest"',
            "-c", 'model_providers.wb.base_url="http://127.0.0.1:%d/v1"' % GW_PORT,
            "-c", 'model_providers.wb.wire_api="responses"',
            "-c", 'model_providers.wb.experimental_bearer_token="%s"' % GATE_TOKEN,
            "-c", 'model_provider="wb"',
            "-c", 'model="mock-model"',
            # 必须覆盖 catalog：否则继承用户的真实目录，其中没有 mock-model，
            # Codex 会认为模型无效、根本不发请求（网关侧看不到流量，客户端报 502）。
            "-c", "model_catalog_json=%s" % json.dumps(test_catalog),
            "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
            "run echo SELFTEST_OK",
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                             encoding="utf-8", errors="replace")
        blob = (out.stdout or "") + (out.stderr or "")
        check("Codex 走网关", "provider: wb" in blob)
        check("工具调用闭环", "exec" in blob and "echo SELFTEST_OK" in blob)
        check("模型回包", "GATEWAY_OK" in blob)

        return 0 if not FAILS else 1
    finally:
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        log.close()
        for name in ("data-selftest.json", "models-selftest.json", "status.json", "selftest.log"):
            p = os.path.join(HERE, name)
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


if __name__ == "__main__":
    # 中途抛异常（比如网关处理请求时 NameError 把连接掐了）以前会直接打出
    # traceback 然后退出，用 grep FAIL 的人会以为"没有失败项"。这里显式标出来。
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback

        print("\nFAIL 测试中途中止：%s: %s" % (type(exc).__name__, exc))
        traceback.print_exc()
        sys.exit(1)
