"""Single-file dashboard. No build step, no CDN — served straight from the proxy."""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>step2api · StepFun Studio 网关</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 16 16%27%3E%3Crect width=%2716%27 height=%2716%27 rx=%274%27 fill=%27%232f81f7%27/%3E%3Cpath d=%27M4 8h8M8 4v8%27 stroke=%27white%27 stroke-width=%272%27/%3E%3C/svg%3E">
<style>
  :root{
    --bg:#0b0d10; --panel:#12161b; --panel-2:#171c23; --line:#232a33;
    --text:#e6edf3; --muted:#8b98a5; --accent:#5eead4; --accent-2:#7dd3fc;
    --warn:#fbbf24; --bad:#f87171; --good:#34d399; --radius:12px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:radial-gradient(1200px 600px at 15% -10%,#12303a 0,var(--bg) 55%) no-repeat,var(--bg);
       color:var(--text);font:14px/1.5 "Inter","Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;}
  a{color:var(--accent-2);text-decoration:none}
  .wrap{max-width:1180px;margin:0 auto;padding:28px 20px 80px}
  header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:22px}
  .logo{width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,#0f766e,#155e75);
        display:grid;place-items:center;font-weight:700;color:#a7f3d0}
  h1{font-size:19px;margin:0;font-weight:650;letter-spacing:.2px}
  .sub{color:var(--muted);font-size:12.5px}
  .spacer{flex:1}
  .grid{display:grid;gap:14px}
  .cards{grid-template-columns:repeat(auto-fit,minmax(180px,1fr));margin-bottom:18px}
  .card{background:linear-gradient(180deg,var(--panel),var(--panel-2));border:1px solid var(--line);
        border-radius:var(--radius);padding:14px 16px}
  .card .k{color:var(--muted);font-size:12px;letter-spacing:.4px;text-transform:uppercase}
  .card .v{font-size:24px;font-weight:650;margin-top:6px;font-variant-numeric:tabular-nums}
  .card .v small{font-size:12px;color:var(--muted);font-weight:400;margin-left:6px}
  section{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
          padding:16px;margin-bottom:16px}
  section h2{font-size:14px;margin:0 0 12px;font-weight:600;letter-spacing:.3px;display:flex;gap:10px;align-items:center}
  section h2 .hint{font-weight:400;color:var(--muted);font-size:12px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
  th{color:var(--muted);font-weight:500;font-size:11.5px;text-transform:uppercase;letter-spacing:.5px}
  tr:last-child td{border-bottom:none}
  .mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}
  .pill{display:inline-flex;align-items:center;gap:6px;padding:2px 9px;border-radius:999px;
        font-size:11.5px;border:1px solid var(--line);background:#0d1116;color:var(--muted)}
  .pill.ok{color:var(--good);border-color:#134e3a;background:#08241b}
  .pill.warn{color:var(--warn);border-color:#4a3a10;background:#241c08}
  .pill.bad{color:var(--bad);border-color:#4a1d1d;background:#240d0d}
  button{cursor:pointer;border-radius:9px;border:1px solid var(--line);background:#1a2029;color:var(--text);
         padding:7px 12px;font-size:12.5px;transition:.15s}
  button:hover{border-color:#33404d;background:#202833}
  button.primary{background:linear-gradient(135deg,#0d9488,#0e7490);border-color:#0f766e;color:#ecfeff}
  button.primary:hover{filter:brightness(1.08)}
  button.ghost{background:transparent}
  button.danger{color:#fca5a5;border-color:#3b1d1d}
  input,select,textarea{background:#0d1116;border:1px solid var(--line);color:var(--text);
        border-radius:9px;padding:8px 10px;font-size:13px;font-family:inherit;width:100%}
  input:focus,select:focus,textarea:focus{outline:none;border-color:#0f766e}
  label{display:block;font-size:12px;color:var(--muted);margin:0 0 5px}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .row3{display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:12px}
  .actions{display:flex;gap:6px;flex-wrap:wrap}
  .toast{position:fixed;right:18px;bottom:18px;background:#111a20;border:1px solid #1f3a3a;
         padding:11px 14px;border-radius:10px;font-size:13px;opacity:0;transform:translateY(8px);
         transition:.2s;max-width:420px;z-index:99}
  .toast.show{opacity:1;transform:none}
  .toast.err{border-color:#4a1d1d;color:#fecaca}
  .empty{color:var(--muted);font-size:13px;padding:14px 4px}
  .hidden{display:none!important}
  .bar{height:6px;border-radius:999px;background:#0d1116;overflow:hidden;border:1px solid var(--line)}
  .bar>i{display:block;height:100%;background:linear-gradient(90deg,#0d9488,#22d3ee)}
  .kv{display:flex;gap:8px;align-items:center;flex-wrap:wrap;color:var(--muted);font-size:12px}
  .login{max-width:380px;margin:12vh auto}
  code{background:#0d1116;border:1px solid var(--line);border-radius:6px;padding:1px 5px;font-size:12px}
  .scroll{max-height:340px;overflow:auto}
  @media(max-width:720px){.row,.row3{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">S2</div>
    <div>
      <h1>step2api <span class="sub">StepFun Studio → OpenAI 兼容网关</span></h1>
      <div class="sub" id="tagline">未登录</div>
    </div>
    <div class="spacer"></div>
    <button class="ghost" id="btn-refresh" title="每 15 秒自动刷新">刷新</button>
    <button class="ghost" id="btn-logout">退出</button>
  </header>

  <div id="login" class="login card">
    <h2 style="margin-bottom:14px">面板登录</h2>
    <label>访问密码</label>
    <input id="password" type="password" placeholder="默认 admin" autocomplete="current-password">
    <div style="height:12px"></div>
    <button class="primary" id="btn-login" style="width:100%">进入面板</button>
    <p class="sub" style="margin-top:12px">面板密码与 API Key 相互独立。首次登录后请到「设置」修改。</p>
  </div>

  <div id="app" class="hidden">
    <div class="grid cards" id="cards"></div>

    <section>
      <h2>账号池 <span class="hint">Oasis-Token 自动续期；多账号轮询</span></h2>
      <table><thead><tr>
        <th>名称</th><th>状态</th><th>Token 到期</th><th>刷新令牌</th><th>请求</th>
        <th>Tokens in / out</th><th>操作</th>
      </tr></thead><tbody id="accounts"></tbody></table>
      <div style="height:14px"></div>
      <div class="row3">
        <div><label>名称</label><input id="acct-name" placeholder="可留空自动命名"></div>
        <div><label>Oasis-Webid</label><input id="acct-webid" placeholder="可留空，从 token 解析"></div>
        <div><label>Oasis-Token</label><input id="acct-token" placeholder="access...refresh"></div>
      </div>
      <div style="height:10px"></div>
      <label>或直接粘贴完整 Cookie（推荐：DevTools → Network → 任意 /api/ 请求 → Cookie）</label>
      <textarea id="acct-cookie" rows="2" placeholder="Oasis-Token=...; Oasis-Webid=..."></textarea>
      <div style="height:10px"></div>
      <div class="actions">
        <button class="primary" id="btn-add-account">添加账号</button>
        <button id="btn-refresh-all">刷新全部 Token</button>
        <button class="ghost" id="btn-export" title="下载 accounts.json（含凭证）">导出备份</button>
      </div>
    </section>

    <section>
      <h2>API Key <span class="hint">未配置时网关对本机开放</span></h2>
      <table><thead><tr><th>名称</th><th>Key</th><th>状态</th><th>操作</th></tr></thead>
        <tbody id="keys"></tbody></table>
      <div style="height:12px"></div>
      <div class="row"><div><label>名称</label><input id="key-name" placeholder="例如 claude-code"></div>
        <div style="display:flex;align-items:flex-end"><button class="primary" id="btn-add-key">生成 Key</button></div></div>
    </section>

    <section>
      <h2>模型 <span class="hint">实时来自上游 GetStudioPlaygroundConfig</span></h2>
      <table><thead><tr><th>ID</th><th>名称</th><th>多模态</th><th>思考档位</th><th>最大输出</th></tr></thead>
        <tbody id="models"></tbody></table>
    </section>

    <section>
      <h2>模型用量 <span class="hint">按最近 500 条流水聚合</span></h2>
      <table><thead><tr>
        <th>模型</th><th>请求</th><th>输入</th><th>输出</th><th>缓存命中</th><th>平均耗时</th><th>错误</th>
      </tr></thead><tbody id="model-stats"></tbody></table>
    </section>

    <section>
      <h2>请求流水 <span class="hint">最近 200 条</span>
        <button class="ghost" id="btn-csv" style="margin-left:auto">导出 CSV</button></h2>
      <div class="scroll"><table><thead><tr>
        <th>时间</th><th>账号</th><th>模型</th><th>模式</th><th>Tokens</th><th>TTFT</th><th>耗时</th><th>状态</th>
      </tr></thead><tbody id="logs"></tbody></table></div>
    </section>

    <section>
      <h2>设置</h2>
      <div class="row">
        <div><label>面板密码</label><input id="set-password" type="password" placeholder="留空则不修改"></div>
        <div><label>默认上游模式</label>
          <select id="set-mode"><option value="playground">playground（纯对话，低延迟）</option>
            <option value="agent">agent（工作区 / 工具）</option></select></div>
      </div>
      <div style="height:10px"></div>
      <button class="primary" id="btn-save-settings">保存设置</button>
    </section>

    <section>
      <h2>接入方式</h2>
      <div class="kv">Base URL</div>
      <pre class="mono" id="base-url"></pre>
      <div class="kv">OpenAI SDK</div>
      <pre class="mono">from openai import OpenAI
client = OpenAI(base_url="<span id="base-url-2"></span>/v1", api_key="&lt;你的 API Key&gt;")
resp = client.chat.completions.create(model="step-5-preview", messages=[{"role":"user","content":"你好"}])</pre>
      <div class="kv">可选请求头</div>
      <div class="sub"><code>X-Step-Mode: agent</code> 走工作区工具模式 · <code>X-Step-Thinking: off</code> 关闭思考 ·
        <code>web_search: true</code>（请求体）开启联网搜索</div>
    </section>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const $ = (id) => document.getElementById(id);
let panelToken = sessionStorage.getItem("step2api_panel") || "";
let state = {};
let modeDirty = false;   // user changed #set-mode but has not saved yet

function toast(msg, isErr) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = "toast"; }, 3200);
}

let pending = 0;
async function api(path, opts = {}) {
  const headers = Object.assign({"Content-Type": "application/json"}, opts.headers || {});
  if (panelToken) headers["X-Panel-Token"] = panelToken;
  pending++;
  try {
    const resp = await fetch(path, Object.assign({}, opts, {headers}));
    const text = await resp.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (e) { data = {raw: text}; }
    if (!resp.ok) {
      if (resp.status === 401 && path !== "/admin/login") {
        // The server forgot this session (it restarted, or the panel token aged out).
        // Fall back to the login gate instead of leaving a page whose buttons all fail.
        logout();
      }
      throw new Error((data.error && data.error.message) || text || ("HTTP " + resp.status));
    }
    return data;
  } finally {
    pending--;
  }
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString("zh-CN", {hour12: false});
}
function fmtNum(n) { const v = Number(n); return (Number.isFinite(v) ? v : 0).toLocaleString("en-US"); }
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

function render() {
  const accounts = state.accounts || [];
  const summary = state.summary || {};
  const quota = state.quota || {};
  $("cards").innerHTML = [
    ["账号", accounts.length, "个"],
    ["在线", accounts.filter(a => !a.disabled && !a.cooling).length, "个"],
    ["请求", fmtNum(summary.requests), "次"],
    ["输入 Tokens", fmtNum(summary.tokens_in), ""],
    ["输出 Tokens", fmtNum(summary.tokens_out), ""],
    ["缓存命中", fmtNum(summary.cached_tokens), ""],
    ["错误", fmtNum(summary.errors), "次"],
    ["免费额度", quota.free_rate == null ? "—" : (quota.free_rate * 100).toFixed(1), "%"],
    ["5h 剩余", quota.five_hour_usage_left_rate == null ? "—" : (quota.five_hour_usage_left_rate * 100).toFixed(0), "%"],
  ].map(([k, v, u]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}<small>${u}</small></div></div>`).join("");

  $("accounts").innerHTML = accounts.length ? accounts.map(a => {
    const exp = a.token_exp ? a.token_exp * 1000 : 0;
    const left = exp ? exp - Date.now() : 0;
    const expText = exp ? `${fmtTime(a.token_exp)}${left > 0 ? "" : "（已过期，将自动续期）"}` : "—";
    const rexp = a.refresh_exp ? a.refresh_exp * 1000 : 0;
    const rdays = rexp ? Math.max(0, Math.round((rexp - Date.now()) / 86400000)) : null;
    const rtext = rexp ? `${rdays} 天` : "—";
    const rpill = rdays == null ? "" : (rdays < 3 ? "bad" : (rdays < 10 ? "warn" : "ok"));
    let status = '<span class="pill ok">正常</span>';
    if (a.disabled) status = `<span class="pill bad" title="${esc(a.last_error || "凭证无效或已停用")}">停用</span>`;
    else if (a.cooling) status = '<span class="pill warn">冷却中</span>';
    else if (a.last_error) status = `<span class="pill warn" title="${esc(a.last_error)}">异常</span>`;
    return `<tr>
      <td><b>${esc(a.name)}</b><div class="mono sub">${esc(a.oasis_id || a.id)}</div></td>
      <td>${status}</td>
      <td class="sub">${esc(expText)}</td>
      <td><span class="pill ${rpill}">${esc(rtext)}</span></td>
      <td>${fmtNum(a.requests)}</td>
      <td class="mono sub">${fmtNum(a.tokens_in)} / ${fmtNum(a.tokens_out)}</td>
      <td><div class="actions">
        <button data-act="test" data-id="${esc(a.id)}">测试</button>
        <button data-act="refresh" data-id="${esc(a.id)}">续期</button>
        <button data-act="${a.disabled ? "enable" : "disable"}" data-id="${esc(a.id)}">${a.disabled ? "启用" : "停用"}</button>
        <button class="danger" data-act="delete" data-id="${esc(a.id)}">删除</button>
      </div></td></tr>`;
  }).join("") : '<tr><td colspan="7" class="empty">还没有账号。粘贴 Cookie 或 Oasis-Token 添加。</td></tr>';

  $("keys").innerHTML = (state.api_keys || []).length ? state.api_keys.map(k => `<tr>
      <td>${esc(k.name)}</td>
      <td class="mono">${esc(k.key)}</td>
      <td>${k.enabled === false ? '<span class="pill warn">停用</span>' : '<span class="pill ok">启用</span>'}</td>
      <td><div class="actions">
        <button data-key-act="copy" data-key="${esc(k.key)}">复制</button>
        <button data-key-act="${k.enabled === false ? "enable" : "disable"}" data-key="${esc(k.key)}">${k.enabled === false ? "启用" : "停用"}</button>
        <button class="danger" data-key-act="delete" data-key="${esc(k.key)}">删除</button>
      </div></td></tr>`).join("") : '<tr><td colspan="4" class="empty">还没有 API Key（网关当前对本机开放）。</td></tr>';

  $("models").innerHTML = (state.models || []).length ? state.models.map(m => `<tr>
      <td class="mono">${esc(m.id)}</td><td>${esc(m.display_name || "")}</td>
      <td>${m.multimodal ? "✓" : "—"}</td>
      <td class="sub">${esc((m.thinking && m.thinking.effort_options || []).join(" / ") || "—")}</td>
      <td class="mono">${m.max_output_tokens ? fmtNum(m.max_output_tokens) : "—"}</td></tr>`).join("")
    : `<tr><td colspan="5" class="empty">${esc(state.model_error || "尚未加载模型目录")}</td></tr>`;

  const byModel = {};
  for (const r of state.usage || []) {
    const key = r.model || "?";
    const row = byModel[key] || (byModel[key] = {n: 0, tin: 0, tout: 0, cached: 0, dur: 0, durN: 0, err: 0});
    row.n += 1;
    row.tin += r.tokens_in || 0;
    row.tout += r.tokens_out || 0;
    row.cached += r.cached_tokens || 0;
    if (typeof r.duration === "number") { row.dur += r.duration; row.durN += 1; }
    if (r.status !== "ok") row.err += 1;
  }
  const stats = Object.entries(byModel).sort((a, b) => b[1].n - a[1].n);
  $("model-stats").innerHTML = stats.length ? stats.map(([model, r]) => `<tr>
      <td class="mono">${esc(model)}</td><td>${fmtNum(r.n)}</td>
      <td class="mono sub">${fmtNum(r.tin)}</td><td class="mono sub">${fmtNum(r.tout)}</td>
      <td class="mono sub">${fmtNum(r.cached)}</td>
      <td class="mono sub">${r.durN ? (r.dur / r.durN).toFixed(2) + "s" : "—"}</td>
      <td>${r.err ? `<span class="pill bad">${r.err}</span>` : "—"}</td></tr>`).join("")
    : '<tr><td colspan="8" class="empty">还没有请求。</td></tr>';

  const logs = (state.usage || []).slice().reverse();
  $("logs").innerHTML = logs.length ? logs.map(r => {
    const ok = r.status === "ok";
    const closed = r.status === "client_closed";
    const hints = [r.request_id, r.tenant, r.client, r.finish_reason, r.error]
                    .filter(Boolean).map(esc).join(" · ");
    const why = hints ? ` title="${hints}"` : "";
    const statusPill = ok ? `<span class="pill ok"${why}>OK</span>`
      : closed ? `<span class="pill"${why}>closed</span>`
      : `<span class="pill bad"${why}>${esc(r.status)}</span>`;
    return `<tr>
      <td class="sub">${fmtTime(r.ts)}</td>
      <td>${esc(r.account || "—")}</td>
      <td class="mono">${esc(r.model || "")}</td>
      <td class="sub">${esc(r.mode || "")}</td>
      <td class="mono sub">${fmtNum(r.tokens_in)} / ${fmtNum(r.tokens_out)}</td>
      <td class="mono sub">${r.ttft ? esc(r.ttft) + "s" : "—"}</td>
      <td class="mono sub">${r.duration != null ? esc(r.duration) + "s" : "—"}</td>
      <td>${statusPill}</td>
    </tr>`;
  }).join("") : '<tr><td colspan="8" class="empty">暂无请求。</td></tr>';

  if (state.password_default) $("tagline").textContent = "已登录 · ⚠ 面板密码仍是默认值 admin";
  else $("tagline").textContent = "已登录 · 默认模式 " + (state.mode || "playground");
  if (!modeDirty && state.settings && state.settings.default_mode) {
    $("set-mode").value = state.settings.default_mode;
  }

  const base = location.origin;
  $("base-url").textContent = base + "/v1";
  $("base-url-2").textContent = base;
}

async function refresh() {
  state = await api("/admin/api/state");
  // Deliberately NOT persisted: the state carries every API key, and localStorage
  // survives logout on a shared machine (and is readable by any same-origin script).
  localStorage.removeItem("step2api_state");
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  render();
}

async function login() {
  const password = $("password").value;
  const data = await api("/admin/login", {method: "POST", body: JSON.stringify({password})});
  panelToken = data.token;
  sessionStorage.setItem("step2api_panel", panelToken);
  await refresh();
}

async function logout() {
  const stale = panelToken;
  panelToken = "";
  sessionStorage.removeItem("step2api_panel");
  localStorage.removeItem("step2api_state");
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
  $("tagline").textContent = "未登录";
  if (stale) {
    // Revoke it server-side as well: a token left valid after "退出" is still a session.
    try { await fetch("/admin/logout", {method: "POST", headers: {"X-Panel-Token": stale}}); }
    catch (e) { /* offline: the token ages out on its own */ }
  }
}

document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act], button[data-key-act]");
  if (!btn) return;
  try {
    if (btn.dataset.keyAct) {
      const key = btn.dataset.key;
      if (btn.dataset.keyAct === "copy") { await navigator.clipboard.writeText(key); toast("已复制"); return; }
      if (btn.dataset.keyAct === "delete") { await api("/admin/api/keys/" + encodeURIComponent(key), {method: "DELETE"}); }
      else { await api("/admin/api/keys/" + encodeURIComponent(key), {method: "PATCH", body: JSON.stringify({enabled: btn.dataset.keyAct === "enable"})}); }
      await refresh(); return;
    }
    const act = btn.dataset.act, id = btn.dataset.id;
    if (act === "delete") {
      if (!confirm("确定删除该账号？")) return;
      await api("/admin/api/accounts/" + id, {method: "DELETE"});
    } else if (act === "disable" || act === "enable") {
      await api("/admin/api/accounts/" + id, {method: "PATCH", body: JSON.stringify({disabled: act === "disable"})});
      toast(act === "disable" ? "已停用" : "已启用");
    } else if (act === "test") {
      const r = await api("/admin/api/accounts/test", {method: "POST", body: JSON.stringify({id})});
      toast(r.ok ? (r.re_enabled ? "连通正常，已解除停用" : "连通正常") : ("失败：" + r.detail), !r.ok);
      await refresh(); return;
    } else if (act === "refresh") {
      const r = await api("/admin/api/accounts/" + id + "/refresh", {method: "POST", body: "{}"});
      toast(r.ok ? "Token 已续期" : ("续期失败：" + r.detail), !r.ok);
      await refresh(); return;
    }
    await refresh();
  } catch (e) { toast(e.message, true); }
});

$("btn-login").onclick = () => login().catch(e => toast(e.message, true));
$("password").addEventListener("keydown", e => { if (e.key === "Enter") login().catch(err => toast(err.message, true)); });
$("btn-logout").onclick = logout;
$("btn-csv").onclick = () => {
  const cols = ["ts", "account", "model", "mode", "tokens_in", "tokens_out", "cached_tokens", "ttft", "duration", "status", "finish_reason", "error", "client", "tenant", "request_id", "replayed"];
  const lines = [cols.join(",")].concat((state.usage || []).map(r =>
    cols.map(c => {
      const v = r[c];
      if (v == null) return "";
      let text = String(v);
      if (text && /^[=+\-@\t\r]/.test(text)) text = "'" + text;   // no CSV formulas
      return /[",\r\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
    }).join(",")));
  const blob = new Blob([lines.join("\n")], {type: "text/csv;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "step2api-usage-" + new Date().toISOString().slice(0, 19).replace(/[:T]/g, "") + ".csv";
  const url = a.href;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);   // same-tick revoke cancels the download
};

$("btn-refresh").onclick = () => refresh().then(() => toast("已刷新")).catch(e => toast(e.message, true));
// Auto-refresh replaces table rows, so never do it while the user is mid-click or while
// one of their actions is still in flight — that would swallow the click.
let lastInteraction = 0;
document.addEventListener("pointerdown", () => { lastInteraction = Date.now(); }, true);
setInterval(() => {
  if (!panelToken || pending > 0) return;
  if (Date.now() - lastInteraction < 5000) return;
  if ($("app").classList.contains("hidden")) return;
  refresh().catch(() => {});
}, 15000);

$("btn-add-account").onclick = async () => {
  try {
    const body = {
      name: $("acct-name").value.trim(),
      webid: $("acct-webid").value.trim(),
      token: $("acct-token").value.trim(),
      cookie: $("acct-cookie").value.trim(),
    };
    const r = await api("/admin/api/accounts", {method: "POST", body: JSON.stringify(body)});
    const verb = r.updated ? "账号已更新" : "账号已添加";
    toast(r.ok ? verb + "并完成续期" : (verb + "，但校验失败（可能是访客会话或凭证已失效）：" + r.detail), !r.ok);
    $("acct-token").value = ""; $("acct-cookie").value = ""; $("acct-name").value = ""; $("acct-webid").value = "";
    await refresh();
  } catch (e) { toast(e.message, true); }
};

$("btn-export").onclick = async () => {
  try {
    const r = await fetch("/admin/api/export", {headers: {"X-Panel-Token": panelToken}});
    if (!r.ok) throw new Error("HTTP " + r.status);
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "step2api-accounts.json";
    const url = a.href;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("已导出 accounts.json（含凭证，注意保管）");
  } catch (e) { toast("导出失败：" + e.message, true); }
};

$("btn-refresh-all").onclick = async () => {
  try { const r = await api("/admin/api/accounts/refresh", {method: "POST", body: "{}"});
    const bad = (r.items || []).filter(x => !x.ok).length;
    toast(bad ? `${bad} 个账号续期失败` : "全部续期成功", bad > 0); await refresh();
  } catch (e) { toast(e.message, true); }
};

$("btn-add-key").onclick = async () => {
  try {
    const r = await api("/admin/api/keys", {method: "POST", body: JSON.stringify({name: $("key-name").value.trim()})});
    $("key-name").value = "";
    toast("已生成：" + r.key.key);
    await refresh();
  } catch (e) { toast(e.message, true); }
};

$("set-mode").onchange = () => { modeDirty = true; };

$("btn-save-settings").onclick = async () => {
  try {
    await api("/admin/api/settings", {method: "POST", body: JSON.stringify({
      panel_password: $("set-password").value, default_mode: $("set-mode").value})});
    $("set-password").value = "";
    modeDirty = false;
    toast("已保存");
    await refresh();
  } catch (e) { toast(e.message, true); }
};

(async function boot() {
  // Any service worker on this origin is a leftover from whatever served this port
  // before (we ship none, and /sw.js 404s). Left alone it keeps serving a stale
  // panel -- including old admin responses that make a dead session look alive.
  if (navigator.serviceWorker) {
    navigator.serviceWorker.getRegistrations()
      .then(rs => rs.forEach(r => r.unregister()))
      .catch(() => {});
  }
  if (!panelToken) { $("login").classList.remove("hidden"); return; }
  try { await refresh(); }
  catch (e) { logout(); }
})();
</script>
</body>
</html>
"""
