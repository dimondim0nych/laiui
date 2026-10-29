(function () {
  "use strict";

  let chats = [];
  let activeId = null;
  let pendingDeleteId = null;
  let generating = false;
  let currentModel = null;
  let abortController = null;

  const sidebar = document.getElementById("sidebar");
  const chatList = document.getElementById("chatList");
  const messages = document.getElementById("messages");
  const thread = document.getElementById("thread");
  const prompt = document.getElementById("prompt");
  const modelName = document.getElementById("modelName");
  const modelBtn = document.getElementById("modelBtn");
  const modelMenu = document.getElementById("modelMenu");
  const instructionsDialog = document.getElementById("instructionsDialog");
  const instructionsText = document.getElementById("instructionsText");
  const deleteDialog = document.getElementById("deleteDialog");
  const deleteHint = document.getElementById("deleteHint");
  const stopBtn = document.getElementById("stopBtn");

  // ---------- API helpers ----------

  function jsonOptions(method, body) {
    return {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    };
  }

  async function api(path, options) {
    const res = await fetch(path, options);
    let data = {};
    try { data = await res.json(); } catch (e) { /* empty body */ }
    if (!res.ok) { throw new Error(data.error || "HTTP " + res.status); }
    return data;
  }

  // ---------- Message rendering ----------

  function isNearBottom() {
    return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 80;
  }

  function scrollToBottom() {
    messages.scrollTop = messages.scrollHeight;
  }

  function addUserMessage(text) {
    const msg = document.createElement("div");
    msg.className = "msg user";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    msg.appendChild(bubble);
    thread.appendChild(msg);
  }

  function addError(text) {
    const msg = document.createElement("div");
    msg.className = "msg error";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = "Ошибка: " + text;
    msg.appendChild(bubble);
    thread.appendChild(msg);
    scrollToBottom();
  }

  function createAssistantMessage() {
    const msg = document.createElement("div");
    msg.className = "msg assistant";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    msg.appendChild(bubble);
    thread.appendChild(msg);
    return { msg: msg, bubble: bubble, details: null, thinkBody: null };
  }

  function ensureThink(view) {
    if (view.details) { return; }
    const details = document.createElement("details");
    details.className = "think";
    details.open = true;
    const summary = document.createElement("summary");
    summary.textContent = "Рассуждения";
    const body = document.createElement("div");
    body.className = "think-body";
    details.appendChild(summary);
    details.appendChild(body);
    view.msg.insertBefore(details, view.bubble);
    view.details = details;
    view.thinkBody = body;
  }

  function renderStoredMessage(message) {
    if (message.role === "user") {
      addUserMessage(message.content);
      return;
    }
    const view = createAssistantMessage();
    if (message.thinking) {
      ensureThink(view);
      view.thinkBody.textContent = message.thinking;
      view.details.open = false;
    }
    view.bubble.textContent = message.content;
    if (message.stopped) { markStopped(view); }
  }

  function markStopped(view) {
    const note = document.createElement("div");
    note.className = "stopped-note";
    note.textContent = "Генерация остановлена";
    view.msg.appendChild(note);
  }

  // ---------- Chats ----------

  function renderChats() {
    chatList.textContent = "";
    chats.forEach(function (chat) {
      const item = document.createElement("div");
      item.className = "chat-item" + (chat.id === activeId ? " active" : "");

      const title = document.createElement("button");
      title.type = "button";
      title.className = "chat-title";
      title.textContent = chat.title;
      title.title = chat.title;
      title.addEventListener("click", function () { openChat(chat.id); });

      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "chat-edit";
      edit.textContent = "✎";
      edit.setAttribute("aria-label", "Переименовать чат «" + chat.title + "»");
      edit.addEventListener("click", function () { startRename(chat, item, title); });

      const del = document.createElement("button");
      del.type = "button";
      del.className = "chat-del";
      del.textContent = "×";
      del.setAttribute("aria-label", "Удалить чат «" + chat.title + "»");
      del.addEventListener("click", function () {
        pendingDeleteId = chat.id;
        deleteHint.textContent = "Чат «" + chat.title + "» будет удалён без возможности восстановления.";
        deleteDialog.showModal();
      });

      item.appendChild(title);
      item.appendChild(edit);
      item.appendChild(del);
      chatList.appendChild(item);
    });
  }

  function startRename(chat, item, titleBtn) {
    const input = document.createElement("input");
    input.type = "text";
    input.className = "chat-rename";
    input.value = chat.title;
    input.maxLength = 100;
    input.setAttribute("aria-label", "Новое название чата");

    let finished = false;
    async function finish(save) {
      if (finished) { return; }
      finished = true;
      const value = input.value.trim();
      if (save && value && value !== chat.title) {
        try {
          const data = await api("/api/chats/" + chat.id, jsonOptions("PATCH", { title: value }));
          chat.title = data.title;
        } catch (err) {
          addError(err.message);
        }
      }
      renderChats();
    }

    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        finish(true);
      } else if (event.key === "Escape") {
        event.stopPropagation();
        finish(false);
      }
    });
    input.addEventListener("blur", function () { finish(true); });

    item.replaceChild(input, titleBtn);
    input.focus();
    input.select();
  }

  async function loadChats() {
    chats = await api("/api/chats");
    renderChats();
  }

  async function openChat(id) {
    if (generating) { return; }
    try {
      const chat = await api("/api/chats/" + id);
      activeId = id;
      thread.textContent = "";
      chat.messages.forEach(renderStoredMessage);
      renderChats();
      sidebar.classList.remove("open");
      scrollToBottom();
    } catch (err) {
      addError(err.message);
    }
  }

  function startNewChat() {
    if (generating) { return; }
    activeId = null;
    thread.textContent = "";
    renderChats();
    sidebar.classList.remove("open");
    prompt.focus();
  }

  // ---------- Sending and streaming ----------

  function handleEvent(event, view, state) {
    const stick = isNearBottom();
    if (event.type === "thinking") {
      ensureThink(view);
      state.thinking += event.text;
      view.thinkBody.textContent = state.thinking;
    } else if (event.type === "content") {
      if (!state.content && view.details) { view.details.open = false; }
      state.content += event.text;
      view.bubble.textContent = state.content;
    } else if (event.type === "error") {
      addError(event.message);
    }
    if (stick) { scrollToBottom(); }
  }

  async function send() {
    const text = prompt.value.trim();
    if (!text || generating) { return; }
    if (!currentModel) {
      addError("сначала выберите модель.");
      return;
    }

    generating = true;
    abortController = new AbortController();
    stopBtn.hidden = false;
    prompt.value = "";
    autoGrow();

    let view = null;
    try {
      if (!activeId) {
        const chat = await api("/api/chats", jsonOptions("POST", {}));
        activeId = chat.id;
      }
      addUserMessage(text);
      scrollToBottom();

      const options = jsonOptions("POST", { content: text });
      options.signal = abortController.signal;
      const res = await fetch("/api/chats/" + activeId + "/messages", options);
      if (!res.ok) {
        let data = {};
        try { data = await res.json(); } catch (e) { /* empty body */ }
        throw new Error(data.error || "HTTP " + res.status);
      }

      view = createAssistantMessage();
      const state = { thinking: "", content: "" };
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) { break; }
        buffer += decoder.decode(chunk.value, { stream: true });
        let newline;
        while ((newline = buffer.indexOf("\n")) !== -1) {
          const line = buffer.slice(0, newline).trim();
          buffer = buffer.slice(newline + 1);
          if (line) { handleEvent(JSON.parse(line), view, state); }
        }
      }
    } catch (err) {
      if (err.name === "AbortError") {
        markStopped(view || createAssistantMessage());
        scrollToBottom();
      } else {
        addError(err.message);
      }
    } finally {
      generating = false;
      abortController = null;
      stopBtn.hidden = true;
      try { await loadChats(); } catch (e) { /* list refresh is not critical */ }
    }
  }

  // ---------- Models ----------

  function updateModelName() {
    modelName.textContent = currentModel || "Модель не выбрана";
  }

  async function loadModels() {
    const data = await api("/api/models");
    currentModel = data.current;
    updateModelName();
    return data.models;
  }

  async function openModelMenu() {
    try {
      const models = await loadModels();
      modelMenu.textContent = "";
      if (!models.length) {
        const empty = document.createElement("li");
        empty.textContent = "В папке models/ нет .gguf файлов";
        modelMenu.appendChild(empty);
      }
      models.forEach(function (name) {
        const li = document.createElement("li");
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", String(name === currentModel));
        li.textContent = name;
        li.addEventListener("click", function () { selectModel(name); });
        modelMenu.appendChild(li);
      });
      modelMenu.hidden = false;
      modelBtn.setAttribute("aria-expanded", "true");
    } catch (err) {
      addError(err.message);
    }
  }

  function closeModelMenu() {
    modelMenu.hidden = true;
    modelBtn.setAttribute("aria-expanded", "false");
  }

  async function selectModel(name) {
    closeModelMenu();
    modelBtn.disabled = true;
    modelName.textContent = "Загрузка " + name + "...";
    try {
      const data = await api("/api/models/select", jsonOptions("POST", { name: name }));
      currentModel = data.current;
    } catch (err) {
      currentModel = null;
      addError("не удалось загрузить модель. " + err.message);
    } finally {
      modelBtn.disabled = false;
      updateModelName();
    }
  }

  // ---------- Misc UI ----------

  function autoGrow() {
    prompt.style.height = "auto";
    prompt.style.height = Math.min(prompt.scrollHeight, 200) + "px";
  }

  document.getElementById("newChatBtn").addEventListener("click", startNewChat);

  document.getElementById("instructionsBtn").addEventListener("click", async function () {
    try {
      const settings = await api("/api/settings");
      instructionsText.value = settings.system_prompt;
      instructionsDialog.showModal();
      sidebar.classList.remove("open");
    } catch (err) {
      addError(err.message);
    }
  });

  instructionsDialog.addEventListener("close", async function () {
    if (instructionsDialog.returnValue !== "save") { return; }
    try {
      await api("/api/settings", jsonOptions("PUT", { system_prompt: instructionsText.value }));
    } catch (err) {
      addError(err.message);
    }
  });

  deleteDialog.addEventListener("close", async function () {
    const id = pendingDeleteId;
    pendingDeleteId = null;
    if (deleteDialog.returnValue !== "delete" || id === null) { return; }
    try {
      await api("/api/chats/" + id, { method: "DELETE" });
      if (activeId === id) {
        activeId = null;
        thread.textContent = "";
      }
      await loadChats();
    } catch (err) {
      addError(err.message);
    }
  });

  modelBtn.addEventListener("click", function (event) {
    event.stopPropagation();
    if (modelMenu.hidden) { openModelMenu(); } else { closeModelMenu(); }
  });

  document.addEventListener("click", function (event) {
    if (!modelMenu.hidden && !modelMenu.contains(event.target)) { closeModelMenu(); }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      closeModelMenu();
      sidebar.classList.remove("open");
    }
  });

  document.getElementById("menuBtn").addEventListener("click", function () {
    sidebar.classList.toggle("open");
  });

  stopBtn.addEventListener("click", function () {
    if (abortController) { abortController.abort(); }
  });

  prompt.addEventListener("input", autoGrow);

  prompt.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  });

  // ---------- Init ----------

  thread.textContent = "";
  updateModelName();
  loadChats().catch(function (err) { addError(err.message); });
  loadModels().catch(function (err) { addError(err.message); });
})();
