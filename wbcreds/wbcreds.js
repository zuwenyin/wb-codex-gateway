/**
 * wbcreds.js — 本机 WorkBuddy 账号服务（供网关调用）
 *
 * 子命令：
 *   --dump        输出账号 JSON（token 已解密）
 *   --doctor      探测环境：auth 目录、app 路径
 *   --models      拉取该账号可用的模型目录（需网络）
 *   --test        用首个账号实测一次流式对话
 */
"use strict";

const { dumpCreds, appCandidates, authDirs, authFiles, authFileReport } = require("./creds-core.js");

const CN_CHAT_BASE = "https://copilot.tencent.com";
const CN_BILLING_BASE = "https://www.codebuddy.cn";
const CLIENT_UA = "CLI/2.63.2 CodeBuddy/2.63.2";

function regionOf(domain) {
  const d = (domain || "").trim().toLowerCase();
  if (d === "codebuddy.ai" || d.endsWith(".codebuddy.ai")) return "global";
  if (d === "workbuddy.ai" || d.endsWith(".workbuddy.ai")) return "global";
  return "cn";
}

function chatBase(acct) {
  return regionOf(acct.domain) === "global" ? "https://www.workbuddy.ai" : CN_CHAT_BASE;
}

function originFor(acct) {
  return regionOf(acct.domain) === "global" ? "https://www.workbuddy.ai" : CN_BILLING_BASE;
}

function commonHeaders(acct) {
  const origin = originFor(acct);
  return {
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": origin,
    "Referer": origin + "/",
    "User-Agent": CLIENT_UA,
  };
}

function chatHeaders(acct) {
  const h = {
    ...commonHeaders(acct),
    "Content-Type": "application/json",
    "Authorization": "Bearer " + acct.access_token,
  };
  if (acct.uid) h["X-User-Id"] = acct.uid;
  else h["X-No-User-Id"] = "1";
  if (acct.enterprise_id) h["X-Enterprise-Id"] = acct.enterprise_id;
  else h["X-No-Enterprise-Id"] = "1";
  if (acct.domain) h["X-Domain"] = acct.domain;
  else h["X-No-Department-Info"] = "1";
  h["X-Product"] = "SaaS";
  return h;
}

async function fetchConfig(acct) {
  const r = await fetch(chatBase(acct) + "/v3/config", {
    headers: commonHeaders(acct) && chatHeaders(acct),
    signal: AbortSignal.timeout(30000),
  });
  if (!r.ok) throw new Error("config http " + r.status);
  const j = await r.json();
  if (j.code !== 0) throw new Error("config code " + j.code + " " + (j.msg || ""));
  return j.data || {};
}

async function main() {
  const mode = process.argv[2] || "--dump";

  if (mode === "--doctor") {
    // 只看环境，不启动桌面端（快）。加 --deep 才实际尝试解密一次。
    const report = {
      ok: true,
      platform: process.platform,
      node: process.version,
      user: process.env.USERNAME || process.env.USER || "",
      home: require("os").homedir(),
      localappdata: process.env.LOCALAPPDATA || null,
      appdata: process.env.APPDATA || null,
      auth_dirs: authFileReport(),
      auth_files: authFiles(),
      app_candidates: appCandidates(),
      env_override: process.env.WORKBUDDY_APP_EXECUTABLE || null,
    };
    if (process.argv.includes("--deep")) {
      const res = dumpCreds();
      report.dump = {
        ok: res.ok,
        code: res.code || null,
        error: res.error || null,
        count: res.count || 0,
        accounts: (res.accounts || []).map((a) => ({
          uin: a.uin, uid: a.uid, nickname: a.nickname,
          has_token: !!a.access_token, is_live: a.is_live,
        })),
        skipped: res.skipped || [],
      };
    }
    console.log(JSON.stringify(report, null, 2));
    return;
  }

  const res = dumpCreds();

  if (mode === "--dump") {
    console.log(JSON.stringify(res));
    return;
  }

  if (!res.ok) {
    console.log(JSON.stringify(res));
    process.exit(2);
  }
  if (!res.accounts.length) {
    console.log(JSON.stringify({
      ok: false,
      code: "NO_ACCOUNT",
      error: "找到 WorkBuddy 安装，但没有可用的登录账号。请先在 WorkBuddy 应用中登录。",
    }));
    process.exit(3);
  }

  const acct = res.accounts.find((a) => a.is_live) || res.accounts[0];

  if (mode === "--models") {
    try {
      const data = await fetchConfig(acct);
      const models = (data.models || []).map((m) => ({
        id: m.id,
        name: m.name || m.id,
        credits: m.credits,
        supports_images: m.supportsImages === true,
        supports_tool_call: m.supportsToolCall !== false,
        reasoning: m.reasoning || null,
        only_reasoning: m.onlyReasoning === true,
      }));
      console.log(JSON.stringify({
        ok: true,
        account: { uid: acct.uid, uin: acct.uin, nickname: acct.nickname },
        count: models.length,
        models,
      }));
    } catch (err) {
      console.log(JSON.stringify({ ok: false, code: "CONFIG_FAIL", error: String(err.message || err) }));
      process.exit(4);
    }
    return;
  }

  if (mode === "--test") {
    const model = process.argv[3] || "deepseek-v4-flash";
    const body = JSON.stringify({
      model,
      messages: [{ role: "user", content: "请只回复两个字：正常" }],
      stream: true,
      max_tokens: 16,
    });
    const started = Date.now();
    try {
      const r = await fetch(chatBase(acct) + "/v2/chat/completions", {
        method: "POST",
        headers: chatHeaders(acct),
        body,
        signal: AbortSignal.timeout(60000),
      });
      if (!r.ok) {
        const txt = await r.text().catch(() => "");
        console.log(JSON.stringify({ ok: false, status: r.status, error: txt.slice(0, 400), ms: Date.now() - started }));
        process.exit(5);
      }
      // 只读一小段，确认真的在产出
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let got = "";
      while (got.length < 300) {
        const { done, value } = await reader.read();
        if (done) break;
        got += dec.decode(value, { stream: true });
        if (got.includes('"content":"')) {
          const m = /"content":"([^"\\]{1,40})/.exec(got);
          if (m && m[1]) break;
        }
      }
      try { reader.cancel(); } catch { /* ignore */ }
      console.log(JSON.stringify({
        ok: true,
        model,
        account: acct.nickname || acct.uin,
        ms: Date.now() - started,
        sample: got.slice(0, 200),
      }));
    } catch (err) {
      console.log(JSON.stringify({ ok: false, error: String(err.message || err), ms: Date.now() - started }));
      process.exit(6);
    }
    return;
  }

  console.log(JSON.stringify({ ok: false, error: "unknown mode: " + mode }));
  process.exit(64);
}

main().catch((err) => {
  console.log(JSON.stringify({ ok: false, error: String(err && err.message || err) }));
  process.exit(70);
});
