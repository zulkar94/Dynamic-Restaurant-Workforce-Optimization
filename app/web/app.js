/* Zero-dependency dashboard client.
   Tokens live in a closure variable only: never localStorage, so an injected
   script cannot read them back out of persistent storage. */
(() => {
  "use strict";

  const API = "/api/v1";
  let accessToken = null;
  let refreshToken = null;
  let schedule = null;
  let activeRole = null;

  const $ = (id) => document.getElementById(id);
  const text = (el, value) => { el.textContent = value; };

  async function api(path, { method = "GET", body = null, retry = true } = {}) {
    const headers = { "Content-Type": "application/json" };
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
    const response = await fetch(API + path, {
      method,
      headers,
      body: body ? JSON.stringify(body) : null,
    });
    if (response.status === 401 && retry && refreshToken) {
      const refreshed = await refreshSession();
      if (refreshed) return api(path, { method, body, retry: false });
    }
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const data = await response.json();
        detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
      } catch (_err) { /* body was not JSON */ }
      throw new Error(detail);
    }
    return response.status === 204 ? null : response.json();
  }

  async function refreshSession() {
    try {
      const data = await api("/auth/refresh", {
        method: "POST",
        body: { refresh_token: refreshToken },
        retry: false,
      });
      accessToken = data.access_token;
      refreshToken = data.refresh_token;
      return true;
    } catch (_err) {
      accessToken = null;
      refreshToken = null;
      return false;
    }
  }

  function fmtMoney(value) {
    return value.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  }

  // ------------------------------------------------------------------ login
  async function login() {
    const note = $("loginNote");
    note.className = "note";
    text(note, "signing in…");
    try {
      const data = await api("/auth/login", {
        method: "POST",
        retry: false,
        body: { email: $("email").value.trim(), password: $("password").value },
      });
      accessToken = data.access_token;
      refreshToken = data.refresh_token;
      const me = await api("/auth/me");
      text($("railStatus"), `${me.email} · ${me.role}`);
      $("loginPanel").hidden = true;
      $("workPanel").hidden = false;
      $("kpis").hidden = false;
      text(note, "");
      await loadModels();
    } catch (err) {
      note.className = "note error";
      text(note, err.message);
    }
  }

  async function loadModels() {
    const { models, default: fallback } = await api("/models");
    const select = $("model");
    select.replaceChildren(...models.map((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      option.selected = name === fallback;
      return option;
    }));
  }

  // --------------------------------------------------------------- planning
  async function train() {
    const note = $("runNote");
    text(note, "training…");
    try {
      const result = await api("/forecast/train", {
        method: "POST",
        body: { model: $("model").value, test_days: 14 },
      });
      const m = result.metrics || {};
      text($("metricNote"),
        `${result.model} · rows ${result.trained_rows} · MAE ${m.mae ?? "–"} covers · MAPE ${m.mape ?? "–"}% · R² ${m.r2 ?? "–"}`);
      text(note, "forecast retrained");
    } catch (err) {
      text(note, err.message);
    }
  }

  async function optimize() {
    const note = $("runNote");
    text(note, "solving…");
    $("optimizeBtn").disabled = true;
    try {
      schedule = await api("/schedules/optimize", {
        method: "POST",
        body: {
          start_day: $("startDay").value,
          days: Number($("days").value),
          model: $("model").value,
          solver: $("solver").value,
          min_rest_hours: Number($("rest").value),
          understaff_penalty: Number($("penalty").value),
        },
      });
      render();
      const meta = schedule.meta || {};
      text(note, `${schedule.solver} · ${meta.solve_status || "done"}`);
      $("publishBtn").disabled = schedule.assignments.length === 0;
    } catch (err) {
      text(note, err.message);
    } finally {
      $("optimizeBtn").disabled = false;
    }
  }

  async function publish() {
    if (!schedule) return;
    try {
      const updated = await api(`/schedules/${schedule.id}/publish`, { method: "POST" });
      text($("runNote"), `schedule ${updated.id} ${updated.status}`);
      $("publishBtn").disabled = true;
    } catch (err) {
      text($("runNote"), err.message);
    }
  }

  // --------------------------------------------------------------- rendering
  function render() {
    text($("kpiCost"), fmtMoney(schedule.labor_cost));
    text($("kpiShifts"), String(schedule.assignments.length));
    text($("kpiUnder"), `${schedule.understaffed_hours}`);
    text($("kpiOver"), `${schedule.overstaffed_hours}`);
    renderRoleTabs();
    renderCoverage();
    renderRoster();
    loadSummary();
  }

  function roles() {
    return [...new Set((schedule.coverage || []).map((c) => c.role))].sort();
  }

  function renderRoleTabs() {
    const list = roles();
    if (!list.includes(activeRole)) activeRole = list[0] || null;
    const tabs = $("roleTabs");
    tabs.replaceChildren(...list.map((role) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = role;
      button.setAttribute("aria-pressed", String(role === activeRole));
      button.addEventListener("click", () => {
        activeRole = role;
        renderRoleTabs();
        renderCoverage();
      });
      return button;
    }));
  }

  function renderCoverage() {
    const table = $("coverageTable");
    const rows = (schedule.coverage || []).filter((c) => c.role === activeRole);
    if (rows.length === 0) { table.replaceChildren(); return; }

    const days = [...new Set(rows.map((r) => r.day))].sort();
    const hours = [...new Set(rows.map((r) => r.hour))].sort((a, b) => a - b);
    const index = new Map(rows.map((r) => [`${r.day}|${r.hour}`, r]));

    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    headRow.appendChild(document.createElement("th")).textContent = "hour";
    for (const day of days) {
      const th = document.createElement("th");
      const parsed = new Date(`${day}T00:00:00`);
      th.textContent = `${parsed.toLocaleDateString(undefined, { weekday: "short" })} ${day.slice(5)}`;
      headRow.appendChild(th);
    }
    thead.appendChild(headRow);

    const tbody = document.createElement("tbody");
    for (const hour of hours) {
      const tr = document.createElement("tr");
      tr.appendChild(document.createElement("th")).textContent = `${String(hour).padStart(2, "0")}:00`;
      for (const day of days) {
        const cell = index.get(`${day}|${hour}`);
        const td = document.createElement("td");
        if (!cell) { tr.appendChild(td); continue; }
        const gap = cell.scheduled - cell.required;
        td.textContent = `${cell.scheduled}/${cell.required}`;
        td.className = gap < -1 ? "short2" : gap < 0 ? "short1" : gap > 0 ? "surplus" : "ok";
        td.title = `${cell.role} ${day} ${hour}:00 — scheduled ${cell.scheduled}, required ${cell.required}`;
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.replaceChildren(thead, tbody);
  }

  function buildTable(target, columns, rows) {
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const col of columns) headRow.appendChild(document.createElement("th")).textContent = col;
    thead.appendChild(headRow);
    const tbody = document.createElement("tbody");
    for (const row of rows) {
      const tr = document.createElement("tr");
      for (const value of row) tr.appendChild(document.createElement("td")).textContent = value;
      tbody.appendChild(tr);
    }
    target.replaceChildren(thead, tbody);
  }

  function renderRoster() {
    const rows = [...schedule.assignments]
      .sort((a, b) => a.day.localeCompare(b.day) || a.start_hour - b.start_hour)
      .map((a) => [
        a.day,
        `${String(a.start_hour).padStart(2, "0")}:00–${String(a.end_hour).padStart(2, "0")}:00`,
        a.role,
        a.employee_name,
        a.hours.toFixed(1),
        fmtMoney(a.cost),
      ]);
    buildTable($("rosterTable"), ["day", "shift", "role", "employee", "hours", "cost"], rows);
  }

  async function loadSummary() {
    try {
      const summary = await api(`/schedules/${schedule.id}/labor-summary`);
      const rows = Object.entries(summary.per_employee)
        .sort((a, b) => b[1].hours - a[1].hours)
        .map(([name, v]) => [
          name,
          String(v.shifts),
          v.hours.toFixed(1),
          fmtMoney(v.cost),
          summary.overtime_hours[name] ? `${summary.overtime_hours[name]} h` : "—",
        ]);
      buildTable($("summaryTable"), ["employee", "shifts", "hours", "cost", "overtime"], rows);
    } catch (err) {
      text($("runNote"), err.message);
    }
  }

  // ------------------------------------------------------------------- init
  const tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  $("startDay").value = tomorrow.toISOString().slice(0, 10);

  $("loginBtn").addEventListener("click", login);
  $("password").addEventListener("keydown", (e) => { if (e.key === "Enter") login(); });
  $("trainBtn").addEventListener("click", train);
  $("optimizeBtn").addEventListener("click", optimize);
  $("publishBtn").addEventListener("click", publish);
})();
