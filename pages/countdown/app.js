/* Countdown panel frontend.
 * Talks to the plugin backend exclusively through the AstrBot plugin
 * page bridge (apiGet / apiPost), mirroring the calendar page's tokens,
 * custom-color handling and background layer. */

(function () {
  const bridge = window.AstrBotPluginPage || window.AstrBotPlugin;
  if (!bridge) {
    document.body.innerHTML =
      '<p class="fatal">桥接不可用，请从 AstrBot WebUI 打开此页面。</p>';
    return;
  }

  const $ = (sel) => document.querySelector(sel);
  const PAGE = 8; // 2 × 4 grid per page
  const UNIT_CN = { week: "按周", day: "按天", hour: "按小时" };
  const REPEAT_CN = { daily: "每天", weekly: "每周", monthly: "每月" };

  const S = {
    unit: "day",
    items: [],
    total: 0,
    page: 0,
    customColors: null,
    bg: { image: "", opacity: 0.3 },
    active: null,
    seq: 0,
    cardRefs: [],
  };

  // ── toast / api ─────────────────────────────────────────────
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
  const escapeHtml = (s) =>
    String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");

  // ── time helpers ────────────────────────────────────────────
  const pad = (n) => String(n).padStart(2, "0");
  const toMs = (str) => new Date(String(str).replace(" ", "T")).getTime();
  const fmtWhen = (str) => {
    const ms = toMs(str);
    if (!Number.isFinite(ms)) return String(str || "");
    const d = new Date(ms);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
      d.getHours(),
    )}:${pad(d.getMinutes())}`;
  };
  // 单单位取整：不足一单位显示「不到1X」
  const fmtCountdown = (sec, unit) => {
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
  const repeatTag = (it) => {
    let s = REPEAT_CN[it.repeat] || "";
    if (s && it.repeat_until) {
      const d = new Date(toMs(it.repeat_until));
      if (Number.isFinite(d.getTime())) {
        s += ` 至 ${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
      }
    }
    return s;
  };

  // ── custom colors (copied from the calendar page) ───────────
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

  // ── background image (same endpoint as the calendar page) ───
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

  // ── render ──────────────────────────────────────────────────
  const pages = () => Math.max(1, Math.ceil(S.total / PAGE));

  const tick = () => {
    for (const r of S.cardRefs) {
      const sec = Math.max(0, Math.round((r.item._t - Date.now()) / 1000));
      const ongoing = sec <= 0;
      r.card.classList.toggle("ongoing", ongoing);
      if (ongoing) {
        r.num.textContent = "进行中";
      } else {
        const f = fmtCountdown(sec, S.unit);
        r.num.innerHTML = `${escapeHtml(f.num)}<small>${escapeHtml(f.unit)}</small>`;
      }
    }
  };

  const render = () => {
    const grid = $("#grid");
    grid.innerHTML = "";
    S.cardRefs = [];
    const start = S.page * PAGE;
    const slice = S.items.slice(start, start + PAGE);
    for (const it of slice) {
      const card = document.createElement("div");
      card.className = "card";
      const color = `var(--c-${it.color || "blue"})`;
      const tag = repeatTag(it);
      card.innerHTML = `
        <span class="bar" style="background:${color}"></span>
        ${tag ? `<span class="cd-tag">${escapeHtml(tag)}</span>` : ""}
        <div class="cd-num"></div>
        <div class="cd-when">${escapeHtml(fmtWhen(it.target))}</div>
        <div class="cd-title">${escapeHtml(it.title || "")}</div>
        ${it.owner ? `<div class="cd-owner">${escapeHtml(it.owner)}</div>` : ""}`;
      card.addEventListener("click", () => openDetail(it));
      grid.appendChild(card);
      S.cardRefs.push({ card, num: card.querySelector(".cd-num"), item: it });
    }
    for (let i = slice.length; i < PAGE; i++) {
      const e = document.createElement("div");
      e.className = "slot-empty";
      e.textContent = "暂无待办";
      grid.appendChild(e);
    }
    tick();
    const ps = pages();
    $("#page-ind").textContent = `${S.page + 1} / ${ps}`;
    $("#btn-prev").disabled = S.page <= 0;
    $("#btn-next").disabled = S.page >= ps - 1;
  };

  const load = async () => {
    const seq = ++S.seq;
    try {
      const data = await apiGet("countdown");
      if (seq !== S.seq) return;
      S.unit = data.unit || S.unit;
      S.items = data.items || [];
      S.total = data.total ?? S.items.length;
      for (const it of S.items) it._t = toMs(it.target);
      const ps = pages();
      if (S.page > ps - 1) S.page = Math.max(0, ps - 1);
      $("#unit-badge").textContent = UNIT_CN[S.unit] || "按天";
      $("#unit-badge").hidden = false;
      $("#refresh-hint").textContent = `共 ${S.total} 条 · 每 30 秒自动刷新`;
      render();
    } catch (e) {
      toast(errText(e));
    }
  };

  // ── detail modal ────────────────────────────────────────────
  const openDetail = (item) => {
    S.active = item;
    $("#d-title").value = item.title || "";
    $("#d-time").value = `${fmtWhen(item.start)} ~ ${fmtWhen(item.end)}${
      item.all_day ? "（全天）" : ""
    }`;
    $("#d-owner").value = item.owner || "";
    $("#d-note").value = item.note || "";
    $("#d-del").textContent = "删除";
    $("#d-del").dataset.armed = "";
    $("#modal").hidden = false;
  };
  const closeDetail = () => {
    $("#modal").hidden = true;
    S.active = null;
  };

  $("#d-done").addEventListener("click", async () => {
    if (!S.active) return;
    try {
      await apiPost("events/update", { id: S.active.id, done: true });
      closeDetail();
      toast("已标记完成");
      await load();
    } catch (e) {
      toast(errText(e));
    }
  });
  $("#d-del").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    if (btn.dataset.armed !== "1") {
      btn.dataset.armed = "1";
      btn.textContent = "确认删除？";
      return;
    }
    if (!S.active) return;
    try {
      await apiPost("events/delete", { id: S.active.id });
      closeDetail();
      toast("已删除");
      await load();
    } catch (e2) {
      toast(errText(e2));
    }
  });
  $("#modal").addEventListener("click", (e) => {
    if (e.target === $("#modal")) closeDetail();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#modal").hidden) closeDetail();
  });

  // ── pager & navigation ──────────────────────────────────────
  const PLUGIN = "astrbot_plugin_calendar";
  // 本页运行在 sandbox iframe 内：无法跨页自跳转（token 按页签发），
  // 仅尝试通知外层 SPA 切路由；失败时提示从 WebUI 插件页菜单进入。
  const jumpPage = (page) => {
    const hashTarget = `#/plugin-page/${PLUGIN}/${page}`;
    for (const w of [window.parent, window.top]) {
      try {
        if (w && w !== window && w.location && "hash" in w.location) {
          w.location.hash = hashTarget;
          return;
        }
      } catch (e) {
        /* sandboxed: cannot cross frames */
      }
    }
    toast("请在 WebUI 插件页菜单中切换页面");
  };
  $("#btn-prev").addEventListener("click", () => {
    if (S.page > 0) {
      S.page -= 1;
      render();
    }
  });
  $("#btn-next").addEventListener("click", () => {
    if (S.page < pages() - 1) {
      S.page += 1;
      render();
    }
  });
  $("#btn-back").addEventListener("click", () => jumpPage("calendar"));

  // ── boot ────────────────────────────────────────────────────
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
      const cfg = await apiGet("config");
      if (cfg.countdown_unit) S.unit = cfg.countdown_unit;
      S.customColors = cfg.custom_colors || null;
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
    await load();
    setInterval(tick, 1000);
    setInterval(() => {
      if (!document.hidden) load();
    }, 30000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) load();
    });
  })();
})();
