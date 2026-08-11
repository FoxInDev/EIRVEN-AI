(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));
  const state = {
    base: "",
    token: "",
    status: null,
    conversationId: null,
    attachments: [],
    recorder: null,
    stream: null,
    chunks: [],
    audioContext: null,
    analyser: null,
    vadFrame: 0,
    recordStarted: 0,
    speechHeard: false,
    silenceStarted: 0,
    busy: false,
    responseController: null,
    responseCancelled: false,
    responseTimedOut: false,
    responsePending: null,
    speechPlayer: null,
    handsFreeActive: false,
    taskTimer: 0,
    toastTimer: 0,
    currentScreen: "chat"
  };

  const storage = {
    get(key, fallback = "") {
      try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
    },
    set(key, value) {
      try { localStorage.setItem(key, value); } catch {}
    },
    remove(key) {
      try { localStorage.removeItem(key); } catch {}
    }
  };

  function toast(message) {
    const node = $("#toast");
    node.textContent = String(message || "");
    node.classList.add("show");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => node.classList.remove("show"), 2600);
  }

  function haptic(pattern = [0, 28]) {
    try {
      if (window.android && typeof window.android.vibrate === "function") window.android.vibrate(pattern);
      else if (navigator.vibrate) navigator.vibrate(pattern.slice(1));
    } catch {}
  }

  function normalizeBase(raw) {
    let value = String(raw || "").trim().replace(/\s+/g, "");
    if (!value) throw new Error("Введи адрес компьютера из настроек EIRVEN.");
    if (!/^https?:\/\//i.test(value)) value = `http://${value}`;
    let parsed;
    try { parsed = new URL(value); } catch { throw new Error("Адрес выглядит неверно. Скопируй его из «Настройки → Телефон»."); }
    if (!parsed.port) parsed.port = "7860";
    parsed.pathname = "";
    parsed.search = "";
    parsed.hash = "";
    return parsed.origin;
  }

  function normalizeToken(raw) {
    return String(raw || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  }

  async function api(path, options = {}) {
    if (!state.base || !state.token) throw new Error("Телефон ещё не подключён к EIRVEN.");
    const headers = { "X-EIRVEN-Mobile-Token": state.token, ...(options.headers || {}) };
    if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    let response;
    try {
      response = await fetch(`${state.base}${path}`, { ...options, headers });
    } catch (error) {
      setConnectionOnline(false);
      const timedOut = error && error.name === "AbortError";
      const prefix = timedOut ? "Компьютер не ответил за 8 секунд." : "Не вижу компьютер по указанному адресу.";
      throw new Error(`${prefix} На ПК открой «Настройки → Телефон»: статус сети должен быть зелёным. Если адресов несколько, выбери Wi‑Fi, а не VPN/виртуальную сеть.`);
    }
    if (!response.ok) {
      let message = "";
      try {
        const data = await response.json();
        message = data.detail || data.message || "";
      } catch { message = await response.text(); }
      if (response.status === 401) message = "Код подключения неверный или был изменён на компьютере.";
      throw new Error(message || `Ошибка HTTP ${response.status}`);
    }
    setConnectionOnline(true);
    const type = response.headers.get("content-type") || "";
    return type.includes("application/json") ? response.json() : response;
  }

  function setConnectionOnline(online) {
    const chip = $("#connection-chip");
    chip.classList.toggle("offline", !online);
    chip.querySelector("span").textContent = online ? "Компьютер рядом" : "Нет связи";
  }

  function conversationKey() {
    return `eirven-mobile-conversation:${state.base}`;
  }

  async function connect({ silent = false } = {}) {
    const button = $("#connect-button");
    const status = $("#pairing-status");
    status.classList.remove("error");
    try {
      state.base = normalizeBase($("#server-input").value);
      state.token = normalizeToken($("#token-input").value);
      if (state.token.length < 16) throw new Error("Введи полный код подключения из настроек EIRVEN.");
      button.disabled = true;
      button.textContent = "Проверяю…";
      const controller = window.AbortController ? new AbortController() : null;
      const timeout = controller ? setTimeout(() => controller.abort(), 8000) : 0;
      let info;
      try { info = await api("/api/mobile/status", controller ? { signal: controller.signal } : {}); }
      finally { if (timeout) clearTimeout(timeout); }
      state.status = info;
      storage.set("eirven-mobile-server", state.base);
      storage.set("eirven-mobile-token", state.token);
      storage.set("eirven-mobile-speak", $("#speak-answers").checked ? "1" : "0");
      applyStatus(info);
      $("#pairing-screen").hidden = true;
      $("#app-shell").hidden = false;
      showScreen("chat");
      await ensureConversation();
      await loadHistory();
      await loadTasks();
      requestMicrophonePermission();
      if (!silent) toast("Телефон подключён к EIRVEN");
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("error");
      $("#pairing-screen").hidden = false;
      $("#app-shell").hidden = true;
    } finally {
      button.disabled = false;
      button.textContent = "Подключиться";
    }
  }

  function applyStatus(info) {
    const name = String(info.assistant_name || "Эрви").trim() || "Эрви";
    $("#assistant-title").textContent = name;
    $("#message-input").placeholder = `Напиши ${name}…`;
    $("#connected-address").textContent = state.base;
    const modelList = $("#model-list");
    modelList.innerHTML = "<p>Приложение использует те же модели, что уже настроены на компьютере.</p>";
  }

  async function ensureConversation(force = false) {
    if (!force) state.conversationId = storage.get(conversationKey(), "") || null;
    if (state.conversationId && !force) return state.conversationId;
    const conversation = await api("/api/conversations", {
      method: "POST",
      body: JSON.stringify({ title: "Телефон", mode: "Друг" })
    });
    state.conversationId = conversation.id;
    storage.set(conversationKey(), state.conversationId);
    return state.conversationId;
  }

  function messageNode(role, text, extra = "") {
    const node = document.createElement("div");
    node.className = `message ${role} ${extra}`.trim();
    node.textContent = String(text || "");
    $("#messages").append(node);
    $("#messages").scrollTop = $("#messages").scrollHeight;
    return node;
  }

  async function loadHistory() {
    if (!state.conversationId) return;
    try {
      const conversation = await api(`/api/conversations/${encodeURIComponent(state.conversationId)}`);
      $("#messages").innerHTML = "";
      (conversation.messages || []).slice(-60).forEach((item) => messageNode(item.role === "user" ? "user" : "assistant", item.content || ""));
    } catch {
      storage.remove(conversationKey());
      state.conversationId = null;
      await ensureConversation(true);
    }
    if (!$("#messages").children.length) {
      messageNode("assistant", `Я на связи через твой компьютер. Можешь написать или нажать сферу и сказать команду. Все привычные команды работают здесь так же.`);
    }
  }

  function setOrb(mode, label) {
    const orb = $("#voice-orb");
    orb.classList.remove("listening", "thinking", "speaking");
    if (mode) orb.classList.add(mode);
    if (label) $("#voice-state").textContent = label;
  }

  function consumeStreamPacket(packet, node, current) {
    let full = current;
    for (const line of packet.split("\n")) {
      if (!line.startsWith("data:")) continue;
      let event;
      try { event = JSON.parse(line.slice(5).trim()); } catch { continue; }
      if (event.type === "token") {
        full = event.full || `${full}${event.content || ""}`;
        node.textContent = full;
        node.classList.remove("pending");
      } else if (event.type === "error") {
        throw new Error(event.message || "Не удалось получить ответ");
      } else if (event.type === "done") {
        full = event.answer || full || "Готово.";
        node.textContent = full;
        node.classList.remove("pending");
      }
    }
    return full;
  }

  async function streamChat(payload, node, controller) {
    let response;
    let inactivityTimer = 0;
    const armTimeout = () => {
      clearTimeout(inactivityTimer);
      inactivityTimer = setTimeout(() => {
        state.responseTimedOut = true;
        controller.abort();
      }, 60000);
    };
    armTimeout();
    try {
      try {
        response = await fetch(`${state.base}/api/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-EIRVEN-Mobile-Token": state.token },
        body: JSON.stringify(payload),
        signal: controller.signal
        });
      } catch {
        if (state.responseCancelled || state.responseTimedOut) throw new DOMException("Ответ остановлен", "AbortError");
        setConnectionOnline(false);
        throw new Error("Связь с компьютером прервалась.");
      }
    if (!response.ok) {
      let text = await response.text();
      try { text = JSON.parse(text).detail || text; } catch {}
      throw new Error(text || `HTTP ${response.status}`);
    }
    setConnectionOnline(true);
    if (!response.body) {
      const packet = await response.text();
      let full = "";
      full = consumeStreamPacket(packet, node, full);
      node.textContent = full || "Готово.";
      node.classList.remove("pending");
      clearTimeout(inactivityTimer);
      return node.textContent;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let full = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      armTimeout();
      buffer += decoder.decode(value, { stream: true });
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const packet = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        full = consumeStreamPacket(packet, node, full);
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) full = consumeStreamPacket(buffer, node, full);
      node.classList.remove("pending");
      return full || node.textContent;
    } finally {
      clearTimeout(inactivityTimer);
    }
  }

  function setBusy(busy) {
    state.busy = busy;
    const button = $("#send-button");
    button.textContent = busy ? "■" : "➤";
    button.classList.toggle("stopping", busy);
    button.setAttribute("aria-label", busy ? "Остановить ответ" : "Отправить");
    $("#voice-orb").setAttribute("aria-label", busy ? "Остановить ответ" : "Начать голосовую команду");
  }

  async function cancelResponse() {
    if (!state.busy) return;
    state.responseCancelled = true;
    state.responseController?.abort();
    const player = state.speechPlayer;
    if (player) {
      try { player.pause(); player.removeAttribute("src"); } catch {}
    }
    if (state.conversationId) {
      try { await api(`/api/chat/${encodeURIComponent(state.conversationId)}/stop`, { method: "POST" }); } catch {}
    }
  }

  async function sendText(text, { fromVoice = false } = {}) {
    const message = String(text || "").trim();
    if (!message) return;
    if (state.busy) {
      toast("Сначала останови текущий ответ или дождись его окончания.");
      return;
    }
    const controller = new AbortController();
    state.responseController = controller;
    state.responseCancelled = false;
    state.responseTimedOut = false;
    setBusy(true);
    let pending = null;
    let sentAttachments = [];
    let slowTimer = 0;
    try {
      await ensureConversation();
      messageNode("user", message);
      pending = messageNode("assistant", "Думаю…", "pending");
      state.responsePending = pending;
      slowTimer = setTimeout(() => {
        if (state.busy && pending.classList.contains("pending")) {
          pending.textContent = "Это занимает дольше обычного. Я всё ещё работаю — ответ можно остановить кнопкой ■.";
          setOrb("thinking", "Всё ещё работаю… можно остановить");
        }
      }, 8000);
      setOrb("thinking", "Думаю и выполняю команду…");
      sentAttachments = state.attachments.slice();
      const attachmentIds = sentAttachments.map((item) => item.id);
      state.attachments = [];
      renderAttachments();
      const answer = await streamChat({
        message,
        conversation_id: state.conversationId,
        mode: "Друг",
        model: "auto",
        attachment_ids: attachmentIds,
        auto_execute: true,
        voice_mode: fromVoice
      }, pending, controller);
      if ($("#speak-answers").checked && answer) await speak(answer);
      else finishVoiceCycle();
    } catch (error) {
      if (sentAttachments.length) {
        state.attachments = [...sentAttachments, ...state.attachments];
        renderAttachments();
      }
      if (!pending) pending = messageNode("assistant", "", "pending");
      pending.classList.remove("pending");
      if (state.responseCancelled) pending.textContent = "Ответ остановлен.";
      else if (state.responseTimedOut || error?.name === "AbortError") pending.textContent = "Компьютер слишком долго не отвечал. Проверь модель или повтори команду.";
      else pending.textContent = `Не получилось: ${error.message}`;
      finishVoiceCycle();
    } finally {
      clearTimeout(slowTimer);
      if (state.responseController === controller) state.responseController = null;
      state.responsePending = null;
      state.speechPlayer = null;
      setBusy(false);
      loadTasks();
    }
  }

  function speechParts(text) {
    const value = String(text || "").slice(0, 6000).trim();
    if (!value) return [];
    const parts = [];
    let rest = value;
    while (rest && parts.length < 7) {
      if (rest.length <= 900) { parts.push(rest); break; }
      const windowText = rest.slice(0, 900);
      const matches = Array.from(windowText.matchAll(/[.!?;:]\s+/g));
      let cut = matches.length ? (matches[matches.length - 1].index + matches[matches.length - 1][0].length) : windowText.lastIndexOf(" ");
      if (cut < 300) cut = 900;
      parts.push(rest.slice(0, cut).trim());
      rest = rest.slice(cut).trim();
    }
    return parts;
  }

  async function speak(text) {
    setOrb("speaking", "Отвечаю…");
    try {
      const player = $("#voice-player");
      state.speechPlayer = player;
      for (const part of speechParts(text)) {
        if (state.responseCancelled) break;
        const ttsController = new AbortController();
        const ttsTimeout = setTimeout(() => ttsController.abort(), 45000);
        let response;
        try {
          response = await fetch(`${state.base}/api/voice/speak`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-EIRVEN-Mobile-Token": state.token },
            body: JSON.stringify({ text: part, emotion: "auto" }),
            signal: ttsController.signal
          });
        } finally { clearTimeout(ttsTimeout); }
        if (!response.ok) throw new Error("Голос сейчас недоступен");
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        player.src = url;
        await new Promise((resolve) => {
          const watchdog = setTimeout(done, 90000);
          function done() {
            clearTimeout(watchdog);
            player.onended = null;
            player.onerror = null;
            URL.revokeObjectURL(url);
            resolve();
          }
          player.onended = done;
          player.onerror = done;
          player.play().catch(done);
        });
      }
    } catch (error) {
      if (!state.responseCancelled) toast(error.name === "AbortError" ? "Озвучка не ответила вовремя" : (error.message || "Не удалось воспроизвести голос"));
    }
    finishVoiceCycle();
  }

  function finishVoiceCycle() {
    if (state.handsFreeActive && $("#handsfree-mode").checked && state.currentScreen === "chat") {
      setOrb("", "Снова слушаю через секунду…");
      setTimeout(() => {
        if (state.handsFreeActive && !state.busy && !state.recorder) startRecording();
      }, 850);
    } else {
      setOrb("", "Нажми сферу и говори");
    }
  }

  function requestMicrophonePermission() {
    try {
      if (window.android && typeof window.android.requestPermission === "function") window.android.requestPermission("RECORD_AUDIO");
    } catch {}
  }

  function supportedMime() {
    if (!window.MediaRecorder) return "";
    const choices = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
    return choices.find((type) => MediaRecorder.isTypeSupported(type)) || "";
  }

  async function startRecording() {
    if (state.busy || state.recorder) return;
    requestMicrophonePermission();
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
      $("#audio-fallback").click();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      const mimeType = supportedMime();
      const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
      state.stream = stream;
      state.recorder = recorder;
      state.chunks = [];
      state.recordStarted = Date.now();
      state.speechHeard = false;
      state.silenceStarted = 0;
      recorder.ondataavailable = (event) => { if (event.data && event.data.size) state.chunks.push(event.data); };
      recorder.onerror = () => stopRecording(false);
      recorder.onstop = handleRecordingStopped;
      recorder.start(250);
      monitorSilence(stream);
      setOrb("listening", "Слушаю… говори команду");
      $("#voice-orb").setAttribute("aria-label", "Закончить голосовую команду");
      if ($("#handsfree-mode").checked) {
        state.handsFreeActive = true;
        $("#stop-handsfree").hidden = false;
      }
      haptic([0, 35]);
    } catch {
      setOrb("", "Нет доступа к микрофону");
      toast("Разреши приложению доступ к микрофону. Если кнопка не сработает, выбери запись через системное окно.");
      $("#audio-fallback").click();
    }
  }

  function monitorSilence(stream) {
    try {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (!AudioContext) return;
      state.audioContext = new AudioContext();
      const source = state.audioContext.createMediaStreamSource(stream);
      state.analyser = state.audioContext.createAnalyser();
      state.analyser.fftSize = 1024;
      source.connect(state.analyser);
      const data = new Uint8Array(state.analyser.fftSize);
      const loop = () => {
        if (!state.recorder || state.recorder.state !== "recording") return;
        state.analyser.getByteTimeDomainData(data);
        let sum = 0;
        for (let index = 0; index < data.length; index += 1) {
          const sample = (data[index] - 128) / 128;
          sum += sample * sample;
        }
        const level = Math.sqrt(sum / data.length);
        const elapsed = Date.now() - state.recordStarted;
        if (level > 0.035) {
          state.speechHeard = true;
          state.silenceStarted = 0;
        } else if (state.speechHeard) {
          if (!state.silenceStarted) state.silenceStarted = Date.now();
          if (Date.now() - state.silenceStarted > 1350 && elapsed > 900) {
            stopRecording(true);
            return;
          }
        }
        if (elapsed > 45000) {
          stopRecording(true);
          return;
        }
        state.vadFrame = requestAnimationFrame(loop);
      };
      loop();
    } catch {}
  }

  function stopRecording(process = true) {
    if (!state.recorder) return;
    state.recorder.datasetProcess = process ? "1" : "0";
    if (state.recorder.state !== "inactive") state.recorder.stop();
  }

  async function handleRecordingStopped() {
    const recorder = state.recorder;
    const shouldProcess = recorder && recorder.datasetProcess !== "0";
    const type = recorder?.mimeType || "audio/webm";
    state.recorder = null;
    cancelAnimationFrame(state.vadFrame);
    if (state.audioContext) state.audioContext.close().catch(() => {});
    state.audioContext = null;
    if (state.stream) state.stream.getTracks().forEach((track) => track.stop());
    state.stream = null;
    $("#voice-orb").setAttribute("aria-label", "Начать голосовую команду");
    haptic([0, 18]);
    if (!shouldProcess || !state.chunks.length) {
      finishVoiceCycle();
      return;
    }
    const extension = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
    await transcribeAndSend(new Blob(state.chunks, { type }), `voice.${extension}`);
  }

  async function transcribeAndSend(blob, filename) {
    setOrb("thinking", "Распознаю речь…");
    try {
      const form = new FormData();
      form.append("audio", blob, filename);
      const result = await api("/api/voice/transcribe", { method: "POST", body: form });
      const text = String(result.text || "").trim();
      if (!text) throw new Error("Речь не распознана. Попробуй ещё раз чуть ближе к микрофону.");
      await sendText(text, { fromVoice: true });
    } catch (error) {
      toast(error.message);
      finishVoiceCycle();
    }
  }

  function renderAttachments() {
    const strip = $("#attachment-strip");
    strip.innerHTML = "";
    strip.hidden = state.attachments.length === 0;
    state.attachments.forEach((item, index) => {
      const chip = document.createElement("div");
      chip.className = "attachment-chip";
      const name = document.createElement("span");
      name.textContent = item.name;
      const remove = document.createElement("button");
      remove.textContent = "×";
      remove.onclick = () => { state.attachments.splice(index, 1); renderAttachments(); };
      chip.append(name, remove);
      strip.append(chip);
    });
  }

  function uploadRow(file) {
    const row = document.createElement("div");
    row.className = "upload-item";
    const head = document.createElement("div");
    const name = document.createElement("span");
    const status = document.createElement("small");
    name.textContent = file.name;
    status.textContent = "0%";
    head.append(name, status);
    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("i");
    bar.append(fill);
    row.append(head, bar);
    $("#upload-list").prepend(row);
    return { row, status, fill };
  }

  function uploadWithProgress(path, field, file, onDone) {
    return new Promise((resolve, reject) => {
      const ui = uploadRow(file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${state.base}${path}`);
      xhr.setRequestHeader("X-EIRVEN-Mobile-Token", state.token);
      xhr.upload.onprogress = (event) => {
        if (!event.lengthComputable) return;
        const percent = Math.round((event.loaded / event.total) * 100);
        ui.fill.style.width = `${percent}%`;
        ui.status.textContent = `${percent}%`;
      };
      xhr.onerror = () => {
        ui.row.classList.add("error");
        ui.status.textContent = "нет связи";
        reject(new Error("Не удалось передать файл на компьютер"));
      };
      xhr.onload = () => {
        let result = {};
        try { result = JSON.parse(xhr.responseText || "{}"); } catch {}
        if (xhr.status < 200 || xhr.status >= 300) {
          ui.row.classList.add("error");
          ui.status.textContent = "ошибка";
          reject(new Error(result.detail || `Ошибка загрузки ${xhr.status}`));
          return;
        }
        ui.fill.style.width = "100%";
        ui.status.textContent = "готово";
        if (onDone) onDone(result);
        resolve(result);
      };
      const form = new FormData();
      form.append(field, file, file.name);
      if (field === "file" && state.conversationId) form.append("conversation_id", state.conversationId);
      xhr.send(form);
    });
  }

  async function uploadDocuments(files) {
    if (!files.length) return;
    await ensureConversation();
    showScreen("files");
    for (const file of files) {
      try {
        await uploadWithProgress("/api/uploads", "file", file, (result) => {
          state.attachments.push({ id: result.id, name: result.name || file.name });
          renderAttachments();
        });
      } catch (error) { toast(`${file.name}: ${error.message}`); }
    }
    toast("Документы готовы. Добавь команду в чате.");
  }

  async function uploadVideos(files) {
    if (!files.length) return;
    showScreen("files");
    let completed = 0;
    for (const file of files) {
      try {
        await uploadWithProgress("/api/mobile/video", "file", file);
        completed += 1;
      } catch (error) { toast(`${file.name}: ${error.message}`); }
    }
    if (completed) {
      toast(`${completed} видео передано. Эрви присвоит номера автоматически.`);
      showScreen("chat");
      messageNode("assistant", `Получила ${completed} видео. После завершения проверки они станут 1, 2, 3… с сохранением настоящего формата. Теперь скажи, что нужно смонтировать.`);
    }
  }

  async function loadTasks() {
    if (!state.base || !state.token || $("#app-shell").hidden) return;
    try {
      const tasks = await api("/api/tasks?limit=40");
      const box = $("#task-list");
      box.innerHTML = "";
      const visible = (tasks || []).filter((task) => !["cancelled"].includes(String(task.status || "").toLowerCase()));
      if (!visible.length) {
        box.innerHTML = '<div class="empty-card">Сейчас фоновых задач нет.</div>';
        return;
      }
      visible.forEach((task) => {
        const card = document.createElement("article");
        card.className = "task-card";
        const header = document.createElement("header");
        const title = document.createElement("h3");
        const status = document.createElement("span");
        title.textContent = task.title || task.kind || "Задача";
        status.textContent = task.status || "";
        header.append(title, status);
        const caption = document.createElement("p");
        caption.textContent = task.current_step || task.error || "";
        const progress = document.createElement("div");
        progress.className = "progress";
        const fill = document.createElement("i");
        fill.style.width = `${Math.max(0, Math.min(100, Math.round((Number(task.progress) || 0) * 100)))}%`;
        progress.append(fill);
        card.append(header, caption, progress);
        if (["queued", "running", "pending"].includes(String(task.status || "").toLowerCase())) {
          const footer = document.createElement("footer");
          const cancel = document.createElement("button");
          cancel.textContent = "Остановить";
          cancel.onclick = async () => {
            try { await api(`/api/tasks/${encodeURIComponent(task.id)}/cancel`, { method: "POST" }); await loadTasks(); }
            catch (error) { toast(error.message); }
          };
          footer.append(cancel);
          card.append(footer);
        }
        box.append(card);
      });
    } catch (error) {
      const box = $("#task-list");
      box.innerHTML = `<div class="empty-card">Не удалось загрузить задачи: ${String(error.message || "нет связи")}. Открой раздел ещё раз, чтобы повторить.</div>`;
    }
  }

  function showScreen(name) {
    state.currentScreen = name;
    $$(".screen").forEach((screen) => screen.classList.toggle("active", screen.dataset.screen === name));
    $$('[data-nav]').forEach((button) => button.classList.toggle("active", button.dataset.nav === name));
    if (name === "tasks") loadTasks();
  }

  function showPairing(erase = false) {
    stopHandsFree();
    if (erase) {
      storage.remove("eirven-mobile-server");
      storage.remove("eirven-mobile-token");
      $("#server-input").value = "";
      $("#token-input").value = "";
    }
    $("#app-shell").hidden = true;
    $("#pairing-screen").hidden = false;
    $("#pairing-status").textContent = erase ? "Адрес и код удалены с телефона." : "Введи новые данные и подключись снова.";
    $("#pairing-status").classList.remove("error");
  }

  function stopHandsFree() {
    state.handsFreeActive = false;
    $("#stop-handsfree").hidden = true;
    if (state.recorder) stopRecording(false);
    setOrb("", "Нажми сферу и говори");
  }

  $("#connect-button").addEventListener("click", () => connect());
  $("#token-input").addEventListener("keydown", (event) => { if (event.key === "Enter") connect(); });
  $("#connection-chip").addEventListener("click", () => showScreen("settings"));
  $$('[data-nav]').forEach((button) => button.addEventListener("click", () => showScreen(button.dataset.nav)));
  $("#composer").addEventListener("submit", (event) => {
    event.preventDefault();
    if (state.busy) {
      cancelResponse();
      return;
    }
    const input = $("#message-input");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    input.style.height = "auto";
    sendText(text);
  });
  $("#message-input").addEventListener("input", (event) => {
    event.target.style.height = "auto";
    event.target.style.height = `${Math.min(100, event.target.scrollHeight)}px`;
  });
  $("#voice-orb").addEventListener("click", () => {
    if (state.busy) cancelResponse();
    else if (state.recorder) stopRecording(true);
    else startRecording();
  });
  $("#stop-handsfree").addEventListener("click", stopHandsFree);
  $("#attach-button").addEventListener("click", () => $("#document-input").click());
  $("#upload-document-button").addEventListener("click", () => $("#document-input").click());
  $("#upload-video-button").addEventListener("click", () => $("#video-input").click());
  $("#document-input").addEventListener("change", (event) => { uploadDocuments(Array.from(event.target.files || [])); event.target.value = ""; });
  $("#video-input").addEventListener("change", (event) => { uploadVideos(Array.from(event.target.files || [])); event.target.value = ""; });
  $("#audio-fallback").addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file) transcribeAndSend(file, file.name || "voice.m4a");
  });
  $("#speak-answers").addEventListener("change", (event) => storage.set("eirven-mobile-speak", event.target.checked ? "1" : "0"));
  $("#handsfree-mode").addEventListener("change", (event) => {
    storage.set("eirven-mobile-handsfree", event.target.checked ? "1" : "0");
    if (!event.target.checked) stopHandsFree();
  });
  $("#change-connection").addEventListener("click", () => showPairing(false));
  $("#forget-connection").addEventListener("click", () => showPairing(true));

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopHandsFree();
  });
  window.addEventListener("online", () => setConnectionOnline(true));
  window.addEventListener("offline", () => setConnectionOnline(false));
  state.taskTimer = setInterval(() => {
    if (state.currentScreen === "tasks") loadTasks();
  }, 1800);

  function init() {
    const savedServer = storage.get("eirven-mobile-server", "");
    const savedToken = storage.get("eirven-mobile-token", "");
    $("#server-input").value = savedServer;
    $("#token-input").value = savedToken;
    $("#speak-answers").checked = storage.get("eirven-mobile-speak", "1") !== "0";
    $("#handsfree-mode").checked = storage.get("eirven-mobile-handsfree", "0") === "1";
    if (savedServer && savedToken) connect({ silent: true });
  }

  init();
})();
