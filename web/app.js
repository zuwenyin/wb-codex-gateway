/* WorkBuddy Gateway 控制台前端（无框架，纯 DOM） */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = { token: localStorage.getItem("wb_token") || "", data: null, timer: null };

  // ---------------- HTTP ----------------
  async function api(path, opts = {}) {
    const headers = { "Content-Type": "application/json" };
    if (state.token) headers["X-Admin-Token"] = state.token;
    const res = await fetch(path, { ...opts, headers: { ...headers, ...(opts.headers || {}) } });
    let body = null;
    try { body = await res.json(); } catch (_) { /* 空响应 */ }
    if (res.status === 401) { showLogin(); throw new Error("未登录"); }
    if (!res.ok) throw new Error((body && (body.error || body.message)) || ("HTTP " + res.status));
    return body || {};
  }

  // ---------------- 工具 ----------------
  function toast(msg, kind = "ok") {
    const el = $("toast");
    el.textContent = msg;
    el.className = "toast " + kind;
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.add("hidden"), 2400);
  }

  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  /**
   * 把对象序列化进单引号 HTML 属性。
   *
   * 用 HTML 实体转义（&amp; &#39; &lt; &gt;），读取时用 JSON.parse 之前
   * 是没问题的 —— 因为浏览器**会**在 attribute value 里还原实体，
   * el.dataset 拿到的已经是原始字符。这里刻意保留双引号不转义，
   * 属性外层用的是单引号，双引号在属性值里合法。
   */
  function attrJson(obj) {
    return JSON.stringify(obj)
      .replace(/&/g, "&amp;")
      .replace(/'/g, "&#39;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function maskNum(n) {
    return n >= 10000 ? (n / 1000).toFixed(1) + "k" : String(n);
  }

  // 积分保留两位小数，但整数不显示 ".00"（官网是 805.24 / 100 这种）
  function fmtNum(n) {
    const v = Number(n || 0);
    if (!isFinite(v)) return "0";
    return Number.isInteger(v) ? String(v) : v.toFixed(2);
  }

  function rate(acc) {
    const req = acc.requests || 0;
    if (!req) return "—";
    return Math.round((acc.successes || 0) / req * 100) + "%";
  }

  function accountBadge(acc) {
    // 让给外部客户端的账号优先显示，否则会误报"可用"（网关其实不会用它）
    if (acc.reserved) {
      return '<span class="badge off" title="已被让给外部客户端（如 dsh），网关不会用该账号发请求">' +
        "● 已让出</span>";
    }
    if (!acc.enabled) return '<span class="badge off">● 已禁用</span>';
    if (acc.cooling_in > 0) {
      return '<span class="badge cool">● 冷却 ' + acc.cooling_in + 's</span>';
    }
    const hasCred = acc.kind === "local" ? !!acc.has_token : !!acc.api_key;
    if (!hasCred) return '<span class="badge err">● 缺凭证</span>';

    // 模型级配额耗尽：账号整体可用，只是某些模型暂时不能用
    const mc = acc.model_cooldowns || {};
    const names = Object.keys(mc);
    if (names.length) {
      const mins = Math.min(...names.map((k) => mc[k].minutes));
      return (
        '<span class="badge warn" title="' + esc(names.join("\n")) + '">' +
        "● " + names.length + " 个模型限流（" + mins + "分）</span>"
      );
    }
    return '<span class="badge on">● 可用</span>';
  }

  function keyCell(acc) {
    if (acc.kind === "local") {
      return '<span class="badge local">本机</span>';
    }
    return '<span class="mono">' + esc(acc.api_key || "") + "</span>";
  }

  /**
   * 模型列：固定宽度 + 省略号，完整信息用自定义气泡展示。
   * 直接渲染一长串会把表格撑到几千像素；用浏览器原生 title 又没法控制
   * 字体、颜色和布局，所以改成 data-* + 自定义 tooltip。
   */
  function modelsCell(acc) {
    const raw = (acc.models || "").trim();
    if (!raw) return '<span class="muted">（默认）</span>';

    const list = raw.split(",").map((s) => s.trim()).filter(Boolean);
    const meta = (state.data && state.data.model_meta) || {};

    const shown = list.slice(0, 2).join(", ");
    const rest = list.length - 2;
    const label = rest > 0 ? shown + " +" + rest : shown;

    const rows = list.map((id) => {
      const info = meta[id] || {};
      return {
        id: id,
        name: info.name || id,
        win: info.context_window ? fmtWindow(info.context_window) : "",
        winFull: info.context_window || 0,
        credit: info.credits_raw || "",
        free: info.credits === 0,
      };
    });

    // 结构化数据塞进 data 属性，由全局 tooltip 读取并排版
    return (
      '<span class="models-cell has-tip" data-tip="models" ' +
      "data-models='" + attrJson(rows) + "'>" +
      esc(label) +
      "</span>"
    );
  }

  // ---------------- 自定义悬浮气泡 ----------------
  // 浏览器原生 title 气泡样式不可控（字体、间距、暗色适配都做不到），
  // 这里自己渲染一个，风格跟界面一致。
  let tipEl = null;

  function ensureTip() {
    if (tipEl) return tipEl;
    tipEl = document.createElement("div");
    tipEl.className = "tip hidden";
    document.body.appendChild(tipEl);
    return tipEl;
  }

  function buildModelsTip(rows) {
    const head =
      '<div class="tip-head">已启用模型 <span class="tip-count">' + rows.length + "</span></div>";
    const body = rows
      .map((r) => {
        const win = r.win
          ? '<span class="tip-win">' + r.win + " 上下文</span>"
          : '<span class="tip-win muted">—</span>';
        let credit = "";
        if (r.credit) {
          credit = r.free
            ? '<span class="tip-credit free">免费</span>'
            : '<span class="tip-credit">' + esc(r.credit) + "</span>";
        }
        return (
          '<div class="tip-row">' +
          '<span class="tip-name">' + esc(r.name) + "</span>" +
          win +
          credit +
          "</div>"
        );
      })
      .join("");
    return head + '<div class="tip-body">' + body + "</div>";
  }

  function showTip(anchor, html) {
    const el = ensureTip();
    el.innerHTML = html;
    el.classList.remove("hidden");

    const a = anchor.getBoundingClientRect();
    const t = el.getBoundingClientRect();
    const pad = 8;
    const GAP = 6;

    // 默认显示在下方；下方不够就翻到上方
    let top = a.bottom + GAP;
    let above = false;
    if (top + t.height > window.innerHeight - pad) {
      top = Math.max(pad, a.top - t.height - GAP);
      above = true;
    }
    let left = a.left;
    if (left + t.width > window.innerWidth - pad) {
      left = Math.max(pad, window.innerWidth - t.width - pad);
    }
    el.style.top = Math.round(top) + "px";
    el.style.left = Math.round(left) + "px";

    // 间隙缓冲条要跟着方向走：气泡在上方时，缓冲条应在下沿
    el.classList.toggle("tip-above", above);

    // 记录锚点，供鼠标在"锚点"与"气泡"之间移动时判断
    tipEl._anchor = anchor;
  }

  function hideTip() {
    if (!tipEl) return;
    tipEl.classList.add("hidden");
    tipEl._anchor = null;
  }

  // 鼠标离开时留一点宽限时间，方便斜着移进气泡；移进就取消隐藏。
  let hideTimer = null;

  function scheduleHide() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hideTip, 180);
  }

  function cancelHide() {
    clearTimeout(hideTimer);
    hideTimer = null;
  }

  document.addEventListener("mouseover", (e) => {
    // 鼠标进入气泡本身：保持显示
    if (tipEl && !tipEl.classList.contains("hidden") && tipEl.contains(e.target)) {
      cancelHide();
      return;
    }

    const el = e.target.closest("[data-tip]");
    if (!el) return;
    cancelHide();

    const kind = el.dataset.tip;
    if (kind === "models") {
      let rows = [];
      try {
        rows = JSON.parse(el.dataset.models || "[]");
      } catch (_) {
        return;
      }
      if (rows.length) showTip(el, buildModelsTip(rows));
    }
  });

  document.addEventListener("mouseout", (e) => {
    // A) 起源于气泡内部
    if (tipEl && tipEl.contains(e.target)) {
      // 仍在气泡内移动 -> 什么都不做
      if (tipEl.contains(e.relatedTarget)) return;
      // 从气泡移回锚点 -> 保持
      const anchor = tipEl._anchor;
      if (anchor && e.relatedTarget && anchor.contains(e.relatedTarget)) {
        cancelHide();
        return;
      }
      // 从气泡移到别处 -> 走宽限隐藏
      scheduleHide();
      return;
    }

    // B) 起源于锚点
    const el = e.target.closest("[data-tip]");
    if (!el) return;

    // 移到气泡里 -> 保持（这段空隙由 .tip::before 补住）
    if (tipEl && e.relatedTarget && tipEl.contains(e.relatedTarget)) {
      cancelHide();
      return;
    }
    // 移到锚点的子元素 -> 保持
    if (e.relatedTarget && el.contains(e.relatedTarget)) return;

    scheduleHide();
  });

  // 气泡自身：进入取消隐藏，离开才安排隐藏
  document.addEventListener(
    "mouseenter",
    (e) => {
      if (tipEl && e.target === tipEl) cancelHide();
    },
    true
  );

  document.addEventListener(
    "mouseleave",
    (e) => {
      if (tipEl && e.target === tipEl) {
        // 移回锚点也算"还在看"
        const anchor = tipEl._anchor;
        if (anchor && anchor.contains(e.relatedTarget)) {
          cancelHide();
          return;
        }
        scheduleHide();
      }
    },
    true
  );

  // 页面滚动时收起，避免气泡跟内容脱节。
  //
  // 坑：capture 阶段的 scroll 监听会把**气泡内部**的滚动也一并捕获，
  // 于是用户在气泡里滚轮翻列表时气泡自己消失。
  // 这里只处理"来自气泡外部"的滚动。
  window.addEventListener(
    "scroll",
    (e) => {
      if (tipEl && tipEl.contains(e.target)) return; // 气泡内部滚动：放行
      hideTip();
    },
    true
  );

  window.addEventListener("resize", hideTip);

  // 鼠标在气泡内滚轮时，阻止事件冒泡到页面，
  // 免得上层滚动容器（表格横向滚动等）跟着动。
  document.addEventListener(
    "wheel",
    (e) => {
      if (tipEl && tipEl.contains(e.target)) {
        e.stopPropagation();
      }
    },
    { passive: true }
  );

  // ---------------- 登录 ----------------
  function showLogin() {
    $("app").classList.add("hidden");
    $("login").classList.remove("hidden");
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
  }

  function showApp() {
    $("login").classList.add("hidden");
    $("app").classList.remove("hidden");
  }

  $("loginForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("loginErr").textContent = "";
    try {
      const out = await api("/admin/api/login", {
        method: "POST",
        body: JSON.stringify({ password: $("password").value }),
      });
      state.token = out.token || "";
      localStorage.setItem("wb_token", state.token);
      await boot();
    } catch (err) {
      $("loginErr").textContent = err.message || "登录失败";
    }
  });

  $("logoutBtn").addEventListener("click", async () => {
    try { await api("/admin/api/logout", { method: "POST" }); } catch (_) { /* ignore */ }
    state.token = "";
    localStorage.removeItem("wb_token");
    showLogin();
  });

  // 停止网关：结束进程，之后本机其它客户端（dsh / Codex）与网关彻底解耦
  $("shutdownBtn").addEventListener("click", async () => {
    if (!confirm("将结束网关进程（本页随后会断开）。\n" +
                 "需要时重新运行 start-gateway.cmd 即可。确定停止？")) return;
    const btn = $("shutdownBtn");
    btn.disabled = true;
    btn.textContent = "正在停止…";
    try {
      await api("/admin/api/shutdown", { method: "POST" });
    } catch (_) { /* 进程可能已退出，连接被断属于正常 */ }
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
    $("statusDot").className = "dot off";
    $("proxyLabel").textContent = "网关已停止";
    document.body.insertAdjacentHTML(
      "afterbegin",
      '<div class="banner danger" style="margin:12px 16px">网关已停止。' +
      "本机其它客户端不再受网关影响；需要时重新运行 <code>start-gateway.cmd</code>。</div>"
    );
  });

  // ---------------- 渲染 ----------------
  function renderStats(d) {
    const accs = d.accounts || [];
    const usable = accs.filter((a) => a.available).length;
    const totalReq = accs.reduce((s, a) => s + (a.requests || 0), 0);
    const totalOk = accs.reduce((s, a) => s + (a.successes || 0), 0);
    const succRate = totalReq ? Math.round(totalOk / totalReq * 100) : 0;

    const cards = [
      { label: "账号总数", value: accs.length, unit: "个" },
      { label: "当前可用", value: usable, unit: "个" },
      { label: "累计请求", value: maskNum(totalReq), unit: "" },
      { label: "成功率", value: totalReq ? succRate : "—", unit: totalReq ? "%" : "" },
      { label: "代理状态", value: d.settings.proxy_enabled ? "运行中" : "已暂停", unit: "" },
    ];

    $("statCards").innerHTML = cards.map((c) =>
      '<div class="card"><div class="label">' + esc(c.label) + '</div>' +
      '<div class="value">' + esc(c.value) + (c.unit ? '<small>' + esc(c.unit) + '</small>' : "") +
      '</div></div>').join("");
  }

  function renderAccounts(d) {
    const accs = d.accounts || [];
    const body = $("accountBody");
    $("emptyHint").classList.toggle("hidden", accs.length > 0);
    $("accountTable").classList.toggle("hidden", accs.length === 0);

    body.innerHTML = accs.map((a) => {
      // 后端已把 last_error 压成一句话；原文放 title 里备查
      const errText = a.last_error
        ? '<span class="err-text" title="' + esc(a.last_error_detail || a.last_error) + '">' +
          esc(a.last_error) + "</span>"
        : '<span class="muted">—</span>';
      const sub = a.kind === "local" && a.uin
        ? '<div class="muted">uin ' + esc(a.uin) + "</div>"
        : (a.note ? '<div class="muted">' + esc(a.note) + "</div>" : "");
      return "<tr>" +
        '<td>' + accountBadge(a) + "</td>" +
        '<td><strong>' + esc(a.name) + "</strong>" + sub + "</td>" +
        '<td class="mono">' + esc(a.base_url || "（默认）") + "</td>" +
        "<td>" + keyCell(a) + "</td>" +
        "<td>" + modelsCell(a) + "</td>" +
        "<td>" + (a.requests || 0) + " / " + rate(a) + "</td>" +
        "<td>" + errText + "</td>" +
        '<td class="right">' +
          '<button class="btn mini" data-act="test" data-id="' + a.id + '">测试</button> ' +
          (a.kind === "local"
            ? '<button class="btn mini" data-act="models" data-id="' + a.id + '">选模型</button> '
            : '<button class="btn mini" data-act="edit" data-id="' + a.id + '">编辑</button> ') +
          '<button class="btn mini" data-act="reserve" data-id="' + a.id + '">' +
            (a.reserved ? "收回自用" : "让给外部") + "</button> " +
          '<button class="btn mini" data-act="reset" data-id="' + a.id + '">重置</button> ' +
          '<button class="btn mini danger" data-act="del" data-id="' + a.id + '">删除</button>' +
        "</td></tr>";
    }).join("");
  }

  function render(d) {
    state.data = d;

    $("proxyToggle").checked = !!d.settings.proxy_enabled;
    $("proxyLabel").textContent = d.settings.proxy_enabled ? "运行中" : "已暂停";
    $("statusDot").className = "dot " + (d.settings.proxy_enabled ? "on" : "off");
    $("pauseBanner").classList.toggle("hidden", !!d.settings.proxy_enabled);
    $("endpointInfo").textContent = "端点 /v1 · 模型 " + (d.models || []).length + " 个";

    // data.json 是从别的机器拷过来的：里面是别人的账号，明确提示
    const owner = d.data_owner_warning;
    const ownerBanner = $("ownerBanner");
    ownerBanner.classList.toggle("hidden", !owner);
    if (owner) {
      const from = ((owner.origin && owner.origin.user) || "?") +
        "@" + ((owner.origin && owner.origin.host) || "?");
      $("ownerBannerText").innerHTML =
        "⚠ data.json 来自另一台机器（<code>" + esc(from) + "</code>），" +
        "其中 " + (owner.accounts || 0) + " 个账号不属于本机，且带着对方的有效凭证。" +
        "建议清空后重新扫描你自己的账号。";
    }

    // 策略：允许后端新增策略而前端选项还没更新，这里做兜底，
    // 避免 select 因为找不到匹配项而显示成空白。
    const sel = $("policySelect");
    const policy = d.settings.policy;
    const known = [...sel.options].some((o) => o.value === policy);
    if (!known && policy) {
      const opt = document.createElement("option");
      opt.value = policy;
      opt.textContent = policy + "（未知策略）";
      sel.appendChild(opt);
    }
    sel.value = policy;
    $("cooldownSec").value = d.settings.cooldown_sec;
    $("maxFailures").value = d.settings.max_consecutive_failures;
    $("sendUsage").checked = d.settings.send_usage;
    $("includeNamespaces").checked = d.settings.include_namespaces;

    // 有异常账号时才显示"恢复全部"，并标出数量
    const broken = d.broken || { total: 0 };
    const btn = $("resetAllBtn");
    btn.classList.toggle("hidden", broken.total === 0);
    btn.textContent = broken.total > 0 ? "↻ 恢复全部 (" + broken.total + ")" : "↻ 恢复全部";
    btn.title = broken.total > 0
      ? "禁用 " + broken.disabled + " · 冷却 " + broken.cooling + " · 缺凭证 " + broken.no_credential
      : "";

    renderCodexMode(d);
    renderStats(d);
    renderAccounts(d);
  }

  // Codex 侧接入模式：改写 ~/.codex/config.toml（网关模型 <-> 官方模型）
  function renderCodexMode(d) {
    const cx = d.codex;
    const panel = $("codexPanel");
    if (!cx || !cx.ok) { panel.classList.add("hidden"); return; }
    panel.classList.remove("hidden");

    $("codexPath").textContent = cx.path;
    const gw = cx.mode === "gateway";
    const badge = $("codexMode");
    badge.textContent = gw ? "WorkBuddy 网关" : "官方模型";
    badge.className = "mode-badge " + (gw ? "gateway" : "official");
    $("codexDesc").textContent = gw
      ? "Codex 使用网关的 " + (cx.models || []).length + " 个 WorkBuddy 模型"
      : "Codex 使用官方账号与模型（" + (cx.model || "-") + "）";

    const sel = $("codexModelSelect");
    const models = cx.models || [];
    sel.innerHTML = models.map((m) => '<option value="' + esc(m) + '">' + esc(m) + "</option>").join("");
    const want = cx.gateway_model || (gw && cx.model) || models[0];
    if (want && models.indexOf(want) >= 0) sel.value = want;
    sel.disabled = !gw;
    $("codexSwitchBtn").textContent = gw ? "切到官方模型" : "切到 WorkBuddy 网关";

    let hint = "改写 " + cx.path + "；切换后需重启 ChatGPT 桌面端才生效（原文件备份为 config.toml.wb-bak）。";
    if (gw && !cx.catalog_exists) hint += " ⚠ models.json 不存在，请先运行 build_catalog.py 生成。";
    $("codexHint").textContent = hint;
  }

  async function refresh() {
    try {
      const d = await api("/admin/api/state");
      render(d);
    } catch (err) {
      if (err.message !== "未登录") toast(err.message, "bad");
    }
  }

  // ---------------- 交互 ----------------
  $("proxyToggle").addEventListener("change", async (e) => {
    const on = e.target.checked;
    try {
      await api("/admin/api/settings", {
        method: "PATCH",
        body: JSON.stringify({ proxy_enabled: on }),
      });
      toast(on ? "代理已开启" : "代理已暂停");
      await refresh();
    } catch (err) {
      toast(err.message, "bad");
      e.target.checked = !on;
    }
  });

  $("policySelect").addEventListener("change", async (e) => {
    try {
      await api("/admin/api/settings", {
        method: "PATCH",
        body: JSON.stringify({ policy: e.target.value }),
      });
      toast("策略已切换");
      await refresh();
    } catch (err) { toast(err.message, "bad"); }
  });

  $("saveSettings").addEventListener("click", async () => {
    const patch = {
      cooldown_sec: Number($("cooldownSec").value) || 60,
      max_consecutive_failures: Number($("maxFailures").value) || 5,
      send_usage: $("sendUsage").checked,
      include_namespaces: $("includeNamespaces").checked,
    };
    const pw = $("newPassword").value.trim();
    if (pw) patch.admin_password = pw;
    try {
      await api("/admin/api/settings", { method: "PATCH", body: JSON.stringify(patch) });
      $("newPassword").value = "";
      $("saveHint").textContent = "已保存 " + new Date().toLocaleTimeString();
      toast("设置已保存");
      if (pw) state.token = "";  // 改密码后旧会话失效，重新登录
      await refresh();
    } catch (err) { toast(err.message, "bad"); }
  });

  // 表格按钮（事件委托）
  $("accountBody").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const id = btn.dataset.id;
    const act = btn.dataset.act;
    const acc = (state.data.accounts || []).find((a) => a.id === id);

    if (act === "edit") { openModal(acc); return; }

    if (act === "models") { openModelPicker(acc); return; }

    if (act === "del") {
      if (!confirm("确定删除账号「" + (acc ? acc.name : id) + "」？")) return;
      try {
        await api("/admin/api/accounts/" + id, { method: "DELETE" });
        toast("已删除");
        await refresh();
      } catch (err) { toast(err.message, "bad"); }
      return;
    }

    if (act === "reset") {
      try {
        await api("/admin/api/accounts/" + id + "/reset", { method: "POST" });
        toast("已恢复并清除冷却");
        await refresh();
      } catch (err) { toast(err.message, "bad"); }
      return;
    }

    // 让出 / 收回：让出后网关不再用该账号发请求，也不查它的积分
    if (act === "reserve") {
      const next = !(acc && acc.reserved);
      try {
        await api("/admin/api/accounts/" + id, {
          method: "PATCH",
          body: JSON.stringify({ reserved: next }),
        });
        toast(next
          ? "已让出「" + (acc ? acc.name : id) + "」，网关不再使用该账号"
          : "已收回「" + (acc ? acc.name : id) + "」", "ok");
        await refresh();
      } catch (err) { toast(err.message, "bad"); }
      return;
    }

    if (act === "test") {
      btn.disabled = true;
      btn.textContent = "测试中";
      try {
        const r = await api("/admin/api/accounts/" + id + "/test", { method: "POST" });
        if (r.ok) toast("连通正常 · " + r.ms + "ms · " + (r.model || ""), "ok");
        else toast("失败：" + (r.error || "未知错误"), "bad");
      } catch (err) {
        toast(err.message, "bad");
      } finally {
        btn.disabled = false;
        btn.textContent = "测试";
        await refresh();
      }
    }
  });

  // ---------------- 模态 ----------------
  function openModal(acc) {
    $("modalTitle").textContent = acc ? "编辑账号" : "添加账号";
    $("editId").value = acc ? acc.id : "";
    $("fName").value = acc ? acc.name : "";
    $("fBase").value = acc ? acc.base_url : "";
    $("fKey").value = "";
    $("fKey").placeholder = acc ? "留空表示不修改（当前 " + acc.api_key + "）" : "sk-...";
    $("fModels").value = acc ? acc.models : "";
    $("fNote").value = acc ? acc.note : "";
    $("modalErr").textContent = "";
    $("modal").classList.remove("hidden");
    $("fName").focus();
  }

  function closeModal() { $("modal").classList.add("hidden"); }

  $("addBtn").addEventListener("click", () => openModal(null));

  // 切换 Codex 接入模式
  $("codexSwitchBtn").addEventListener("click", async () => {
    const cx = state.data && state.data.codex;
    if (!cx || !cx.ok) return;
    const toOfficial = cx.mode === "gateway";
    const mode = toOfficial ? "official" : "gateway";
    const model = $("codexModelSelect").value;
    const msg = toOfficial
      ? "将把 Codex 切回官方模型（" + (cx.original_model || "gpt-5.6-terra") + "），" +
        "并注释掉 model_provider / model_catalog_json。\n切换后需重启 ChatGPT 桌面端。确定？"
      : "将让 Codex 使用网关模型 " + model + "，并把请求指向本机网关。\n" +
        "切换后需重启 ChatGPT 桌面端。确定？";
    if (!confirm(msg)) return;

    const btn = $("codexSwitchBtn");
    btn.disabled = true;
    btn.textContent = "切换中…";
    try {
      await api("/admin/api/codex", { method: "POST", body: JSON.stringify({ mode, model }) });
      toast(toOfficial ? "已切到官方模型，请重启 ChatGPT 桌面端" : "已切到 WorkBuddy 网关，请重启 ChatGPT 桌面端", "ok");
      await refresh();
    } catch (err) {
      toast(err.message, "bad");
    } finally {
      btn.disabled = false;
    }
  });

  // 清空从别处拷来的账号池
  $("purgeAccountsBtn").addEventListener("click", async () => {
    const n = ((state.data && state.data.accounts) || []).length;
    if (!confirm("将删除账号池中的 " + n + " 个账号（它们来自另一台机器）。\n" +
                 "删除后可用「扫描本机 WorkBuddy 账号」重新添加你自己的账号。确定？")) return;
    const btn = $("purgeAccountsBtn");
    btn.disabled = true;
    btn.textContent = "清空中…";
    try {
      const r = await api("/admin/api/accounts/purge", { method: "POST" });
      toast("已清空 " + (r.removed || 0) + " 个外来账号", "ok");
      await refresh();
      loadCredits();
    } catch (err) {
      toast(err.message, "bad");
    } finally {
      btn.disabled = false;
      btn.textContent = "清空这些账号";
    }
  });

  // 一键恢复全部账号
  $("resetAllBtn").addEventListener("click", async () => {
    const broken = (state.data && state.data.broken) || { total: 0 };
    const msg = broken.total > 0
      ? "将恢复全部 " + (state.data.accounts || []).length + " 个账号（当前有 " +
        broken.total + " 个异常），确定？"
      : "将重置全部账号的冷却与失败计数，确定？";
    if (!confirm(msg)) return;

    const btn = $("resetAllBtn");
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = "恢复中…";
    try {
      const r = await api("/admin/api/accounts/reset-all", { method: "POST" });
      toast("已恢复 " + (r.changed || 0) + " 个账号（共 " + (r.count || 0) + " 个）", "ok");
      await refresh();
    } catch (err) {
      toast(err.message, "bad");
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  });

  // 扫描本机 WorkBuddy 账号
  $("scanBtn").addEventListener("click", async () => {
    const btn = $("scanBtn");
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = "扫描中…";
    try {
      const r = await api("/admin/api/scan-local", { method: "POST" });
      if (r.ok) {
        const parts = [];
        if (r.added) parts.push("新增 " + r.added);
        if (r.updated) parts.push("更新 " + r.updated);
        toast("扫描完成：" + (parts.join("，") || "无变化") + "（共 " + r.count + " 个）", "ok");
      } else {
        toast("扫描失败：" + (r.error || r.code || "未知"), "bad");
      }
      await refresh();
    } catch (err) {
      toast(err.message, "bad");
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  });
  $("cancelBtn").addEventListener("click", closeModal);
  $("modal").addEventListener("click", (e) => { if (e.target === $("modal")) closeModal(); });

  $("saveAccBtn").addEventListener("click", async () => {
    const id = $("editId").value;
    const payload = {
      name: $("fName").value.trim(),
      base_url: $("fBase").value.trim(),
      models: $("fModels").value.trim(),
      note: $("fNote").value.trim(),
    };
    const key = $("fKey").value.trim();
    if (key) payload.api_key = key;

    if (!payload.name) { $("modalErr").textContent = "请填写名称"; return; }
    if (!id && !key) { $("modalErr").textContent = "请填写 API Key"; return; }

    try {
      if (id) {
        await api("/admin/api/accounts/" + id, { method: "PATCH", body: JSON.stringify(payload) });
        toast("账号已更新");
      } else {
        await api("/admin/api/accounts", { method: "POST", body: JSON.stringify(payload) });
        toast("账号已添加");
      }
      closeModal();
      await refresh();
    } catch (err) {
      $("modalErr").textContent = err.message;
    }
  });

  // ---------------- 签到与积分 ----------------
  function daysUntil(dateStr) {
    if (!dateStr) return -1;
    const t = Date.parse(String(dateStr).replace(/-/g, "/"));
    if (isNaN(t)) return -1;
    return Math.ceil((t - Date.now()) / 86400000);
  }

  function renderCredits(c) {
    const body = $("creditBody");
    if (!c || !c.accounts || !c.accounts.length) {
      body.innerHTML = '<p class="muted credit-loading">暂无账号</p>';
      return;
    }

    const head =
      '<div class="credit-summary">' +
      '<div class="cs-item"><span class="cs-label">可用积分（积分包）</span>' +
      '<span class="cs-value">' + fmtNum(c.available_credits) + "</span></div>" +
      '<div class="cs-item"><span class="cs-label">套餐额度</span>' +
      '<span class="cs-value">' + fmtNum(c.plan_credits) + "</span></div>" +
      '<div class="cs-item"><span class="cs-label">累计获得</span>' +
      '<span class="cs-value">' + (c.total_credits || 0) + "</span></div>" +
      '<div class="cs-item"><span class="cs-label">今日签到</span>' +
      '<span class="cs-value">' + (c.checked_in_today || 0) + " / " + (c.account_count || 0) + "</span></div>" +
      "</div>";

    const rows = c.accounts
      .map((a) => {
        if (!a.ok) {
          return (
            '<div class="credit-row bad">' +
            '<div class="cr-name">' + esc(a.name) + "</div>" +
            '<div class="cr-stat"><span class="badge err">查询失败</span></div>' +
            '<div class="cr-err">' + esc(a.error || "") + "</div>" +
            "</div>"
          );
        }
        const done = a.today_checked_in;
        const badge = done
          ? '<span class="badge on">✓ 今天已签</span>'
          : '<span class="badge cool">未签到</span>';
        const week = (a.week_progress || [])
          .map((v) => '<i class="wk' + (v ? " on" : "") + '"></i>')
          .join("");

        // 最早过期的积分包
        let expire = "";
        if (a.nearest_expire) {
          const days = daysUntil(a.nearest_expire);
          const cls = days <= 0 ? "exp-gone" : days <= 7 ? "exp-soon" : "exp-ok";
          expire =
            '<div class="cr-expire ' + cls + '" title="最早过期的积分包">' +
            "⏳ " + esc(a.nearest_expire.slice(0, 10)) +
            (days >= 0 ? "（" + days + " 天）" : "（已过期）") +
            "</div>";
        }

        return (
          '<div class="credit-row">' +
          '<div class="cr-name">' + esc(a.name) +
          (a.uin ? '<span class="muted"> · ' + esc(a.uin) + "</span>" : "") + "</div>" +
          '<div class="cr-stat">' + badge +
          '<span class="cr-streak">连续 ' + (a.streak_days || 0) + " 天</span></div>" +
          '<div class="cr-credit"><b>' + fmtNum(a.available_credits) + "</b> 可用积分" +
          '<span class="muted"> · ' + (a.usable_packages || 0) + " 个包</span>" +
          (a.plan_credits ? '<span class="muted"> · 套餐 ' + fmtNum(a.plan_credits) + "</span>" : "") +
          '<span class="muted"> · 今日 +' + (a.today_credit || 0) + "</span>" +
          '<span class="muted"> · 累计 ' + (a.total_credits || 0) + "</span>" +
          "</div>" +
          '<div class="cr-expire-wrap">' + expire + "</div>" +
          '<div class="cr-week" title="本周签到">' + week + "</div>" +
          '<div class="cr-actions">' +
          '<button class="btn mini" data-act="credit-detail" data-id="' + a.id + '">详情</button>' +
          (done ? "" : '<button class="btn mini primary" data-act="credit-checkin" data-id="' + a.id + '">签到</button>') +
          "</div>" +
          "</div>"
        );
      })
      .join("");

    body.innerHTML = head + rows;
    state.credits = c;
  }

  async function loadCredits() {
    try {
      const c = await api("/admin/api/credit-summary", { method: "POST" });
      renderCredits(c);
    } catch (err) {
      $("creditBody").innerHTML = '<p class="muted credit-loading">查询失败：' + esc(err.message) + "</p>";
    }
  }

  $("creditRefresh").addEventListener("click", async (e) => {
    e.preventDefault();
    const btn = $("creditRefresh");
    btn.disabled = true;
    await loadCredits();
    btn.disabled = false;
    toast("积分已刷新");
  });

  $("checkinAllBtn").addEventListener("click", async () => {
    const btn = $("checkinAllBtn");
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = "签到中…";
    try {
      const r = await api("/admin/api/checkin-all", { method: "POST" });
      const parts = [];
      if (r.checked) parts.push("新签到 " + r.checked + " 个");
      if (r.already) parts.push("已签过 " + r.already + " 个");
      if (r.failed) parts.push("失败 " + r.failed + " 个");
      if (r.total_credit) parts.push("+" + r.total_credit + " 积分");
      toast(parts.length ? parts.join("，") : "没有可签到的账号", r.failed ? "bad" : "ok");
      await loadCredits();
    } catch (err) {
      toast(err.message, "bad");
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  });

  // 积分表格里的按钮
  $("creditBody").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const id = btn.dataset.id;
    const act = btn.dataset.act;
    const acc = ((state.credits && state.credits.accounts) || []).find((a) => a.id === id);

    if (act === "credit-checkin") {
      btn.disabled = true;
      btn.textContent = "签到中";
      try {
        const r = await api("/admin/api/accounts/" + id + "/checkin", { method: "POST" });
        if (r.ok) toast(r.already ? "今天已经签过了" : "签到成功 +" + r.credit + " 积分", "ok");
        else toast(r.error || "签到失败", "bad");
        await loadCredits();
      } catch (err) {
        toast(err.message, "bad");
      } finally {
        btn.disabled = false;
      }
      return;
    }

    if (act === "credit-detail") {
      openCreditDetail(acc);
    }
  });

  function openCreditDetail(acc) {
    if (!acc) return;
    $("creditModalHint").textContent =
      "账号 " + acc.name + (acc.uin ? " · uin " + acc.uin : "") +
      (acc.activity_name ? " · " + acc.activity_name : "");
    const dates = (acc.checkin_dates || []).slice(0, 31);
    const cells = dates
      .map((d) => '<div class="cd-date">' + esc(d) + "</div>")
      .join("");

    // 积分包（按过期时间正序，最上面的最先过期）
    let pkgHtml = "";
    const pkgs = (acc.packages || [])
      .filter((p) => p.usable || p.remain > 0)
      .sort((x, y) => (x.end_ms || 9e18) - (y.end_ms || 9e18))
      .slice(0, 20);
    if (pkgs.length) {
      pkgHtml =
        '<h3 class="cd-title">资源明细（剩余/总量，按过期时间先后，越上面越先用）</h3>' +
        '<div class="cd-pkgs">' +
        pkgs
          .map((p) => {
            const d = daysUntil(p.end_time);
            const cls = d <= 0 ? "exp-gone" : d <= 7 ? "exp-soon" : "";
            const tag = p.kind === "plan"
              ? '<span class="pk-tag plan">套餐额度</span>'
              : '<span class="pk-tag pack">积分包</span>';
            return (
              '<div class="cd-pkg">' +
              tag +
              '<span class="pk-name">' + esc(p.name) + "</span>" +
              '<span class="pk-remain">' + fmtNum(p.remain) + " / " + fmtNum(p.size) + "</span>" +
              '<span class="pk-exp ' + cls + '">' + esc((p.end_time || "").slice(0, 10)) + "</span>" +
              "</div>"
            );
          })
          .join("") +
        "</div>";
    } else if (acc.packages_error) {
      pkgHtml =
        '<h3 class="cd-title">积分包</h3>' +
        '<p class="muted">查询失败：' + esc(acc.packages_error) + "</p>";
    }

    $("creditDetail").innerHTML =
      '<div class="cd-grid">' +
      '<div><span class="muted">可用积分（' + (acc.usable_packages || 0) + ' 个积分包）</span><b>' +
      fmtNum(acc.available_credits) + "</b></div>" +
      '<div><span class="muted">套餐额度（' + (acc.plan_packages || 0) + ' 个）</span><b>' +
      fmtNum(acc.plan_credits) + "</b></div>" +
      '<div><span class="muted">累计获得</span><b>' + (acc.total_credits || 0) + "</b></div>" +
      '<div><span class="muted">连续签到</span><b>' + (acc.streak_days || 0) + " 天</b></div>" +
      '<div><span class="muted">每日签到</span><b>' + (acc.daily_credit || 0) + " 积分</b></div>" +
      '<div><span class="muted">活动截止</span><b>' + esc(acc.end_time || "-") + "</b></div>" +
      "</div>" +
      pkgHtml +
      '<h3 class="cd-title">签到记录（近 ' + dates.length + " 次）</h3>" +
      (dates.length ? '<div class="cd-dates">' + cells + "</div>" : '<p class="muted">暂无记录</p>');
    $("creditModal").classList.remove("hidden");
  }

  $("creditModalClose").addEventListener("click", () =>
    $("creditModal").classList.add("hidden")
  );
  $("creditModal").addEventListener("click", (e) => {
    if (e.target === $("creditModal")) $("creditModal").classList.add("hidden");
  });

  // ---------------- 模型选择 ----------------
  let modelTarget = null;

  function fmtWindow(n) {
    if (!n) return "—";
    if (n >= 1000000) return (n / 1000000).toFixed(n % 1000000 ? 1 : 0) + "M";
    if (n >= 1000) return Math.round(n / 1000) + "K";
    return String(n);
  }

  async function openModelPicker(acc) {
    modelTarget = acc;
    $("modelErr").textContent = "";
    $("modelList").innerHTML = "";
    $("modelHint").textContent = "正在从 WorkBuddy 拉取可用模型…";
    $("modelModal").classList.remove("hidden");

    try {
      const r = await api("/admin/api/local-models", { method: "POST" });
      if (!r.ok) {
        $("modelHint").textContent = "";
        $("modelErr").textContent = "拉取失败：" + (r.error || r.code || "未知");
        return;
      }

      const meta = (state.data && state.data.model_meta) || {};
      const selected = new Set(
        (acc.models || "").split(",").map((s) => s.trim()).filter(Boolean)
      );
      $("modelHint").textContent =
        "账号 " + (r.account.nickname || r.account.uin) + " · 共 " + r.count +
        " 个模型（勾选后保存；窗口 = 上下文上限，倍率 = 消耗系数）";

      $("modelList").innerHTML = r.models
        .map((m) => {
          const info = meta[m.id] || {};
          const win = info.context_window || m.max_input_tokens || 0;
          const credit = info.credits_raw || m.credits || "";
          return (
            '<label class="model-row">' +
            '<input type="checkbox" value="' + esc(m.id) + '"' +
            (selected.has(m.id) || selected.size === 0 ? " checked" : "") + ">" +
            '<span class="m-name">' + esc(m.name) + "</span>" +
            '<span class="m-id mono">' + esc(m.id) + "</span>" +
            '<span class="m-win" title="上下文窗口 ' + (win || "未知") + ' tokens">' +
              esc(fmtWindow(win)) + "</span>" +
            (credit ? '<span class="m-credit">' + esc(credit) + "</span>" : "<span></span>") +
            "</label>"
          );
        })
        .join("");
    } catch (err) {
      $("modelHint").textContent = "";
      $("modelErr").textContent = err.message;
    }
  }

  $("modelCancel").addEventListener("click", () => $("modelModal").classList.add("hidden"));
  $("modelModal").addEventListener("click", (e) => {
    if (e.target === $("modelModal")) $("modelModal").classList.add("hidden");
  });

  $("modelSave").addEventListener("click", async () => {
    if (!modelTarget) return;
    const picked = [...$("modelList").querySelectorAll("input:checked")].map((i) => i.value);
    if (!picked.length) { $("modelErr").textContent = "至少选择一个模型"; return; }
    try {
      await api("/admin/api/accounts/" + modelTarget.id, {
        method: "PATCH",
        body: JSON.stringify({ models: picked.join(",") }),
      });
      $("modelModal").classList.add("hidden");
      toast("已启用 " + picked.length + " 个模型");
      await refresh();
    } catch (err) {
      $("modelErr").textContent = err.message;
    }
  });

  // ---------------- 启动 ----------------
  async function boot() {
    try {
      const d = await api("/admin/api/state");
      showApp();
      render(d);
      loadCredits();
      if (state.timer) clearInterval(state.timer);
      state.timer = setInterval(() => {
        // 页面在后台时不轮询，省资源
        if (!document.hidden) refresh();
      }, 3000);
    } catch (err) {
      showLogin();
      throw err;
    }
  }

  window.addEventListener("DOMContentLoaded", () => {
    if (state.token) {
      boot().catch(() => showLogin());
    } else {
      // 先探一次：若服务端未设密码则直接进入
      api("/admin/api/state")
        .then((d) => {
          showApp();
          render(d);
          loadCredits();
          state.timer = setInterval(refresh, 3000);
        })
        .catch(() => showLogin());
    }
  });
})();
