"use strict";
// 网页 Agent 前端：登录（企业微信确认）、对话列表、实时消息（SSE）、附件、审批、回退。
const $ = (id) => document.getElementById(id);
const state = {
  chatId: null, chat: null, lastId: 0, es: null, listEs: null, folders: [], pending: [], rewindable: new Set(), tools: {}, perms: {},
  questions: {}, models: [], efforts: [], defaults: {}, queue: [],
};
const BUSY = ["running", "waiting"];
const EFFORT_NAMES = { low: "low（快）", medium: "medium", high: "high", xhigh: "xhigh", max: "max（最仔细）" };

function modelName(id) { return (state.models.find((m) => m.id === id) || {}).name || id || ""; }
function fillSettings(modelSel, effortSel, model, effort) {
  const dm = modelName(state.defaults.model) || "电脑上的设置";
  modelSel.innerHTML = `<option value="">默认（${esc(dm)}）</option>` + state.models.map((m) => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join("");
  effortSel.innerHTML = `<option value="">默认（${esc(state.defaults.effort || "电脑上的设置")}）</option>` + state.efforts.map((e) => `<option value="${esc(e)}">${esc(EFFORT_NAMES[e] || e)}</option>`).join("");
  modelSel.value = model || "";
  effortSel.value = effort || "";
}
const svg = (d) => `<svg viewBox="0 0 24 24"><path d="${d}"/></svg>`;
const ICON = {
  folder: svg("M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"),
  file: svg("M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8zM14 3v5h5"),
  up: svg("M15 6l-6 6 6 6"),
};
function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  const d = new Date(ts * 1000);
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}
function fmtTokens(n) { n = +n || 0; return n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n); }
function fmtSize(n) { n = +n || 0; return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : n >= 1024 ? Math.round(n / 1024) + " KB" : n + " B"; }

async function api(path, opts = {}) {
  const init = { credentials: "same-origin", ...opts, headers: { ...(opts.headers || {}) } };
  if (init.method && init.method !== "GET") init.headers["X-Requested-With"] = "webagent";
  if (opts.json !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.json); }
  const r = await fetch(path, init);
  if (r.status === 401) { showLogin(); throw new Error("请先登录"); }
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : {};
  if (!r.ok) throw new Error(data.detail || `请求失败（${r.status}）`);
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// 简单 Markdown：先整体转义，再还原常用格式（不会放行任何 HTML）
function md(src) {
  const blocks = [];
  let s = esc(src).replace(/```[^\n]*\n([\s\S]*?)```/g, (_, code) => { blocks.push(code); return `\u0000${blocks.length - 1}\u0000`; });
  s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out = [];
  let list = null; // { tag: "ul" | "ol", items: [] }
  const flush = () => { if (list) { out.push(`<${list.tag}>${list.items.join("")}</${list.tag}>`); list = null; } };
  const cells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const SEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
  const lines = s.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    // 表格：表头行 + 分隔行（|---|:--:|）+ 若干数据行
    if (line.includes("|") && i + 1 < lines.length && SEP.test(lines[i + 1]) && lines[i + 1].includes("-")) {
      flush();
      const head = cells(line);
      const align = cells(lines[i + 1]).map((c) => (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : ""));
      const td = (tag, c, k) => `<${tag}${align[k] ? ` style="text-align:${align[k]}"` : ""}>${c}</${tag}>`;
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
      i--;
      out.push(`<div class="table-wrap"><table><thead><tr>${head.map((c, k) => td("th", c, k)).join("")}</tr></thead>`
        + `<tbody>${rows.map((r) => `<tr>${head.map((_, k) => td("td", r[k] ?? "", k)).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ul || ol) {
      const tag = ul ? "ul" : "ol";
      if (list && list.tag !== tag) flush();
      if (!list) list = { tag, items: [] };
      list.items.push(`<li>${(ul || ol)[1]}</li>`);
      continue;
    }
    flush();
    if (h) out.push(`<h${h[1].length + 1}>${h[2]}</h${h[1].length + 1}>`);
    else if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) out.push("<hr>");
    else if (/^&gt;\s?/.test(line)) out.push(`<blockquote>${line.replace(/^&gt;\s?/, "")}</blockquote>`);
    else if (line.trim() === "") out.push("");
    else out.push(`<p>${line}</p>`);
  }
  flush();
  return out.join("\n").replace(/\u0000(\d+)\u0000/g, (_, i) => `<pre><code>${blocks[+i]}</code></pre>`);
}

// ------------------------------------------------------------------ 登录
function showLogin() {
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
}

$("loginBtn").onclick = async () => {
  $("loginErr").textContent = "";
  $("loginBtn").disabled = true;
  try {
    const r = await fetch("api/login/request", { method: "POST", credentials: "same-origin" });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "发送失败");
    $("loginCode").textContent = d.code;
    $("loginWait").classList.remove("hidden");
    const started = Date.now();
    const poll = async () => {
      const s = await (await fetch(`api/login/status?id=${encodeURIComponent(d.id)}`, { credentials: "same-origin" })).json();
      if (s.status === "ok") return location.reload();
      if (s.status === "expired" || s.status === "missing" || Date.now() - started > 300000) {
        $("loginHint").textContent = "请求已过期，请重新发送。";
        $("loginBtn").disabled = false;
        return;
      }
      setTimeout(poll, 2000);
    };
    poll();
  } catch (e) {
    $("loginErr").textContent = e.message;
    $("loginBtn").disabled = false;
  }
};

$("logoutBtn").onclick = async () => { await api("api/logout", { method: "POST" }); location.reload(); };

// ------------------------------------------------------------------ 对话列表
async function loadChats() {
  const d = await api("api/chats");
  const ul = $("chatList");
  ul.innerHTML = "";
  for (const c of d.chats) {
    const li = document.createElement("li");
    li.className = c.id === state.chatId ? "active" : "";
    const name = (state.folders.find((f) => f.path === c.folder) || {}).name || c.folder.split(/[\\/]/).pop();
    li.innerHTML = `<div class="t"><span class="name">${esc(c.title)}</span><span class="badge ${esc(c.status)}"></span></div>
      <div class="f"><span class="where">${esc(name)}</span><span class="when">${ago(c.updated_at)}</span></div>`;
    li.onclick = () => { location.hash = c.id; $("side").classList.remove("open"); };
    ul.appendChild(li);
  }
  if (!d.chats.length) ul.innerHTML = '<li class="none">还没有对话</li>';
  const st = $("agentState");
  st.textContent = d.agent_online ? "家里电脑在线" : "家里电脑离线";
  st.title = d.agent_online ? "" : "家里电脑的 Agent 不在线，暂时不能对话";
  st.className = "agent-state" + (d.agent_online ? "" : " off");
}

function watchList() {
  if (state.listEs) state.listEs.close();
  state.listEs = new EventSource("api/events");
  state.listEs.onmessage = () => loadChats().catch(() => {});
}

$("newBtn").onclick = () => {
  const sel = $("folderSel");
  sel.innerHTML = state.folders.map((f) => `<option value="${esc(f.path)}">${esc(f.name)}（${esc(f.path)}）</option>`).join("");
  if (!state.folders.length) sel.innerHTML = "<option value=''>家里电脑还没上报可用目录</option>";
  fillSettings($("newModel"), $("newEffort"), "", "");
  $("newDlg").showModal();
};
$("createBtn").onclick = async (e) => {
  e.preventDefault();
  const folder = $("folderSel").value;
  if (!folder) return;
  try {
    const c = await api("api/chats", { method: "POST", json: { folder, mode: $("newMode").value, model: $("newModel").value, effort: $("newEffort").value } });
    $("newDlg").close();
    location.hash = c.id;
  } catch (err) { alert(err.message); }
};
// 侧栏：电脑上可以收起（记住选择），手机上是抽屉
const isMobile = () => window.innerWidth <= 760;
function setCollapsed(on) {
  $("app").classList.toggle("collapsed", on);
  try { localStorage.setItem("wa_side_collapsed", on ? "1" : ""); } catch {}
}
try { if (localStorage.getItem("wa_side_collapsed")) $("app").classList.add("collapsed"); } catch {}
$("openSide").onclick = () => (isMobile() ? $("side").classList.add("open") : setCollapsed(false));
$("collapseSide").onclick = () => setCollapsed(true);
$("newSmall").onclick = () => $("newBtn").click();
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b" && !isMobile()) {
    e.preventDefault();
    setCollapsed(!$("app").classList.contains("collapsed"));
  }
});
$("closeSide").onclick = () => $("side").classList.remove("open");
$("scrim").onclick = () => $("side").classList.remove("open");

const EMPTY = `<div class="empty">
  <div class="logo lg"><svg viewBox="0 0 24 24"><path d="M7 9l4 3-4 3M13 16h4"/></svg></div>
  <h2>有什么要做的？</h2>
  <p>选一个项目目录，让家里电脑上的 Claude Code 帮你读代码、改代码、回答问题。</p>
  <button class="btn primary" onclick="document.getElementById('newBtn').click()">开始新对话</button>
  <div class="tips">
    <div class="tip"><b>只读提问</b>权限模式选「只读」，Claude 只看代码不改动</div>
    <div class="tip"><b>图片和文件</b>直接粘贴截图，或拖进对话里</div>
    <div class="tip"><b>随时回退</b>每条消息都能回到发送之前的状态</div>
  </div>
</div>`;

// ------------------------------------------------------------------ 打开对话
async function openChat(id) {
  if (state.es) { state.es.close(); state.es = null; }
  state.chatId = id;
  state.tools = {};
  state.perms = {};
  state.questions = {};
  state.lastId = 0;
  const box = $("messages");
  if (!id) {
    box.innerHTML = EMPTY;
    for (const el of ["composer", "modeSel", "setBtn", "ctxBtn", "filesBtn", "archiveBtn", "statusBar"]) $(el).classList.add("hidden");
    $("chatTitle").textContent = "网页 Agent";
    $("chatFolder").textContent = "";
    return;
  }
  let d;
  try { d = await api(`api/chats/${id}`); } catch (e) { box.innerHTML = `<div class="empty err">${esc(e.message)}</div>`; return; }
  state.chat = d.chat;
  state.rewindable = new Set(d.rewindable);
  $("chatTitle").textContent = d.chat.title;
  $("chatFolder").textContent = d.chat.folder;
  $("modeSel").value = d.chat.mode;
  for (const el of ["composer", "modeSel", "setBtn", "filesBtn", "archiveBtn"]) $(el).classList.remove("hidden");
  showSettings();
  showUsage(d.chat.usage ? JSON.parse(d.chat.usage) : null);
  renderQueue(d.queue || []);
  box.innerHTML = "";
  for (const ev of d.events) render(ev);
  setStatus(d.chat.status);
  scrollDown(true);
  if (window.innerWidth > 760) $("input").focus();
  loadChats().catch(() => {});
  state.es = new EventSource(`api/chats/${id}/stream?after=${state.lastId}`);
  state.es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.type === "chat_status") {
      const was = state.chat?.status;
      // 一轮结束：重新加载一次，刷新标题、回退按钮和列表
      if (ev.data.status === "idle" && was && was !== "idle") return openChat(id);
      return setStatus(ev.data.status);
    }
    if (ev.type === "queue") return renderQueue(ev.data.items || []);
    if (ev.type === "progress") { if (state.chat?.status === "running") $("statusText").textContent = ev.data.text; return; }
    if (ev.reload) return openChat(id);
    if (ev.id && ev.id <= state.lastId) return;
    render(ev);
    scrollDown();
  };
}

window.addEventListener("hashchange", () => openChat(location.hash.slice(1) || null));

function scrollDown(force) {
  const box = $("messages");
  if (force || box.scrollHeight - box.scrollTop - box.clientHeight < 200) box.scrollTop = box.scrollHeight;
}

function setStatus(status) {
  if (state.chat) state.chat.status = status;
  const bar = $("statusBar");
  const busy = status === "running" || status === "waiting" || status === "rewinding";
  bar.classList.toggle("hidden", !busy);
  bar.classList.toggle("waiting", status === "waiting");
  $("statusText").textContent = { running: "Claude 正在工作，新消息会排队", waiting: "等你批准或回答", rewinding: "正在回退文件…" }[status] || "";
  $("stopBtn").classList.toggle("hidden", status === "rewinding");
  const working = BUSY.includes(status);
  $("sendBtn").disabled = status === "rewinding";
  $("sendBtn").textContent = working ? "排队" : "发送";
  $("sendBtn").title = working ? "这一轮做完后自动发出" : "";
  $("interruptBtn").classList.toggle("hidden", !working);
  $("ctxBtn").disabled = busy;
  document.querySelectorAll(".rewind-btn").forEach((b) => (b.disabled = busy));
}

function showSettings() {
  const c = state.chat || {};
  const m = modelName(c.model || state.defaults.model) || "默认模型";
  $("setBtn").textContent = `${m} · ${c.effort || state.defaults.effort || "默认"}`;
}

function showUsage(u) {
  const b = $("ctxBtn");
  if (!u || u.context_pct == null) { b.classList.add("hidden"); return; }
  b.classList.remove("hidden");
  b.textContent = `上下文 ${Math.round(u.context_pct)}%`;
  b.className = "pill ctx" + (u.context_pct >= 80 ? " warn" : "");
  b.style.setProperty("--pct", Math.min(100, u.context_pct) + "%");
  b.title = `已用 ${fmtTokens(u.context_tokens)} / ${fmtTokens(u.context_max)} tokens。点击压缩（/compact）：把之前的对话总结成摘要，腾出空间`;
}

function renderQueue(items) {
  state.queue = items;
  const box = $("queueList");
  box.classList.toggle("hidden", !items.length);
  box.innerHTML = items.length ? '<div class="small muted">排队中，这一轮做完后按顺序自动发出：</div>' : "";
  for (const it of items) {
    const row = document.createElement("div");
    row.className = "queued";
    row.innerHTML = `<span class="qtext">${esc(it.text || "")}${it.files?.length ? ` <span class="muted">（${it.files.length} 个附件）</span>` : ""}</span><button title="取消这条">✕</button>`;
    row.querySelector("button").onclick = () => api(`api/chats/${state.chatId}/unqueue`, { method: "POST", json: { id: it.id } }).catch((e) => alert(e.message));
    box.appendChild(row);
  }
}

// ------------------------------------------------------------------ 渲染事件
function add(html, cls = "msg") {
  const div = document.createElement("div");
  div.className = cls;
  div.innerHTML = html;
  $("messages").appendChild(div);
  return div;
}

function fileThumbs(files) {
  if (!files?.length) return "";
  return `<div class="thumbs">${files.map((f) => (f.content_type || "").startsWith("image/")
    ? `<a href="api/files/${esc(f.file_id)}" target="_blank"><img src="api/files/${esc(f.file_id)}" alt="${esc(f.name)}"></a>`
    : `<a class="file" href="api/files/${esc(f.file_id)}?dl=1">${ICON.file}${esc(f.name)}</a>`).join("")}</div>`;
}

function toolSummary(name, input) {
  const i = input || {};
  const v = i.command || i.file_path || i.notebook_path || i.path || i.pattern || i.url || i.description || i.query || "";
  return `<span class="name">${esc(name)}</span><span class="arg">${esc(String(v).split("\n")[0].slice(0, 160))}</span>`;
}

function render(ev) {
  if (ev.id) state.lastId = Math.max(state.lastId, ev.id);
  const d = ev.data || {};
  const hidden = ev.hidden ? " hidden-turn" : "";
  switch (ev.type) {
    case "user": {
      const div = add(`<div class="user-text">${esc(d.text)}</div>${fileThumbs(d.files)}`, "msg user" + hidden);
      if (!ev.hidden && state.rewindable.has(ev.turn)) {
        const tools = document.createElement("div");
        tools.className = "tools";
        const b = document.createElement("button");
        b.className = "rewind-btn";
        b.textContent = "↶ 回退到这条之前";
        b.title = "文件恢复到发这条消息之前的样子，对话也从那里继续";
        b.onclick = () => rewind(ev.turn, d.text);
        tools.appendChild(b);
        div.appendChild(tools);
      }
      break;
    }
    case "text":
      add(md(d.text), "msg assistant" + hidden);
      break;
    case "tool_use": {
      const el = add(`<details><summary>${toolSummary(d.name, d.input)}</summary><div class="body"><pre>${esc(JSON.stringify(d.input, null, 2))}</pre><div class="result pending">运行中…</div></div></details>`, "msg tool" + hidden);
      const fp = d.input?.file_path || d.input?.notebook_path;
      if (fp && ["Write", "Edit", "MultiEdit", "NotebookEdit", "Read"].includes(d.name)) {
        const b = document.createElement("button");
        b.className = "view-btn";
        b.textContent = "查看文件";
        b.onclick = (e) => { e.preventDefault(); openPreview(fp); };
        el.querySelector("summary").appendChild(b);
      }
      state.tools[d.id] = el;
      break;
    }
    case "question": {
      const el = add(questionCard(d), "msg question" + hidden);
      state.questions[d.request_id] = el;
      wireQuestion(el, d);
      break;
    }
    case "question_done": {
      const el = state.questions[d.request_id];
      if (el) {
        el.classList.add("done");
        const a = Object.entries(d.answers || {});
        el.querySelector(".actions").innerHTML = `<span class="muted small">${a.length ? "已回答：" + esc(a.map(([, v]) => v).join("；")) : "已跳过，让 Claude 自己决定"}</span>`;
        el.querySelectorAll("button, input").forEach((b) => (b.disabled = true));
      }
      break;
    }
    case "changes":
      add(changesCard(d), "msg changes" + hidden).querySelectorAll("[data-path]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); openPreview(a.dataset.path); }));
      break;
    case "tool_result": {
      const el = state.tools[d.tool_use_id];
      const html = `<pre>${esc(String(d.content ?? "").slice(0, 20000))}</pre>`;
      if (el) {
        const r = el.querySelector(".result");
        r.className = "result" + (d.is_error ? " err" : "");
        r.innerHTML = html;
      } else add(`<details><summary>工具结果</summary><div class="body">${html}</div></details>`, "msg tool" + hidden);
      break;
    }
    case "permission": {
      const el = add(`<div class="title">Claude 想要使用 ${esc(d.tool)}，需要你批准</div><pre>${esc(d.input?.command || d.input?.file_path ? (d.input.command || d.input.file_path) : JSON.stringify(d.input, null, 2))}</pre>
        <div class="actions"><button class="btn primary" data-a="allow">允许</button><button class="btn" data-a="always">本对话都允许 ${esc(d.tool)}</button><button class="btn danger" data-a="deny">拒绝</button></div>`, "msg perm" + hidden);
      state.perms[d.request_id] = el;
      el.querySelectorAll("button").forEach((b) => (b.onclick = () => decide(d.request_id, b.dataset.a)));
      break;
    }
    case "permission_done": {
      const el = state.perms[d.request_id];
      if (el) {
        el.classList.add("done");
        el.querySelector(".actions").innerHTML = `<span class="muted small">${d.allow ? (d.always ? "已允许（本对话不再询问）" : "已允许") : "已拒绝"}</span>`;
      }
      break;
    }
    case "result": {
      const parts = [d.ok ? "完成" : "未完成"];
      if (d.duration_ms) parts.push(`用时 ${Math.round(d.duration_ms / 1000)} 秒`);
      const u = d.usage;
      if (u) {
        parts.push(`输入 ${fmtTokens(u.input_tokens)} / 输出 ${fmtTokens(u.output_tokens)} tokens`);
        if (u.context_pct != null) parts.push(`上下文 ${Math.round(u.context_pct)}%`);
        if (u.model) parts.push(modelName(u.model));
        if (!ev.hidden) showUsage(u);
      }
      if (d.error) parts.push(d.error);
      add(esc(parts.join(" · ")), "msg note" + (d.ok ? "" : " err") + hidden);
      break;
    }
    case "file":
      add(`<div class="thumbs">${fileThumbs([d]).replace(/^<div class="thumbs">|<\/div>$/g, "")}</div><div class="small muted">${esc(d.path || "")}</div>`, "msg" + hidden);
      break;
    case "rewound":
      add(`已回退到第 ${esc(d.turn)} 条消息之前，文件已恢复${d.changed?.length ? `（${d.changed.length} 个文件）` : ""}。可以重新发消息。`
        + (d.skipped?.length ? `<br>注意：目录里独立的 git 仓库没有回退：${esc(d.skipped.join("、"))}` : ""), "msg note ok");
      break;
    case "notice":
      add(esc(d.text), "msg note" + hidden);
      break;
    case "error":
      add(esc(d.text), "msg note err" + hidden);
      break;
  }
}

// Claude 的提问（AskUserQuestion）：每个问题一组选项，可以多选的用复选，另外都能填「其他」
function questionCard(d) {
  const qs = (d.questions || []).map((q, i) => `
    <div class="q" data-i="${i}">
      ${q.header ? `<span class="qh">${esc(q.header)}</span>` : ""}<div class="qt">${esc(q.question)}${q.multiSelect ? ' <span class="muted small">（可多选）</span>' : ""}</div>
      <div class="opts">${(q.options || []).map((o) => `<button type="button" class="opt" data-label="${esc(o.label)}"><b>${esc(o.label)}</b>${o.description ? `<span class="muted small">${esc(o.description)}</span>` : ""}</button>`).join("")}</div>
      <input class="other" placeholder="其他（自己填写）">
    </div>`).join("");
  return `<div class="title">Claude 想问你</div>${qs}
    <div class="actions"><button class="btn primary" data-a="send">提交回答</button><button class="btn" data-a="skip">跳过，让 Claude 自己决定</button></div>`;
}

function wireQuestion(el, d) {
  el.querySelectorAll(".q").forEach((qel) => {
    const q = d.questions[+qel.dataset.i];
    qel.querySelectorAll(".opt").forEach((b) => (b.onclick = () => {
      if (!q.multiSelect) qel.querySelectorAll(".opt").forEach((x) => x !== b && x.classList.remove("on"));
      b.classList.toggle("on");
    }));
  });
  el.querySelector('[data-a="send"]').onclick = () => {
    const answers = {};
    let missing = false;
    el.querySelectorAll(".q").forEach((qel) => {
      const q = d.questions[+qel.dataset.i];
      const picked = [...qel.querySelectorAll(".opt.on")].map((b) => b.dataset.label);
      const other = qel.querySelector(".other").value.trim();
      if (other) picked.push(other);
      if (picked.length) answers[q.question] = picked.join(", ");
      else missing = true;
    });
    if (missing && !confirm("有的问题还没选，没选的让 Claude 自己决定？")) return;
    answer(d.request_id, answers);
  };
  el.querySelector('[data-a="skip"]').onclick = () => answer(d.request_id, {});
}

async function answer(requestId, answers) {
  try { await api(`api/chats/${state.chatId}/answer`, { method: "POST", json: { request_id: requestId, answers } }); } catch (e) { alert(e.message); }
}

// 一轮结束后，这一轮改动的文件；改过的图片直接显示
function changesCard(d) {
  const label = { A: "新建", M: "修改", D: "删除" };
  const imgs = (d.files || []).filter((f) => f.file_id);
  const list = (d.files || []).map((f) => `<li><span class="st st-${esc(f.status)}">${label[f.status] || esc(f.status)}</span>${f.status === "D"
    ? `<span class="muted">${esc(f.path)}</span>` : `<a href="#" data-path="${esc(f.path)}">${esc(f.path)}</a>`}</li>`).join("");
  const more = d.total > (d.files || []).length ? `<li class="muted">…还有 ${d.total - d.files.length} 个</li>` : "";
  return `<details${(d.files || []).length <= 8 ? " open" : ""}><summary>这一轮改动了 ${esc(d.total)} 个文件</summary><ul class="changed">${list}${more}</ul></details>`
    + (imgs.length ? `<div class="thumbs big">${imgs.map((f) => `<a href="#" data-path="${esc(f.path)}" title="${esc(f.path)}"><img src="api/files/${esc(f.file_id)}" alt="${esc(f.path)}"></a>`).join("")}</div>` : "");
}

// ------------------------------------------------------------------ 文件浏览 / 预览
const TEXT_EXT = /\.(txt|md|markdown|log|csv|tsv|json|jsonl|ya?ml|toml|ini|cfg|conf|env|xml|html?|css|scss|less|js|mjs|cjs|jsx|ts|tsx|vue|svelte|py|go|rs|java|kt|kts|swift|c|h|cc|cpp|hpp|cs|rb|php|sh|bash|zsh|ps1|psm1|bat|cmd|sql|gradle|properties|lock|gitignore|dockerfile|makefile|r|lua|dart|proto)$/i;
const files = { path: "" };

function openFiles(path = "") {
  $("filesDlg").showModal();
  return listDir(path);
}

async function listDir(path) {
  files.path = path;
  $("filesPreview").classList.add("hidden");
  $("filesList").classList.remove("hidden");
  $("filesBack").classList.add("hidden");
  const root = (state.folders.find((f) => f.path === state.chat?.folder) || {}).name || "项目";
  const segs = path ? path.split("/") : [];
  $("filesPath").innerHTML = [`<a href="#" data-p="">${esc(root)}</a>`, ...segs.map((s, i) => `<a href="#" data-p="${esc(segs.slice(0, i + 1).join("/"))}">${esc(s)}</a>`)].join(" / ");
  $("filesPath").querySelectorAll("a").forEach((a) => (a.onclick = (e) => { e.preventDefault(); listDir(a.dataset.p); }));
  const box = $("filesList");
  box.innerHTML = '<div class="muted pad">读取中…</div>';
  try {
    const d = await api(`api/chats/${state.chatId}/ls?path=${encodeURIComponent(path)}`);
    box.innerHTML = "";
    if (path) d.entries.unshift({ name: "..", dir: true, up: true });
    for (const e of d.entries) {
      const row = document.createElement("div");
      row.className = "frow" + (e.dir ? " dir" : "");
      const p = e.up ? segs.slice(0, -1).join("/") : (path ? path + "/" : "") + e.name;
      row.innerHTML = `${e.up ? ICON.up : e.dir ? ICON.folder : ICON.file}<span class="fname">${e.up ? "上一级" : esc(e.name)}</span><span class="muted small">${e.dir ? "" : fmtSize(e.size)}</span>`;
      row.onclick = () => (e.dir ? listDir(p) : openPreview(p));
      box.appendChild(row);
    }
    if (!d.entries.length) box.innerHTML = '<div class="muted pad">空目录</div>';
    if (d.truncated) box.insertAdjacentHTML("beforeend", '<div class="muted pad small">文件太多，只显示了前 1000 个</div>');
  } catch (e) { box.innerHTML = `<div class="err pad">${esc(e.message)}</div>`; }
}

async function openPreview(path) {
  if (!$("filesDlg").open) $("filesDlg").showModal();
  $("filesList").classList.add("hidden");
  $("filesBack").classList.remove("hidden");
  const box = $("filesPreview");
  box.classList.remove("hidden");
  $("filesPath").textContent = path;
  box.innerHTML = '<div class="muted pad">正在从电脑取文件…</div>';
  try {
    const d = await api(`api/chats/${state.chatId}/open?path=${encodeURIComponent(path)}`);
    files.path = d.path.split("/").slice(0, -1).join("/");
    const head = `<div class="pv-head"><b>${esc(d.name)}</b><span class="muted small">${fmtSize(d.size)}</span>
      <a class="btn" href="${esc(d.url)}" target="_blank" rel="noopener">新窗口打开</a><a class="btn primary" href="${esc(d.url)}?dl=1">下载</a></div>`;
    const ct = d.content_type || "";
    if (ct.startsWith("image/")) {
      box.innerHTML = head + `<div class="pv-img"><img src="${esc(d.url)}" alt="${esc(d.name)}"></div>`;
    } else if (ct.startsWith("text/") || TEXT_EXT.test(d.name) || d.size < 512 * 1024) {
      const r = await fetch(d.url, { credentials: "same-origin" });
      const buf = new Uint8Array(await r.arrayBuffer());
      if (buf.slice(0, 8000).includes(0)) box.innerHTML = head + '<div class="muted pad">二进制文件，无法预览，请下载。</div>';
      else {
        const limit = 400 * 1024;
        const text = new TextDecoder("utf-8").decode(buf.slice(0, limit));
        box.innerHTML = head + `<pre class="pv-text">${esc(text)}${buf.length > limit ? "\n\n…（文件较大，只显示前 400 KB，完整内容请下载）" : ""}</pre>`;
      }
    } else if (ct === "application/pdf") {
      box.innerHTML = head + '<div class="muted pad">PDF 请点「新窗口打开」查看。</div>';
    } else box.innerHTML = head + '<div class="muted pad">这种文件不能在网页上预览，请下载。</div>';
  } catch (e) { box.innerHTML = `<div class="err pad">${esc(e.message)}</div>`; }
}
$("filesBack").onclick = () => listDir(files.path || "");
$("filesClose").onclick = () => $("filesDlg").close();
$("filesBtn").onclick = () => openFiles("");

// ------------------------------------------------------------------ 操作
async function decide(requestId, action) {
  try {
    await api(`api/chats/${state.chatId}/permission`, { method: "POST", json: { request_id: requestId, allow: action !== "deny", always: action === "always" } });
  } catch (e) { alert(e.message); }
}

async function rewind(turn, text) {
  if (!confirm(`回退到第 ${turn} 条消息之前？\n这之后 Claude 对文件的改动都会撤销（包括新建的文件），对话也从那里重新开始。`)) return;
  try {
    await api(`api/chats/${state.chatId}/rewind`, { method: "POST", json: { turn } });
    $("input").value = text || "";
  } catch (e) { alert(e.message); }
}

$("stopBtn").onclick = async () => {
  $("stopBtn").disabled = true;
  $("statusText").textContent = "正在停止…";
  try { await api(`api/chats/${state.chatId}/stop`, { method: "POST" }); } catch (e) { alert(e.message); }
  setTimeout(() => ($("stopBtn").disabled = false), 5000);
};
$("modeSel").onchange = () => api(`api/chats/${state.chatId}/mode`, { method: "POST", json: { mode: $("modeSel").value } }).catch((e) => alert(e.message));
$("archiveBtn").onclick = async () => {
  if (!confirm("归档这个对话？（只是从列表移除，电脑上的文件不受影响）")) return;
  await api(`api/chats/${state.chatId}/archive`, { method: "POST" });
  location.hash = "";
};
$("setBtn").onclick = () => {
  fillSettings($("setModel"), $("setEffort"), state.chat?.model, state.chat?.effort);
  $("setDlg").showModal();
};
$("saveSetBtn").onclick = async (e) => {
  e.preventDefault();
  const body = { model: $("setModel").value, effort: $("setEffort").value };
  try {
    await api(`api/chats/${state.chatId}/settings`, { method: "POST", json: body });
    Object.assign(state.chat, body);
    showSettings();
    $("setDlg").close();
  } catch (err) { alert(err.message); }
};
$("ctxBtn").onclick = async () => {
  if (!confirm("压缩上下文？\nClaude 会把之前的对话总结成一份摘要，腾出上下文空间（细节可能会丢一些）。")) return;
  try { await api(`api/chats/${state.chatId}/messages`, { method: "POST", json: { text: "/compact", files: [] } }); } catch (e) { alert(e.message); }
};

// 附件：选择 / 粘贴 / 拖拽
async function uploadFiles(files) {
  for (const file of files) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = `上传中 ${file.name || "图片"}…`;
    $("attachList").appendChild(chip);
    try {
      const fd = new FormData();
      fd.append("file", file, file.name || "image.png");
      const d = await api("api/upload", { method: "POST", body: fd });
      state.pending.push(d);
      chip.innerHTML = `${esc(d.name)} <button title="移除">✕</button>`;
      chip.querySelector("button").onclick = () => { state.pending = state.pending.filter((p) => p !== d); chip.remove(); };
    } catch (e) { chip.textContent = `上传失败：${e.message}`; setTimeout(() => chip.remove(), 4000); }
  }
}
$("fileInput").onchange = (e) => { uploadFiles([...e.target.files]); e.target.value = ""; };
$("input").addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); uploadFiles(files); }
});
$("messages").addEventListener("dragover", (e) => e.preventDefault());
$("messages").addEventListener("drop", (e) => { e.preventDefault(); if (state.chatId) uploadFiles([...e.dataTransfer.files]); });

async function sendMessage(interrupt = false) {
  const text = $("input").value;
  if (!text.trim() && !state.pending.length) return;
  const working = BUSY.includes(state.chat?.status);
  $("sendBtn").disabled = true;
  try {
    await api(`api/chats/${state.chatId}/messages`, {
      method: "POST", json: { text, files: state.pending.map((p) => p.file_id), queue: working, interrupt: working && interrupt },
    });
    $("input").value = "";
    state.pending = [];
    $("attachList").innerHTML = "";
    autosize();
  } catch (e) { alert(e.message); }
  $("sendBtn").disabled = state.chat?.status === "rewinding";
}
$("sendBtn").onclick = () => sendMessage(false);
$("interruptBtn").onclick = () => sendMessage(true);
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && window.innerWidth > 760) { e.preventDefault(); sendMessage(); }
});
function autosize() { const t = $("input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, window.innerHeight * 0.4) + "px"; }
$("input").addEventListener("input", autosize);

// ------------------------------------------------------------------ 启动
(async function init() {
  let me;
  try { me = await api("api/me"); } catch { return; }
  state.folders = me.folders;
  state.models = me.models || [];
  state.efforts = me.efforts || [];
  state.defaults = me.defaults || {};
  if (window.innerWidth <= 760) $("input").placeholder = "给 Claude 发消息";
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  await loadChats();
  watchList();
  openChat(location.hash.slice(1) || null);
})();
