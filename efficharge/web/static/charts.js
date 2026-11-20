// Small-multiple charts for the dashboard and the stats page.
// One series per chart, one shared time axis, no dual axes. Colours come from CSS
// custom properties (light/dark), so the palette lives in app.css only.
"use strict";

(function () {
  const root = document.getElementById("charts");
  if (!root || !window.Chart) return;

  const css = (name) => getComputedStyle(root).getPropertyValue(name).trim();
  const SERIES = { price: "--series-price", green: "--series-green", power: "--series-power",
                   eur: "--series-price", co2: "--series-green" };
  let charts = {};
  let payload = null;
  let loadedAt = 0;  // performance.now() when the payload was requested

  // A fast-forwarding demo: how far its clock has moved since the payload (ms), so "now"
  // glides between two refreshes. Stops where the server's fast-forward stops.
  function drift() {
    const c = payload && payload.clock;
    if (!c || c.speed <= 1) return 0;
    const d = (performance.now() - loadedAt) * c.speed;
    return c.until == null ? d : Math.min(d, Math.max(0, c.until - payload.now));
  }
  const edge = (b, end) => b[end] + (b.live && b.live.includes(end) ? drift() : 0);
  const rangeEdge = (i) => payload.range[i] + (payload.range_live && payload.range_live[i] ? drift() : 0);

  // ── time formatting in the user's time zone ──
  function fmt(opts) {
    return new Intl.DateTimeFormat(payload.locale, { timeZone: payload.timezone, ...opts });
  }
  // formatToParts, not format(): German formats a bare hour as "18 Uhr"
  const hourOf = (ms) => Number(fmt({ hour: "numeric", hourCycle: "h23" })
    .formatToParts(ms).find((p) => p.type === "hour").value);

  function localHourTicks(min, max, every) {
    const ticks = [];
    for (let t = Math.ceil(min / 3600e3) * 3600e3; t <= max; t += 3600e3) {
      if (hourOf(t) % every === 0) ticks.push({ value: t });
    }
    return ticks;
  }

  function tickLabel(ms) {
    const h = hourOf(ms);
    return h === 0 ? fmt({ weekday: "short" }).format(ms)
      : fmt({ hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(ms);
  }

  // ── bands (planned window, baseline, charging) and vertical lines (now, deadline) ──
  const bandsPlugin = {
    id: "ecBands",
    beforeDatasetsDraw(chart) {
      if (!payload || !payload.bands) return;
      const { ctx, chartArea: a, scales: { x } } = chart;
      ctx.save();
      // gray "right away" first, so an overlapping green best window stays on top
      const order = { baseline: 0, plan: 1, charging: 2, remaining: 3, next: 4 };
      const bands = [...payload.bands].sort((p, q) => order[p.kind] - order[q.kind]);
      for (const b of bands) {
        const x0 = Math.max(a.left, x.getPixelForValue(edge(b, "from")));
        const x1 = Math.min(a.right, x.getPixelForValue(edge(b, "to")));
        if (x1 <= x0) continue;
        if (b.kind === "next") {  // only a possibility: outlined, not filled
          ctx.strokeStyle = css("--good-ink");
          ctx.lineWidth = 1.5;
          ctx.setLineDash([4, 3]);
          ctx.strokeRect(x0 + 0.75, a.top + 0.75, x1 - x0 - 1.5, a.bottom - a.top - 1.5);
          ctx.setLineDash([]);
          continue;
        }
        ctx.fillStyle = css(`--band-${b.kind}`);
        ctx.fillRect(x0, a.top, x1 - x0, a.bottom - a.top);
      }
      ctx.restore();
    },
    afterDatasetsDraw(chart) {
      if (!payload || !payload.lines) return;
      const { ctx, chartArea: a, scales: { x } } = chart;
      ctx.save();
      ctx.font = `11px ${css("--font")}`;
      ctx.textBaseline = "top";
      const placed = [];
      for (const l of payload.lines) {
        const px = Math.round(x.getPixelForValue(l.kind === "now" ? l.x + drift() : l.x)) + 0.5;
        if (px < a.left || px > a.right) continue;
        ctx.strokeStyle = css(l.kind === "now" ? "--ink-primary" : "--ink-secondary");
        ctx.lineWidth = l.kind === "now" ? 1 : 1.5;
        ctx.beginPath(); ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); ctx.stroke();
        if (chart.canvas.dataset.chart === firstChartId()) {  // label once, on the top chart
          const w = ctx.measureText(l.label).width;
          const tx = px + 4 + w > a.right ? px - 4 - w : px + 4;
          // next free row: labels must not overlap (e.g. "now" and "ready by …")
          let row = 0;
          while (placed.some((p) => p.row === row && tx < p.x + p.w + 6 && p.x < tx + w + 6)) row++;
          placed.push({ x: tx, w, row });
          const ty = a.top + 2 + row * 15;
          ctx.fillStyle = css("--surface");  // keep text readable on top of bands and lines
          ctx.fillRect(tx - 2, ty - 1, w + 4, 14);
          ctx.fillStyle = css("--ink-secondary");
          ctx.fillText(l.label, tx, ty);
        }
      }
      ctx.restore();
    },
  };

  const firstChartId = () => payload.charts[0].id;

  // ── hover sync across the stacked charts ──
  function syncHover(source, evt) {
    const x = source.scales.x.getValueForPixel(evt.x);
    for (const c of Object.values(charts)) {
      if (c === source) continue;
      const pts = c.data.datasets[0].data;
      if (!pts.length) { c.tooltip.setActiveElements([], { x: 0, y: 0 }); c.update("none"); continue; }
      let best = 0;
      for (let i = 0; i < pts.length; i++) if (pts[i].x <= x) best = i;
      const el = [{ datasetIndex: 0, index: best }];
      c.setActiveElements(el);
      c.tooltip.setActiveElements(el, { x: c.scales.x.getPixelForValue(pts[best].x), y: 0 });
      c.update("none");
    }
  }

  function clearHover(source) {
    for (const c of Object.values(charts)) {
      if (c === source) continue;
      c.setActiveElements([]);
      c.tooltip.setActiveElements([], { x: 0, y: 0 });
      c.update("none");
    }
  }

  function makeChart(canvas, spec) {
    const color = css(SERIES[spec.id]);
    const isTimeline = Boolean(payload.range);
    const ink2 = css("--ink-secondary"), muted = css("--ink-muted");
    const grid = css("--gridline"), axis = css("--axis");
    const number = new Intl.NumberFormat(payload.locale, { maximumFractionDigits: 1 });
    return new Chart(canvas, {
      type: "line",
      data: { datasets: [{
        data: spec.points.map(([x, y]) => ({ x, y })),
        borderColor: color, backgroundColor: color, borderWidth: 2,
        stepped: spec.stepped ? true : false,  // value holds until the next point
        pointRadius: isTimeline ? 0 : 3, pointHoverRadius: 4, pointHitRadius: 12,
        fill: false,
      }] },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: "nearest", axis: "x", intersect: false },
        onHover: (evt, _els, chart) => isTimeline && syncHover(chart, evt),
        plugins: {
          legend: { display: false },  // one series per chart: the caption names it
          tooltip: {
            displayColors: false,
            callbacks: {
              title: (items) => items.length ? fmt(isTimeline
                ? { weekday: "short", hour: "2-digit", minute: "2-digit" }
                : { day: "numeric", month: "short", year: "numeric" }).format(items[0].parsed.x) : "",
              label: (item) => `${number.format(item.parsed.y)} ${spec.unit}`,
            },
          },
        },
        scales: {
          x: {
            type: "linear",
            min: payload.range ? rangeEdge(0) : (payload.xrange || [])[0],
            max: payload.range ? rangeEdge(1) : (payload.xrange || [])[1],
            grid: { color: grid }, border: { color: axis },
            afterBuildTicks: isTimeline ? (scale) => {
              const span = (scale.max - scale.min) / 3600e3;
              const narrow = scale.chart.width < 640;
              scale.ticks = localHourTicks(scale.min, scale.max, span > 30 || narrow ? 6 : 3);
            } : undefined,
            ticks: {
              color: muted, font: { size: 11 }, maxRotation: 0, autoSkip: !isTimeline,
              callback: (v) => {
                if (isTimeline) return tickLabel(v);
                const [a, b] = payload.xrange || [0, 0];
                return b - a < 3 * 86400e3  // short span: day + time, else just the date
                  ? fmt({ weekday: "short", hour: "2-digit", hourCycle: "h23" }).format(v)
                  : fmt({ day: "numeric", month: "short" }).format(v);
              },
            },
          },
          y: {
            min: spec.min, max: spec.max, beginAtZero: spec.min === 0,
            suggestedMax: spec.suggested_max,  // grows with the data, never cuts it
            grace: spec.max === undefined ? "10%" : 0,  // keep lines off the top border
            // same width in every chart, so the stacked plot areas (and bands) line up
            afterFit: (scale) => { scale.width = 44; },
            grid: { color: grid }, border: { display: false },
            ticks: { color: muted, font: { size: 11 }, maxTicksLimit: 4,
                     callback: (v) => number.format(v) },
          },
        },
      },
      plugins: [bandsPlugin],
    });
  }

  function captionFor(canvas, spec) {
    const cap = canvas.closest("figure").querySelector("figcaption");
    cap.textContent = `${spec.title} (${spec.unit})`;
    if (!spec.points.length) cap.textContent += " – " + (root.dataset.empty || "–");
  }

  function fillTable() {
    const tbody = root.querySelector(".table-view tbody");
    if (!tbody) return;
    const price = payload.charts.find((c) => c.id === "price");
    const green = new Map((payload.charts.find((c) => c.id === "green") || { points: [] })
      .points.map(([x, y]) => [x, y]));
    const f = fmt({ weekday: "short", hour: "2-digit", minute: "2-digit" });
    const n = new Intl.NumberFormat(payload.locale, { maximumFractionDigits: 1 });
    tbody.replaceChildren(...price.points.slice(0, -1).map(([x, y]) => {
      const tr = document.createElement("tr");
      for (const text of [f.format(x), green.has(x) ? n.format(green.get(x)) : "–", n.format(y)]) {
        const td = document.createElement("td");
        td.textContent = text;
        tr.appendChild(td);
      }
      return tr;
    }));
  }

  // The legend lists exactly the bands drawn (no "expected rest" when nothing charges).
  function renderLegend() {
    const legend = root.querySelector("#legend");
    if (!legend || !payload.bands) return;
    const seen = new Map();
    for (const b of payload.bands) if (!seen.has(b.kind)) seen.set(b.kind, b.label);
    legend.replaceChildren(...[...seen].flatMap(([kind, label]) => {
      const key = document.createElement("span");
      key.className = `key key-${kind}`;
      return [key, document.createTextNode(` ${label} `)];
    }));
  }

  function render() {
    renderLegend();
    for (const spec of payload.charts) {
      const canvas = root.querySelector(`canvas[data-chart="${spec.id}"]`);
      if (!canvas) continue;
      captionFor(canvas, spec);
      const existing = charts[spec.id];
      if (existing) {  // refresh in place: no flash, no layout jump
        existing.data.datasets[0].data = spec.points.map(([x, y]) => ({ x, y }));
        if (payload.range) {
          existing.options.scales.x.min = rangeEdge(0);
          existing.options.scales.x.max = rangeEdge(1);
        }
        existing.update("none");
      } else {
        charts[spec.id] = makeChart(canvas, spec);
        canvas.addEventListener("mouseleave", () => clearHover(charts[spec.id]));
      }
    }
    fillTable();
  }

  async function load() {
    try {
      const asked = performance.now();
      const res = await fetch(root.dataset.url, { credentials: "same-origin" });
      if (!res.ok) return;
      payload = await res.json();
      loadedAt = asked;
      render();
      if (payload.clock && payload.clock.speed > 1) glide();
    } catch (e) {
      console.warn("chart refresh failed", e);
    }
  }

  function rebuild() {  // colour scheme changed: recreate with the new CSS values
    for (const c of Object.values(charts)) c.destroy();
    charts = {};
    if (payload) render();
  }

  // While a demo fast-forwards: redraw about 30× a second with the moved "now" (the data
  // itself comes with the next refresh). Ends by itself at normal speed.
  let gliding = false;
  function glide() {
    if (gliding) return;
    gliding = true;
    let lastFrame = 0;
    const frame = (t) => {
      if (!payload || !payload.clock || payload.clock.speed <= 1 || !root.isConnected) {
        gliding = false;
        return;
      }
      if (t - lastFrame >= 33) {
        lastFrame = t;
        for (const c of Object.values(charts)) {
          if (payload.range_live && payload.range_live.some(Boolean)) {
            c.options.scales.x.min = rangeEdge(0);
            c.options.scales.x.max = rangeEdge(1);
            c.update("none");
          } else {
            c.draw();
          }
        }
      }
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  load();
  // Refresh rate follows the status card (data-chart-refresh): 60 s normally, 2 s while a
  // demo fast-forwards, so the "now" line glides instead of jumping.
  let last = Date.now();
  setInterval(() => {
    const status = document.getElementById("status");
    const every = Number((status && status.dataset.chartRefresh) || root.dataset.refresh || 0);
    if (every && Date.now() - last >= every * 1000) {
      last = Date.now();
      load();
    }
  }, 500);
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", rebuild);
  // Redraw as soon as the state changes (plugged in, new plan, charging, …), not only on
  // the timer: the status card carries a version of what it shows.
  let version = document.getElementById("status")?.dataset.version;
  document.body.addEventListener("htmx:afterSettle", () => {
    const current = document.getElementById("status")?.dataset.version;
    if (current !== version) {
      version = current;
      last = Date.now();
      load();
    }
  });
})();
