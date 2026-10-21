(() => {
  "use strict";

  const API = Object.freeze({
    metrics: "/api/v1/metrics",
    calls: "/api/v1/calls",
    simulate: "/api/v1/simulate",
  });

  const POLL_INTERVAL_MS = 20_000;
  const REQUEST_TIMEOUT_MS = 12_000;
  const MAX_CALL_ROWS = 8;
  const MAX_HANDOFF_ROWS = 4;
  const dashboardApiKey = new URLSearchParams(window.location.search).get("key")?.trim() || "local-demo-key";

  const elements = {
    totalCalls: document.getElementById("totalCalls"),
    activeCalls: document.getElementById("activeCalls"),
    containmentRate: document.getElementById("containmentRate"),
    containmentBar: document.getElementById("containmentBar"),
    averageTurns: document.getElementById("averageTurns"),
    handoffCount: document.getElementById("handoffCount"),
    appointmentCount: document.getElementById("appointmentCount"),
    handoffTrend: document.getElementById("handoffTrend"),
    callsTableBody: document.getElementById("callsTableBody"),
    callsEmpty: document.getElementById("callsEmpty"),
    handoffList: document.getElementById("handoffList"),
    handoffBadge: document.getElementById("handoffBadge"),
    connectionStatus: document.getElementById("connectionStatus"),
    refreshButton: document.getElementById("refreshButton"),
    currentDate: document.getElementById("currentDate"),
    lastSynced: document.getElementById("lastSynced"),
    form: document.getElementById("simulatorForm"),
    sessionId: document.getElementById("sessionId"),
    caller: document.getElementById("caller"),
    messageInput: document.getElementById("messageInput"),
    sendButton: document.getElementById("sendButton"),
    transcript: document.getElementById("transcript"),
    turnMetadata: document.getElementById("turnMetadata"),
    resetConversation: document.getElementById("resetConversation"),
    toast: document.getElementById("toast"),
  };

  const state = {
    calls: [],
    isRefreshing: false,
    isSending: false,
    toastTimer: null,
  };

  const numberFormatter = new Intl.NumberFormat("en-PK", { maximumFractionDigits: 1 });
  const dateFormatter = new Intl.DateTimeFormat("en-PK", {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
  });
  const timeFormatter = new Intl.DateTimeFormat("en-PK", {
    hour: "2-digit",
    minute: "2-digit",
  });

  function asText(value, fallback = "—") {
    if (value === null || value === undefined || value === "") return fallback;
    return String(value);
  }

  function asFiniteNumber(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function formatNumber(value) {
    return numberFormatter.format(asFiniteNumber(value));
  }

  function normalizePercentage(value) {
    let percentage = asFiniteNumber(value);
    if (percentage > 0 && percentage <= 1) percentage *= 100;
    return Math.min(100, Math.max(0, percentage));
  }

  function formatPercentage(value) {
    const percentage = normalizePercentage(value);
    const digits = Number.isInteger(percentage) ? 0 : 1;
    return `${percentage.toFixed(digits)}%`;
  }

  function parseDate(value) {
    if (!value) return null;
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  function formatRelativeDate(value) {
    const parsed = parseDate(value);
    if (!parsed) return "—";
    const difference = Date.now() - parsed.getTime();
    if (difference >= 0 && difference < 60_000) return "Just now";
    if (difference >= 0 && difference < 3_600_000) {
      return `${Math.max(1, Math.floor(difference / 60_000))}m ago`;
    }
    if (difference >= 0 && difference < 86_400_000) {
      return `${Math.floor(difference / 3_600_000)}h ago`;
    }
    return `${parsed.toLocaleDateString("en-PK", { day: "numeric", month: "short" })} · ${timeFormatter.format(parsed)}`;
  }

  function readableLabel(value, fallback = "General enquiry") {
    const text = asText(value, fallback).replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
    return text || fallback;
  }

  function maskCaller(value) {
    const caller = asText(value, "Anonymous caller").trim();
    if (!caller || caller === "Anonymous caller") return "Anonymous caller";
    const digits = caller.replace(/\D/g, "");
    if (digits.length < 7) return caller;
    const prefix = caller.startsWith("+") ? "+" : "";
    return `${prefix}${digits.slice(0, Math.min(3, digits.length - 4))} ••• ${digits.slice(-4)}`;
  }

  function setText(element, value) {
    if (element) element.textContent = asText(value);
  }

  function clearElement(element) {
    while (element.firstChild) element.removeChild(element.firstChild);
  }

  function makeElement(tagName, className, textValue) {
    const element = document.createElement(tagName);
    if (className) element.className = className;
    if (textValue !== undefined) element.textContent = asText(textValue, "");
    return element;
  }

  async function fetchJson(url, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(url, {
        ...options,
        headers: {
          Accept: "application/json",
          "X-API-Key": dashboardApiKey,
          ...(options.body ? { "Content-Type": "application/json" } : {}),
          ...(options.headers || {}),
        },
        signal: controller.signal,
      });

      let payload = null;
      const contentType = response.headers.get("content-type") || "";
      if (contentType.includes("application/json")) {
        payload = await response.json();
      } else {
        const text = await response.text();
        payload = text ? { detail: text } : {};
      }

      if (!response.ok) {
        const message = payload?.detail || payload?.message || payload?.error || `Request failed (${response.status})`;
        throw new Error(asText(message, "Request failed"));
      }
      return payload || {};
    } catch (error) {
      if (error?.name === "AbortError") throw new Error("The server took too long to respond.");
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function setConnection(status, label) {
    elements.connectionStatus.classList.remove("connecting", "offline");
    if (status !== "online") elements.connectionStatus.classList.add(status);
    const labelElement = elements.connectionStatus.querySelector("span");
    setText(labelElement, label);
  }

  function renderMetrics(payload = {}) {
    const metrics = payload?.data && typeof payload.data === "object" ? payload.data : payload;
    const totalCalls = metrics.total_calls ?? metrics.calls ?? state.calls.length;
    const activeCalls = metrics.active_calls ?? metrics.active ?? 0;
    const handoffs = metrics.handoffs ?? metrics.human_handoffs ?? 0;
    const appointments = metrics.appointments ?? metrics.appointments_booked ?? 0;
    const averageTurns = metrics.average_turns ?? metrics.avg_turns ?? 0;
    const containment = normalizePercentage(metrics.containment_rate ?? metrics.automation_rate ?? 0);

    setText(elements.totalCalls, formatNumber(totalCalls));
    setText(elements.activeCalls, formatNumber(activeCalls));
    setText(elements.containmentRate, formatPercentage(containment));
    elements.containmentBar.style.width = `${containment}%`;
    setText(elements.averageTurns, formatNumber(averageTurns));
    setText(elements.handoffCount, formatNumber(handoffs));
    setText(elements.appointmentCount, formatNumber(appointments));
    setText(elements.handoffTrend, asFiniteNumber(handoffs) > 0 ? "Needs review" : "All clear");

    [
      elements.totalCalls,
      elements.containmentRate,
      elements.averageTurns,
      elements.handoffCount,
    ].forEach((element) => element.classList.remove("skeleton-text"));
  }

  function normalizeCalls(payload) {
    if (Array.isArray(payload)) return payload;
    if (Array.isArray(payload?.items)) return payload.items;
    if (Array.isArray(payload?.calls)) return payload.calls;
    if (Array.isArray(payload?.data?.items)) return payload.data.items;
    if (Array.isArray(payload?.data?.calls)) return payload.data.calls;
    return [];
  }

  function outcomeClass(call) {
    const value = `${call?.outcome || ""} ${call?.status || ""}`.toLowerCase();
    if (/handoff|escalat|transfer|queued/.test(value) || call?.handoff_reason) return "handoff";
    if (/fail|error|abandon|missed/.test(value)) return "failed";
    if (/complet|resolved|booked|success|contained/.test(value)) return "success";
    return "";
  }

  function callRequiresHandoff(call) {
    if (!call || typeof call !== "object") return false;
    if (call.handoff === true || call.handoff_required === true || call.handoff_reason) return true;
    const stateText = `${call.status || ""} ${call.outcome || ""}`.toLowerCase();
    return /handoff|escalat|transfer|queued|human/.test(stateText);
  }

  function createCallsRow(call) {
    const row = document.createElement("tr");

    const callerCell = document.createElement("td");
    const callerWrap = makeElement("div", "caller-cell");
    callerWrap.appendChild(makeElement("strong", "", maskCaller(call.caller || call.from)));
    callerWrap.appendChild(makeElement("span", "", call.call_sid || call.session_id || "web-session"));
    callerCell.appendChild(callerWrap);

    const intentCell = document.createElement("td");
    intentCell.appendChild(makeElement("span", "intent-pill", readableLabel(call.intent)));

    const outcomeCell = document.createElement("td");
    const outcome = readableLabel(call.outcome || call.status, "In progress");
    const outcomePill = makeElement("span", `outcome-pill ${outcomeClass(call)}`.trim());
    outcomePill.appendChild(makeElement("i"));
    outcomePill.appendChild(document.createTextNode(outcome));
    outcomeCell.appendChild(outcomePill);

    const turnsCell = makeElement("td", "", formatNumber(call.turn_count ?? call.turns ?? 0));
    const startedCell = makeElement("td", "", formatRelativeDate(call.started_at || call.created_at));
    if (call.summary) startedCell.title = asText(call.summary, "");

    row.append(callerCell, intentCell, outcomeCell, turnsCell, startedCell);
    return row;
  }

  function renderCalls(payload) {
    const calls = normalizeCalls(payload)
      .slice()
      .sort((a, b) => {
        const aDate = parseDate(a.started_at || a.created_at)?.getTime() || 0;
        const bDate = parseDate(b.started_at || b.created_at)?.getTime() || 0;
        return bDate - aDate;
      });
    state.calls = calls;
    clearElement(elements.callsTableBody);

    const table = elements.callsTableBody.closest("table");
    if (!calls.length) {
      table.classList.add("hidden");
      elements.callsEmpty.classList.remove("hidden");
      renderHandoffs([]);
      return;
    }

    table.classList.remove("hidden");
    elements.callsEmpty.classList.add("hidden");
    const fragment = document.createDocumentFragment();
    calls.slice(0, MAX_CALL_ROWS).forEach((call) => fragment.appendChild(createCallsRow(call)));
    elements.callsTableBody.appendChild(fragment);
    renderHandoffs(calls);
  }

  function handoffReason(call) {
    return readableLabel(call.handoff_reason || call.summary || call.intent, "Human assistance requested");
  }

  function callerInitials(value) {
    const caller = asText(value, "AI").replace(/\D/g, "");
    return caller.length >= 2 ? caller.slice(-2) : "AI";
  }

  function renderHandoffs(calls) {
    const handoffs = calls.filter(callRequiresHandoff).slice(0, MAX_HANDOFF_ROWS);
    clearElement(elements.handoffList);
    setText(elements.handoffBadge, handoffs.length);

    if (!handoffs.length) {
      const clearCard = makeElement("div", "handoff-clear");
      const content = document.createElement("div");
      const icon = makeElement("span");
      icon.setAttribute("aria-hidden", "true");
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 24 24");
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", "M9.5 16.2 5.3 12l-1.4 1.4 5.6 5.6L21 7.5l-1.4-1.4-10.1 10.1Z");
      svg.appendChild(path);
      icon.appendChild(svg);
      content.appendChild(icon);
      content.appendChild(makeElement("strong", "", "Queue is clear"));
      content.appendChild(makeElement("p", "", "No callers are waiting for a human agent."));
      clearCard.appendChild(content);
      elements.handoffList.appendChild(clearCard);
      return;
    }

    const fragment = document.createDocumentFragment();
    handoffs.forEach((call) => {
      const item = makeElement("div", "handoff-item");
      item.appendChild(makeElement("span", "handoff-avatar", callerInitials(call.caller)));

      const copy = makeElement("div", "handoff-copy");
      copy.appendChild(makeElement("strong", "", maskCaller(call.caller)));
      const reason = makeElement("span", "", handoffReason(call));
      reason.title = handoffReason(call);
      copy.appendChild(reason);
      item.appendChild(copy);

      item.appendChild(makeElement("span", "handoff-status", readableLabel(call.status, "Queued")));
      fragment.appendChild(item);
    });
    elements.handoffList.appendChild(fragment);
  }

  function showToast(message, type = "info") {
    window.clearTimeout(state.toastTimer);
    setText(elements.toast, message);
    elements.toast.classList.toggle("error", type === "error");
    elements.toast.classList.add("visible");
    state.toastTimer = window.setTimeout(() => elements.toast.classList.remove("visible"), 3600);
  }

  async function refreshDashboard({ quiet = false } = {}) {
    if (state.isRefreshing) return;
    state.isRefreshing = true;
    elements.refreshButton.classList.add("loading");
    setConnection("connecting", "Syncing");

    const [metricsResult, callsResult] = await Promise.allSettled([
      fetchJson(API.metrics),
      fetchJson(API.calls),
    ]);

    if (callsResult.status === "fulfilled") renderCalls(callsResult.value);
    if (metricsResult.status === "fulfilled") renderMetrics(metricsResult.value);

    const successful = [metricsResult, callsResult].filter((result) => result.status === "fulfilled").length;
    if (successful === 2) {
      setConnection("online", "API connected");
      setText(elements.lastSynced, timeFormatter.format(new Date()));
      if (!quiet) showToast("Dashboard is up to date.");
    } else if (successful === 1) {
      setConnection("connecting", "Partial data");
      setText(elements.lastSynced, timeFormatter.format(new Date()));
      if (!quiet) showToast("Some dashboard data could not be loaded.", "error");
    } else {
      setConnection("offline", "API unavailable");
      if (!quiet) showToast("Could not connect to the CallFlow API.", "error");
    }

    state.isRefreshing = false;
    elements.refreshButton.classList.remove("loading");
  }

  function currentTimeLabel() {
    return timeFormatter.format(new Date());
  }

  function appendMessage(role, message, { error = false } = {}) {
    const wrapper = makeElement("div", `message ${role === "user" ? "user-message" : "agent-message"}${error ? " error-message" : ""}`);
    wrapper.appendChild(makeElement("span", "message-author", role === "user" ? "You · Caller" : "Ava"));
    wrapper.appendChild(makeElement("div", "message-bubble", message));
    wrapper.appendChild(makeElement("time", "", currentTimeLabel()));
    elements.transcript.appendChild(wrapper);
    elements.transcript.scrollTop = elements.transcript.scrollHeight;
    return wrapper;
  }

  function appendTypingIndicator() {
    const wrapper = makeElement("div", "message agent-message typing-message");
    wrapper.appendChild(makeElement("span", "message-author", "Ava is thinking"));
    const bubble = makeElement("div", "message-bubble typing-bubble");
    bubble.setAttribute("aria-label", "Agent is preparing a response");
    bubble.append(makeElement("i"), makeElement("i"), makeElement("i"));
    wrapper.appendChild(bubble);
    elements.transcript.appendChild(wrapper);
    elements.transcript.scrollTop = elements.transcript.scrollHeight;
    return wrapper;
  }

  function summarizeToolResult(toolResult) {
    if (!toolResult) return "";
    if (typeof toolResult === "string") return toolResult;
    if (Array.isArray(toolResult)) return toolResult.map((entry) => asText(entry, "")).filter(Boolean).join(", ");
    if (typeof toolResult === "object") {
      const preferred = toolResult.message || toolResult.status || toolResult.result || toolResult.appointment_id;
      if (preferred) return asText(preferred, "");
      try {
        return JSON.stringify(toolResult);
      } catch (_error) {
        return "Tool completed";
      }
    }
    return asText(toolResult, "");
  }

  function renderTurnMetadata(payload) {
    clearElement(elements.turnMetadata);
    const entries = [];
    if (payload.action) entries.push({ label: `Action · ${readableLabel(payload.action)}`, handoff: false });
    const toolSummary = summarizeToolResult(payload.tool_result);
    if (toolSummary) entries.push({ label: `Tool · ${toolSummary.slice(0, 120)}`, handoff: false });
    if (payload.handoff || payload.handoff_reason) {
      entries.push({
        label: `Handoff · ${readableLabel(payload.handoff_reason, "Human requested")}`,
        handoff: true,
      });
    }

    if (!entries.length) {
      elements.turnMetadata.classList.add("hidden");
      return;
    }

    entries.forEach((entry) => {
      elements.turnMetadata.appendChild(makeElement("span", `meta-chip${entry.handoff ? " handoff-meta" : ""}`, entry.label));
    });
    elements.turnMetadata.classList.remove("hidden");
  }

  function resizeMessageInput() {
    elements.messageInput.style.height = "auto";
    elements.messageInput.style.height = `${Math.min(elements.messageInput.scrollHeight, 116)}px`;
  }

  function setSending(isSending) {
    state.isSending = isSending;
    elements.sendButton.disabled = isSending;
    elements.messageInput.disabled = isSending;
    elements.sendButton.querySelector("span").textContent = isSending ? "Sending" : "Send";
  }

  async function submitSimulation(event) {
    event.preventDefault();
    if (state.isSending) return;

    const text = elements.messageInput.value.trim();
    if (!text) {
      elements.messageInput.focus();
      return;
    }

    const request = {
      session_id: elements.sessionId.value.trim() || undefined,
      caller: elements.caller.value.trim() || undefined,
      text,
    };

    appendMessage("user", text);
    elements.messageInput.value = "";
    resizeMessageInput();
    renderTurnMetadata({});
    setSending(true);
    const typingIndicator = appendTypingIndicator();

    try {
      const payload = await fetchJson(API.simulate, {
        method: "POST",
        body: JSON.stringify(request),
      });
      typingIndicator.remove();

      const responseText = payload.reply || payload.agent_response || payload.response || payload.message || "I’m ready for your next question.";
      appendMessage("agent", asText(responseText, "I’m ready for your next question."));
      if (payload.session_id) elements.sessionId.value = asText(payload.session_id, "");
      renderTurnMetadata(payload);

      if (payload.handoff || payload.handoff_reason) {
        showToast("Handoff prepared with full conversation context.");
      }
      window.setTimeout(() => refreshDashboard({ quiet: true }), 500);
    } catch (error) {
      typingIndicator.remove();
      appendMessage("agent", error?.message || "The simulator could not reach the voice agent.", { error: true });
      showToast(error?.message || "Simulator request failed.", "error");
    } finally {
      setSending(false);
      elements.messageInput.focus();
    }
  }

  function resetConversation() {
    clearElement(elements.transcript);
    appendMessage("agent", "Hi! I’m Ava, the CallFlow AI assistant. How can I help you today?");
    elements.sessionId.value = "";
    elements.messageInput.value = "";
    renderTurnMetadata({});
    resizeMessageInput();
    elements.messageInput.focus();
    showToast("A fresh test session is ready.");
  }

  function initializeNavigation() {
    const navItems = Array.from(document.querySelectorAll(".nav-item"));
    navItems.forEach((link) => {
      link.addEventListener("click", () => {
        navItems.forEach((item) => {
          item.classList.remove("active");
          item.removeAttribute("aria-current");
        });
        link.classList.add("active");
        link.setAttribute("aria-current", "page");
      });
    });
  }

  function initializeSimulator() {
    elements.form.addEventListener("submit", submitSimulation);
    elements.resetConversation.addEventListener("click", resetConversation);
    elements.messageInput.addEventListener("input", resizeMessageInput);
    elements.messageInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        elements.form.requestSubmit();
      }
    });

    document.querySelectorAll("[data-prompt]").forEach((button) => {
      button.addEventListener("click", () => {
        elements.messageInput.value = button.dataset.prompt || "";
        resizeMessageInput();
        elements.messageInput.focus();
      });
    });
  }

  function initialize() {
    setText(elements.currentDate, dateFormatter.format(new Date()));
    initializeNavigation();
    initializeSimulator();
    elements.refreshButton.addEventListener("click", () => refreshDashboard());
    window.addEventListener("online", () => refreshDashboard({ quiet: true }));
    window.addEventListener("offline", () => setConnection("offline", "Browser offline"));

    refreshDashboard({ quiet: true });
    window.setInterval(() => {
      if (!document.hidden && navigator.onLine) refreshDashboard({ quiet: true });
    }, POLL_INTERVAL_MS);
  }

  initialize();
})();
