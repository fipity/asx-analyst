// Whole-row links: a table row that links to exactly one company opens that company when clicked
// anywhere. Rows with their own controls keep them (buttons, inputs, other links win); rows with an
// external link (news/announcements) or links to several companies are left alone. Event delegation,
// so rows swapped in by HTMX work too. Ctrl/Cmd/middle click opens a new tab, like the code link.
(function () {
  "use strict";
  const INTERACTIVE = "a, button, input, select, textarea, label, summary, details, form, [onclick], [hx-get], [hx-post]";

  function rowTarget(tr) {
    if (!tr || tr.closest("thead")) return null;
    if (tr.dataset.href !== undefined) return tr.dataset.href || null;
    let href = "";
    const links = tr.querySelectorAll("a[href]");
    const companies = new Set();
    for (const a of links) {
      const h = a.getAttribute("href");
      if (/^https?:/i.test(h)) { companies.clear(); break; }
      const m = h.match(/^\/company\/([A-Za-z0-9]+)\/?$/);
      if (m) companies.add(m[1].toUpperCase());
    }
    if (companies.size === 1) href = "/company/" + [...companies][0];
    tr.dataset.href = href;          // cache (empty = not a row link)
    if (href) tr.classList.add("row-link");
    return href || null;
  }

  // Mark rows up front so the pointer cursor shows on hover; re-run after HTMX swaps.
  const mark = (root) => root.querySelectorAll && root.querySelectorAll("tbody tr").forEach(rowTarget);
  document.addEventListener("DOMContentLoaded", () => mark(document));
  document.addEventListener("htmx:afterSettle", (e) => mark(e.target));

  function open(e, href) {
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.button === 1) window.open(href, "_blank", "noopener");
    else window.location.href = href;
  }

  document.addEventListener("click", (e) => {
    if (e.button !== 0 || e.defaultPrevented) return;
    if (e.target.closest(INTERACTIVE)) return;
    if (window.getSelection && String(window.getSelection()).length) return;   // selecting text
    const href = rowTarget(e.target.closest("tr"));
    if (href) open(e, href);
  });
  document.addEventListener("auxclick", (e) => {
    if (e.button !== 1 || e.target.closest(INTERACTIVE)) return;
    const href = rowTarget(e.target.closest("tr"));
    if (href) { e.preventDefault(); open(e, href); }
  });
})();

// Header "Refresh data" button + data bar. The button starts the daily update without leaving the page;
// while it runs the bar shows the current step ("prices 40/200") with a progress line, and when it
// finishes it offers a reload. When idle it re-checks every minute (keeps "x min ago" current and
// notices scheduled updates).
(function () {
  "use strict";
  const bar = document.getElementById("data-bar");
  const form = document.getElementById("refresh-form");
  const btn = document.getElementById("refresh-btn");
  if (!bar || !form || !btn) return;
  const text = bar.querySelector(".data-bar-text");
  const prog = bar.querySelector(".data-bar-progress span");
  const label = btn.querySelector(".refresh-label");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let wasRunning = bar.dataset.running === "1";
  let timer = null;

  function render(st, justFinished) {
    bar.classList.toggle("running", !!st.running);
    bar.classList.toggle("done", !!justFinished);
    btn.disabled = !!st.running;
    label.textContent = st.running ? "Refreshing…" : "Refresh data";
    if (st.running) {
      text.innerHTML = "<strong>Refreshing data…</strong> " + (st.message ? esc(st.message) : "getting ready") +
        (st.started_text ? ` <span class="muted">· started ${esc(st.started_text)}</span>` : "");
      if (st.total) { prog.classList.remove("indeterminate"); prog.style.width = Math.round(100 * st.done / st.total) + "%"; }
      else { prog.classList.add("indeterminate"); prog.style.width = ""; }
      return;
    }
    let html;
    if (st.last_update_text) {
      html = (justFinished ? "Refresh finished. " : "") + `Data updated <strong>${esc(st.last_update_text)}</strong>` +
        ` <span class="muted">(${esc(st.last_update_age)})</span>` +
        (st.price_date_text ? ` · prices to close ${esc(st.price_date_text)}` : "");
      if (st.last_status === "error" || st.last_status === "interrupted")
        html += ` · <a href="/status" class="warn-inline">last refresh ${st.last_status === "error" ? "failed" : "was interrupted"}</a>`;
    } else html = "No data update has finished yet.";
    if (justFinished) html += ' · <a href="" onclick="location.reload(); return false;">Reload to see the new data</a>';
    text.innerHTML = html;
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(poll, wasRunning ? 3000 : 60000);
  }

  function poll() {
    if (document.hidden) { schedule(); return; }          // don't poll from background tabs
    fetch("/api/refresh", { credentials: "same-origin" })
      .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then((st) => {
        const finished = wasRunning && !st.running;
        wasRunning = !!st.running;
        // Keep the "Refresh finished · Reload" message until a new refresh starts.
        if (st.running || finished || !bar.classList.contains("done")) render(st, finished);
        schedule();
      })
      .catch(schedule);
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    btn.disabled = true;
    fetch("/api/refresh", { method: "POST", credentials: "same-origin" })
      .then((r) => r.json())
      .then((st) => { wasRunning = true; render({ ...st, running: true }); schedule(); })
      .catch(() => { btn.disabled = false; text.textContent = "Couldn't start the refresh. Try the Status page."; });
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  schedule();
})();
