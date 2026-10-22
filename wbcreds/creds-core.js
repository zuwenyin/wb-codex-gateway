/**
 * creds-core.js — 从本机 WorkBuddy 桌面端读取并解密账号凭证（可复用模块）
 *
 * 供 wbcreds.js（CLI）与网关（Python 通过子进程调用）共用。
 *
 * 桌面端可执行文件的定位逻辑参考了 dsh-workbuddy-xdpool 1.8.3：
 *   1. 环境变量显式指定（用户自己知道装哪了，最可信）
 *   2. 卸载注册表（安装程序自己记的，含自定义安装目录，如 D:\Program Files\WorkBuddy）
 *   3. 派生兜底：所有固定盘符的 Program Files / 盘根 + 名字像 WorkBuddy 的目录
 * 只读不写桌面端的任何文件。
 */
"use strict";

const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync } = require("child_process");

const AAD_DOMAIN = Buffer.from("WB-AAD\0", "ascii");
const FORMAT_ID_FIELD = "WBEV1";
const SYMMETRIC_SCHEME = "sym-v1";
const FRAMING_CODE_FIELD = 2;
const LIVE_FILENAME = "workbuddy-desktop.info";
const APP_NAMES = ["WorkBuddy.exe", "WorkBuddyAI.exe"];
/** 盘根目录下"看起来像 WorkBuddy 安装目录"的名字 */
const APP_DIR_RE = /^(workbuddy|codebuddy)/iu;
/** 探测单个可执行文件取密钥的超时 */
const KEY_FETCH_TIMEOUT_MS = 30000;

function u32(n) {
  const b = Buffer.allocUnsafe(4);
  b.writeUInt32BE(n >>> 0);
  return b;
}

function lp(str) {
  const b = Buffer.from(str, "utf8");
  return Buffer.concat([u32(b.length), b]);
}

function fieldAad(keyId, suite) {
  if (!/^[0-9a-f]{16}$/.test(keyId)) throw new Error("envelope keyId malformed");
  return Buffer.concat([
    AAD_DOMAIN,
    Buffer.from([1]),
    lp(FORMAT_ID_FIELD),
    lp(SYMMETRIC_SCHEME),
    u32(suite),
    lp(keyId),
    Buffer.from([FRAMING_CODE_FIELD]),
    Buffer.from([0]),
    Buffer.from([0]),
  ]);
}

function isEncField(v) {
  return (
    v && typeof v === "object" && !Array.isArray(v) &&
    v["$wbEncrypted"] === 1 && typeof v["envelope"] === "string"
  );
}

function deriveKey(payloadJson) {
  const payload = JSON.parse(payloadJson);
  const secret = payload["atRestSecretKey"];
  if (typeof secret !== "string" || secret === "") {
    throw new Error("at-rest key payload carries no atRestSecretKey");
  }
  // 注意：对 base64 字符串本身做摘要，不是对解码后的字节
  return crypto.createHash("sha256").update(secret, "utf8").digest();
}

function keyIdOf(key) {
  return crypto.createHash("sha256").update(key).digest("hex").slice(0, 16);
}

function openField(field, keys) {
  const env = JSON.parse(Buffer.from(field.envelope, "base64").toString("utf8"));
  // 一台机器上可能装了多个构建（WorkBuddy.exe / WorkBuddyAI.exe），
  // 各自派生的密钥不同；信封里写了 keyId，按它挑对应的密钥，
  // 而不是假定全机器只有一把钥匙。
  const key = keys instanceof Map ? keys.get(env.keyId) : keys;
  if (!key) throw new Error(`没有 keyId=${env.keyId} 对应的解密密钥`);
  if (keyIdOf(key) !== env.keyId) {
    throw new Error(`keyId mismatch (${keyIdOf(key)} vs ${env.keyId})`);
  }
  const d = crypto.createDecipheriv("aes-256-gcm", key, Buffer.from(env.nonce, "base64"));
  d.setAAD(fieldAad(env.keyId, env.suite));
  d.setAuthTag(Buffer.from(env.authTag, "base64"));
  return Buffer.concat([
    d.update(Buffer.from(env.ciphertext, "base64")),
    d.final(),
  ]).toString("utf8");
}

function readField(v, keys) {
  if (isEncField(v)) {
    try {
      return { value: openField(v, keys), ok: true };
    } catch (err) {
      return { value: "", ok: false, error: String(err.message || err) };
    }
  }
  if (typeof v === "string") return { value: v, ok: true };
  return { value: "", ok: true };
}

// ---------------------------------------------------------------------------
// 定位 WorkBuddy 桌面端可执行文件
// ---------------------------------------------------------------------------

/** 从注册表的卸载信息里取安装位置（DisplayIcon 是文件，InstallLocation 是目录）。 */
function registryHints() {
  if (process.platform !== "win32") return { files: [], dirs: [] };
  const viaPs = registryHintsViaPowerShell();
  if (viaPs.files.length > 0 || viaPs.dirs.length > 0) return viaPs;
  return registryHintsViaReg();
}

function parseHintLines(text) {
  const files = [];
  const dirs = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (line === "") continue;
    const tab = line.indexOf("\t");
    if (tab < 0) continue;
    const kind = line.slice(0, tab);
    const value = line.slice(tab + 1).trim();
    if (kind === "ICON") {
      const cleaned = cleanIconPath(value);
      if (cleaned !== "") files.push(cleaned);
    } else if (kind === "DIR") {
      if (value !== "") dirs.push(value);
    }
  }
  return { files: [...new Set(files)], dirs: [...new Set(dirs)] };
}

/**
 * 优先走 PowerShell：脚本里把 [Console]::OutputEncoding 设成 UTF-8，
 * 中文安装路径（如 D:\软件\WorkBuddy）不会在管道里变成乱码。
 */
function registryHintsViaPowerShell() {
  const script = [
    "$ErrorActionPreference='SilentlyContinue'",
    "try { [Console]::OutputEncoding=[System.Text.Encoding]::UTF8 } catch {}",
    "$roots=@('HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*'," +
      "'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*'," +
      "'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*')",
    "foreach($r in $roots){",
    "  Get-ItemProperty -Path $r | ForEach-Object {",
    "    $dn=[string]$_.DisplayName; $di=[string]$_.DisplayIcon; $il=[string]$_.InstallLocation",
    "    if($dn -match 'WorkBuddy|CodeBuddy' -or $di -match 'WorkBuddy|CodeBuddy'){",
    "      if($di -ne ''){ Write-Output \"ICON`t$di\" }",
    "      if($il -ne ''){ Write-Output \"DIR`t$il\" }",
    "    }",
    "  }",
    "}",
  ].join("\n");
  let stdout;
  try {
    stdout = execFileSync("powershell", [
      "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script,
    ], {
      encoding: "utf8",
      timeout: 20000,
      windowsHide: true,
      maxBuffer: 1 << 23,
    });
  } catch {
    return { files: [], dirs: [] };
  }
  return parseHintLines(stdout);
}

/** PowerShell 不可用时的兜底：reg.exe。chcp 65001 让输出按 UTF-8 解码。 */
function registryHintsViaReg() {
  const files = [];
  const dirs = [];
  const hives = [
    "HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall",
    "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall",
    "HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall",
  ];
  for (const hive of hives) {
    for (const [name, sink] of [["DisplayIcon", files], ["InstallLocation", dirs]]) {
      let stdout;
      try {
        stdout = execFileSync(
          "cmd",
          ["/c", `chcp 65001 >nul && reg query "${hive}" /s /v ${name}`],
          { encoding: "utf8", timeout: 15000, windowsHide: true, maxBuffer: 1 << 23 }
        );
      } catch {
        continue;
      }
      for (const m of stdout.matchAll(/REG_(?:SZ|EXPAND_SZ)\s+(.+?)\s*$/gm)) {
        const value = m[1].trim();
        if (!/workbuddy|codebuddy/iu.test(value)) continue;
        if (sink === files) {
          const cleaned = cleanIconPath(value);
          if (cleaned !== "") files.push(cleaned);
        } else {
          dirs.push(value);
        }
      }
    }
  }
  return { files: [...new Set(files)], dirs: [...new Set(dirs)] };
}

/** DisplayIcon 形如 `D:\Program Files\WorkBuddy\WorkBuddy.exe,0`。 */
function cleanIconPath(raw) {
  if (typeof raw !== "string") return "";
  let s = raw.trim().replace(/^"+|"+$/g, "");
  const comma = s.lastIndexOf(",");
  if (comma > 0 && /^\d+$/.test(s.slice(comma + 1).trim())) s = s.slice(0, comma);
  s = s.trim().replace(/^"+|"+$/g, "");
  return /\.exe$/i.test(s) ? s : "";
}

/** 注册表没覆盖到时的兜底：所有固定盘符的 Program Files 与盘根。 */
function fallbackAppPaths(env) {
  const roots = new Set();
  for (const key of ["ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"]) {
    const value = (env[key] || "").trim();
    if (value !== "") roots.add(value);
  }
  // 装到非系统盘很常见，而没有任何环境变量指向那里 —— 只能枚举盘符
  for (let code = 67; code <= 90; code += 1) {
    const drive = String.fromCharCode(code) + ":\\";
    try {
      if (!fs.existsSync(drive)) continue;
    } catch {
      continue;
    }
    roots.add(path.join(drive, "Program Files"));
    roots.add(path.join(drive, "Program Files (x86)"));
    roots.add(drive);
  }
  const out = [];
  for (const root of roots) {
    for (const name of APP_NAMES) {
      out.push(path.join(root, "WorkBuddy", name));
      out.push(path.join(root, "WorkBuddy AI", name));
      out.push(path.join(root, "Programs", "WorkBuddy", name));
      out.push(path.join(root, name));
    }
    // 盘根下一级里名字像 WorkBuddy 的目录（如 D:\Apps 里的 WorkBuddy）
    try {
      for (const entry of fs.readdirSync(root)) {
        if (!APP_DIR_RE.test(entry)) continue;
        for (const name of APP_NAMES) out.push(path.join(root, entry, name));
      }
    } catch {
      /* 无权限或不存在，忽略 */
    }
  }
  return out;
}

function appCandidates(env = process.env) {
  const out = [];
  const envExe = (env.WORKBUDDY_APP_EXECUTABLE || "").trim();
  if (envExe) out.push(envExe);

  const hints = registryHints();
  out.push(...hints.files);
  for (const dir of hints.dirs) {
    for (const name of APP_NAMES) {
      out.push(path.join(dir, name));
      out.push(path.join(dir, "WorkBuddy", name));
    }
  }

  out.push(...fallbackAppPaths(env));

  for (const extra of (env.WB_APP_SEARCH_DIRS || "").split(";")) {
    if (!extra.trim()) continue;
    for (const name of APP_NAMES) {
      out.push(path.join(extra.trim(), name));
      out.push(path.join(extra.trim(), "WorkBuddy", name));
    }
  }

  return [...new Set(out)].filter((p) => {
    try {
      return fs.statSync(p).isFile();
    } catch {
      return false;
    }
  });
}

/**
 * 让桌面端可执行文件自己交出 at-rest 密钥。
 * 首选 `-e` 内联（不留临时文件），失败再退回临时脚本文件。
 */
function fetchKeyPayload(exe) {
  const script =
    "try{process.stdout.write(process._linkedBinding('electron_browser_workbuddy_storage').loggerGet())}" +
    "catch(e){process.exitCode=3;process.stderr.write(String(e&&e.message||e))}";
  const spawnOpts = {
    env: { ...process.env, ELECTRON_RUN_AS_NODE: "1" },
    timeout: KEY_FETCH_TIMEOUT_MS,
    windowsHide: true,
    maxBuffer: 1 << 20,
    encoding: "utf8",
  };
  try {
    const inline = execFileSync(exe, ["-e", script], spawnOpts).trim();
    if (inline) return inline;
  } catch {
    /* 部分构建不支持 -e，走下面的临时文件方式 */
  }

  const tmp = path.join(os.tmpdir(), "wb-key-" + process.pid + "-" + Date.now() + ".js");
  fs.writeFileSync(tmp, script, "utf8");
  try {
    return execFileSync(exe, [tmp], spawnOpts).trim();
  } finally {
    try {
      fs.unlinkSync(tmp);
    } catch {
      /* ignore */
    }
  }
}

// ---------------------------------------------------------------------------
// 登录态文件
// ---------------------------------------------------------------------------

function authDirs(env = process.env) {
  const home = os.homedir();
  if (process.platform === "darwin") {
    return [path.join(home, "Library", "Application Support", "CodeBuddyExtension", "Data", "Public", "auth")];
  }
  if (process.platform !== "win32") {
    const config = (env.XDG_CONFIG_HOME || "").trim() || path.join(home, ".config");
    return [path.join(config, "CodeBuddyExtension", "Data", "Public", "auth")];
  }
  const local = env.LOCALAPPDATA || path.join(home, "AppData", "Local");
  const roaming = env.APPDATA || path.join(home, "AppData", "Roaming");
  const dirs = [];

  // 已知位置（实测 WorkBuddy 桌面端与 CodeBuddy 扩展共用这一份登录态）
  const known = [
    path.join(local, "CodeBuddyExtension", "Data", "Public", "auth"),
    path.join(roaming, "CodeBuddyExtension", "Data", "Public", "auth"),
  ];
  dirs.push(...known);

  // 不同版本/不同制品可能换了根目录，追加常见的 Electron userData 位置
  for (const base of [local, roaming]) {
    for (const product of ["WorkBuddy", "CodeBuddy", "CodeBuddy CN", "CodeBuddyExtension"]) {
      dirs.push(path.join(base, product, "Data", "Public", "auth"));
      dirs.push(path.join(base, product, "auth"));
    }
  }

  // 用户自己知道在哪时优先用这个
  for (const extra of (env.WB_AUTH_SEARCH_DIRS || "").split(";")) {
    if (extra.trim()) dirs.push(extra.trim());
  }

  return [...new Set(dirs)];
}

/** 目录里有没有 .info 文件（用于 doctor 展示"目录存在但为空"） */
function listInfoFiles(dir) {
  try {
    return fs.readdirSync(dir)
      .filter((n) => n.endsWith(".info"))
      .map((n) => path.join(dir, n));
  } catch {
    return [];
  }
}

/**
 * 在浅层目录里搜 workbuddy-desktop.info。
 *
 * 只在已知位置都没命中时才跑：新版本可能把登录态挪到没预料到的子目录，
 * 与其让用户猜，不如自己找一遍（深度限制，避免扫全盘）。
 */
function searchAuthFilesShallow(roots, depth = 4) {
  const found = [];
  const seen = new Set();
  const walk = (dir, left) => {
    if (left < 0 || found.length >= 20) return;
    let entries = [];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isFile() && entry.name === LIVE_FILENAME) {
        found.push(full);
      } else if (entry.isDirectory() && left > 0) {
        // 跳过明显无关又庞大的目录
        if (/^(node_modules|Cache|GPUCache|logs|Crashpad|\.git)$/i.test(entry.name)) continue;
        if (seen.has(full)) continue;
        seen.add(full);
        walk(full, left - 1);
      }
    }
  };
  for (const root of roots) {
    try {
      if (!fs.statSync(root).isDirectory()) continue;
    } catch {
      continue;
    }
    walk(root, depth);
  }
  return found;
}

function authFileReport(env = process.env) {
  /** 诊断用：每个候选目录是否存在、有几个 .info */
  return authDirs(env).map((dir) => ({ dir, files: listInfoFiles(dir).length }));
}

function authFiles(env = process.env) {
  const override = (env.WORKBUDDY_AUTH_FILE || "").trim();
  if (override) {
    try {
      if (fs.statSync(override).isDirectory()) return listInfoFiles(override);
      return [override];
    } catch {
      /* 覆盖路径无效则继续自动探测 */
    }
  }

  const files = [];
  for (const dir of authDirs(env)) {
    files.push(...listInfoFiles(dir));
  }
  if (files.length > 0) return [...new Set(files)];

  // 已知位置全空：浅层搜一遍（本机首次登录、版本换了目录都能兜住）
  const home = os.homedir();
  const roots = process.platform === "win32"
    ? [
        env.LOCALAPPDATA || path.join(home, "AppData", "Local"),
        env.APPDATA || path.join(home, "AppData", "Roaming"),
      ]
    : [path.join(home, ".config"), path.join(home, "Library", "Application Support")];
  return searchAuthFilesShallow(roots);
}

/** expiresAt 有的版本给秒、有的给毫秒，统一成秒。 */
function expiryToSec(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return 0;
  return n > 1e11 ? Math.floor(n / 1000) : Math.floor(n);
}

/**
 * 读取全部本机账号。
 * 返回 { ok, accounts:[...], app, error?, code?, skipped? }
 */
function dumpCreds(options = {}) {
  const exes = appCandidates();

  // 探测每一个候选：某个构建超时/报错不该连累其它构建
  const keysById = new Map();
  let usedExe = "";
  let lastErr = "";
  for (const exe of exes) {
    let payload = "";
    try {
      payload = fetchKeyPayload(exe);
    } catch (err) {
      lastErr = String(err.message || err);
      continue;
    }
    if (!payload) continue;
    try {
      const key = deriveKey(payload);
      const id = keyIdOf(key);
      if (!keysById.has(id)) keysById.set(id, key);
      if (!usedExe) usedExe = exe;
    } catch (err) {
      lastErr = String(err.message || err);
    }
  }

  if (keysById.size === 0) {
    return {
      ok: false,
      code: "NO_APP",
      error: "找不到可提供解密密钥的 WorkBuddy 桌面应用",
      tried: exes,
      detail: lastErr,
      accounts: [],
    };
  }

  // 同一 uin 的多份快照：实时文件优先，其次按最近刷新时间
  const byIdentity = new Map();
  const skipped = [];

  for (const file of authFiles()) {
    let doc;
    try {
      doc = JSON.parse(fs.readFileSync(file, "utf8"));
    } catch {
      skipped.push({ source: file, reason: "malformed" });
      continue;
    }
    if (!doc || typeof doc !== "object" || Array.isArray(doc)) {
      skipped.push({ source: file, reason: "malformed" });
      continue;
    }
    // 新版本是 {account, auth} 嵌套，老版本是扁平结构
    const account = doc["account"] && typeof doc["account"] === "object" ? doc["account"] : doc;
    const auth = doc["auth"] && typeof doc["auth"] === "object" ? doc["auth"] : doc;

    const access = readField(auth["accessToken"], keysById);
    if (!access.ok) {
      skipped.push({ source: file, reason: "encrypted" });
      continue;
    }
    if (!access.value) {
      skipped.push({ source: file, reason: "no_token" });
      continue;
    }
    // refresh token 已过期 = 这个登录态不可能再续期了，直接跳过
    const refreshExpiresAt = expiryToSec(auth["refreshExpiresAt"]);
    if (refreshExpiresAt > 0 && refreshExpiresAt < Math.floor(Date.now() / 1000)) {
      skipped.push({ source: file, reason: "refresh_expired" });
      continue;
    }

    const refresh = readField(auth["refreshToken"], keysById);
    const nick = readField(account["nickname"], keysById);

    const uin = String(account["uin"] || "");
    const uid = String(account["uid"] || "");
    const identity = uin || uid;
    if (!identity) {
      skipped.push({ source: file, reason: "no_identity" });
      continue;
    }

    const item = {
      uin,
      uid,
      nickname: nick.ok && nick.value ? nick.value : "",
      access_token: access.value,
      refresh_token: refresh.ok ? refresh.value : "",
      expires_at: expiryToSec(auth["expiresAt"]),
      refresh_expires_at: refreshExpiresAt,
      last_refresh_at: expiryToSec(auth["lastRefreshTime"]),
      domain: typeof auth["domain"] === "string" ? auth["domain"] : "",
      enterprise_id: String(account["enterpriseId"] || ""),
      source: file,
      is_live: path.basename(file) === LIVE_FILENAME,
      last_login: account["lastLogin"] === true,
    };

    const prev = byIdentity.get(identity);
    if (!prev) {
      byIdentity.set(identity, item);
      continue;
    }
    const better =
      (item.is_live && !prev.is_live) ||
      (item.is_live === prev.is_live && item.last_refresh_at > prev.last_refresh_at);
    if (better) byIdentity.set(identity, item);
  }

  const accounts = [...byIdentity.values()].sort((a, b) => {
    if (a.is_live !== b.is_live) return a.is_live ? -1 : 1;
    return b.last_refresh_at - a.last_refresh_at;
  });

  // 一个都没解出来时不能说"成功 0 个" —— 使用者根本分不清是没登录、
  // 目录不对，还是密钥不匹配。这里给明确的原因和可执行的提示。
  if (accounts.length === 0) {
    const scanned = authFiles();
    const reasons = [...new Set(skipped.map((s) => s.reason))];
    let code = "NO_ACCOUNT";
    let error = "没有找到 WorkBuddy 登录态文件";
    if (scanned.length === 0) {
      error =
        "没有找到 workbuddy-desktop.info：请确认已在本机 WorkBuddy 桌面端登录，" +
        "且用的是同一个 Windows 账号运行网关";
    } else if (reasons.includes("encrypted")) {
      code = "DECRYPT_FAILED";
      error =
        "找到 " + scanned.length + " 个登录态文件，但用本机桌面端提供的密钥解不开（版本不匹配）";
    } else if (reasons.includes("refresh_expired")) {
      code = "REFRESH_EXPIRED";
      error = "登录态已过期：请在 WorkBuddy 桌面端重新登录后再扫描";
    } else if (reasons.includes("no_token") || reasons.includes("no_identity")) {
      code = "NO_TOKEN";
      error = "登录态文件里没有可用的 token：请重新登录 WorkBuddy 后再扫描";
    }
    return {
      ok: false,
      code,
      error,
      app: usedExe,
      count: 0,
      accounts: [],
      scanned_files: scanned,
      auth_dirs: authFileReport(),
      skipped,
    };
  }

  return {
    ok: true,
    app: usedExe,
    count: accounts.length,
    accounts,
    skipped,
  };
}

module.exports = {
  dumpCreds, appCandidates, authDirs, authFiles, authFileReport,
  deriveKey, openField, isEncField, cleanIconPath, expiryToSec,
};
