// Company price chart: lightweight-charts v5 (vendored), overlays for dividends,
// price-sensitive announcements, director trades and ASIC short %.
(function () {
  "use strict";
  const LC = window.LightweightCharts;
  const el = document.getElementById("chart");
  const legend = document.getElementById("chart-legend");
  if (!LC || !el) return;

  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const colors = () => ({
    bg: css("--card"), fg: css("--fg"), muted: css("--muted"), line: css("--line"),
    accent: css("--accent"), up: css("--up"), down: css("--down"),
    div: css("--c-div"), ann: css("--c-ann"), short: css("--c-short"),
    sma20: css("--c-sma20"), sma50: css("--c-sma50"), sma200: css("--c-sma200"),
  });

  const state = {
    range: "1Y", interval: "D", candles: false,
    overlays: { dividend: true, announcement: true, trades: true, shorts: true },
    ind: { sma20: false, sma50: true, sma200: true, bb: false, rsi: false, macd: false },
  };
  document.querySelectorAll("[data-ind]").forEach((cb) => { state.ind[cb.dataset.ind] = cb.checked; });
  let chart, priceSeries, volSeries, markersApi, data, eventsByDate = new Map(), barsByDate = new Map();
  let indData = null;                 // fetched once from /api/company/{code}/indicators
  const overlaySeries = [];           // SMA / Bollinger lines on the price pane
  const lowerSeries = [];             // short %, RSI, MACD (each in its own pane)
  const rsiByDate = new Map();
  const PANE_H = 110;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const timeKey = (t) => (typeof t === "string" ? t
    : t && t.year ? `${t.year}-${String(t.month).padStart(2, "0")}-${String(t.day).padStart(2, "0")}` : null);
  const fmtPrice = (v) => (v == null ? "—" : v < 1 ? v.toFixed(3) : v.toFixed(2));
  const fmtVol = (v) => (v == null ? "—" : v >= 1e6 ? (v / 1e6).toFixed(2) + "M" : v >= 1e3 ? (v / 1e3).toFixed(0) + "K" : String(v));

  function chartOptions() {
    const c = colors();
    return {
      autoSize: true,
      layout: { background: { color: c.bg }, textColor: c.muted, fontFamily: "system-ui, sans-serif",
                panes: { separatorColor: c.line, enableResize: true } },
      grid: { vertLines: { color: c.line }, horzLines: { color: c.line } },
      rightPriceScale: { borderColor: c.line },
      timeScale: { borderColor: c.line, minBarSpacing: 0.05 },
      crosshair: { mode: 0 },
      localization: { dateFormat: "dd/MM/yyyy" },
    };
  }

  function buildPriceSeries() {
    const c = colors();
    if (priceSeries) chart.removeSeries(priceSeries);
    if (state.candles) {
      priceSeries = chart.addSeries(LC.CandlestickSeries, {
        upColor: c.up, downColor: c.down, borderVisible: false, wickUpColor: c.up, wickDownColor: c.down,
      }, 0);
      priceSeries.setData(data.ohlcv.filter((b) => b[1] != null).map((b) => ({ time: b[0], open: b[1], high: b[2], low: b[3], close: b[4] })));
    } else {
      priceSeries = chart.addSeries(LC.LineSeries, { color: c.accent, lineWidth: 2 }, 0);
      priceSeries.setData(data.ohlcv.filter((b) => b[4] != null).map((b) => ({ time: b[0], value: b[4] })));
    }
    markersApi = LC.createSeriesMarkers(priceSeries, []);
    applyMarkers();
  }

  function applyMarkers() {
    const c = colors();
    const o = state.overlays;
    const out = [];
    for (const [d, evs] of eventsByDate) {
      const kinds = new Set(evs.map((e) => e.kind));
      if (o.dividend && kinds.has("dividend")) out.push({ time: d, position: "belowBar", shape: "circle", color: c.div, text: "D", size: 0.7 });
      if (o.announcement && kinds.has("announcement")) out.push({ time: d, position: "aboveBar", shape: "circle", color: c.ann, size: 0.6 });
      if (o.trades && kinds.has("buy")) out.push({ time: d, position: "belowBar", shape: "arrowUp", color: c.up, text: "B", size: 0.8 });
      if (o.trades && kinds.has("sell")) out.push({ time: d, position: "aboveBar", shape: "arrowDown", color: c.down, text: "S", size: 0.8 });
    }
    out.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
    markersApi.setMarkers(out);
  }

  // indData is {dates: [...], name: [value|null...]} aligned to dates.
  const pts = (key) => {
    const v = indData[key] || [], out = [];
    for (let i = 0; i < v.length; i++) if (v[i] != null) out.push({ time: indData.dates[i], value: v[i] });
    return out;
  };

  function buildOverlays() {
    while (overlaySeries.length) chart.removeSeries(overlaySeries.pop());
    if (!indData) return;
    const c = colors();
    const line = (key, color, opts = {}) => {
      if (!indData[key] || !indData[key].some((x) => x != null)) return;
      const ser = chart.addSeries(LC.LineSeries, { color, lineWidth: 1, lastValueVisible: false, priceLineVisible: false,
        crosshairMarkerVisible: false, ...opts }, 0);
      ser.setData(pts(key));
      overlaySeries.push(ser);
    };
    for (const k of ["sma20", "sma50", "sma200"]) if (state.ind[k]) line(k, c[k], { lineWidth: 1.5 });
    if (state.ind.bb) {
      line("bb_upper", c.muted, { lineStyle: 2 });
      line("bb_lower", c.muted, { lineStyle: 2 });
    }
  }

  function buildLowerPanes() {
    // Remove and re-add in a fixed order so pane indices stay consecutive.
    while (lowerSeries.length) chart.removeSeries(lowerSeries.pop());
    chart.panes().slice(1).reverse().forEach((p) => { if (!p.getSeries().length) chart.removePane(p.paneIndex()); });
    const c = colors();
    let pane = 1;
    if (state.overlays.shorts && data.shorts.length) {
      const ser = chart.addSeries(LC.LineSeries, {
        color: c.short, lineWidth: 1.5, priceFormat: { type: "custom", formatter: (v) => v.toFixed(2) + "%" },
        title: "Short %", priceLineVisible: false,
      }, pane);
      ser.setData(data.shorts.map((x) => ({ time: x[0], value: x[1] })));
      lowerSeries.push(ser); pane++;
    }
    if (indData && state.ind.rsi && indData.dates && indData.dates.length) {
      const ser = chart.addSeries(LC.LineSeries, { color: c.ann, lineWidth: 1.5, title: "RSI", priceLineVisible: false,
        autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }) }, pane);
      ser.setData(pts("rsi14"));
      ser.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.08 } });
      for (const lvl of [70, 30]) ser.createPriceLine({ price: lvl, color: c.muted, lineWidth: 1, lineStyle: 2, axisLabelVisible: false });
      lowerSeries.push(ser); pane++;
    }
    if (indData && state.ind.macd && indData.dates && indData.dates.length) {
      const hist = chart.addSeries(LC.HistogramSeries, { priceLineVisible: false, lastValueVisible: false, title: "MACD" }, pane);
      hist.setData(pts("macd_hist").map((p) => ({ ...p, color: p.value >= 0 ? c.up : c.down })));
      const m = chart.addSeries(LC.LineSeries, { color: c.accent, lineWidth: 1, priceLineVisible: false, lastValueVisible: false }, pane);
      m.setData(pts("macd"));
      const sig = chart.addSeries(LC.LineSeries, { color: c.div, lineWidth: 1, priceLineVisible: false, lastValueVisible: false }, pane);
      sig.setData(pts("macd_signal"));
      lowerSeries.push(hist, m, sig); pane++;
    }
    // Grow the chart by one panel height per lower pane so the price pane keeps its size.
    const lower = pane - 1;
    el.style.height = lower ? `calc(var(--chart-h) + ${lower * PANE_H}px)` : "";
    requestAnimationFrame(() => chart.panes().slice(1).forEach((p) => p.setHeight(PANE_H)));
  }

  function setRange(r) {
    state.range = r;
    document.querySelectorAll("[data-range]").forEach((b) => b.classList.toggle("active", b.dataset.range === r));
    const bars = data.ohlcv;
    if (!bars.length) return;
    if (r === "Max") { chart.timeScale().fitContent(); return; }
    const last = new Date(bars[bars.length - 1][0] + "T00:00:00Z");
    const n = parseInt(r, 10);
    const from = new Date(last);
    if (r.endsWith("M")) from.setUTCMonth(from.getUTCMonth() - n); else from.setUTCFullYear(from.getUTCFullYear() - n);
    let fromKey = from.toISOString().slice(0, 10);
    if (fromKey < bars[0][0]) fromKey = bars[0][0];
    chart.timeScale().setVisibleRange({ from: fromKey, to: bars[bars.length - 1][0] });
  }

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const dmy = (key) => `${key.slice(8, 10)}/${key.slice(5, 7)}/${key.slice(0, 4)}`;   // YYYY-MM-DD -> DD/MM/YYYY
  const barLabel = (key) => (state.interval === "W" ? `Week of ${dmy(key)}`
    : state.interval === "M" ? `${MONTHS[+key.slice(5, 7) - 1]} ${key.slice(0, 4)}` : dmy(key));

  function showLegend(key) {
    const b = barsByDate.get(key);
    if (!b) { legend.innerHTML = "Hover or tap the chart to see the prices and events."; return; }
    const prev = b.i > 0 ? data.ohlcv[b.i - 1][4] : null;
    const chg = prev ? (b.bar[4] / prev - 1) * 100 : null;
    let html = `<strong>${esc(barLabel(key))}</strong> · O ${fmtPrice(b.bar[1])} H ${fmtPrice(b.bar[2])} L ${fmtPrice(b.bar[3])} ` +
      `C <strong>${fmtPrice(b.bar[4])}</strong>` +
      (chg == null ? "" : ` <span class="${chg >= 0 ? "up" : "down"}">${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%</span>`) +
      ` · Vol ${fmtVol(b.bar[5])}`;
    const sh = shortsByDate.get(key);
    if (sh != null) html += ` · Short ${sh.toFixed(2)}%`;
    const rv = rsiByDate.get(key);
    if (rv != null && state.ind.rsi) html += ` · RSI ${rv.toFixed(0)}`;
    const evs = (eventsByDate.get(key) || []).filter((e) =>
      (e.kind === "dividend" && state.overlays.dividend) || (e.kind === "announcement" && state.overlays.announcement) ||
      ((e.kind === "buy" || e.kind === "sell") && state.overlays.trades));
    if (evs.length) {
      html += "<ul>" + evs.map((e) => `<li><span class="dot ${e.kind === "dividend" ? "div" : e.kind === "announcement" ? "ann" : e.kind}"></span>` +
        (e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.text)}</a>` : esc(e.text)) + "</li>").join("") + "</ul>";
    }
    legend.innerHTML = html;
  }

  const shortsByDate = new Map();

  // (Re)load everything that depends on the bar interval. Daily data is what the page starts with.
  function applyData(payload) {
    // Drop every series from the previous interval first: their (e.g. daily) times would otherwise
    // stay on the time scale and stretch the visible range.
    indReq++; indData = null; rsiByDate.clear();
    buildOverlays();
    while (lowerSeries.length) chart.removeSeries(lowerSeries.pop());
    if (priceSeries) { chart.removeSeries(priceSeries); priceSeries = null; }
    data = payload;
    barsByDate.clear(); eventsByDate.clear(); shortsByDate.clear();
    data.ohlcv.forEach((bar, i) => barsByDate.set(bar[0], { bar, i }));
    data.events.forEach((e) => { if (!eventsByDate.has(e.date)) eventsByDate.set(e.date, []); eventsByDate.get(e.date).push(e); });
    data.shorts.forEach((s) => shortsByDate.set(s[0], s[1]));
    volSeries.setData(data.ohlcv.filter((b) => b[5] != null).map((b) => ({ time: b[0], value: b[5] })));
    buildPriceSeries();
    buildLowerPanes();
    setRange(state.range);
    showLegend(data.ohlcv[data.ohlcv.length - 1][0]);
    loadIndicators();
  }

  function init(payload) {
    el.innerHTML = "";
    if (!payload.ohlcv.length) { el.innerHTML = '<div class="muted chart-loading">No price history stored for this company.</div>'; return; }

    chart = LC.createChart(el, chartOptions());
    volSeries = chart.addSeries(LC.HistogramSeries, {
      priceScaleId: "vol", priceFormat: { type: "volume" }, color: colors().line, lastValueVisible: false, priceLineVisible: false,
    }, 0);
    volSeries.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    applyData(payload);

    chart.subscribeCrosshairMove((p) => { if (p && p.time) showLegend(timeKey(p.time)); });
    chart.subscribeClick((p) => { if (p && p.time) showLegend(timeKey(p.time)); });

    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener && mq.addEventListener("change", () => {
      chart.applyOptions(chartOptions());
      volSeries.applyOptions({ color: colors().line });
      buildPriceSeries(); buildOverlays(); buildLowerPanes();
    });
  }

  document.querySelectorAll("[data-range]").forEach((b) => b.addEventListener("click", () => setRange(b.dataset.range)));
  document.querySelectorAll("[data-overlay]").forEach((cb) => cb.addEventListener("change", () => {
    if (!chart) return;
    const k = cb.dataset.overlay;
    if (k === "candles") {
      state.candles = cb.checked;
      const vr = chart.timeScale().getVisibleRange();
      buildPriceSeries();
      if (vr) chart.timeScale().setVisibleRange(vr);
    } else if (k === "shorts") {
      state.overlays.shorts = cb.checked; buildLowerPanes();
    } else {
      state.overlays[k] = cb.checked; applyMarkers();
    }
  }));

  const api = (what) => `/api/company/${encodeURIComponent(el.dataset.code)}/${what}?interval=${state.interval}`;
  let indReq = 0;

  function loadIndicators() {
    const req = ++indReq;
    indData = null; rsiByDate.clear();
    buildOverlays();
    fetch(api("indicators"), { credentials: "same-origin" })
      .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then((d) => {
        if (req !== indReq) return;          // an interval switch happened meanwhile
        indData = d;
        (d.rsi14 || []).forEach((v, i) => { if (v != null) rsiByDate.set(d.dates[i], v); });
        buildOverlays(); buildLowerPanes();
      })
      .catch(() => { document.querySelectorAll("[data-ind]").forEach((cb) => { cb.disabled = true; }); });
  }

  document.querySelectorAll("[data-ind]").forEach((cb) => cb.addEventListener("change", () => {
    state.ind[cb.dataset.ind] = cb.checked;
    if (!chart || !indData) return;
    if (cb.dataset.ind === "rsi" || cb.dataset.ind === "macd") buildLowerPanes(); else buildOverlays();
  }));

  // Full screen: the whole chart block (controls + chart + legend). Native Fullscreen API where
  // available; otherwise (iPhone Safari) a fixed overlay. Esc or the button exits; F toggles.
  const wrap = document.getElementById("chart-wrap");
  const fsBtn = document.getElementById("chart-fs");
  const nativeFs = !!(wrap && wrap.requestFullscreen && document.fullscreenEnabled);
  let fallbackFs = false;
  const isFs = () => fallbackFs || (nativeFs && document.fullscreenElement === wrap);

  function afterFsChange() {
    const on = isFs();
    wrap.classList.toggle("is-fs", on);
    if (!on) wrap.classList.remove("show-opts");
    document.body.classList.toggle("fs-lock", fallbackFs);
    fsBtn.setAttribute("aria-pressed", String(on));
    fsBtn.textContent = on ? "✕ Exit full screen" : "⛶ Full screen";
    fsBtn.title = on ? "Exit full screen (Esc)" : "Full screen (F)";
    refit();
  }

  // Keep the same bars in view and the lower panes at their fixed height after the chart resizes.
  function refit() {
    if (!chart) return;
    const lr = chart.timeScale().getVisibleLogicalRange();
    requestAnimationFrame(() => requestAnimationFrame(() => {
      chart.panes().slice(1).forEach((p) => p.setHeight(PANE_H));
      if (lr) chart.timeScale().setVisibleLogicalRange(lr);
    }));
  }

  function toggleFs() {
    if (!wrap) return;
    if (fallbackFs) { fallbackFs = false; afterFsChange(); return; }
    if (nativeFs) {
      if (isFs()) document.exitFullscreen().catch(() => {});
      else wrap.requestFullscreen({ navigationUI: "hide" }).catch(() => { fallbackFs = true; afterFsChange(); });
    } else {
      fallbackFs = !fallbackFs;
      afterFsChange();
    }
  }

  if (wrap && fsBtn) {
    fsBtn.addEventListener("click", toggleFs);
    const optsBtn = document.getElementById("chart-opts");
    if (optsBtn) optsBtn.addEventListener("click", () => {
      const open = wrap.classList.toggle("show-opts");
      optsBtn.setAttribute("aria-expanded", String(open));
      optsBtn.textContent = open ? "Hide options" : "Options";
      refit();
    });
    if (nativeFs) document.addEventListener("fullscreenchange", afterFsChange);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && fallbackFs) { toggleFs(); return; }
      if ((e.key === "f" || e.key === "F") && !e.ctrlKey && !e.metaKey && !e.altKey &&
          !e.target.closest("input, textarea, select, [contenteditable]")) toggleFs();
    });
  }

  // Bar interval: Daily / Weekly / Monthly (built on the server from the daily prices).
  const UNITS = { D: "day", W: "week", M: "month" };
  let chartReq = 0;
  function changeInterval(iv) {
    if (iv === state.interval || !chart) return;
    state.interval = iv;
    document.querySelectorAll("[data-interval]").forEach((b) => b.classList.toggle("active", b.dataset.interval === iv));
    document.querySelectorAll("[data-unit]").forEach((s) => { s.textContent = UNITS[iv]; });
    const req = ++chartReq;
    fetch(api("chart"), { credentials: "same-origin" })
      .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then((p) => { if (req === chartReq && p.ohlcv.length) applyData(p); })
      .catch((e) => { legend.innerHTML = `<span class="warn-inline">Couldn't load the ${({ D: "daily", W: "weekly", M: "monthly" })[iv]} bars (${esc(e.message)}).</span>`; });
  }
  document.querySelectorAll("[data-interval]").forEach((b) => b.addEventListener("click", () => changeInterval(b.dataset.interval)));

  fetch(api("chart"), { credentials: "same-origin" })
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(init)
    .catch((e) => { el.innerHTML = `<div class="warn chart-loading">Couldn't load the chart (${esc(e.message)}).</div>`; });
})();
