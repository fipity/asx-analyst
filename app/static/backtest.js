// Backtest equity curves (weekly points) with lightweight-charts.
(function () {
  "use strict";
  const LC = window.LightweightCharts, el = document.getElementById("bt-chart");
  const raw = document.getElementById("bt-data");
  if (!LC || !el || !raw) return;
  const curves = JSON.parse(raw.textContent || "{}");
  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const palette = [css("--muted"), css("--accent"), css("--c-ann"), css("--c-div"), css("--up"), css("--down"), css("--c-short")];
  const chart = LC.createChart(el, {
    autoSize: true,
    layout: { background: { color: css("--card") }, textColor: css("--muted"), fontFamily: "system-ui, sans-serif" },
    grid: { vertLines: { color: css("--line") }, horzLines: { color: css("--line") } },
    rightPriceScale: { borderColor: css("--line") }, timeScale: { borderColor: css("--line") },
    localization: { dateFormat: "dd/MM/yyyy", priceFormatter: (v) => "$" + Math.round(v).toLocaleString() },
  });
  Object.entries(curves).forEach(([name, pts], i) => {
    const bench = name.startsWith("ASX 200 buy-and-hold");
    const s = chart.addSeries(LC.LineSeries, {
      color: palette[i % palette.length], lineWidth: bench ? 3 : 1.5, lineStyle: name.includes("price index") ? 2 : 0,
      title: name.replace(/ \(.*\)$/, ""), priceLineVisible: false,
    });
    s.setData(pts.map((p) => ({ time: p[0], value: p[1] })));
  });
  chart.timeScale().fitContent();
  const log = document.getElementById("bt-log");
  if (log) log.addEventListener("change", () => chart.priceScale("right").applyOptions({ mode: log.checked ? 1 : 0 }));
})();
