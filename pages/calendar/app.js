/* Calendar panel frontend.
 * Talks to the plugin backend exclusively through the AstrBot plugin
 * page bridge (apiGet / apiPost — the bridge has no PUT/DELETE). */

(function () {
  const bridge = window.AstrBotPluginPage || window.AstrBotPlugin;
  if (!bridge) {
    document.body.innerHTML =
      '<p class="fatal">桥接不可用，请从 AstrBot WebUI 打开此面板。</p>';
    return;
  }

  const $ = (sel) => document.querySelector(sel);
  const MONTH_CN = [
    "一月", "二月", "三月", "四月", "五月", "六月",
    "七月", "八月", "九月", "十月", "十一月", "十二月",
  ];
  const WEEK_CN = ["日", "一", "二", "三", "四", "五", "六"];
  const COLOR_CN = { blue: "蓝", teal: "青", indigo: "靛", slate: "灰" };
  const HOUR_H = 52; // px per hour in day view
  const SNAP_MS = 15 * 60 * 1000;

  const S = {
    view: "month", // month | day | year
    y: 2026,
    m: 10, // 1-12
    d: 1,
    cfg: null,
    mode: "panel",
    range: { events: [], festival: [], meta: {} },
    cals: { festival: true, blue: true, teal: true, indigo: true, slate: true },
    editing: null,
    seq: 0,
    split: false, // month (2) + day (1) side-by-side
    aiHist: [], // {role, content} chat history for the AI dialog
    aiBusy: false,
    bg: { image: "", opacity: 0.3 },
  };

  // ── date helpers ────────────────────────────────────────────────
  const pad = (n) => String(n).padStart(2, "0");
  const iso = (y, m, d) => `${y}-${pad(m)}-${pad(d)}`;
  const dayKey = (dt) => iso(dt.getFullYear(), dt.getMonth() + 1, dt.getDate());
  const addDays = (dt, n) => {
    const x = new Date(dt);
    x.setDate(x.getDate() + n);
    return x;
  };
  const dayDiff = (a, b) =>
    Math.round((new Date(b) - new Date(a)) / 86400000);
  const isSameDay = (a, b) => dayKey(a) === dayKey(b);
  const startOfWeek = (dt) => addDays(dt, -dt.getDay());
  const hm = (min) => `${pad(Math.floor(min / 60))}:${pad(min % 60)}`;
  const minOfDay = (ms) => {
    const d = new Date(ms);
    return d.getHours() * 60 + d.getMinutes();
  };
  const toLocalMs = (str) => new Date(str.replace(" ", "T")).getTime();
  const fmtMs = (ms) => {
    const d = new Date(ms);
    return `${dayKey(d)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  const today = () => new Date();

  // ── toast / api ─────────────────────────────────────────────────
  let toastTimer = null;
  const toast = (msg) => {
    const el = $("#toast");
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (el.hidden = true), 2600);
  };
  const errText = (e) => e?.message || String(e) || "请求失败";
  const apiGet = (endpoint, params) => bridge.apiGet(endpoint, params || {});
  const apiPost = (endpoint, body) => bridge.apiPost(endpoint, body || {});

  // ── visibility filters ─────────────────────────────────────────
  const visible = (ev) => {
    if (ev.category === "festival") return S.cals.festival;
    return S.cals[ev.color] !== false;
  };
  const colorVar = (name) => `var(--c-${name || "blue"})`;

  // ── range loading ──────────────────────────────────────────────
  async function loadRange(from, to) {
    const seq = ++S.seq;
    try {
      const data = await apiGet("events", { from, to });
      if (seq !== S.seq) return false; // a newer load superseded this one
      S.range = {
        events: data.events || [],
        festival: data.festival || [],
        meta: data.meta || {},
      };
      return true;
    } catch (e) {
      toast(errText(e));
      return false;
    }
  }

  async function refreshCurrent() {
    if (S.view === "year") {
      await loadRange(`${S.y}-01-01`, `${S.y}-12-31`);
    } else if (S.view === "month") {
      const weeks = monthWeeks();
      await loadRange(dayKey(weeks[0][0]), dayKey(weeks[weeks.length - 1][6]));
    } else {
      const ws = startOfWeek(new Date(S.y, S.m - 1, S.d));
      await loadRange(dayKey(ws), dayKey(addDays(ws, 6)));
    }
    render();
  }

  // ── month view ─────────────────────────────────────────────────
  function monthWeeks() {
    const first = new Date(S.y, S.m - 1, 1);
    const offset = first.getDay();
    const dim = new Date(S.y, S.m, 0).getDate();
    const weeks = Math.ceil((offset + dim) / 7);
    const gridStart = new Date(S.y, S.m - 1, 1 - offset);
    const out = [];
    for (let w = 0; w < weeks; w++) {
      const days = [];
      for (let i = 0; i < 7; i++) days.push(addDays(gridStart, w * 7 + i));
      out.push(days);
    }
    return out;
  }

  function dayPills(key, out) {
    // Everything renders below the date: all-day events & festivals for
    // every day they cover, timed events starting this day. Overflow is
    // handled by fitPills().
    const rank = (it) =>
      it.all_day ? (it.category === "festival" ? 1 : 0) : 2;
    const items = [...S.range.events, ...S.range.festival]
      .filter(
        (it) =>
          visible(it) &&
          it.start.slice(0, 10) <= key &&
          it.end.slice(0, 10) >= key &&
          (it.all_day || it.start.slice(0, 10) === key),
      )
      .sort(
        (a, b) =>
          rank(a) - rank(b) ||
          a.start.localeCompare(b.start) ||
          a.title.localeCompare(b.title),
      );
    for (const it of items) out.push(it);
    return items.length;
  }

  function fitPills(root) {
    // Show as many event pills as fit in the cell, then a "+N" row, so
    // long titles are never clipped mid-glyph by the cell boundary.
    const ROW = 18; // 16px pill + 2px gap
    root.querySelectorAll(".pills").forEach((box) => {
      box.querySelectorAll(".pill-more").forEach((el) => el.remove());
      const kids = [...box.children];
      kids.forEach((k) => (k.style.display = ""));
      if (!kids.length) return;
      const max = Math.floor((box.clientHeight + 2) / ROW);
      if (kids.length <= max) return;
      const keep = Math.max(0, max - 1);
      kids.forEach((k, i) => {
        if (i >= keep) k.style.display = "none";
      });
      const more = document.createElement("div");
      more.className = "pill-more";
      more.textContent = `+${kids.length - keep}`;
      box.appendChild(more);
    });
  }

  let fitTimer = 0;
  window.addEventListener("resize", () => {
    clearTimeout(fitTimer);
    fitTimer = setTimeout(() => {
      if (S.view === "month" || S.split) fitPills($("#month-view"));
    }, 150);
  });

  function renderMonth() {
    const root = $("#month-view");
    root.innerHTML = "";
    const weeks = monthWeeks();
    const now = today();
    const nowKey = dayKey(now);
    for (const week of weeks) {
      const row = document.createElement("div");
      row.className = "week-row";
      for (const day of week) {
        const key = dayKey(day);
        const meta = S.range.meta[key] || {};
        const cell = document.createElement("div");
        cell.className = "cell";
        cell.dataset.date = key;
        if (day.getMonth() + 1 !== S.m) cell.classList.add("other");
        if (day > now) cell.classList.add("future");

        const head = document.createElement("div");
        head.className = "cell-head";
        const num = document.createElement("span");
        num.className = "num" + (key === nowKey ? " today" : "");
        num.textContent = day.getDate();
        head.appendChild(num);
        if (meta.badge === "休" || meta.badge === "班") {
          const b = document.createElement("span");
          b.className = "badge " + (meta.badge === "休" ? "rest" : "work");
          b.textContent = meta.badge;
          head.appendChild(b);
        }
        cell.appendChild(head);

        const lunar = document.createElement("div");
        lunar.className = "lunar";
        lunar.textContent = meta.lunar || "";
        cell.appendChild(lunar);

        const pills = [];
        dayPills(key, pills);
        const box = document.createElement("div");
        box.className = "pills";
        for (const p of pills) {
          const fest = p.category === "festival";
          const pill = document.createElement("div");
          pill.className =
            "pill-ev" + (fest ? " festival" : "") + (p.done ? " done" : "");
          pill.dataset.key = p.key;
          if (!fest) {
            const sw = document.createElement("span");
            sw.className = "swatch";
            sw.style.background = colorVar(p.color);
            pill.appendChild(sw);
          }
          const lbl = document.createElement("span");
          lbl.className = "lbl";
          lbl.textContent = p.title;
          pill.appendChild(lbl);
          box.appendChild(pill);
        }
        cell.appendChild(box);
        row.appendChild(cell);
      }
      root.appendChild(row);
    }
    fitPills(root);
  }

  // ── year view ──────────────────────────────────────────────────
  function renderYear() {
    const root = $("#year-grid");
    root.innerHTML = "";
    const now = today();
    const nowKey = dayKey(now);
    for (let m = 1; m <= 12; m++) {
      const card = document.createElement("div");
      card.className = "month-card";
      card.dataset.month = m;
      const h = document.createElement("h3");
      h.textContent = MONTH_CN[m - 1];
      card.appendChild(h);

      const wk = document.createElement("div");
      wk.className = "mini-week";
      for (const w of WEEK_CN) {
        const s = document.createElement("span");
        s.textContent = w;
        wk.appendChild(s);
      }
      card.appendChild(wk);

      const grid = document.createElement("div");
      grid.className = "mini";
      const first = new Date(S.y, m - 1, 1);
      const offset = first.getDay();
      const dim = new Date(S.y, m, 0).getDate();
      const cells = Math.ceil((offset + dim) / 7) * 7;
      for (let i = 0; i < cells; i++) {
        const day = addDays(first, i - offset);
        const key = dayKey(day);
        const cell = document.createElement("div");
        cell.className = "d";
        cell.textContent = day.getDate();
        if (day.getMonth() + 1 !== m) cell.classList.add("out");
        if (key === nowKey) cell.classList.add("today");
        const hasEv =
          S.range.events.some(
            (ev) => visible(ev) && ev.start.slice(0, 10) === key,
          ) ||
          (S.cals.festival &&
            S.range.festival.some((f) => f.start.slice(0, 10) === key));
        if (hasEv) {
          cell.classList.add("has");
          const dot = document.createElement("span");
          dot.className = "dot";
          cell.appendChild(dot);
        }
        cell.dataset.date = key;
        grid.appendChild(cell);
      }
      card.appendChild(grid);
      root.appendChild(card);
    }
  }

  // ── day view ───────────────────────────────────────────────────
  function layoutBlocks(items) {
    // Cluster overlapping events and assign equal-width columns.
    const sorted = [...items].sort((a, b) => a.s - b.s || b.e - a.e);
    const clusters = [];
    let cur = [];
    let curEnd = -1;
    for (const it of sorted) {
      if (cur.length && it.s >= curEnd) {
        clusters.push(cur);
        cur = [];
        curEnd = -1;
      }
      cur.push(it);
      curEnd = Math.max(curEnd, it.e);
    }
    if (cur.length) clusters.push(cur);
    for (const cluster of clusters) {
      const colEnds = [];
      for (const it of cluster) {
        let ci = colEnds.findIndex((end) => end <= it.s);
        if (ci < 0) {
          ci = colEnds.length;
          colEnds.push(it.e);
        } else {
          colEnds[ci] = it.e;
        }
        it.col = ci;
      }
      for (const it of cluster) it.cols = colEnds.length;
    }
    return sorted;
  }

  function renderDay() {
    const root = $("#day-view");
    root.innerHTML = "";
    const date = new Date(S.y, S.m - 1, S.d);
    const now = today();
    const nowKey = dayKey(now);
    const key = dayKey(date);
    const meta = S.range.meta[key] || {};
    const startHour = S.cfg?.day_start_hour ?? 8;

    // week strip
    const wsStart = startOfWeek(date);
    const strip = document.createElement("div");
    strip.className = "weekstrip";
    for (let i = 0; i < 7; i++) {
      const d = addDays(wsStart, i);
      const k = dayKey(d);
      const dm = S.range.meta[k] || {};
      const cell = document.createElement("div");
      cell.className = "ws-cell";
      if (k === key) cell.classList.add("sel");
      if (k === nowKey) cell.classList.add("today");
      cell.dataset.date = k;
      const w = document.createElement("span");
      w.className = "w";
      w.textContent = WEEK_CN[d.getDay()];
      const n = document.createElement("span");
      n.className = "n";
      n.textContent = d.getDate();
      cell.appendChild(w);
      cell.appendChild(n);
      if (dm.badge === "休" || dm.badge === "班") {
        const b = document.createElement("span");
        b.className = "b";
        b.textContent = dm.badge;
        if (dm.badge === "班") b.style.color = "var(--work-fg)";
        cell.appendChild(b);
      }
      strip.appendChild(cell);
    }
    root.appendChild(strip);

    // head
    const head = document.createElement("div");
    head.className = "dayhead";
    const t = document.createElement("div");
    t.className = "dh-title";
    t.textContent = `${S.y}年${S.m}月${S.d}日 - 周${WEEK_CN[date.getDay()]}`;
    const l = document.createElement("div");
    l.className = "dh-lunar";
    l.textContent = meta.header || meta.lunar || "";
    head.appendChild(t);
    head.appendChild(l);
    root.appendChild(head);

    // items on this day (inclusive by date part)
    const allItems = [...S.range.events, ...S.range.festival]
      .filter(visible)
      .filter((it) => it.start.slice(0, 10) <= key && it.end.slice(0, 10) >= key);
    const allday = allItems.filter((it) => it.all_day);
    const timed = allItems.filter((it) => !it.all_day);

    const adRow = document.createElement("div");
    adRow.className = "allday-row";
    const adLabel = document.createElement("div");
    adLabel.className = "ad-label";
    adLabel.textContent = "全天";
    const adItems = document.createElement("div");
    adItems.className = "ad-items";
    for (const it of allday) {
      const pill = document.createElement("div");
      pill.className = "pill-ev" + (it.done ? " done" : "");
      pill.dataset.key = it.key;
      if (it.category !== "festival") {
        const sw = document.createElement("span");
        sw.className = "swatch";
        sw.style.background = colorVar(it.color);
        pill.appendChild(sw);
      }
      const lbl = document.createElement("span");
      lbl.className = "lbl";
      lbl.textContent = it.title;
      pill.appendChild(lbl);
      adItems.appendChild(pill);
    }
    adRow.appendChild(adLabel);
    adRow.appendChild(adItems);
    root.appendChild(adRow);

    if (!allItems.length) {
      const empty = document.createElement("div");
      empty.className = "day-empty";
      empty.textContent = "这一天没有日程，点击时间段可新建";
      root.appendChild(empty);
    }

    // hours grid
    const hours = document.createElement("div");
    hours.className = "hours";
    const inner = document.createElement("div");
    inner.className = "hours-inner";
    const startMin = startHour * 60;
    for (let h = startHour; h <= 23; h++) {
      const line = document.createElement("div");
      line.className = "hourline";
      line.dataset.hour = h;
      const lb = document.createElement("div");
      lb.className = "hourlabel";
      lb.textContent = `${pad(h)}:00`;
      line.appendChild(lb);
      inner.appendChild(line);
    }
    inner.style.height = `${(24 - startHour) * HOUR_H + 10}px`;

    // event blocks
    const lane = document.createElement("div");
    lane.className = "evlane";
    lane.style.height = `${(24 - startHour) * HOUR_H}px`;
    const blocks = timed.map((it) => {
      const sMs = toLocalMs(it.start);
      const eMs = toLocalMs(it.end);
      return { it, sMs, eMs };
    });
    for (const b of blocks) {
      // clip into this day
      const day0 = new Date(date);
      day0.setHours(0, 0, 0, 0);
      const day1 = new Date(date);
      day1.setHours(24, 0, 0, 0);
      const cs = Math.max(b.sMs, day0.getTime());
      const ce = Math.min(b.eMs, day1.getTime());
      const sMin = (cs - day0.getTime()) / 60000;
      const eMin = (ce - day0.getTime()) / 60000;
      b.s = sMin;
      b.e = Math.max(eMin, sMin + 10);
      b.clippedStart = b.sMs < day0.getTime();
      b.clippedEnd = b.eMs > day1.getTime();
    }
    layoutBlocks(blocks);
    for (const b of blocks) {
      const el = document.createElement("div");
      el.className = "evblock" + (b.it.done ? " done" : "");
      el.dataset.key = b.it.key;
      el.style.top = `${((b.s - startMin) / 60) * HOUR_H}px`;
      el.style.height = `${Math.max(((b.e - b.s) / 60) * HOUR_H, 18)}px`;
      el.style.left = `calc(${(b.col * 100) / b.cols}% + 2px)`;
      el.style.width = `calc(${100 / b.cols}% - 5px)`;
      el.style.borderLeftColor = colorVar(b.it.color);
      const et = document.createElement("div");
      et.className = "et";
      et.textContent = (b.clippedStart ? "↑ " : "") + b.it.title;
      const eh = document.createElement("div");
      eh.className = "eh";
      eh.textContent = `${b.clippedStart ? "昨天 " : ""}${hm(
        minOfDay(b.sMs),
      )}-${b.clippedEnd ? "次日 " : ""}${hm(minOfDay(b.eMs))}`;
      el.appendChild(et);
      el.appendChild(eh);
      lane.appendChild(el);
      attachDrag(el, b, startMin, date);
    }
    inner.appendChild(lane);

    // now line
    if (key === nowKey) {
      const nowMin = now.getHours() * 60 + now.getMinutes();
      if (nowMin >= startMin) {
        const nl = document.createElement("div");
        nl.className = "nowline";
        nl.style.top = `${((nowMin - startMin) / 60) * HOUR_H}px`;
        inner.appendChild(nl);
      }
    }
    hours.appendChild(inner);
    root.appendChild(hours);
  }

  // ── day view drag / resize ─────────────────────────────────────
  function attachDrag(el, b, startMin, date) {
    el.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      const mode =
        e.clientY > rect.bottom - 8
          ? "resize-end"
          : e.clientY < rect.top + 8
            ? "resize-start"
            : "move";
      const startY = e.clientY;
      const dur = b.eMs - b.sMs;
      const day0 = new Date(date).setHours(0, 0, 0, 0);
      const dayEnd = day0 + 86400000;
      let moved = false;
      let ns = b.sMs;
      let ne = b.eMs;
      const ehEl = el.querySelector(".eh");

      const move = (ev2) => {
        const dyMin = ((ev2.clientY - startY) / HOUR_H) * 60;
        if (!moved && Math.abs(ev2.clientY - startY) < 4) return;
        moved = true;
        el.classList.add("dragging");
        const snap = (min) =>
          Math.round((day0 + min * 60000) / SNAP_MS) * SNAP_MS;
        if (mode === "move") {
          const base = Math.round((b.sMs - day0) / 60000) + dyMin;
          ns = snap(
            Math.min(Math.max(base, startMin), 1440 - dur / 60000),
          );
          ne = ns + dur;
        } else if (mode === "resize-end") {
          const base = Math.round((b.eMs - day0) / 60000) + dyMin;
          ne = snap(Math.min(Math.max(base, Math.round((b.sMs - day0) / 60000) + 15), 1440));
          ns = b.sMs;
        } else {
          const base = Math.round((b.sMs - day0) / 60000) + dyMin;
          ns = snap(Math.min(Math.max(base, startMin), Math.round((b.eMs - day0) / 60000) - 15));
          ne = b.eMs;
        }
        el.style.top = `${(((ns - day0) / 60000 - startMin) / 60) * HOUR_H}px`;
        el.style.height = `${Math.max((((ne - ns) / 60000) / 60) * HOUR_H, 18)}px`;
        ehEl.textContent = `${hm(Math.round((ns - day0) / 60000))}-${hm(
          Math.round((ne - day0) / 60000),
        )}`;
      };

      const up = async () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        if (!moved) {
          openEdit(b.it);
          return;
        }
        try {
          await apiPost("events/update", {
            id: b.it.id,
            start: fmtMs(ns),
            end: fmtMs(ne),
          });
          toast("已调整时间");
          await refreshCurrent();
        } catch (err) {
          toast(errText(err));
          await refreshCurrent();
        }
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });
  }

  // ── render dispatcher ──────────────────────────────────────────
  function render() {
    const v = S.split ? "month" : S.view;
    $(".views").classList.toggle("split", S.split);
    $("#view-year").hidden = v !== "year";
    $("#view-month").hidden = v !== "month";
    $("#view-day").hidden = !(S.split || S.view === "day");
    const showWk = S.split || S.view === "month";
    $("#weekday-row").style.visibility = showWk ? "visible" : "hidden";
    $("#weekday-row").style.height = showWk ? "" : "0";
    $("#digest").hidden = !(
      S.view !== undefined &&
      S.mode === "panel" &&
      !S.split
    );

    const ctx = $("#btn-ctx");
    const yStep = [$("#btn-year-prev"), $("#btn-year-next")];
    if (v === "day") {
      ctx.hidden = false;
      ctx.textContent = `‹ ${MONTH_CN[S.m - 1]}`;
      $("#big-title").hidden = false;
      $("#big-title").textContent = `${S.m}月${S.d}日`;
      yStep.forEach((b) => (b.hidden = true));
    } else if (v === "month") {
      ctx.hidden = false;
      ctx.textContent = `‹ ${S.y}年`;
      $("#big-title").hidden = false;
      $("#big-title").textContent = MONTH_CN[S.m - 1];
      yStep.forEach((b) => (b.hidden = true));
    } else {
      ctx.hidden = true;
      $("#big-title").hidden = false;
      $("#big-title").textContent = `${S.y}年`;
      yStep.forEach((b) => (b.hidden = false));
    }

    if (v === "month") renderMonth();
    else if (v === "year") renderYear();
    else renderDay();
    if (S.split) renderDay();
  }

  const syncSplitBtn = () => $("#btn-split").classList.toggle("on", S.split);

  async function setView(view) {
    S.view = view;
    S.split = false;
    syncSplitBtn();
    await refreshCurrent();
  }

  // ── modal ──────────────────────────────────────────────────────
  function buildColorSwatches() {
    const wrap = $("#f-colors");
    wrap.innerHTML = "";
    const colors = S.cfg?.colors || ["blue", "teal", "indigo", "slate"];
    for (const c of colors) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "swatch-btn";
      btn.dataset.color = c;
      btn.style.background = colorVar(c);
      btn.title = COLOR_CN[c] || c;
      btn.addEventListener("click", () => {
        wrap.querySelectorAll(".swatch-btn").forEach((x) => x.classList.remove("on"));
        btn.classList.add("on");
      });
      wrap.appendChild(btn);
    }
  }

  function dtLocalValue(str) {
    return str ? str.slice(0, 16).replace(" ", "T") : "";
  }

  function openCreate(dateKey, hour) {
    S.editing = null;
    $("#modal-title").textContent = "新建日程";
    $("#f-submit").textContent = "添加";
    $("#f-del").hidden = true;
    $("#f-title").value = "";
    $("#f-allday").checked = false;
    const hk = dateKey || iso(S.y, S.m, S.d);
    const h = Math.min(hour ?? (S.cfg?.day_start_hour ?? 9), 23);
    $("#f-start").value = `${hk}T${pad(h)}:00`;
    $("#f-end").value = `${hk}T${h < 23 ? pad(h + 1) : "23"}:${
      h < 23 ? "00" : "59"
    }`;
    $("#f-repeat").value = "none";
    const defRemind = String(S.cfg?.remind_default_minutes ?? 15);
    const remindSel = $("#f-remind");
    remindSel.value = [...remindSel.options].some((o) => o.value === defRemind)
      ? defRemind
      : "15";
    $("#f-note").value = "";
    buildColorSwatches();
    $("#f-colors").querySelector(".swatch-btn")?.classList.add("on");
    $("#modal").hidden = false;
    $("#f-title").focus();
  }

  function openEdit(item) {
    if (item.category === "festival") {
      S.d = Number(item.start.slice(8, 10));
      S.m = Number(item.start.slice(5, 7));
      S.y = Number(item.start.slice(0, 4));
      setView("day");
      return;
    }
    S.editing = item;
    $("#modal-title").textContent = "编辑日程";
    $("#f-submit").textContent = "保存";
    $("#f-del").hidden = false;
    $("#f-del").textContent = "删除";
    $("#f-del").dataset.armed = "";
    $("#f-title").value = item.title || "";
    $("#f-allday").checked = !!item.all_day;
    $("#f-start").value = dtLocalValue(item.start);
    $("#f-end").value = dtLocalValue(item.end);
    $("#f-repeat").value = item.repeat || "none";
    const rv = String(item.remind_minutes ?? 0);
    const remindSel = $("#f-remind");
    if (![...remindSel.options].some((o) => o.value === rv)) {
      remindSel.add(new Option(`提前 ${rv} 分钟`, rv));
    }
    remindSel.value = rv;
    $("#f-note").value = item.note || "";
    buildColorSwatches();
    const sw = $("#f-colors").querySelector(
      `.swatch-btn[data-color="${item.color || "blue"}"]`,
    );
    (sw || $("#f-colors").querySelector(".swatch-btn"))?.classList.add("on");
    $("#modal").hidden = false;
    $("#f-title").focus();
  }

  function closeModal() {
    $("#modal").hidden = true;
    S.editing = null;
  }

  $("#f-allday").addEventListener("change", () => {
    if ($("#f-allday").checked) {
      const s = $("#f-start").value.slice(0, 10);
      const e = $("#f-end").value.slice(0, 10);
      if (s) $("#f-start").value = `${s}T00:00`;
      $("#f-end").value = `${e || s}T23:59`;
    }
  });

  $("#f-cancel").addEventListener("click", closeModal);
  $("#modal").addEventListener("click", (e) => {
    if (e.target === $("#modal")) closeModal();
  });

  $("#f-del").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    if (btn.dataset.armed !== "1") {
      btn.dataset.armed = "1";
      btn.textContent = "确认删除？";
      return;
    }
    if (!S.editing) return;
    try {
      await apiPost("events/delete", { id: S.editing.id });
      closeModal();
      toast("已删除");
      await refreshCurrent();
    } catch (err) {
      toast(errText(err));
    }
  });

  $("#ev-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const title = $("#f-title").value.trim();
    if (!title) {
      toast("标题不能为空");
      return;
    }
    const allDay = $("#f-allday").checked;
    let start = $("#f-start").value.replace("T", " ");
    let end = $("#f-end").value.replace("T", " ");
    if (allDay) {
      start = `${start.slice(0, 10)} 00:00`;
      end = `${end.slice(0, 10)} 23:59`;
    }
    if (!start || !end || end <= start) {
      toast("结束时间必须晚于开始时间");
      return;
    }
    const color =
      $("#f-colors").querySelector(".swatch-btn.on")?.dataset.color || "blue";
    const body = {
      title,
      start,
      end,
      all_day: allDay,
      repeat: $("#f-repeat").value,
      remind_minutes: parseInt($("#f-remind").value, 10) || 0,
      color,
      note: $("#f-note").value,
    };
    try {
      if (S.editing) {
        await apiPost("events/update", { id: S.editing.id, ...body });
        toast("已保存");
      } else {
        await apiPost("events", body);
        toast("已添加日程");
      }
      closeModal();
      await refreshCurrent();
    } catch (err) {
      toast(errText(err));
    }
  });

  // ── navigation ─────────────────────────────────────────────────
  $("#btn-ctx").addEventListener("click", () => {
    if (S.view === "day") setView("month");
    else if (S.view === "month") setView("year");
  });
  $("#btn-view").addEventListener("click", () => {
    if (S.split) {
      S.split = false;
      syncSplitBtn();
    }
    if (S.view === "day") setView("month");
    else setView("day");
  });
  $("#btn-split").addEventListener("click", async () => {
    S.split = !S.split;
    if (S.split && S.view !== "month") S.view = "month";
    syncSplitBtn();
    try {
      await refreshCurrent();
    } catch (e) {
      toast(errText(e));
    }
  });
  $("#btn-today").addEventListener("click", () => {
    const n = today();
    S.y = n.getFullYear();
    S.m = n.getMonth() + 1;
    S.d = n.getDate();
    if (S.view === "year") S.view = "month";
    refreshCurrent();
  });
  $("#btn-year-prev").addEventListener("click", () => {
    S.y -= 1;
    refreshCurrent();
  });
  $("#btn-year-next").addEventListener("click", () => {
    S.y += 1;
    refreshCurrent();
  });
  $("#btn-add").addEventListener("click", () => openCreate());

  // month / year / day cell clicks
  $("#view-month").addEventListener("click", (e) => {
    const pill = e.target.closest(".pill-ev");
    if (pill) {
      const item = [...S.range.events, ...S.range.festival].find(
        (x) => x.key === pill.dataset.key,
      );
      if (item) openEdit(item);
      return;
    }
    const cell = e.target.closest(".cell");
    if (cell) {
      const [y, m, d] = cell.dataset.date.split("-").map(Number);
      S.y = y;
      S.m = m;
      S.d = d;
      if (S.split) refreshCurrent();
      else setView("day");
    }
  });
  $("#view-year").addEventListener("click", (e) => {
    const dEl = e.target.closest(".d");
    if (dEl?.dataset.date) {
      const [y, m, d] = dEl.dataset.date.split("-").map(Number);
      S.y = y;
      S.m = m;
      S.d = d;
      setView("day");
      return;
    }
    const card = e.target.closest(".month-card");
    if (card) {
      S.m = Number(card.dataset.month);
      setView("month");
    }
  });
  $("#view-day").addEventListener("click", (e) => {
    const ws = e.target.closest(".ws-cell");
    if (ws) {
      const [y, m, d] = ws.dataset.date.split("-").map(Number);
      S.y = y;
      S.m = m;
      S.d = d;
      refreshCurrent();
      return;
    }
    const pill = e.target.closest(".pill-ev");
    if (pill) {
      const item = [...S.range.events, ...S.range.festival].find(
        (x) => x.key === pill.dataset.key,
      );
      if (item) openEdit(item);
      return;
    }
    if (e.target.closest(".evblock")) return; // handled by drag pointerup
    const line = e.target.closest(".hourline");
    if (line) openCreate(iso(S.y, S.m, S.d), Number(line.dataset.hour));
  });

  // ── search ─────────────────────────────────────────────────────
  let searchTimer = null;
  $("#btn-search").addEventListener("click", () => {
    const box = $("#searchbox");
    box.hidden = !box.hidden;
    if (!box.hidden) {
      $("#search-input").focus();
    } else {
      $("#search-results").hidden = true;
    }
  });
  $("#search-input").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    const q = e.target.value.trim();
    searchTimer = setTimeout(async () => {
      const box = $("#search-results");
      if (!q) {
        box.hidden = true;
        return;
      }
      try {
        const res = await apiGet("events", { q });
        const list = res.events || [];
        box.innerHTML = "";
        if (!list.length) {
          const d = document.createElement("div");
          d.className = "search-empty";
          d.textContent = "没有匹配的日程";
          box.appendChild(d);
        } else {
          list.forEach((r, i) => {
            const btn = document.createElement("button");
            btn.type = "button";
            const t = document.createElement("span");
            t.textContent = r.title;
            const w = document.createElement("span");
            w.className = "when";
            const nx = (r.next || r.start).slice(0, 10);
            w.textContent = `${nx}${r.repeat && r.repeat !== "none" ? " · 重复" : ""}`;
            btn.appendChild(t);
            btn.appendChild(w);
            btn.addEventListener("click", () => {
              const [y, m, d] = nx.split("-").map(Number);
              S.y = y;
              S.m = m;
              S.d = d;
              $("#searchbox").hidden = true;
              box.hidden = true;
              setView("day");
            });
            box.appendChild(btn);
          });
        }
        box.hidden = false;
      } catch (err) {
        toast(errText(err));
      }
    }, 250);
  });

  // ── AI dialog ─────────────────────────────────────────────────
  const AI_HINT_HTML = $("#ai-msgs").innerHTML;
  const aiAdd = (text, cls) => {
    const d = document.createElement("div");
    d.className = `ai-msg ${cls}`;
    d.textContent = text;
    const box = $("#ai-msgs");
    box.appendChild(d);
    box.scrollTop = box.scrollHeight;
    return d;
  };
  const closeAi = () => {
    $("#ai-modal").hidden = true;
  };
  $("#btn-ai").addEventListener("click", () => {
    $("#ai-modal").hidden = false;
    $("#ai-text").focus();
  });
  $("#ai-close").addEventListener("click", closeAi);
  $("#ai-modal").addEventListener("click", (e) => {
    if (e.target === $("#ai-modal")) closeAi();
  });
  $("#ai-clear").addEventListener("click", () => {
    S.aiHist = [];
    $("#ai-msgs").innerHTML = AI_HINT_HTML;
  });
  $("#ai-text").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shift && !e.isComposing) {
      e.preventDefault();
      $("#ai-form").requestSubmit();
    }
  });
  $("#ai-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (S.aiBusy) return;
    const text = $("#ai-text").value.trim();
    if (!text) return;
    $("#ai-text").value = "";
    $("#ai-msgs").querySelector(".ai-hint")?.remove();
    aiAdd(text, "user");
    S.aiHist.push({ role: "user", content: text });
    if (S.aiHist.length > 40) S.aiHist = S.aiHist.slice(-40);
    const think = aiAdd("思考中…", "think");
    S.aiBusy = true;
    $("#ai-send").disabled = true;
    try {
      const history = S.aiHist.slice(0, -1).slice(-10);
      const res = await apiPost("ai", { text, history });
      think.remove();
      const reply = (res.reply || "").trim() || "（无回复）";
      aiAdd(reply, "bot");
      S.aiHist.push({ role: "assistant", content: reply });
      if (res.events && res.events.length) {
        toast(`AI 已创建 ${res.events.length} 个日程`);
        await refreshCurrent();
      }
      if (res.errors && res.errors.length) toast(res.errors[0]);
    } catch (err) {
      think.remove();
      const msg = `出错了：${errText(err)}`;
      aiAdd(msg, "bot");
      S.aiHist.push({ role: "assistant", content: msg });
    } finally {
      S.aiBusy = false;
      $("#ai-send").disabled = false;
      $("#ai-text").focus();
    }
  });

  // ── background image ──────────────────────────────────────────
  const applyBg = () => {
    const el = $("#bg-layer");
    if (S.bg && S.bg.image) {
      el.style.backgroundImage = `url("${S.bg.image}")`;
      el.style.opacity = String(S.bg.opacity ?? 0.3);
      el.hidden = false;
    } else {
      el.hidden = true;
      el.style.backgroundImage = "";
    }
  };
  const readDataURL = (file) =>
    new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve(fr.result);
      fr.onerror = () => reject(new Error("读取图片失败"));
      fr.readAsDataURL(file);
    });
  $("#btn-bg").addEventListener("click", () => {
    $("#bg-file").value = "";
    const op = Math.min(
      60,
      Math.max(10, Math.round((S.bg?.opacity ?? 0.3) * 100)),
    );
    $("#bg-op").value = String(op);
    $("#bg-op-val").textContent = `${op}%`;
    $("#bg-remove").hidden = !(S.bg && S.bg.image);
    $("#bg-modal").hidden = false;
  });
  $("#bg-op").addEventListener("input", (e) => {
    $("#bg-op-val").textContent = `${e.target.value}%`;
  });
  $("#bg-cancel").addEventListener("click", () => {
    $("#bg-modal").hidden = true;
  });
  $("#bg-modal").addEventListener("click", (e) => {
    if (e.target === $("#bg-modal")) $("#bg-modal").hidden = true;
  });
  $("#bg-remove").addEventListener("click", async () => {
    try {
      await apiPost("bg/clear", {});
      S.bg = { ...S.bg, image: "" };
      applyBg();
      $("#bg-modal").hidden = true;
      toast("已移除背景");
    } catch (err) {
      toast(errText(err));
    }
  });
  $("#bg-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const opacity = Number($("#bg-op").value) / 100;
    const file = $("#bg-file").files[0];
    try {
      if (file) {
        const ok = ["image/png", "image/jpeg", "image/webp", "image/gif"];
        if (!ok.includes(file.type)) {
          toast("仅支持 PNG/JPEG/WebP/GIF 图片");
          return;
        }
        if (file.size > 8 * 1024 * 1024) {
          toast("图片不能超过 8MB");
          return;
        }
        const dataURL = await readDataURL(file);
        const res = await apiPost("bg", { image: dataURL, opacity });
        S.bg = { image: res.image || dataURL, opacity: res.opacity ?? opacity };
      } else {
        const res = await apiPost("bg", { opacity });
        S.bg = { image: S.bg?.image || "", opacity: res.opacity ?? opacity };
      }
      applyBg();
      $("#bg-modal").hidden = true;
      toast("背景已更新");
    } catch (err) {
      toast(errText(err));
    }
  });

  // ── calendar filter popover ────────────────────────────────────
  $("#btn-cals").addEventListener("click", (e) => {
    e.stopPropagation();
    const pop = $("#cal-pop");
    if (!pop.hidden) {
      pop.hidden = true;
      return;
    }
    const counts = { festival: S.range.festival.length };
    for (const c of Object.keys(COLOR_CN)) counts[c] = 0;
    for (const ev of S.range.events) {
      if (counts[ev.color] !== undefined) counts[ev.color] += 1;
    }
    const items = [
      { key: "festival", label: "节日·节气", color: "" },
      ...Object.keys(COLOR_CN).map((c) => ({
        key: c,
        label: COLOR_CN[c],
        color: colorVar(c),
      })),
    ];
    pop.innerHTML = "";
    for (const it of items) {
      const label = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = S.cals[it.key] !== false;
      cb.addEventListener("change", () => {
        S.cals[it.key] = cb.checked;
        render();
      });
      label.appendChild(cb);
      if (it.color) {
        const sw = document.createElement("span");
        sw.className = "sw";
        sw.style.background = it.color;
        label.appendChild(sw);
      }
      const txt = document.createElement("span");
      txt.textContent = it.label;
      label.appendChild(txt);
      const cnt = document.createElement("span");
      cnt.className = "cnt";
      cnt.textContent = String(counts[it.key] || 0);
      label.appendChild(cnt);
      pop.appendChild(label);
    }
    pop.hidden = false;
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".cal-fab-wrap")) $("#cal-pop").hidden = true;
    if (
      !e.target.closest("#searchbox") &&
      !e.target.closest("#btn-search")
    ) {
      $("#searchbox").hidden = true;
      $("#search-results").hidden = true;
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!$("#modal").hidden) closeModal();
      if (!$("#ai-modal").hidden) $("#ai-modal").hidden = true;
      if (!$("#bg-modal").hidden) $("#bg-modal").hidden = true;
      $("#searchbox").hidden = true;
      $("#cal-pop").hidden = true;
    }
  });

  // ── panel / window switch ──────────────────────────────────────
  $("#btn-pos").addEventListener("click", async () => {
    try {
      const d = await apiGet("display");
      const next = d.mode === "panel" ? "window" : "panel";
      await apiPost("display/mode", { mode: next });
      S.mode = next;
      render();
      toast(next === "window" ? "已切换到桌面小窗" : "已切回面板");
      refreshDigest();
    } catch (err) {
      toast(errText(err));
    }
  });

  // ── digest aside ───────────────────────────────────────────────
  async function refreshDigest() {
    try {
      const d = await apiGet("digest");
      $("#dg-day").textContent = d.day;
      $("#dg-sub").textContent = `${d.weekday} · ${d.lunar}`;
      const ul = $("#dg-lines");
      ul.innerHTML = "";
      const lines = d.lines || [];
      $("#dg-empty").hidden = lines.length > 0;
      for (const ln of lines) {
        const li = document.createElement("li");
        if (ln.done) li.classList.add("done");
        const t = document.createElement("span");
        t.className = "t";
        t.textContent = ln.time;
        const n = document.createElement("span");
        n.className = "n";
        n.textContent = ln.title;
        li.appendChild(t);
        li.appendChild(n);
        ul.appendChild(li);
      }
    } catch (e) {
      // digest is decorative; ignore transient failures
    }
  }

  // ── boot ───────────────────────────────────────────────────────
  (async function boot() {
    const applyTheme = (isDark) => {
      document.documentElement.dataset.theme = isDark ? "dark" : "light";
    };
    applyTheme(!!bridge.context?.isDark);
    bridge.onContext?.((ctx) => applyTheme(!!ctx?.isDark));
    try {
      await bridge.ready();
    } catch (e) {
      /* ready may not exist on older bridges */
    }
    try {
      S.cfg = await apiGet("config");
      S.mode = S.cfg.display_mode || "panel";
    } catch (e) {
      toast(errText(e));
    }
    try {
      const bg = await apiGet("bg");
      S.bg = { image: bg.image || "", opacity: bg.opacity ?? 0.3 };
    } catch (e) {
      /* background is decorative */
    }
    applyBg();
    const n = today();
    S.y = n.getFullYear();
    S.m = n.getMonth() + 1;
    S.d = n.getDate();
    await refreshCurrent();
    refreshDigest();
    setInterval(() => {
      if (document.hidden || document.querySelector(".evblock.dragging")) return;
      refreshCurrent();
      refreshDigest();
    }, 60000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        refreshCurrent();
        refreshDigest();
      }
    });
  })();
})();
