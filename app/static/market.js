// Market detail chart (/market/{slug}): one line (step line for the cash rate), range buttons, legend.
(function () {
  "use strict";
  const LC = window.LightweightCharts;
  const el = document.getElementById("chart");
  const legend = document.getElementById("chart-legend");
  if (!LC || !el) return;

  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const dmy = (k) => `${k.slice(8, 10)}/${k.slice(5, 7)}/${k.slice(0, 4)}`;
  const timeKey = (t) => (typeof t === "string" ? t
    : t && t.year ? `${t.year}-${String(t.month).padStart(2, "0")}-${String(t.day).padStart(2, "0")}` : null);
  let chart, line, data, byDate = new Map(), range = el.dataset.range || "1Y";

  const fmt = (v) => (v == null ? "—" : data.step ? v.toFixed(2) + "%"
    : v.toLocaleString("en-AU", { minimumFractionDigits: data.digits, maximumFractionDigits: data.digits }));

  function options() {
    return {
      autoSize: true,
      layout: { background: { color: css("--card") }, textColor: css("--muted"), fontFamily: "system-ui, sans-serif" },
      grid: { vertLines: { color: css("--line") }, horzLines: { color: css("--line") } },
      rightPriceScale: { borderColor: css("--line") },
      timeScale: { borderColor: css("--line"), minBarSpacing: 0.02 },
      crosshair: { mode: 0 },
      localization: { dateFormat: "dd/MM/yyyy", priceFormatter: fmt },
    };
  }

  function setRange(r) {
    range = r;
    document.querySelectorAll("[data-range]").forEach((b) => b.classList.toggle("active", b.dataset.range === r));
    const pts = data.points;
    if (r === "Max") { chart.timeScale().fitContent(); return; }
    const last = new Date(pts[pts.length - 1][0] + "T00:00:00Z");
    const from = new Date(last), n = parseInt(r, 10);
    if (r.endsWith("M")) from.setUTCMonth(from.getUTCMonth() - n); else from.setUTCFullYear(from.getUTCFullYear() - n);
    let f = from.toISOString().slice(0, 10);
    if (f < pts[0][0]) f = pts[0][0];
    chart.timeScale().setVisibleRange({ from: f, to: pts[pts.length - 1][0] });
  }

  function show(key) {
    const i = byDate.get(key);
    if (i == null) return;
    const v = data.points[i][1];
    let html = `<strong>${dmy(key)}</strong> · ${fmt(v)}`;
    if (!data.step && i > 0) {
      const chg = (v / data.points[i - 1][1] - 1) * 100;
      html += ` <span class="${chg >= 0 ? "up" : "down"}">${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%</span>`;
    }
    legend.innerHTML = html;
  }

  function build() {
    if (line) chart.removeSeries(line);
    line = chart.addSeries(LC.LineSeries, {
      color: css("--accent"), lineWidth: 2, priceLineVisible: false,
      lineType: data.step ? 1 : 0,     // 1 = WithSteps: a rate holds until the next decision
    });
    line.setData(data.points.map((p) => ({ time: p[0], value: p[1] })));
  }

  fetch(`/api/market/${encodeURIComponent(el.dataset.slug)}/series`, { credentials: "same-origin" })
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then((d) => {
      data = d;
      el.innerHTML = "";
      if (!d.points.length) { el.innerHTML = '<div class="muted chart-loading">No history stored.</div>'; return; }
      d.points.forEach((p, i) => byDate.set(p[0], i));
      chart = LC.createChart(el, options());
      build();
      setRange(range);
      show(d.points[d.points.length - 1][0]);
      chart.subscribeCrosshairMove((p) => { if (p && p.time) show(timeKey(p.time)); });
      chart.subscribeClick((p) => { if (p && p.time) show(timeKey(p.time)); });
      document.querySelectorAll("[data-range]").forEach((b) => b.addEventListener("click", () => setRange(b.dataset.range)));
      const mq = window.matchMedia("(prefers-color-scheme: dark)");
      mq.addEventListener && mq.addEventListener("change", () => { chart.applyOptions(options()); build(); setRange(range); });
    })
    .catch((e) => { el.innerHTML = `<div class="warn chart-loading">Couldn't load the chart (${e.message}).</div>`; });
})();
