const params = new URLSearchParams(window.location.search);
const runId = params.get("run_id") || "";

function el(id) {
  return document.getElementById(id);
}

function card(label, value, cls = "") {
  const d = document.createElement("div");
  d.className = `card ${cls}`;
  d.innerHTML = `<div class="label">${label}</div><div class="value">${value}</div>`;
  return d;
}

async function fetchJson(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function renderStatus(data) {
  el("runMeta").textContent = `Run: ${data.run_id} | Excel: ${data.excel_path || "—"} | Started: ${data.started_at || "—"}`;

  const overall = el("overall");
  overall.innerHTML = "";
  let total = 0,
    ok = 0,
    fail = 0;
  for (const et of Object.keys(data.entities || {})) {
    const st = data.entities[et];
    for (const [k, v] of Object.entries(st)) {
      total += v;
      if (k === "success") ok += v;
      if (k === "failed") fail += v;
    }
  }
  overall.appendChild(card("Total entities", total));
  overall.appendChild(card("Success", ok, "success"));
  overall.appendChild(card("Failed", fail, "failed"));

  const ec = el("entityCards");
  ec.innerHTML = "";
  for (const [etype, st] of Object.entries(data.entities || {})) {
    const line = Object.entries(st)
      .map(([k, v]) => `${k}: ${v}`)
      .join(" ");
    ec.appendChild(card(etype, line || "0"));
  }

  const fc = el("fileCards");
  fc.innerHTML = "";
  for (const [k, v] of Object.entries(data.files || {})) {
    fc.appendChild(card(k, v, k === "uploaded" ? "success" : k === "failed" ? "failed" : "pending"));
  }
}

function renderFailures(rows) {
  const tbody = el("failTable").querySelector("tbody");
  tbody.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${r.entity_type || ""}</td><td>${(r.zoho_key || "").slice(0, 40)}</td><td>${r.display_label || ""}</td><td>${(r.error || "").slice(0, 120)}</td>`;
    tbody.appendChild(tr);
  }
}

function renderActivity(rows) {
  const ul = el("activity");
  ul.innerHTML = "";
  for (const r of rows) {
    const li = document.createElement("li");
    li.textContent = `${r.created_at || ""} [${r.event_type}] ${r.message || ""}`;
    ul.appendChild(li);
  }
}

async function refresh() {
  if (!runId) {
    el("runMeta").textContent = "Add ?run_id=YYYY-MM-DD to the URL";
    return;
  }
  try {
    const status = await fetchJson(`/api/status?run_id=${encodeURIComponent(runId)}`);
    renderStatus(status);
    const fails = await fetchJson(`/api/failures?run_id=${encodeURIComponent(runId)}&limit=50`);
    renderFailures(fails);
    const act = await fetchJson(`/api/activity?run_id=${encodeURIComponent(runId)}&limit=20`);
    renderActivity(act);
  } catch (e) {
    el("runMeta").textContent = `Error: ${e.message}`;
  }
}

refresh();
setInterval(() => {
  if (el("autoRefresh").checked) refresh();
}, 10000);
