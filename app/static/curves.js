// Equity curves for any <div data-curves="json-script-id"> (paper portfolio vs benchmark).
(function () {
  "use strict";
  const LC = window.LightweightCharts;
  if (!LC) return;
  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  document.querySelectorAll("[data-curves]").forEach((el) => {
    const raw = document.getElementById(el.dataset.curves);
    if (!raw) return;
    const curves = JSON.parse(raw.textContent || "{}");
    const palette = [css("--accent"), css("--muted"), css("--c-ann"), css("--c-div")];
    const chart = LC.createChart(el, {
      autoSize: true,
      layout: { background: { color: css("--card") }, textColor: css("--muted"), fontFamily: "system-ui, sans-serif" },
      grid: { vertLines: { color: css("--line") }, horzLines: { color: css("--line") } },
      rightPriceScale: { borderColor: css("--line") }, timeScale: { borderColor: css("--line") },
      localization: { dateFormat: "dd/MM/yyyy", priceFormatter: (v) => "$" + Math.round(v).toLocaleString() },
    });
    (Array.isArray(curves) ? curves : Object.entries(curves)).forEach(([name, pts], i) => {
      if (!pts.length) return;
      const s = chart.addSeries(LC.LineSeries, {
        color: palette[i % palette.length], lineWidth: i === 0 ? 2.5 : 1.5, lineStyle: i === 0 ? 0 : 2,
        title: name, priceLineVisible: false,
      });
      s.setData(pts.map((p) => ({ time: p[0], value: p[1] })));
    });
    chart.timeScale().fitContent();
  });
})();
