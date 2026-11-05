// NexusAI web client: threads, live chat over WebSocket, search, model switching.
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const md = window.NexusMarkdown;

  // Optional auth token (NEXUS_AUTH_TOKEN): taken from ?token=… once, then remembered.
  const params = new URLSearchParams(location.search);
  let token = params.get("token");
  try {
    if (token) localStorage.setItem("nexus-token", token);
    else token = localStorage.getItem("nexus-token");
  } catch (_) { /* storage unavailable */ }
  if (params.has("token")) history.replaceState(null, "", location.pathname + location.hash);

  const state = {
    threads: [],
    threadId: null,
    models: [],
    model: null,
    shown: new Set(),       // message ids rendered in the current thread
    streaming: new Map(),   // thread_id -> {el, text, model}
    pendingSend: null,      // text waiting for a thread to be created
    ws: null,
    retry: 0,
  };

  // ---------- HTTP ----------

  async function api(path, opts = {}) {
    const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    if (token) headers["Authorization"] = "Bearer " + token;
    const res = await fetch(path, Object.assign({}, opts, { headers }));
    if (!res.ok) {
      let detail = res.statusText;
      try { const body = await res.json(); detail = body.detail || detail; } catch (_) {}
      if (typeof detail !== "string") detail = JSON.stringify(detail);
      throw new Error(detail);
    }
    return res.status === 204 ? null : res.json();
  }

  // ---------- rendering ----------

  function fmtTime(ts) {
    const d = new Date(ts * 1000);
    const today = new Date().toDateString() === d.toDateString();
    return today ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
                 : d.toLocaleDateString([], { month: "short", day: "numeric" });
  }

  function renderThreads() {
    const ul = $("#threads");
    ul.textContent = "";
    if (!state.threads.length) {
      const li = document.createElement("li");
      li.className = "muted-item";
      li.textContent = "No threads yet";
      ul.appendChild(li);
    }
    for (const t of state.threads) {
      const li = document.createElement("li");
      li.className = "thread" + (t.id === state.threadId ? " active" : "");
      li.dataset.id = t.id;
      const title = document.createElement("span");
      title.className = "thread-title";
      title.textContent = t.title || "Untitled";
      const meta = document.createElement("span");
      meta.className = "thread-meta";
      meta.textContent = t.model + " · " + fmtTime(t.updated_at);
      const del = document.createElement("button");
      del.className = "thread-del";
      del.title = "Delete thread";
      del.setAttribute("aria-label", "Delete thread");
      del.textContent = "×";
      del.addEventListener("click", (e) => { e.stopPropagation(); deleteThread(t); });
      li.append(title, meta, del);
      li.addEventListener("click", () => openThread(t.id));
      ul.appendChild(li);
    }
    const current = state.threads.find((t) => t.id === state.threadId);
    $("#thread-title").textContent = current ? current.title : "New chat";
    document.title = current ? current.title + " · NexusAI" : "NexusAI";
  }

  function renderModels() {
    const sel = $("#model");
    sel.textContent = "";
    for (const m of state.models) {
      const opt = document.createElement("option");
      opt.value = m.name;
      opt.textContent = m.available ? m.name : m.name + " (no key)";
      sel.appendChild(opt);
    }
    sel.value = state.model;
    updateBanner();
  }

  function updateBanner(text) {
    const banner = $("#banner");
    const info = state.models.find((m) => m.name === state.model);
    const msg = text || (info && !info.available
      ? info.missing_key + " is not set — add it to .env and restart the server to use " + info.name + "."
      : "");
    banner.textContent = msg;
    banner.hidden = !msg;
  }

  function scrollToBottom(force) {
    const box = $("#messages");
    const near = box.scrollHeight - box.scrollTop - box.clientHeight < 160;
    if (force || near) box.scrollTop = box.scrollHeight;
  }

  function bubble(role, header) {
    $("#empty").hidden = true;
    const el = document.createElement("article");
    el.className = "msg " + role;
    const h = document.createElement("div");
    h.className = "msg-head";
    h.textContent = header;
    const body = document.createElement("div");
    body.className = "msg-body";
    el.append(h, body);
    return el;
  }

  function messageHeader(m) {
    if (m.role === "user") return "You";
    const model = (m.model || "assistant").split("/").pop();
    return m.provider ? model + " · " + m.provider : model;
  }

  function addMessage(m) {
    if (m.thread_id !== state.threadId || state.shown.has(m.id)) return;
    state.shown.add(m.id);
    const el = bubble(m.role, messageHeader(m));
    el.dataset.id = m.id;
    el.querySelector(".msg-body").innerHTML = md.render(m.content);
    const box = $("#messages");
    // Keep chronological order even if events arrive out of order.
    const later = Array.from(box.querySelectorAll("article.msg[data-id]"))
      .find((x) => Number(x.dataset.id) > m.id);
    const stream = state.streaming.get(m.thread_id);
    if (later) box.insertBefore(el, later);
    else if (stream && stream.el && stream.el.isConnected && m.role === "user") box.insertBefore(el, stream.el);
    else box.appendChild(el);
    scrollToBottom(m.role === "user");
  }

  function addNote(text, kind) {
    const el = bubble(kind || "note", kind === "error" ? "Error" : "NexusAI");
    el.querySelector(".msg-body").textContent = text;
    $("#messages").appendChild(el);
    scrollToBottom(true);
  }

  function clearMessages() {
    const box = $("#messages");
    box.querySelectorAll("article").forEach((a) => a.remove());
    $("#empty").hidden = false;
    state.shown = new Set();
  }

  function setBusy() {
    const busy = state.threadId !== null && state.streaming.has(state.threadId);
    $("#send").disabled = busy;
    $("#send").textContent = busy ? "…" : "Send";
  }

  // ---------- streaming ----------

  function streamStart(threadId, model) {
    const s = { el: null, text: "", model: model };
    state.streaming.set(threadId, s);
    setBusy();
  }

  function streamDelta(threadId, text) {
    let s = state.streaming.get(threadId);
    if (!s) { s = { el: null, text: "", model: "" }; state.streaming.set(threadId, s); }
    s.text += text;
    if (threadId !== state.threadId) return;
    if (!s.el || !s.el.isConnected) {
      s.el = bubble("assistant streaming", (s.model || "assistant") + " · streaming…");
      $("#messages").appendChild(s.el);
    }
    s.el.querySelector(".msg-body").innerHTML = md.render(s.text);
    scrollToBottom(false);
  }

  function streamFinish(threadId) {
    const s = state.streaming.get(threadId);
    if (s && s.el) s.el.remove();
    state.streaming.delete(threadId);
    setBusy();
  }

  // ---------- actions ----------

  async function loadThreads() {
    const requestedFor = state.threadId;
    const threads = await api("/api/threads");
    // A newer thread may have been created/opened while this request was in flight.
    if (state.threadId !== requestedFor) return;
    state.threads = threads;
    if (state.threadId && !state.threads.some((t) => t.id === state.threadId)) {
      state.threadId = null;
      clearMessages();
      history.replaceState(null, "", location.pathname);
    }
    renderThreads();
  }

  async function openThread(id) {
    closeSidebarOnMobile();
    const t = state.threads.find((x) => x.id === id) || await api("/api/threads/" + id).catch(() => null);
    if (!t) { addNote("That thread no longer exists.", "error"); return; }
    state.threadId = id;
    if (location.hash !== "#" + id) history.replaceState(null, "", "#" + id);
    clearMessages();
    if (state.models.some((m) => m.name === t.model)) { state.model = t.model; renderModels(); }
    renderThreads();
    const msgs = await api("/api/threads/" + id + "/messages?limit=500");
    if (state.threadId !== id) return;
    msgs.forEach(addMessage);
    const s = state.streaming.get(id);
    if (s && s.text) streamDelta(id, "");
    scrollToBottom(true);
    setBusy();
    $("#input").focus();
  }

  let creating = null; // in-flight newThread() promise, so a fast send targets the new thread

  function newThread() {
    if (!creating) {
      creating = (async () => {
        const t = await api("/api/threads", { method: "POST", body: JSON.stringify({ model: state.model }) });
        state.threads.unshift(t);
        await openThread(t.id);
        return t;
      })().finally(() => { creating = null; });
    }
    return creating;
  }

  async function deleteThread(t) {
    if (!confirm('Delete "' + t.title + '"? This cannot be undone.')) return;
    try {
      await api("/api/threads/" + t.id, { method: "DELETE" });
      await loadThreads();
    } catch (e) { addNote(e.message, "error"); }
  }

  async function renameThread() {
    const t = state.threads.find((x) => x.id === state.threadId);
    if (!t) return;
    const title = prompt("Rename thread", t.title);
    if (!title || !title.trim() || title === t.title) return;
    try {
      await api("/api/threads/" + t.id, { method: "PATCH", body: JSON.stringify({ title: title.trim() }) });
      await loadThreads();
    } catch (e) { addNote(e.message, "error"); }
  }

  async function changeModel(name) {
    state.model = name;
    updateBanner();
    if (!state.threadId) return;
    try {
      await api("/api/threads/" + state.threadId, { method: "PATCH", body: JSON.stringify({ model: name }) });
      await loadThreads();
    } catch (e) { addNote(e.message, "error"); }
  }

  async function send(text) {
    const info = state.models.find((m) => m.name === state.model);
    if (info && !info.available) { updateBanner(); return false; }
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
      addNote("Not connected to the server — reconnecting…", "error");
      return false;
    }
    if (creating) await creating;
    if (!state.threadId) await newThread();
    if (state.streaming.has(state.threadId)) return false;
    streamStart(state.threadId, state.model);
    state.ws.send(JSON.stringify({ type: "send", thread_id: state.threadId, content: text, model: state.model }));
    return true;
  }

  async function runSearch(q) {
    const list = $("#search-list");
    list.textContent = "";
    $("#search-results").hidden = false;
    $("#threads").hidden = true;
    let hits = [];
    try { hits = await api("/api/search?q=" + encodeURIComponent(q) + "&k=20"); }
    catch (e) { addNote(e.message, "error"); }
    if (!hits.length) {
      const li = document.createElement("li");
      li.className = "muted-item";
      li.textContent = "No matches";
      list.appendChild(li);
    }
    for (const h of hits) {
      const li = document.createElement("li");
      li.className = "hit";
      const title = document.createElement("span");
      title.className = "thread-title";
      title.textContent = (h.thread_title || "Untitled") + " · " + h.role;
      const snip = document.createElement("span");
      snip.className = "hit-snippet";
      snip.textContent = h.content.length > 180 ? h.content.slice(0, 179) + "…" : h.content;
      li.append(title, snip);
      li.addEventListener("click", () => openThread(h.thread_id));
      list.appendChild(li);
    }
  }

  function closeSearch() {
    $("#search-results").hidden = true;
    $("#threads").hidden = false;
    $("#search").value = "";
  }

  function closeSidebarOnMobile() {
    if (window.matchMedia("(max-width: 760px)").matches) document.body.classList.remove("sidebar-open");
  }

  // ---------- WebSocket ----------

  function setConn(ok, text) {
    $("#conn").classList.toggle("ok", ok);
    $("#conn-text").textContent = text;
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    const url = proto + location.host + "/ws" + (token ? "?token=" + encodeURIComponent(token) : "");
    const ws = new WebSocket(url);
    state.ws = ws;
    ws.onopen = async () => {
      state.retry = 0;
      setConn(true, "Live");
      // Catch up on anything missed while disconnected.
      try {
        await loadThreads();
        if (state.threadId) {
          const msgs = await api("/api/threads/" + state.threadId + "/messages?limit=500");
          msgs.forEach(addMessage);
        }
      } catch (_) {}
    };
    ws.onmessage = (ev) => handleEvent(JSON.parse(ev.data));
    ws.onclose = () => {
      // Keep streaming threads marked busy: the server-side turn keeps running. The
      // "active" event sent on reconnect tells us which ones actually finished.
      setConn(false, "Reconnecting…");
      const delay = Math.min(1000 * 2 ** state.retry++, 15000);
      setTimeout(connect, delay);
    };
  }

  let threadsTimer = null;
  function refreshThreadsSoon() {
    clearTimeout(threadsTimer);
    threadsTimer = setTimeout(() => loadThreads().catch(() => {}), 150);
  }

  function handleEvent(e) {
    switch (e.type) {
      case "message":
        addMessage(e.message);
        if (e.message.role === "assistant" && state.streaming.has(e.message.thread_id)) {
          const s = state.streaming.get(e.message.thread_id);
          if (s.el) s.el.remove();
          s.el = null;
        }
        refreshThreadsSoon();
        break;
      case "stream_start":
        if (!state.streaming.has(e.thread_id)) streamStart(e.thread_id, e.model);
        break;
      case "delta":
        streamDelta(e.thread_id, e.text);
        break;
      case "stream_end":
        streamFinish(e.thread_id);
        if (e.error && e.thread_id === state.threadId) addNote(e.error, "error");
        refreshThreadsSoon();
        break;
      case "active": {
        const active = new Set(e.thread_ids);
        for (const id of Array.from(state.streaming.keys())) {
          if (!active.has(id)) streamFinish(id);
        }
        for (const id of active) {
          if (!state.streaming.has(id)) streamStart(id, "");
        }
        break;
      }
      case "threads_changed":
        refreshThreadsSoon();
        break;
      case "error":
        if (e.thread_id) streamFinish(e.thread_id);
        if (!e.thread_id || e.thread_id === state.threadId) addNote(e.detail, "error");
        break;
    }
  }

  // ---------- wiring ----------

  function autosize() {
    const ta = $("#input");
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 240) + "px";
  }

  async function submitComposer(ev) {
    if (ev) ev.preventDefault();
    const ta = $("#input");
    const text = ta.value.trim();
    if (!text) return;
    try {
      if (await send(text)) { ta.value = ""; autosize(); }
    } catch (e) { addNote(e.message, "error"); }
  }

  async function init() {
    $("#composer").addEventListener("submit", submitComposer);
    $("#input").addEventListener("input", autosize);
    $("#input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) submitComposer(e);
    });
    $("#new-thread").addEventListener("click", () => newThread().catch((e) => addNote(e.message, "error")));
    $("#model").addEventListener("change", (e) => changeModel(e.target.value));
    $("#thread-title").addEventListener("dblclick", renameThread);
    $("#search-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const q = $("#search").value.trim();
      if (q) runSearch(q); else closeSearch();
    });
    $("#search").addEventListener("search", () => { if (!$("#search").value) closeSearch(); });
    $("#clear-search").addEventListener("click", closeSearch);
    $("#toggle-sidebar").addEventListener("click", (e) => {
      e.stopPropagation();
      document.body.classList.toggle("sidebar-open");
    });
    $("#main").addEventListener("click", () => document.body.classList.remove("sidebar-open"));
    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey && e.altKey && e.key.toLowerCase() === "n") {
        e.preventDefault();
        newThread().catch((err) => addNote(err.message, "error"));
      }
    });

    try {
      const info = await api("/api/models");
      state.models = info.models;
      state.model = info.default;
      renderModels();
      await loadThreads();
      const wanted = location.hash.slice(1);
      if (wanted && state.threads.some((t) => t.id === wanted)) await openThread(wanted);
      else if (state.threads.length) await openThread(state.threads[0].id);
    } catch (e) {
      updateBanner("Could not reach the NexusAI server: " + e.message);
    }
    connect();
  }

  init();
})();
