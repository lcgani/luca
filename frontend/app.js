const config = window.LUCA_CONFIG || { apiBaseUrl: "http://127.0.0.1:8000" };
const state = {
  sessionId: null,
  pollHandle: null,
};

const apiBase = config.apiBaseUrl.replace(/\/$/, "");
document.getElementById("api-base").textContent = apiBase;

const el = {
  form: document.getElementById("session-form"),
  sessionId: document.getElementById("session-id"),
  status: document.getElementById("session-status"),
  phase: document.getElementById("session-phase"),
  endpointCount: document.getElementById("endpoint-count"),
  authType: document.getElementById("auth-type"),
  discoverBtn: document.getElementById("discover-btn"),
  generateBtn: document.getElementById("generate-btn"),
  events: document.getElementById("events"),
  endpoints: document.getElementById("endpoints"),
  artifacts: document.getElementById("artifacts"),
};

el.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = buildCreateSessionPayload();
  const response = await fetch(`${apiBase}/api/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  state.sessionId = data.session_id;
  el.sessionId.textContent = data.session_id;
  el.discoverBtn.disabled = false;
  el.generateBtn.disabled = true;
  await refreshSession();
});

el.discoverBtn.addEventListener("click", async () => {
  if (!state.sessionId) return;
  await fetch(`${apiBase}/api/sessions/${state.sessionId}/discover`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  startPolling();
});

el.generateBtn.addEventListener("click", async () => {
  if (!state.sessionId) return;
  await fetch(`${apiBase}/api/sessions/${state.sessionId}/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ language: "python" }),
  });
  startPolling();
});

function buildCreateSessionPayload() {
  const apiUrl = document.getElementById("api-url").value.trim();
  const docsUrl = document.getElementById("docs-url").value.trim();
  const token = document.getElementById("token").value.trim();
  const headerName = document.getElementById("header-name").value.trim();
  const payload = { api_url: apiUrl };
  if (docsUrl) payload.docs_url = docsUrl;
  if (token) {
    payload.auth_input = {
      token,
      header_name: headerName || null,
    };
  }
  return payload;
}

async function refreshSession() {
  if (!state.sessionId) return;
  const [sessionRes, eventsRes, artifactsRes] = await Promise.all([
    fetch(`${apiBase}/api/sessions/${state.sessionId}`),
    fetch(`${apiBase}/api/sessions/${state.sessionId}/events`),
    fetch(`${apiBase}/api/sessions/${state.sessionId}/artifacts`),
  ]);

  if (!sessionRes.ok) return;
  const session = await sessionRes.json();
  const events = await eventsRes.json();
  const artifacts = await artifactsRes.json();

  el.status.textContent = session.status;
  el.phase.textContent = session.phase;
  el.endpointCount.textContent = String(session.endpoint_count);
  el.authType.textContent = `${session.auth_result.auth_type} (${session.auth_result.confidence.toFixed(2)})`;
  el.generateBtn.disabled = session.endpoint_count === 0 || session.status === "running";

  renderEvents(events);
  renderEndpoints(session.endpoints_preview || []);
  renderArtifacts(artifacts || []);

  if (session.status !== "running" && session.status !== "queued" && state.pollHandle) {
    clearInterval(state.pollHandle);
    state.pollHandle = null;
  }
}

function startPolling() {
  if (state.pollHandle) clearInterval(state.pollHandle);
  refreshSession();
  state.pollHandle = setInterval(refreshSession, 2500);
}

function renderEvents(events) {
  if (!events.length) {
    el.events.innerHTML = "<li>No events yet.</li>";
    return;
  }
  el.events.innerHTML = events.map((item) => `
    <li>
      <strong>${escapeHtml(item.event_type)}</strong>
      <small>${escapeHtml(item.message)}</small>
      <small>${escapeHtml(new Date(item.timestamp).toLocaleString())}</small>
    </li>
  `).join("");
}

function renderEndpoints(endpoints) {
  if (!endpoints.length) {
    el.endpoints.innerHTML = "<li>No endpoints discovered yet.</li>";
    return;
  }
  el.endpoints.innerHTML = endpoints.map((endpoint) => `
    <li>
      <strong>${escapeHtml(endpoint.method)} ${escapeHtml(endpoint.path)}</strong>
      <small>${escapeHtml(endpoint.summary || endpoint.description || endpoint.source)}</small>
    </li>
  `).join("");
}

function renderArtifacts(artifacts) {
  if (!artifacts.length) {
    el.artifacts.innerHTML = "<li>No artifacts available yet.</li>";
    return;
  }
  el.artifacts.innerHTML = artifacts.map((artifact) => `
    <li>
      <a href="${apiBase}/api/sessions/${state.sessionId}/artifacts/${encodeURIComponent(artifact.name)}">${escapeHtml(artifact.name)}</a>
      <small>${escapeHtml(artifact.content_type)} · ${artifact.size} bytes</small>
    </li>
  `).join("");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
