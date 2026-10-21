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
    customColors: null, // {primary, secondary} | null = default palette
    sideDiary: [], // 分栏侧栏：选中日期的日记
    sideDiaryDate: "",
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
    if (S.cals["own:" + (ev.owner || "")] === false) return false;
    return S.cals[ev.color] !== false;
  };
  const colorVar = (name) => `var(--c-${name || "blue"})`;
  const PERSON_DOT = ["#8b9bb4", "#b48b9e", "#9e9a8b", "#8bb4a2", "#a28bb4"];
  const personDot = (o) => {
    if (!o) return "#a7aebc";
    let h = 0;
    for (const ch of o) h = (h * 31 + ch.codePointAt(0)) >>> 0;
    return PERSON_DOT[h % PERSON_DOT.length];
  };
  function refreshOwnerList() {
    const dl = $("#f-owner-list");
    if (!dl) return;
    const owners = [
      ...new Set(S.range.events.map((e) => e.owner || "").filter(Boolean)),
    ];
    dl.innerHTML = "";
    for (const o of owners) {
      const opt = document.createElement("option");
      opt.value = o;
      dl.appendChild(opt);
    }
  }

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

  function renderDay(target) {
    const root = typeof target === "string" ? $(target) : target || $("#view-day");
    const keepScroll = root.querySelector(".hours")?.scrollTop || 0;
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
    if (keepScroll) hours.scrollTop = keepScroll;
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

  // ── day sidebar (desktop client style, elongated) ───────────────
  let sideSel = { id: null, confirmDel: false };

  function renderSide() {
    const root = $("#day-side");
    root.innerHTML = "";
    const date = new Date(S.y, S.m - 1, S.d);
    const key = dayKey(date);
    const meta = S.range.meta[key] || {};
    const isToday = key === dayKey(today());

    const num = document.createElement("div");
    num.className = "ds-num";
    num.textContent = String(S.d);
    root.appendChild(num);

    const sub = document.createElement("div");
    sub.className = "ds-sub";
    const lunar = meta.lunar || "";
    sub.textContent = lunar
      ? `${WEEK_CN[date.getDay()]} · ${lunar}`
      : WEEK_CN[date.getDay()];
    root.appendChild(sub);

    const evs = (S.range.events || [])
      .filter((e) => String(e.start).slice(0, 10) === key)
      .sort((a, b) => String(a.start).localeCompare(String(b.start)));
    if (sideSel.id && !evs.some((e) => e.id === sideSel.id)) {
      sideSel = { id: null, confirmDel: false };
    }

    const list = document.createElement("div");
    list.className = "ds-list";
    const dys =
      S.sideDiaryDate === key && Array.isArray(S.sideDiary) ? S.sideDiary : [];
    if (!evs.length && !dys.length) {
      const empty = document.createElement("div");
      empty.className = "ds-empty";
      empty.textContent = isToday ? "今天暂无安排" : "当天暂无安排";
      list.appendChild(empty);
    }
    for (const e of evs) {
      const pill = document.createElement("div");
      pill.className = "ds-pill" + (e.done ? " done" : "");
      if (sideSel.id === e.id) pill.classList.add("sel");
      pill.dataset.id = e.id;
      const t = document.createElement("span");
      t.className = "t";
      t.textContent =
        e.all_day || String(e.start).slice(0, 10) !== key
          ? "全天"
          : String(e.start).slice(11, 16);
      const n = document.createElement("span");
      n.className = "n";
      n.textContent = e.title || "(无标题)";
      pill.appendChild(t);
      pill.appendChild(n);
      if (sideSel.id === e.id) {
        const ok = document.createElement("button");
        ok.type = "button";
        ok.className = "ds-act ok";
        ok.dataset.act = "done";
        ok.textContent = "✓";
        const del = document.createElement("button");
        del.type = "button";
        del.className = "ds-act del" + (sideSel.confirmDel ? " armed" : "");
        del.dataset.act = "del";
        del.textContent = "✕";
        pill.appendChild(ok);
        pill.appendChild(del);
      }
      list.appendChild(pill);
    }
    if (dys.length) {
      const hd = document.createElement("div");
      hd.className = "ds-subhead";
      hd.textContent = `日记 · ${dys.length}`;
      list.appendChild(hd);
      for (const d of dys) {
        const p = document.createElement("div");
        p.className = "ds-pill diary";
        p.dataset.did = d.id;
        const t = document.createElement("span");
        t.className = "t";
        t.textContent = String(d.created_at || "").slice(11, 16) || "日记";
        const n = document.createElement("span");
        n.className = "n";
        n.textContent = d.title || "(无标题)";
        p.appendChild(t);
        p.appendChild(n);
        if (d.pinned) {
          const pk = document.createElement("span");
          pk.className = "t";
          pk.textContent = "★";
          pk.style.marginLeft = "auto";
          p.appendChild(pk);
        }
        list.appendChild(p);
      }
    }
    root.appendChild(list);

    const status = document.createElement("div");
    status.className = "ds-status";
    status.textContent = sideSel.id
      ? sideSel.confirmDel
        ? "再点 ✕ 一次确认删除"
        : "点 ✓ 标记完成 · ✕ 删除"
      : "点日程完成/删除 · 点日记打开";
    root.appendChild(status);
  }

  // ── render dispatcher ──────────────────────────────────────────
  function render() {
    if (CD.active || DI.active) return; // 标签页期间不重绘日历视图
    const sy = window.scrollY || document.documentElement.scrollTop || 0;
    const v = S.split ? "month" : S.view;
    $(".views").classList.toggle("split", S.split);
    $("#view-year").hidden = v !== "year";
    $("#view-month").hidden = v !== "month";
    $("#view-day").hidden = !(S.split || S.view === "day");
    const showWk = S.split || S.view === "month";
    $("#weekday-row").style.visibility = showWk ? "visible" : "hidden";
    $("#weekday-row").style.height = showWk ? "" : "0";

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
    if (S.split || v === "day") {
      $("#day-side").hidden = !S.split;
      $("#day-view").hidden = S.split;
      if (S.split) {
        renderSide();
        loadSideDiary();
      } else renderDay("#day-view");
    }

    // Re-rendering collapses scrollable content; restore the page scroll
    // so drag/refresh never yanks the whole page back to the top.
    if (sy > 0) {
      window.scrollTo(0, sy);
      requestAnimationFrame(() => window.scrollTo(0, sy));
    }
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
    $("#f-owner").value = "";
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
    refreshOwnerList();
    $("#modal").hidden = false;
    $("#f-title").focus();
  }

  function openEdit(item) {
    if (item.category === "festival") {
      S.d = Number(item.start.slice(8, 10));
      S.m = Number(item.start.slice(5, 7));
      S.y = Number(item.start.slice(0, 4));
      refreshCurrent();
      return;
    }
    S.editing = item;
    $("#modal-title").textContent = "编辑日程";
    $("#f-submit").textContent = "保存";
    $("#f-del").hidden = false;
    $("#f-del").textContent = "删除";
    $("#f-del").dataset.armed = "";
    $("#f-title").value = item.title || "";
    $("#f-owner").value = item.owner || "";
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
    refreshOwnerList();
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
      owner: $("#f-owner").value.trim(),
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
    if (CD.active) {
      setCountdownTab(false);
      return;
    }
    if (DI.active) {
      setDiaryTab(false);
      return;
    }
    if (S.view === "day") setView("month");
    else if (S.view === "month") setView("year");
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
      // 分栏模式：仅选中该天更新右侧列表；普通月视图：直接进入该天日视图
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
  const onDayClick = (e) => {
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
  };
  $("#view-day").addEventListener("click", onDayClick);
  $("#day-side").addEventListener("click", async (e) => {
    const act = e.target.closest(".ds-act");
    const pill = e.target.closest(".ds-pill");
    if (pill?.dataset.did) {
      openDiaryFromSide(pill.dataset.did);
      return;
    }
    if (!pill?.dataset.id) return;
    const id = pill.dataset.id;
    const ev = (S.range.events || []).find((x) => x.id === id);
    if (!ev) return;
    if (act?.dataset.act === "done") {
      try {
        await apiPost("events/update", { id, done: !ev.done });
        sideSel = { id: null, confirmDel: false };
        await refreshCurrent();
      } catch (err) {
        toast(errText(err));
      }
      return;
    }
    if (act?.dataset.act === "del") {
      if (!sideSel.confirmDel) {
        sideSel.confirmDel = true;
        renderSide();
        return;
      }
      try {
        await apiPost("events/delete", { id });
        sideSel = { id: null, confirmDel: false };
        await refreshCurrent();
      } catch (err) {
        toast(errText(err));
      }
      return;
    }
    if (sideSel.id === id) sideSel = { id: null, confirmDel: false };
    else sideSel = { id, confirmDel: false };
    renderSide();
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

  // ── custom colors ────────────────────────────────────────
  const PRESET_COLORS = [
    "#2563eb", "#0d9488", "#6366f1", "#64748b",
    "#dc2626", "#9333ea", "#ea580c", "#16a34a",
  ];
  const DEFAULT_PRIMARY = "#2563eb";
  const DEFAULT_SECONDARY = "#3e6bb0";
  const COLOR_VARS = [
    "--accent", "--today", "--rest-bg", "--rest-fg", "--work-bg", "--work-fg",
    "--deep-fill", "--deep-text", "--c-blue", "--c-teal", "--c-indigo", "--c-slate",
  ];
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const hexToRgb = (hex) => {
    const s = String(hex || "").trim().replace(/^#/, "");
    if (!/^[0-9a-fA-F]{6}$/.test(s)) return null;
    const n = parseInt(s, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  };
  const rgbToHsl = (r, g, b) => {
    r /= 255; g /= 255; b /= 255;
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
    const l = (mx + mn) / 2;
    let h = 0, s = 0;
    if (mx !== mn) {
      const d = mx - mn;
      s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
      if (mx === r) h = (g - b) / d + (g < b ? 6 : 0);
      else if (mx === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h *= 60;
    }
    return [h, s * 100, l * 100];
  };
  const wrapHue = (h) => Math.round(((h % 360) + 360) % 360);
  const deriveVars = (priHex, secHex, dark) => {
    const pr = hexToRgb(priHex) || hexToRgb(DEFAULT_PRIMARY);
    const sr = hexToRgb(secHex) || hexToRgb(DEFAULT_SECONDARY);
    const [ph, ps, pl] = rgbToHsl(pr[0], pr[1], pr[2]);
    const [bh, bs] = rgbToHsl(sr[0], sr[1], sr[2]);
    const ah = Math.round(ph);
    const bhue = wrapHue(bh);
    const priS = clamp(ps, 35, 85);
    const catS = clamp(bs, 35, 80);
    const accS = clamp(ps, 30, 90);
    const accL = dark ? clamp(pl < 58 ? pl + 12 : pl, 58, 72) : clamp(pl, 28, 56);
    const v = {};
    v["--accent"] = `hsl(${ah}, ${accS}%, ${accL}%)`;
    v["--today"] = `hsl(${ah}, ${dark ? priS : clamp(ps, 45, 85)}%, ${dark ? 60 : clamp(pl, 45, 60)}%)`;
    v["--rest-bg"] = `hsla(${ah}, ${priS}%, 50%, ${dark ? 0.16 : 0.12})`;
    v["--rest-fg"] = `hsl(${ah}, ${priS}%, ${dark ? 74 : 45}%)`;
    v["--work-bg"] = `hsla(${bhue}, ${catS}%, 50%, ${dark ? 0.16 : 0.1})`;
    v["--work-fg"] = `hsl(${bhue}, ${catS}%, ${dark ? 74 : 43}%)`;
    v["--deep-fill"] = dark
      ? `hsla(${bhue}, ${catS}%, 58%, 0.32)`
      : `hsl(${bhue}, ${clamp(bs * 0.7, 25, 70)}%, 93%)`;
    v["--deep-text"] = dark
      ? `hsl(${bhue}, 55%, 88%)`
      : `hsl(${bhue}, ${clamp(bs, 25, 60)}%, 28%)`;
    v["--c-blue"] = `hsl(${bhue}, ${catS}%, ${dark ? 66 : 44}%)`;
    v["--c-teal"] = `hsl(${wrapHue(bh - 30)}, ${catS}%, ${dark ? 62 : 38}%)`;
    v["--c-indigo"] = `hsl(${wrapHue(bh + 22)}, ${catS}%, ${dark ? 70 : 48}%)`;
    v["--c-slate"] = `hsl(${bhue}, ${clamp(bs * 0.3, 8, 30)}%, ${dark ? 62 : 45}%)`;
    return v;
  };
  const applyCustomColors = () => {
    const root = document.documentElement;
    const cc = S.customColors;
    if (!cc || !cc.primary || !cc.secondary) {
      for (const k of COLOR_VARS) root.style.removeProperty(k);
      return;
    }
    const dark = root.dataset.theme === "dark";
    for (const [k, val] of Object.entries(deriveVars(cc.primary, cc.secondary, dark))) {
      root.style.setProperty(k, val);
    }
  };
  const buildPaletteSwatches = (rowSel, inputSel) => {
    const row = $(rowSel);
    const input = $(inputSel);
    row.innerHTML = "";
    for (const hex of PRESET_COLORS) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "csw";
      b.style.background = hex;
      b.dataset.hex = hex;
      b.addEventListener("click", () => {
        input.value = hex;
        syncColorRow(rowSel, inputSel);
        refreshColorPreview();
      });
      row.appendChild(b);
    }
    syncColorRow(rowSel, inputSel);
  };
  const syncColorRow = (rowSel, inputSel) => {
    const cur = ($(inputSel).value || "").toLowerCase();
    $(rowSel).querySelectorAll(".csw").forEach((s) => {
      s.classList.toggle("on", s.dataset.hex === cur);
    });
  };
  const refreshColorPreview = () => {
    const pri = $("#color-primary").value;
    const sec = $("#color-secondary").value;
    const dark = document.documentElement.dataset.theme === "dark";
    const vars = deriveVars(pri, sec, dark);
    const wrap = $("#color-preview");
    wrap.innerHTML = "<span>预览</span>";
    const priDot = document.createElement("i");
    priDot.className = "pri";
    priDot.style.background = vars["--accent"];
    wrap.appendChild(priDot);
    for (const k of ["--c-blue", "--c-teal", "--c-indigo", "--c-slate"]) {
      const dot = document.createElement("i");
      dot.style.background = vars[k];
      wrap.appendChild(dot);
    }
  };
  const colorDist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
  const extractPalette = (file) =>
    new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        URL.revokeObjectURL(url);
        try {
          const N = 64;
          const cv = document.createElement("canvas");
          cv.width = N;
          cv.height = N;
          const ctx = cv.getContext("2d", { willReadFrequently: true });
          ctx.drawImage(img, 0, 0, N, N);
          const px = ctx.getImageData(0, 0, N, N).data;
          const buckets = new Map();
          for (let i = 0; i < px.length; i += 4) {
            if (px[i + 3] < 128) continue;
            const r = px[i], g = px[i + 1], b = px[i + 2];
            const lum = 0.299 * r + 0.587 * g + 0.114 * b;
            if (lum < 40 || lum > 235) continue; // skip near-black / near-white
            const key = ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4); // 4bit/channel
            const e = buckets.get(key);
            if (e) {
              e.n++; e.r += r; e.g += g; e.b += b;
            } else {
              buckets.set(key, { n: 1, r, g, b });
            }
          }
          const cand = [...buckets.values()]
            .sort((a, b) => b.n - a.n)
            .map((e) => [Math.round(e.r / e.n), Math.round(e.g / e.n), Math.round(e.b / e.n)]);
          if (!cand.length) {
            reject(new Error("未能从图片中提取到颜色"));
            return;
          }
          const hex = (c) =>
            "#" + c.map((x) => x.toString(16).padStart(2, "0")).join("");
          const primary = cand[0];
          const secondary = cand.find((c) => colorDist(c, primary) > 100);
          if (!secondary) {
            reject(new Error("图片色彩过于单一，请换一张图片"));
            return;
          }
          resolve({ primary: hex(primary), secondary: hex(secondary) });
        } catch (err) {
          reject(err);
        }
      };
      img.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error("读取图片失败"));
      };
      img.src = url;
    });
  $("#btn-color").addEventListener("click", () => {
    const pri = S.customColors?.primary || DEFAULT_PRIMARY;
    const sec = S.customColors?.secondary || DEFAULT_SECONDARY;
    $("#color-primary").value = pri;
    $("#color-secondary").value = sec;
    $("#color-file").value = "";
    buildPaletteSwatches("#sw-primary", "#color-primary");
    buildPaletteSwatches("#sw-secondary", "#color-secondary");
    refreshColorPreview();
    $("#color-modal").hidden = false;
  });
  for (const id of ["#color-primary", "#color-secondary"]) {
    $(id).addEventListener("input", () => {
      syncColorRow(id === "#color-primary" ? "#sw-primary" : "#sw-secondary", id);
      refreshColorPreview();
    });
  }
  $("#color-cancel").addEventListener("click", () => {
    $("#color-modal").hidden = true;
  });
  $("#color-modal").addEventListener("click", (e) => {
    if (e.target === $("#color-modal")) $("#color-modal").hidden = true;
  });
  $("#color-file").addEventListener("change", async () => {
    const file = $("#color-file").files[0];
    if (!file) return;
    const ok = ["image/png", "image/jpeg", "image/webp", "image/gif"];
    if (!ok.includes(file.type)) {
      toast("仅支持 PNG/JPEG/WebP/GIF 图片");
      return;
    }
    if (file.size > 8 * 1024 * 1024) {
      toast("图片不能超过 8MB");
      return;
    }
    try {
      const pal = await extractPalette(file);
      $("#color-primary").value = pal.primary;
      $("#color-secondary").value = pal.secondary;
      syncColorRow("#sw-primary", "#color-primary");
      syncColorRow("#sw-secondary", "#color-secondary");
      refreshColorPreview();
      toast("已提取配色，确认后点「应用」");
    } catch (err) {
      toast(errText(err));
    }
  });
  $("#color-reset").addEventListener("click", async () => {
    try {
      await apiPost("colors/clear", {});
      S.customColors = null;
      applyCustomColors();
      $("#color-modal").hidden = true;
      toast("已恢复默认配色");
    } catch (err) {
      toast(errText(err));
    }
  });
  $("#color-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const primary = $("#color-primary").value;
    const secondary = $("#color-secondary").value;
    try {
      await apiPost("colors", { primary, secondary });
      S.customColors = { primary, secondary };
      applyCustomColors();
      $("#color-modal").hidden = true;
      toast("配色已应用");
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
      const ok = "own:" + (ev.owner || "");
      counts[ok] = (counts[ok] || 0) + 1;
    }
    const ownerKeys = Object.keys(counts)
      .filter((k) => k.startsWith("own:"))
      .sort((a, b) =>
        a === "own:" ? -1 : b === "own:" ? 1 : a.localeCompare(b),
      );
    const items = [
      { key: "festival", label: "节日·节气", color: "" },
      ...Object.keys(COLOR_CN).map((c) => ({
        key: c,
        label: COLOR_CN[c],
        color: colorVar(c),
      })),
      { sec: "按人" },
      ...ownerKeys.map((k) => ({
        key: k,
        label: k.slice(4) || "未分配",
        color: personDot(k.slice(4)),
        person: true,
      })),
    ];
    pop.innerHTML = "";
    for (const it of items) {
      if (it.sec) {
        const sec = document.createElement("div");
        sec.className = "sec";
        sec.textContent = it.sec;
        pop.appendChild(sec);
        continue;
      }
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
        sw.className = it.person ? "sw person" : "sw";
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
      if (!$("#color-modal").hidden) $("#color-modal").hidden = true;
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
    } catch (err) {
      toast(errText(err));
    }
  });

  // ── boot ───────────────────────────────────────────────────────
  // ── countdown tab（页内标签，复用 GET /countdown 与统一视觉） ──
  const CD_PAGE = 8; // 2×4 方块一页
  const CD = {
    unit: "day",
    items: [],
    total: 0,
    page: 0,
    active: false,
    seq: 0,
    cardRefs: [],
    item: null,
  };
  const UNIT_CN = { week: "按周", day: "按天", hour: "按小时" };
  const REPEAT_CN = { daily: "每天", weekly: "每周", monthly: "每月" };
  const cdEsc = (s) =>
    String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  const cdWhen = (str) => fmtMs(toLocalMs(str));
  const cdRepeatTag = (it) => {
    let s = REPEAT_CN[it.repeat] || "";
    if (s && it.repeat_until) {
      const d = new Date(toLocalMs(it.repeat_until));
      if (Number.isFinite(d.getTime())) {
        s += ` 至 ${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
      }
    }
    return s;
  };
  // 单单位取整：不足一单位显示「不到1X」
  const cdFmt = (sec) => {
    const unit = CD.unit;
    if (unit === "week") {
      const w = Math.floor(sec / 604800);
      return w > 0 ? { num: String(w), unit: "周" } : { num: "不到", unit: "1周" };
    }
    if (unit === "hour") {
      const h = Math.floor(sec / 3600);
      return h > 0 ? { num: String(h), unit: "小时" } : { num: "不到", unit: "1小时" };
    }
    const d = Math.floor(sec / 86400);
    return d > 0 ? { num: String(d), unit: "天" } : { num: "不到", unit: "1天" };
  };
  const cdPages = () => Math.max(1, Math.ceil(CD.total / CD_PAGE));
  const cdTick = () => {
    for (const r of CD.cardRefs) {
      const sec = Math.max(0, Math.round((r.item._t - Date.now()) / 1000));
      r.card.classList.toggle("ongoing", sec <= 0);
      if (sec <= 0) {
        r.num.textContent = "进行中";
      } else {
        const f = cdFmt(sec);
        r.num.innerHTML = `${cdEsc(f.num)}<small>${cdEsc(f.unit)}</small>`;
      }
    }
  };
  const cdRender = () => {
    const grid = $("#cd-grid");
    grid.innerHTML = "";
    CD.cardRefs = [];
    const start = CD.page * CD_PAGE;
    const slice = CD.items.slice(start, start + CD_PAGE);
    for (const it of slice) {
      const card = document.createElement("div");
      card.className = "card";
      const tag = cdRepeatTag(it);
      card.innerHTML = `
        <span class="bar" style="background:var(--c-${cdEsc(it.color || "blue")})"></span>
        ${tag ? `<span class="cd-tag">${cdEsc(tag)}</span>` : ""}
        <div class="cd-num"></div>
        <div class="cd-when">${cdEsc(cdWhen(it.target))}</div>
        <div class="cd-title">${cdEsc(it.title || "")}</div>
        ${it.owner ? `<div class="cd-owner">${cdEsc(it.owner)}</div>` : ""}`;
      card.addEventListener("click", () => cdOpenDetail(it));
      grid.appendChild(card);
      CD.cardRefs.push({ card, num: card.querySelector(".cd-num"), item: it });
    }
    for (let i = slice.length; i < CD_PAGE; i++) {
      const e = document.createElement("div");
      e.className = "slot-empty";
      e.textContent = "暂无待办";
      grid.appendChild(e);
    }
    cdTick();
    $("#cd-ind").textContent = `${CD.page + 1} / ${cdPages()}`;
    $("#cd-prev").disabled = CD.page <= 0;
    $("#cd-next").disabled = CD.page >= cdPages() - 1;
  };
  const cdLoad = async () => {
    const seq = ++CD.seq;
    try {
      const data = await apiGet("countdown");
      if (seq !== CD.seq) return;
      CD.unit = data.unit || CD.unit;
      CD.items = data.items || [];
      CD.total = data.total ?? CD.items.length;
      for (const it of CD.items) it._t = toLocalMs(it.target);
      const ps = cdPages();
      if (CD.page > ps - 1) CD.page = Math.max(0, ps - 1);
      $("#cd-unit").textContent = UNIT_CN[CD.unit] || "按天";
      $("#cd-unit").hidden = false;
      $("#cd-hint").textContent = `共 ${CD.total} 条 · 每 30 秒自动刷新`;
      cdRender();
    } catch (e) {
      toast(errText(e));
    }
  };
  const cdOpenDetail = (item) => {
    CD.item = item;
    $("#cd-title").value = item.title || "";
    $("#cd-time").value = `${cdWhen(item.start)} ~ ${cdWhen(item.end)}${
      item.all_day ? "（全天）" : ""
    }`;
    $("#cd-owner").value = item.owner || "";
    $("#cd-note").value = item.note || "";
    $("#cd-del").textContent = "删除";
    $("#cd-del").dataset.armed = "";
    $("#cd-modal").hidden = false;
  };
  const cdCloseDetail = () => {
    $("#cd-modal").hidden = true;
    CD.item = null;
  };
  $("#cd-done").addEventListener("click", async () => {
    if (!CD.item) return;
    try {
      await apiPost("events/update", { id: CD.item.id, done: true });
      cdCloseDetail();
      toast("已标记完成");
      await cdLoad();
    } catch (e) {
      toast(errText(e));
    }
  });
  $("#cd-del").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    if (btn.dataset.armed !== "1") {
      btn.dataset.armed = "1";
      btn.textContent = "确认删除？";
      return;
    }
    if (!CD.item) return;
    try {
      await apiPost("events/delete", { id: CD.item.id });
      cdCloseDetail();
      toast("已删除");
      await cdLoad();
    } catch (e2) {
      toast(errText(e2));
    }
  });
  $("#cd-modal").addEventListener("click", (e) => {
    if (e.target === $("#cd-modal")) cdCloseDetail();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#cd-modal").hidden) cdCloseDetail();
  });
  $("#cd-prev").addEventListener("click", () => {
    if (CD.page > 0) {
      CD.page -= 1;
      cdRender();
    }
  });
  $("#cd-next").addEventListener("click", () => {
    if (CD.page < cdPages() - 1) {
      CD.page += 1;
      cdRender();
    }
  });
  const setCountdownTab = (on) => {
    if (CD.active === on) return;
    if (on && DI.active) setDiaryTab(false); // 标签互斥
    CD.active = on;
    $("#btn-countdown").classList.toggle("on", on);
    $("#cd-view").hidden = !on;
    $(".views").hidden = on;
    if (on) {
      $("#big-title").textContent = "倒计时";
      cdLoad();
    } else {
      render(); // 恢复标题/返回胶囊/视图状态
    }
  };
  $("#btn-countdown").addEventListener("click", () => setCountdownTab(!CD.active));
  setInterval(() => {
    if (CD.active && !document.hidden) cdLoad();
  }, 30000);
  document.addEventListener("visibilitychange", () => {
    if (CD.active && !document.hidden) cdLoad();
  });

  // ── diary tab（邮箱式日记：列表 + 阅读/编辑，#标签 + 搜索 + 导出） ──
  const DI = {
    active: false,
    mode: "read", // read | compose | edit
    entries: [],
    tags: [],
    q: "",
    selTags: new Set(),
    current: null,
    seq: 0,
  };
  let dySearchTimer = null;

  const dyExtractTags = (text) => {
    const out = [];
    const re = /(?:^|\s)#([^\s#]{1,30})/g;
    let m;
    while ((m = re.exec(String(text || "")))) {
      const t = m[1]
        .replace(/^[，。！？、；：,.!?;:"'）)】\]》…]+/, "")
        .replace(/[，。！？、；：,.!?;:"'）)】\]》…]+$/, "");
      if (t && !out.includes(t)) out.push(t);
    }
    return out;
  };
  const dyDateLabel = (dateStr) => {
    const d = new Date(String(dateStr || "") + "T00:00:00");
    if (!Number.isFinite(d.getTime())) return String(dateStr || "");
    const diff = dayDiff(d, today());
    if (diff === 0) return "今天";
    if (diff === 1) return "昨天";
    if (d.getFullYear() === today().getFullYear()) {
      return `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    }
    return dateStr;
  };
  const dyDownload = (entry, kind) => {
    const isMd = kind === "md";
    const tags = (entry.tags || []).map((t) => `#${t}`).join(" ");
    const text = isMd
      ? `# ${entry.title}\n\n> ${entry.date}${tags ? " · " + tags : ""}\n\n${entry.content}\n`
      : `${entry.title}\n${entry.date}${tags ? "  " + tags : ""}\n\n${entry.content}\n`;
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${entry.title || "日记"}-${entry.date || ""}.${isMd ? "md" : "txt"}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast(isMd ? "已导出 Markdown" : "已导出文本");
  };
  const dyRenderTags = () => {
    const root = $("#dy-tags");
    if (!root) return;
    let html = `<button class="dy-chip${DI.selTags.size ? "" : " on"}" data-tag="">全部</button>`;
    for (const t of DI.tags) {
      const on = DI.selTags.has(t.tag) ? " on" : "";
      html += `<button class="dy-chip${on}" data-tag="${cdEsc(t.tag)}">#${cdEsc(
        t.tag,
      )}<span class="n">${t.count}</span></button>`;
    }
    root.innerHTML = html;
  };
  const dyItemHtml = (e) => {
    const tags = (e.tags || []).map((t) => `<span>#${cdEsc(t)}</span>`).join("");
    const snippet = cdEsc(
      String(e.content || "").replace(/\s+/g, " ").slice(0, 60),
    );
    const sel = DI.current && DI.current.id === e.id ? " sel" : "";
    return `<div class="dy-item${sel}" data-id="${cdEsc(e.id)}">
      <div class="t">${e.pinned ? '<span class="pin">★</span>' : ""}${cdEsc(
        e.title || "(无标题)",
      )}</div>
      <div class="s">${snippet || "(无正文)"}</div>
      ${tags ? `<div class="tg">${tags}</div>` : ""}
    </div>`;
  };
  const dyRenderList = () => {
    const root = $("#dy-list");
    if (!root) return;
    if (!DI.entries.length) {
      root.innerHTML =
        '<div class="dy-empty">没有匹配的日记<br/>点「新写日记」开始记录</div>';
      return;
    }
    let html = "";
    const pinned = DI.entries.filter((e) => e.pinned);
    const rest = DI.entries.filter((e) => !e.pinned);
    if (pinned.length) {
      html += '<div class="dy-date">置顶</div>' + pinned.map(dyItemHtml).join("");
    }
    let lastGroup = null;
    for (const e of rest) {
      const g = e.date || "";
      if (g !== lastGroup) {
        html += `<div class="dy-date">${cdEsc(dyDateLabel(g))}</div>`;
        lastGroup = g;
      }
      html += dyItemHtml(e);
    }
    root.innerHTML = html;
  };
  const dySave = async () => {
    const title = $("#dy-in-title").value.trim();
    if (!title) {
      toast("标题不能为空");
      return;
    }
    const body = { title, content: $("#dy-in-content").value };
    const dateVal = $("#dy-in-date").value;
    if (dateVal) body.date = dateVal;
    try {
      if (DI.mode === "edit" && DI.current) {
        const r = await apiPost("diary/update", { id: DI.current.id, ...body });
        DI.current = r.entry;
        toast("已保存");
      } else {
        const r = await apiPost("diary", body);
        DI.current = r.entry;
        toast("已写入日记");
      }
      DI.mode = "read";
      await dyLoad();
    } catch (e) {
      toast(errText(e));
    }
  };
  const dyRenderPane = () => {
    const root = $("#dy-pane");
    if (!root) return;
    if (DI.mode === "compose" || DI.mode === "edit") {
      const e = DI.mode === "edit" ? DI.current : null;
      root.innerHTML = `
        <div class="dy-editor">
          <h2>${DI.mode === "edit" ? "编辑日记" : "新日记"}</h2>
          <label>标题
            <input id="dy-in-title" maxlength="200" value="${cdEsc(
              e?.title || "",
            )}" placeholder="给这一天起个标题" />
          </label>
          <div class="row">
            <label>日期
              <input id="dy-in-date" type="date" value="${cdEsc(
                e?.date || dayKey(today()),
              )}" />
            </label>
          </div>
          <label>正文（写 #标签 自动生成标签，如 #学习 #碎碎念）
            <textarea id="dy-in-content" maxlength="20000" placeholder="今天发生了什么…">${cdEsc(
              e?.content || "",
            )}</textarea>
          </label>
          <div class="dy-preview" id="dy-preview"></div>
          <div class="dy-actions">
            <button type="button" class="btn" data-act="cancel">取消</button>
            <div class="spacer"></div>
            <button type="button" class="btn primary" data-act="save">保存</button>
          </div>
        </div>`;
      const upd = () => {
        const tags = dyExtractTags(
          $("#dy-in-title").value + "\n" + $("#dy-in-content").value,
        );
        $("#dy-preview").innerHTML = tags
          .map((t) => `<span>#${cdEsc(t)}</span>`)
          .join("");
      };
      $("#dy-in-title").addEventListener("input", upd);
      $("#dy-in-content").addEventListener("input", upd);
      upd();
      root.querySelector('[data-act="save"]').addEventListener("click", dySave);
      root
        .querySelector('[data-act="cancel"]')
        .addEventListener("click", () => {
          DI.mode = "read";
          dyRenderPane();
        });
      return;
    }
    const e = DI.current;
    if (!e) {
      root.innerHTML =
        '<div class="dy-empty">选择左侧日记阅读<br/>或点「＋ 新写日记」</div>';
      return;
    }
    const tags = (e.tags || [])
      .map((t) => `<span class="tg">#${cdEsc(t)}</span>`)
      .join(" ");
    root.innerHTML = `
      <h2>${cdEsc(e.title || "(无标题)")}</h2>
      <div class="dy-meta">${cdEsc(e.date || "")} · 创建 ${cdEsc(
        String(e.created_at || "").replace("T", " ").slice(0, 16),
      )} ${tags}${e.pinned ? " · ★ 置顶" : ""}</div>
      <div class="dy-content">${cdEsc(e.content || "(无内容)")}</div>
      <div class="dy-actions">
        <button type="button" class="btn ghost" data-act="del">删除</button>
        <div class="spacer"></div>
        <button type="button" class="btn" data-act="pin">${e.pinned ? "取消置顶" : "置顶"}</button>
        <button type="button" class="btn" data-act="md">导出MD</button>
        <button type="button" class="btn" data-act="txt">导出TXT</button>
        <button type="button" class="btn" data-act="edit">编辑</button>
      </div>`;
    root.querySelector('[data-act="edit"]').addEventListener("click", () => {
      DI.mode = "edit";
      dyRenderPane();
    });
    root
      .querySelector('[data-act="pin"]')
      .addEventListener("click", async () => {
        try {
          const r = await apiPost("diary/update", {
            id: e.id,
            pinned: !e.pinned,
          });
          DI.current = r.entry;
          toast(r.entry.pinned ? "已置顶" : "已取消置顶");
          await dyLoad();
        } catch (err) {
          toast(errText(err));
        }
      });
    root
      .querySelector('[data-act="md"]')
      .addEventListener("click", () => dyDownload(e, "md"));
    root
      .querySelector('[data-act="txt"]')
      .addEventListener("click", () => dyDownload(e, "txt"));
    const delBtn = root.querySelector('[data-act="del"]');
    delBtn.addEventListener("click", async () => {
      if (delBtn.dataset.armed !== "1") {
        delBtn.dataset.armed = "1";
        delBtn.textContent = "确认删除？";
        return;
      }
      try {
        await apiPost("diary/delete", { id: e.id });
        DI.current = null;
        DI.mode = "read";
        toast("已删除");
        await dyLoad();
      } catch (err) {
        toast(errText(err));
      }
    });
  };
  const dyLoad = async () => {
    const seq = ++DI.seq;
    try {
      const params = {};
      if (DI.q) params.q = DI.q;
      if (DI.selTags.size) params.tag = [...DI.selTags].join(",");
      const [listRes, tagRes] = await Promise.all([
        apiGet("diary", params),
        apiGet("diary/tags"),
      ]);
      if (seq !== DI.seq) return;
      DI.entries = listRes.entries || [];
      DI.tags = tagRes.tags || [];
      if (DI.current) {
        const fresh = DI.entries.find((x) => x.id === DI.current.id);
        if (fresh) DI.current = fresh;
      }
      dyRenderTags();
      dyRenderList();
      if (DI.mode === "read") dyRenderPane();
    } catch (e) {
      toast(errText(e));
    }
  };
  const setDiaryTab = (on) => {
    if (DI.active === on) return;
    if (on && CD.active) setCountdownTab(false); // 标签互斥
    DI.active = on;
    $("#btn-diary").classList.toggle("on", on);
    $("#diary-view").hidden = !on;
    $(".views").hidden = on;
    if (on) {
      $("#big-title").textContent = "日记";
      dyLoad();
    } else {
      render(); // 恢复标题/返回胶囊/视图状态
    }
  };
  const openDiaryFromSide = (id) => {
    const entry = (S.sideDiary || []).find((d) => d.id === id);
    if (!entry) return;
    setDiaryTab(true);
    DI.mode = "read";
    DI.current = entry;
    dyRenderPane();
    dyRenderList();
  };
  const loadSideDiary = async () => {
    if (!S.split) return;
    const key = dayKey(new Date(S.y, S.m - 1, S.d));
    try {
      const r = await apiGet("diary", { date: key });
      S.sideDiary = r.entries || [];
      S.sideDiaryDate = key;
    } catch (e) {
      S.sideDiary = [];
      S.sideDiaryDate = "";
    }
    if (S.split && !CD.active && !DI.active) renderSide();
  };

  // diary listeners
  $("#btn-diary").addEventListener("click", () => setDiaryTab(!DI.active));
  $("#dy-new").addEventListener("click", () => {
    DI.mode = "compose";
    dyRenderPane();
    $("#dy-in-title")?.focus();
  });
  $("#dy-search").addEventListener("input", (e) => {
    clearTimeout(dySearchTimer);
    dySearchTimer = setTimeout(() => {
      DI.q = String(e.target.value || "").trim();
      dyLoad();
    }, 300);
  });
  $("#dy-tags").addEventListener("click", (e) => {
    const chip = e.target.closest(".dy-chip");
    if (!chip) return;
    const tag = chip.dataset.tag || "";
    if (!tag) DI.selTags.clear();
    else if (DI.selTags.has(tag)) DI.selTags.delete(tag);
    else DI.selTags.add(tag);
    dyLoad();
  });
  $("#dy-list").addEventListener("click", (e) => {
    const item = e.target.closest(".dy-item");
    if (!item?.dataset.id) return;
    const entry = DI.entries.find((x) => x.id === item.dataset.id);
    if (!entry) return;
    DI.current = entry;
    DI.mode = "read";
    dyRenderList();
    dyRenderPane();
  });
  setInterval(() => {
    if (DI.active && DI.mode === "read" && !document.hidden) dyLoad();
  }, 60000);
  document.addEventListener("visibilitychange", () => {
    if (DI.active && DI.mode === "read" && !document.hidden) dyLoad();
  });

  (async function boot() {
    const applyTheme = (isDark) => {
      document.documentElement.dataset.theme = isDark ? "dark" : "light";
      applyCustomColors();
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
      S.customColors = S.cfg.custom_colors || null;
      applyCustomColors();
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
    setInterval(() => {
      if (document.hidden || document.querySelector(".evblock.dragging")) return;
      refreshCurrent();
    }, 60000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        refreshCurrent();
      }
    });
  })();
})();
