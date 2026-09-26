"use strict";

const $ = (id) => document.getElementById(id);
const events = [];
let apiKey = "";
let sessionId = "";
let selectedEvent = null;
let busy = false;

function status(message) {
  $("status").textContent = message;
}

function showEvent(event) {
  selectedEvent = event;
  $("http-status").textContent = event.status;
  $("request-path").textContent = `${event.method} ${event.path}`;
  $("request-json").textContent = event.requestText;
  $("response-raw").textContent = event.responseText;
  const parsed = event.parsed;
  if (parsed && parsed.choices) {
    $("parsed-result").textContent = JSON.stringify({
      answer: parsed.choices[0]?.message?.content ?? null,
      model: parsed.model ?? null,
      session_id: parsed.session_id ?? null,
      usage: parsed.usage ?? null,
      timings: parsed.timings ?? null,
    }, null, 2);
  } else {
    $("parsed-result").textContent = parsed ? JSON.stringify(parsed, null, 2) : "No JSON response yet.";
  }
  $("model-json").textContent = parsed?.inspection?.backend_request
    ? JSON.stringify(parsed.inspection.backend_request, null, 2)
    : "Available after a chat response.";
  renderEvents();
}

function renderEvents() {
  const list = $("requests");
  list.replaceChildren();
  for (const event of events.slice(0, 12)) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${event.method} ${event.path} · ${event.status}`;
    button.className = event === selectedEvent ? "active" : "";
    button.addEventListener("click", () => showEvent(event));
    list.append(button);
  }
}

async function api(method, path, body) {
  const requestText = body === undefined ? "(no request body)" : JSON.stringify(body);
  const event = { method, path, requestText, responseText: "Waiting for response…", parsed: null, status: "Pending" };
  events.unshift(event);
  if (events.length > 12) events.length = 12;
  showEvent(event);
  const started = performance.now();
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: {
        Authorization: `Bearer ${apiKey}`,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      ...(body === undefined ? {} : { body: requestText }),
      cache: "no-store",
    });
    event.responseText = await response.text();
  } catch (error) {
    event.status = "Network error";
    event.responseText = String(error);
    showEvent(event);
    throw error;
  }
  try {
    event.parsed = JSON.parse(event.responseText);
  } catch (_) {
    event.parsed = null;
  }
  event.status = `${response.status} · ${((performance.now() - started) / 1000).toFixed(1)}s`;
  showEvent(event);
  if (!response.ok) {
    throw new Error(event.parsed?.error || `HTTP ${response.status}`);
  }
  return event.parsed;
}

function addMessage(role, content) {
  $("messages").querySelector(".empty")?.remove();
  const bubble = document.createElement("div");
  bubble.className = `message ${role}`;
  const label = document.createElement("span");
  label.className = "role";
  label.textContent = role === "user" ? "You" : "Pi assistant";
  const text = document.createElement("span");
  text.textContent = content;
  bubble.append(label, text);
  $("messages").append(bubble);
  $("messages").scrollTop = $("messages").scrollHeight;
  return bubble;
}

function clearMessages() {
  $("messages").replaceChildren();
  const empty = document.createElement("p");
  empty.className = "empty";
  empty.textContent = "Send a message. Replies can take a minute on this Pi.";
  $("messages").append(empty);
}

function addSessionOption(id, title) {
  if ([...$("sessions").options].some((option) => option.value === id)) return;
  const option = document.createElement("option");
  option.value = id;
  option.textContent = title || id.slice(0, 8);
  $("sessions").append(option);
}

async function connect() {
  if (busy) return;
  apiKey = $("key").value.trim();
  if (!apiKey) { status("Paste your API key first."); return; }
  status("Checking API…");
  try {
    const models = await api("GET", "/v1/models");
    const data = await api("GET", "/api/sessions?limit=30");
    $("sessions").replaceChildren(new Option("New chat", ""));
    for (const session of data.sessions || []) addSessionOption(session.id, session.title);
    $("sessions").disabled = false;
    $("send").disabled = false;
    $("connection").textContent = `Connected · ${models.data?.[0]?.id || "Pi"}`;
    $("connection").classList.add("online");
    status("Ready");
  } catch (error) {
    $("connection").textContent = "Connection failed";
    $("connection").classList.remove("online");
    status(error.message);
  }
}

async function loadSession(id) {
  sessionId = id;
  clearMessages();
  if (!id) { status("New chat ready"); return; }
  status("Loading saved conversation…");
  try {
    const data = await api("GET", `/api/sessions/${id}/messages?limit=100`);
    for (const message of data.messages || []) addMessage(message.role, message.content);
    status(`Loaded ${data.messages?.length || 0} recent messages`);
  } catch (error) {
    status(error.message);
  }
}

async function sendMessage(event) {
  event.preventDefault();
  if (busy || !apiKey) return;
  const prompt = $("prompt").value.trim();
  if (!prompt) return;
  busy = true;
  $("send").disabled = true;
  $("new-chat").disabled = true;
  $("sessions").disabled = true;
  const bubble = addMessage("user", prompt);
  const started = performance.now();
  const ticker = setInterval(() => status(`Pi is thinking… ${Math.floor((performance.now() - started) / 1000)}s`), 1000);
  status("Pi is thinking… 0s");
  try {
    if (!sessionId) {
      const created = await api("POST", "/api/sessions", { title: prompt.slice(0, 50) });
      sessionId = created.session_id;
      addSessionOption(sessionId, prompt.slice(0, 50));
      $("sessions").value = sessionId;
    }
    const result = await api("POST", "/v1/chat/completions", {
      model: "pi-local-assistant",
      session_id: sessionId,
      inspect: true,
      messages: [{ role: "user", content: prompt }],
    });
    addMessage("assistant", result.choices[0].message.content);
    $("prompt").value = "";
    status(`Answered in ${((performance.now() - started) / 1000).toFixed(1)}s`);
  } catch (error) {
    bubble.remove();
    status(`Request failed: ${error.message}`);
  } finally {
    clearInterval(ticker);
    busy = false;
    $("send").disabled = false;
    $("new-chat").disabled = false;
    $("sessions").disabled = false;
  }
}

$("connect").addEventListener("click", connect);
$("key").addEventListener("keydown", (event) => { if (event.key === "Enter") connect(); });
$("new-chat").addEventListener("click", () => { if (!busy) { sessionId = ""; $("sessions").value = ""; clearMessages(); status("New chat ready"); } });
$("sessions").addEventListener("change", (event) => { if (!busy) loadSession(event.target.value); });
$("chat-form").addEventListener("submit", sendMessage);
$("prompt").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("chat-form").requestSubmit();
  }
});
