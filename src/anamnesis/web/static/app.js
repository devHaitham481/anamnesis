// Anamnesis web client: browse and search the shared memory, with live updates over WebSocket.
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const md = window.AnamnesisMarkdown;

  // Optional auth token (ANAMNESIS_AUTH_TOKEN): taken from ?token=… once, then remembered.
  const params = new URLSearchParams(location.search);
  let token = params.get("token");
  try {
    if (token) localStorage.setItem("anamnesis-token", token);
    else token = localStorage.getItem("anamnesis-token");
  } catch (_) { /* storage unavailable */ }
  if (params.has("token")) history.replaceState(null, "", location.pathname + location.hash);

  const state = {
    threads: [],
    threadId: null,
    shown: new Set(),       // message ids rendered in the current thread
    ws: null,
    retry: 0,
    source: "",             // thread-list / search filter ("" = all tools)
    sourceLabels: {},       // source name -> human label
  };

  function sourceLabel(name) { return state.sourceLabels[name] || name || ""; }

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

  function fmtDateTime(ts) {
    return new Date(ts * 1000).toLocaleString([], {
      year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  }

  function currentThread() { return state.threads.find((t) => t.id === state.threadId); }

  function renderThreads() {
    const ul = $("#threads");
    ul.textContent = "";
    if (!state.threads.length) {
      const li = document.createElement("li");
      li.className = "muted-item";
      li.textContent = "No conversations yet";
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
      meta.textContent = sourceLabel(t.source) + " · " + fmtTime(t.updated_at);
      const del = document.createElement("button");
      del.className = "thread-del";
      del.title = "Delete from memory";
      del.setAttribute("aria-label", "Delete from memory");
      del.textContent = "×";
      del.addEventListener("click", (e) => { e.stopPropagation(); deleteThread(t); });
      li.append(title, meta, del);
      li.addEventListener("click", () => openThread(t.id));
      ul.appendChild(li);
    }
    renderHeader();
  }

  function renderHeader() {
    const t = currentThread();
    $("#thread-title").textContent = t ? t.title : "Anamnesis";
    $("#thread-meta").textContent = t
      ? [sourceLabel(t.source), t.project].filter(Boolean).join(" · ")
      : "";
    document.title = t ? t.title + " · Anamnesis" : "Anamnesis";
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
    const t = currentThread();
    const source = t ? t.source : "";
    let who;
    if (m.role === "user") who = source === "note" ? "Note" : "You";
    else who = (m.model || "").split("/").pop() || sourceLabel(source) || "Assistant";
    return who + " · " + fmtDateTime(m.created_at);
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
    if (later) box.insertBefore(el, later);
    else box.appendChild(el);
    scrollToBottom(false);
  }

  function addNote(text, kind) {
    const el = bubble(kind || "note", kind === "error" ? "Error" : "Anamnesis");
    el.querySelector(".msg-body").textContent = text;
    $("#messages").appendChild(el);
    scrollToBottom(true);
  }

  function clearMessages() {
    const box = $("#messages");
    box.querySelectorAll("article").forEach((a) => a.remove());
    $("#empty").hidden = state.threads.length > 0;
    state.shown = new Set();
  }

  // ---------- actions ----------

  async function loadSources() {
    const sources = await api("/api/sources");
    const select = $("#source-filter");
    const keep = state.source;
    select.length = 1;  // keep "All tools"
    let total = 0;
    for (const s of sources) {
      state.sourceLabels[s.name] = s.label;
      total += s.threads;
      const opt = document.createElement("option");
      opt.value = s.name;
      opt.textContent = s.label + " (" + s.threads + ")";
      select.appendChild(opt);
    }
    select.value = sources.some((s) => s.name === keep) ? keep : "";
    state.source = select.value;
    $("#memory-count").textContent = total ? total + " conversations" : "";
  }

  async function loadThreads() {
    const requestedFor = state.threadId;
    const filter = state.source;
    const threads = await api("/api/threads" + (filter ? "?source=" + encodeURIComponent(filter) : ""));
    // Another thread may have been opened (or the filter changed) while in flight.
    if (state.threadId !== requestedFor || state.source !== filter) return;
    state.threads = threads;
    // With a filter active the open thread may just be hidden, not deleted.
    if (!filter && state.threadId && !state.threads.some((t) => t.id === state.threadId)) {
      state.threadId = null;
      clearMessages();
      history.replaceState(null, "", location.pathname);
    }
    renderThreads();
  }

  async function openThread(id) {
    closeSidebarOnMobile();
    let t = state.threads.find((x) => x.id === id);
    if (!t) {
      t = await api("/api/threads/" + id).catch(() => null);
      if (t) state.threads.push(t);  // e.g. a search hit hidden by the source filter
    }
    if (!t) { addNote("That conversation no longer exists.", "error"); return; }
    state.threadId = id;
    if (location.hash !== "#" + id) history.replaceState(null, "", "#" + id);
    clearMessages();
    $("#empty").hidden = true;
    renderThreads();
    const msgs = await api("/api/threads/" + id + "/messages?limit=500");
    if (state.threadId !== id) return;
    msgs.forEach(addMessage);
    if (!msgs.length) addNote("(no messages)");
    scrollToBottom(true);
  }

  async function deleteThread(t) {
    if (!confirm('Delete "' + t.title + '" from memory? This cannot be undone.')) return;
    try {
      await api("/api/threads/" + t.id, { method: "DELETE" });
      await loadThreads();
    } catch (e) { addNote(e.message, "error"); }
  }

  async function renameThread() {
    const t = currentThread();
    if (!t) return;
    const title = prompt("Rename conversation", t.title);
    if (!title || !title.trim() || title === t.title) return;
    try {
      await api("/api/threads/" + t.id, { method: "PATCH", body: JSON.stringify({ title: title.trim() }) });
      await loadThreads();
    } catch (e) { addNote(e.message, "error"); }
  }

  async function runSearch(q) {
    const list = $("#search-list");
    list.textContent = "";
    $("#search-results").hidden = false;
    $("#threads").hidden = true;
    let hits = [];
    const src = state.source ? "&source=" + encodeURIComponent(state.source) : "";
    try { hits = await api("/api/search?q=" + encodeURIComponent(q) + "&k=20" + src); }
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
      const badge = document.createElement("span");
      badge.className = "src-badge";
      badge.textContent = sourceLabel(h.source);
      title.append(badge, (h.thread_title || "Untitled") + " · " + h.role);
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

  // ---------- live updates ----------

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
        await loadSources();
        await loadThreads();
        if (state.threadId) {
          const msgs = await api("/api/threads/" + state.threadId + "/messages?limit=500");
          msgs.forEach(addMessage);
        }
      } catch (_) {}
    };
    ws.onmessage = (ev) => handleEvent(JSON.parse(ev.data));
    ws.onclose = () => {
      setConn(false, "Reconnecting…");
      const delay = Math.min(1000 * 2 ** state.retry++, 15000);
      setTimeout(connect, delay);
    };
  }

  let threadsTimer = null;
  function refreshThreadsSoon() {
    clearTimeout(threadsTimer);
    threadsTimer = setTimeout(() => {
      loadSources().catch(() => {});
      loadThreads().catch(() => {});
    }, 150);
  }

  function handleEvent(e) {
    switch (e.type) {
      case "message":
        addMessage(e.message);
        refreshThreadsSoon();
        break;
      case "threads_changed":
        refreshThreadsSoon();
        break;
      case "error":
        addNote(e.detail, "error");
        break;
    }
  }

  // ---------- wiring ----------

  async function init() {
    $("#thread-title").addEventListener("dblclick", renameThread);
    $("#search-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const q = $("#search").value.trim();
      if (q) runSearch(q); else closeSearch();
    });
    $("#search").addEventListener("search", () => { if (!$("#search").value) closeSearch(); });
    $("#clear-search").addEventListener("click", closeSearch);
    $("#source-filter").addEventListener("change", (e) => {
      state.source = e.target.value;
      const q = $("#search").value.trim();
      loadThreads().catch((err) => addNote(err.message, "error"));
      if (q) runSearch(q);
    });
    $("#toggle-sidebar").addEventListener("click", (e) => {
      e.stopPropagation();
      document.body.classList.toggle("sidebar-open");
    });
    $("#main").addEventListener("click", () => document.body.classList.remove("sidebar-open"));
    document.addEventListener("keydown", (e) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
      if (e.key === "/" && !typing) {
        e.preventDefault();
        document.body.classList.add("sidebar-open");
        $("#search").focus();
      }
    });

    try {
      await loadSources().catch(() => {});
      await loadThreads();
      const wanted = location.hash.slice(1);
      if (wanted) await openThread(wanted);
      else if (state.threads.length) await openThread(state.threads[0].id);
      else $("#empty").hidden = false;
    } catch (e) {
      addNote("Could not reach the Anamnesis server: " + e.message, "error");
    }
    connect();
  }

  init();
})();
