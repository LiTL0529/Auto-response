"use strict";
// 网页 Agent 前端：登录（企业微信确认）、对话列表、实时消息（SSE）、附件、审批、回退。
const $ = (id) => document.getElementById(id);
const state = { chatId: null, chat: null, lastId: 0, es: null, listEs: null, folders: [], pending: [], rewindable: new Set(), tools: {}, perms: {} };

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
  let list = null;
  for (const line of s.split("\n")) {
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    const li = line.match(/^\s*(?:[-*+]|\d+\.)\s+(.*)$/);
    if (li) { if (!list) { list = []; } list.push(`<li>${li[1]}</li>`); continue; }
    if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; }
    if (h) out.push(`<h${h[1].length + 1}>${h[2]}</h${h[1].length + 1}>`);
    else if (line.trim() === "") out.push("");
    else out.push(`<p>${line}</p>`);
  }
  if (list) out.push(`<ul>${list.join("")}</ul>`);
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
    const name = (state.folders.find((f) => f.path === c.folder) || {}).name || c.folder;
    li.innerHTML = `<div class="t"><span class="badge ${esc(c.status)}"></span>${esc(c.title)}</div><div class="f">${esc(name)}</div>`;
    li.onclick = () => { location.hash = c.id; $("side").classList.remove("open"); };
    ul.appendChild(li);
  }
  const st = $("agentState");
  st.textContent = d.agent_online ? "● 家里电脑在线" : "● 家里电脑离线，暂时不能对话";
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
  $("newDlg").showModal();
};
$("createBtn").onclick = async (e) => {
  e.preventDefault();
  const folder = $("folderSel").value;
  if (!folder) return;
  try {
    const c = await api("api/chats", { method: "POST", json: { folder, mode: $("newMode").value } });
    $("newDlg").close();
    location.hash = c.id;
  } catch (err) { alert(err.message); }
};
$("openSide").onclick = () => $("side").classList.add("open");
$("closeSide").onclick = () => $("side").classList.remove("open");

// ------------------------------------------------------------------ 打开对话
async function openChat(id) {
  if (state.es) { state.es.close(); state.es = null; }
  state.chatId = id;
  state.tools = {};
  state.perms = {};
  state.lastId = 0;
  const box = $("messages");
  if (!id) {
    box.innerHTML = '<div class="empty muted">左边选一个对话，或点「新对话」选择项目目录开始。</div>';
    for (const el of ["composer", "modeSel", "fetchBtn", "archiveBtn", "statusBar"]) $(el).classList.add("hidden");
    $("chatTitle").textContent = "选择或新建一个对话";
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
  for (const el of ["composer", "modeSel", "fetchBtn", "archiveBtn"]) $(el).classList.remove("hidden");
  box.innerHTML = "";
  for (const ev of d.events) render(ev);
  setStatus(d.chat.status);
  scrollDown(true);
  loadChats().catch(() => {});
  state.es = new EventSource(`api/chats/${id}/stream?after=${state.lastId}`);
  state.es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.type === "chat_status") return setStatus(ev.data.status);
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
  $("statusText").textContent = { running: "Claude 正在工作…", waiting: "等你批准", rewinding: "正在回退文件…" }[status] || "";
  $("stopBtn").classList.toggle("hidden", status === "rewinding");
  $("sendBtn").disabled = busy;
  document.querySelectorAll(".rewind-btn").forEach((b) => (b.disabled = busy));
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
    : `<a class="file" href="api/files/${esc(f.file_id)}">📄 ${esc(f.name)}</a>`).join("")}</div>`;
}

function toolSummary(name, input) {
  const i = input || {};
  const v = i.command || i.file_path || i.path || i.pattern || i.url || i.description || i.query || "";
  return `<span class="name">${esc(name)}</span> ${esc(String(v).split("\n")[0].slice(0, 120))}`;
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
      state.tools[d.id] = el;
      break;
    }
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
        <div class="actions"><button class="primary" data-a="allow">允许</button><button data-a="always">本对话都允许 ${esc(d.tool)}</button><button class="danger" data-a="deny">拒绝</button></div>`, "msg perm" + hidden);
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
      if (d.error) parts.push(d.error);
      add(esc(parts.join(" · ")), "msg note" + (d.ok ? "" : " err") + hidden);
      break;
    }
    case "file":
      add(`<div class="thumbs">${fileThumbs([d]).replace(/^<div class="thumbs">|<\/div>$/g, "")}</div><div class="small muted">${esc(d.path || "")}</div>`, "msg" + hidden);
      break;
    case "rewound":
      add(`已回退到第 ${esc(d.turn)} 条消息之前，文件已恢复${d.changed?.length ? `（${d.changed.length} 个文件）` : ""}。可以重新发消息。`, "msg note ok");
      break;
    case "notice":
      add(esc(d.text), "msg note" + hidden);
      break;
    case "error":
      add(esc(d.text), "msg note err" + hidden);
      break;
  }
}

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

$("stopBtn").onclick = () => api(`api/chats/${state.chatId}/stop`, { method: "POST" }).catch((e) => alert(e.message));
$("modeSel").onchange = () => api(`api/chats/${state.chatId}/mode`, { method: "POST", json: { mode: $("modeSel").value } }).catch((e) => alert(e.message));
$("archiveBtn").onclick = async () => {
  if (!confirm("归档这个对话？（只是从列表移除，电脑上的文件不受影响）")) return;
  await api(`api/chats/${state.chatId}/archive`, { method: "POST" });
  location.hash = "";
};
$("fetchBtn").onclick = async () => {
  const path = prompt("要取回的文件路径（相对项目目录，或绝对路径）：");
  if (path) api(`api/chats/${state.chatId}/fetch`, { method: "POST", json: { path } }).catch((e) => alert(e.message));
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

async function sendMessage() {
  const text = $("input").value;
  if (!text.trim() && !state.pending.length) return;
  $("sendBtn").disabled = true;
  try {
    await api(`api/chats/${state.chatId}/messages`, { method: "POST", json: { text, files: state.pending.map((p) => p.file_id) } });
    $("input").value = "";
    state.pending = [];
    $("attachList").innerHTML = "";
    autosize();
  } catch (e) { alert(e.message); $("sendBtn").disabled = false; }
}
$("sendBtn").onclick = sendMessage;
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
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  await loadChats();
  watchList();
  openChat(location.hash.slice(1) || null);
})();
