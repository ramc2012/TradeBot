// Shared client helpers for all dashboard pages.
async function fetchJSON(url) {
  const r = await fetch(url, { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
  return r.json();
}

function escapeHTML(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtTS(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString("en-IN", { dateStyle: "short", timeStyle: "medium" });
  } catch { return iso; }
}

function alertErr(e) {
  console.error(e);
  const m = document.querySelector("main");
  if (m) m.insertAdjacentHTML("afterbegin",
    `<div class="card" style="border-color:var(--neg)"><b class="neg">Error</b> <span class="muted">${escapeHTML(e.message || e)}</span></div>`);
}

const _chartTextColor = "#9aa5be";
const _chartGrid = "rgba(255,255,255,0.05)";

function chartOpts(extra) {
  return {
    responsive: true, maintainAspectRatio: false,
    animation: { duration: 250 },
    plugins: { legend: { labels: { color: _chartTextColor } } },
    scales: {
      x: { ticks: { color: _chartTextColor, autoSkip: true, maxRotation: 60 }, grid: { color: _chartGrid },
           stacked: extra && extra.stacked },
      y: { ticks: { color: _chartTextColor }, grid: { color: _chartGrid },
           stacked: extra && extra.stacked },
    },
  };
}

function makeBar(canvasId, labels, data, label, color) {
  return new Chart(document.getElementById(canvasId).getContext("2d"), {
    type: "bar",
    data: { labels, datasets: [{ label, data, backgroundColor: color }] },
    options: chartOpts(),
  });
}

function makeHist(canvasId, bins, label, color) {
  const labels = bins.map(b => ((b.x_lo + b.x_hi) / 2).toFixed(3));
  const data = bins.map(b => b.count);
  return makeBar(canvasId, labels, data, label, color);
}

// Multi-series line chart. `series` = [{label, data:[..], color}], `labels` = x axis.
function makeLines(canvasId, labels, series) {
  return new Chart(document.getElementById(canvasId).getContext("2d"), {
    type: "line",
    data: {
      labels,
      datasets: series.map(s => ({
        label: s.label, data: s.data, borderColor: s.color,
        backgroundColor: s.color, pointRadius: 0, borderWidth: 1.5, tension: 0.2,
      })),
    },
    options: chartOpts(),
  });
}
