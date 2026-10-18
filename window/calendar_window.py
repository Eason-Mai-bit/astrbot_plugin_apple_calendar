"""Desktop mini window for the calendar plugin.

A small always-on-top panel showing today's digest plus pending reminder
cards. It talks to the plugin's loopback HTTP server (127.0.0.1, random
port, per-run secret token) using GET /state and POST /action.

Launched by the plugin as:

    python -u calendar_window.py --port P --token T --ratio 12 \\
        --theme light --panel-url URL [--x X --y Y] [--snap on|off]
        [--glass on|off]

Self-termination rules:
- 401 from the server means the token rotated (plugin reloaded) -> exit.
- The loopback port dies with the plugin; after ~30s of refused
  connections the window exits too, so no orphans are left behind.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

import tkinter as tk
import tkinter.font as tkfont

try:  # optional system tray (see requirements.txt)
    import pystray
    from PIL import Image, ImageDraw

    _HAS_TRAY = True
except Exception:  # pragma: no cover - depends on deployed environment
    _HAS_TRAY = False

POLL_MS = 2000
FAIL_EXIT_AFTER = 15  # consecutive connection failures after a successful connect (~30s) -> exit
CONNECT_GRACE_MS = 15000  # before first successful connect, retry this long then give up (dead port)
THEME_ORDER = ["dark", "light", "aurora", "sakura"]  # must match main.py

# Per-theme color-key for glass mode: pixels of this exact color render fully
# transparent. The value sits right next to the theme bg, so antialiased text
# edges blend to an invisible near-bg tone instead of a bright fringe.

THEMES = {
    "dark": {
        "accent": "#3B82F6",
        "bg": "#1E2430",
        "key": "#1E2430",
        "text": "#F3F6FB",
        "sub": "#9AA6BC",
        "card": "#2A3242",
        "chip": "#33415C",
        "border": "#39425A",
        "glass": 0xB030241E,  # ABGR acrylic tint
    },
    "light": {
        "accent": "#2563EB",
        "bg": "#FFFFFF",
        "key": "#FEFDFF",
        "text": "#1B1E26",
        "sub": "#6B7382",
        "card": "#F2F5FA",
        "chip": "#E3EAF7",
        "border": "#DDE3ED",
        "glass": 0xA0FFFFFF,
    },
    "aurora": {
        "accent": "#22B8F0",
        "bg": "#12242E",
        "key": "#12242E",
        "text": "#EAF7FC",
        "sub": "#8FB6C6",
        "card": "#193140",
        "chip": "#1E4050",
        "border": "#245065",
        "glass": 0xB02E2412,
    },
    "sakura": {
        "accent": "#E11D4E",
        "bg": "#FDF2F5",
        "key": "#FDF2F5",
        "text": "#2A1A20",
        "sub": "#9A707C",
        "card": "#FAE7EC",
        "chip": "#F6DCE4",
        "border": "#F0D7DF",
        "glass": 0xA0F5F2FD,
    },
}


_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _norm_hex(value) -> str | None:
    s = str(value or "").strip()
    if len(s) == 4 and s.startswith("#"):
        s = "#" + "".join(ch * 2 for ch in s[1:])
    return s.lower() if _HEX_RE.match(s) else None


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    n = int(hex_color[1:], 16)
    return (n >> 16) & 255, (n >> 8) & 255, n & 255


def _rgb_to_hsl(r: int, g: int, b: int) -> tuple[float, float, float]:
    rf, gf, bf = r / 255.0, g / 255.0, b / 255.0
    mx, mn = max(rf, gf, bf), min(rf, gf, bf)
    l = (mx + mn) / 2.0
    h = s = 0.0
    if mx != mn:
        d = mx - mn
        s = d / (2.0 - mx - mn) if l > 0.5 else d / (mx + mn)
        if mx == rf:
            h = (gf - bf) / d + (6.0 if gf < bf else 0.0)
        elif mx == gf:
            h = (bf - rf) / d + 2.0
        else:
            h = (rf - gf) / d + 4.0
        h *= 60.0
    return h, s * 100.0, l * 100.0


def _hsl_to_hex(h: float, s: float, l: float) -> str:
    s /= 100.0
    l /= 100.0
    c = (1.0 - abs(2.0 * l - 1.0)) * s
    x = c * (1.0 - abs((h / 60.0) % 2.0 - 1.0))
    m = l - c / 2.0
    if h < 60:
        rp, gp, bp = c, x, 0.0
    elif h < 120:
        rp, gp, bp = x, c, 0.0
    elif h < 180:
        rp, gp, bp = 0.0, c, x
    elif h < 240:
        rp, gp, bp = 0.0, x, c
    elif h < 300:
        rp, gp, bp = x, 0.0, c
    else:
        rp, gp, bp = c, 0.0, x
    return "#%02x%02x%02x" % (
        max(0, min(255, round((rp + m) * 255))),
        max(0, min(255, round((gp + m) * 255))),
        max(0, min(255, round((bp + m) * 255))),
    )


def _fit_accent(hex_color: str, dark_bg: bool) -> str:
    r, g, b = _hex_to_rgb(hex_color)
    h, s, l = _rgb_to_hsl(r, g, b)
    s = min(90.0, max(30.0, s))
    if dark_bg:
        if l < 58.0:
            l = min(72.0, l + 12.0)
        l = min(72.0, max(58.0, l))
    else:
        l = min(56.0, max(28.0, l))
    return _hsl_to_hex(h, s, l)


class CalendarWindow:
    """Frameless mini window bound to the plugin loopback API."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.base = f"http://127.0.0.1:{args.port}"
        self.token = args.token
        self.ratio = max(8, min(18, args.ratio))
        self.theme_name = args.theme if args.theme in THEMES else "light"
        self.custom_colors: dict | None = None  # {primary, secondary} from /state
        self.snap = getattr(args, "snap", "on") != "off"
        self.glass = getattr(args, "glass", "on") != "off"
        self.panel_url = args.panel_url
        self.start_x = args.x
        self.start_y = args.y
        self.failures = 0
        self.ever_connected = False
        self.start_ms = int(time.monotonic() * 1000)
        self.last_state = None
        self.drag_origin: tuple[int, int] | None = None
        self.drag_press: tuple[int, int, float] | None = None  # x, y, t (click vs drag)
        self._drag_moved_far = False  # set once total motion exceeds 8px
        self._snap_anim: dict | None = None  # active dock animation state
        self._open_anim_after: object | None = None  # startup slide-in chain
        self.selected: str | None = None  # pill key shown in the detail overlay
        self.detail_layer: tk.Frame | None = None  # in-window detail overlay
        self.detail_key: str | None = None
        self.detail_ev: dict | None = None
        self.detail_serial: str | None = None  # overlay payload change detector
        self.detail_btn_cv: tk.Canvas | None = None
        self._detail_del_armed = False  # 删除 clicked once; click again deletes
        self._detail_hover: str | None = None  # "done"|"del" hit under cursor
        self._detail_inner = 0  # overlay content width (root w - 2*pad)
        self._detail_size_sig: tuple | None = None  # (w, h) overlay drawn for
        self._f9b: tkfont.Font | None = None  # 9pt bold, footer button text
        # note box scrolling (mouse wheel)
        self._note_cv: tk.Canvas | None = None
        self._note_tid: int | None = None
        self._note_scroll = 0
        self._note_scroll_max = 0
        self._note_view_h = 0
        self._note_thumb_id: int | None = None
        self._hwnd: int | None = None
        self._rgn_sig: tuple | None = None  # last (hwnds, w, h) the region was set for
        self.tray = None  # pystray.Icon once the system tray starts
        self.tray_queue: "queue.Queue[str]" = queue.Queue()

        self.root = tk.Tk()
        self.root.title("日历日程")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", 0.0)  # invisible until placed + animated
        except tk.TclError:
            pass
        self._enable_glass()
        self._f9 = tkfont.Font(family="Microsoft YaHei UI", size=9)
        self.pills: list[tuple[tk.Canvas, dict]] = []
        self.more_lbl: tk.Label | None = None

        self._build_layout()
        self._poll_once_now()  # first render so the initial size is final
        self._place()
        self._apply_theme()
        self._animate_open()

        self.root.bind("<ButtonPress-1>", self._drag_start)
        self.root.bind("<B1-Motion>", self._drag_move)
        self.root.bind("<ButtonRelease-1>", self._drag_end)
        self.root.bind("<Configure>", self._on_root_moved)
        self.root.bind("<Escape>", self._detail_escape)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

        self.root.after(200, self._poll)
        self.root.after(250, self._drain_tray)

        hook = os.environ.get("CALENDAR_WIN_TEST", "")
        if hook:
            self.root.after(1500, lambda: self._test_hook(hook))

    def _test_hook(self, spec: str) -> None:
        """Debug only: CALENDAR_WIN_TEST=select,check,trash drives pill clicks."""
        for act in [a.strip() for a in spec.split(",") if a.strip()]:
            if act == "quit":
                self._quit()
                return
            if act == "snap":
                # Park ~100px from the right edge (beyond the old threshold)
                # and verify release-docking glues it flush anyway.
                self.root.update_idletasks()
                wa_l, wa_t, wa_r, wa_b = self._work_area()
                self.root.geometry(f"+{wa_r - self.root.winfo_width() - 100}+300")
                self.root.update_idletasks()
                x0 = self.root.winfo_x()
                print(f"SNAPTEST snap={self.snap} x0={x0}", flush=True)
                self._snap_release()
                # Drive the Tk event loop synchronously until the 320ms
                # glide finishes (or 1.5s cap), then assert the geometry.
                t_end = time.time() + 1.5
                while time.time() < t_end:
                    self.root.update()
                    time.sleep(0.02)
                target = wa_r - self.root.winfo_width()
                x1 = self.root.winfo_x()
                y1 = self.root.winfo_y()
                print(
                    f"SNAPRESULT x1={x1} target={target} "
                    f"docked={x1 == target} y_kept={y1 == 300}",
                    flush=True,
                )
                return
            if not self.pills:
                break
            name, _, idx_s = act.partition(":")
            if name == "openkey":
                # debug only: open the popup for an arbitrary event id
                self._open_detail(idx_s)
                print(
                    f"DETAIL openkey title={(self.detail_ev or {}).get('title')}",
                    flush=True,
                )
                continue
            try:
                idx = max(0, min(int(idx_s), len(self.pills) - 1))
            except ValueError:
                idx = 0
            cv, ln = self.pills[idx]
            if cv.winfo_exists():
                if name == "select":
                    # click a pill: detail popup opens for that task
                    self._on_pill_click(cv, ln, type("E", (), {"x": 8})())
                    print(
                        f"DETAIL open title={(self.detail_ev or {}).get('title')}",
                        flush=True,
                    )
                elif name == "check" and self.detail_key:
                    self._detail_toggle_done()
                    print(
                        f"DETAIL check title={(self.detail_ev or {}).get('title')}",
                        flush=True,
                    )
                elif name == "trash" and self.detail_key:
                    self._detail_delete()  # arm confirm
                    self._detail_delete()  # actually delete
                    print("DETAIL trash done", flush=True)
                elif name == "wheel":
                    try:
                        n = int(idx_s)
                    except ValueError:
                        n = 0
                    for _ in range(abs(n)):
                        self._detail_note_wheel(
                            type("E", (), {"delta": -120 if n > 0 else 120})()
                        )
                    print(
                        f"DETAIL wheel scroll={self._note_scroll}"
                        f" max={self._note_scroll_max}",
                        flush=True,
                    )
                elif name == "dump":
                    layer = self.detail_layer
                    if layer is not None and layer.winfo_exists():
                        layer.update_idletasks()
                        print(
                            "DUMP root w=%d h=%d | layer w=%d h=%d"
                            % (self.root.winfo_width(), self.root.winfo_height(),
                               layer.winfo_width(), layer.winfo_height()),
                            flush=True,
                        )
                        for ch in layer.winfo_children():
                            extra = ""
                            if isinstance(ch, tk.Canvas):
                                extra = " items=%d" % len(ch.find_all())
                            print(
                                "DUMP %s y=%d h=%d w=%d%s"
                                % (type(ch).__name__, ch.winfo_y(),
                                   ch.winfo_height(), ch.winfo_width(), extra),
                                flush=True,
                            )

    # ── layout ──────────────────────────────────────────────────────────

    def _build_layout(self) -> None:
        r = self.root
        self.outer = tk.Frame(r, bd=0)
        self.outer.pack(fill="both", expand=True)

        # topbar: back-to-panel (left) + theme/close (right); all hand-drawn
        # canvas icons (no text, no emoji).
        self._icons: list[tuple[tk.Canvas, callable]] = []
        self.topbar = tk.Frame(self.outer)
        self.topbar.pack(fill="x", padx=8, pady=(7, 0))
        self.panel_btn = self._icon_button(
            self.topbar, self._draw_panel_icon, self._back_to_panel, side="left"
        )
        self.close_btn = self._icon_button(
            self.topbar, self._draw_close_icon, self._quit, side="right"
        )
        self.theme_btn = self._icon_button(
            self.topbar, self._draw_theme_icon, self._cycle_theme, side="right"
        )
        self.snap_btn = self._icon_button(
            self.topbar, self._draw_snap_icon, self._toggle_snap, side="right"
        )

        # centered date block (per sketch: big number, weekday · lunar below)
        self.day_lbl = tk.Label(
            self.outer,
            text="",
            font=("Microsoft YaHei UI", 34, "bold"),
            anchor="center",
            justify="center",
        )
        self.day_lbl.pack(fill="x", pady=(6, 0))
        self.sub_lbl = tk.Label(
            self.outer,
            text="",
            font=("Microsoft YaHei UI", 11),
            anchor="center",
            justify="center",
        )
        self.sub_lbl.pack(fill="x")

        # reminder cards (shown above digest when pending)
        self.cards_frame = tk.Frame(self.outer)
        self.cards_frame.pack(fill="x", padx=12, pady=(10, 0))

        # digest: capsule pill rows + centered "+N" overflow (per sketch)
        self.dig_frame = tk.Frame(self.outer)
        self.dig_frame.pack(fill="x", padx=14, pady=(10, 0))
        self.empty_lbl = tk.Label(
            self.dig_frame,
            text="今天暂无安排",
            font=("Microsoft YaHei UI", 9),
            anchor="center",
        )

        self.status_lbl = tk.Label(
            self.outer,
            text="点击日程可标记完成或删除",
            font=("Microsoft YaHei UI", 8),
            anchor="center",
        )
        self.status_lbl.pack(fill="x", padx=12, pady=(8, 7))

    def _place(self) -> None:
        self.root.update_idletasks()
        wa_l, wa_t, wa_r, wa_b = self._work_area()
        w = max(240, int(self.root.winfo_screenwidth() * self.ratio / 100))
        h = self.root.winfo_reqheight()
        x = self.start_x if self.start_x is not None else wa_r - w - 24
        y = self.start_y if self.start_y is not None else wa_b - h - 24
        x = min(max(wa_l, x), max(wa_l, wa_r - w))
        y = min(max(wa_t, y), max(wa_t, wa_b - h))
        self.root.geometry(f"{w}x{h}+{int(x)}+{int(y)}")
        self._apply_round_region()

    def _animate_open(self) -> None:
        """Fade + slide the window in once at startup (~180ms)."""
        self.root.update_idletasks()
        steps, delay = 9, 20
        # mainloop has not started yet, so winfo_x/y can still report the stale
        # pre-placement position; the wm geometry string carries the pending
        # placement, which is authoritative here.
        m = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", self.root.geometry())
        if m:
            x0, y0 = int(m.group(3)), int(m.group(4))
        else:
            x0, y0 = self.root.winfo_x(), self.root.winfo_y()
        lift = 16
        target = self._glass_alpha()
        try:
            self.root.attributes("-alpha", 0.0)
        except tk.TclError:
            return

        def step(i: int) -> None:
            if i > steps:
                try:
                    self.root.attributes("-alpha", target)
                except tk.TclError:
                    pass
                self.root.geometry(f"+{x0}+{y0}")
                self._open_anim_after = None
                return
            f = i / steps
            try:
                self.root.attributes("-alpha", f * target)
            except tk.TclError:
                pass
            self.root.geometry(f"+{x0}+{int(y0 + lift * (1 - f))}")
            self._open_anim_after = self.root.after(delay, step, i + 1)

        step(1)

    def _stop_open_anim(self) -> None:
        """Cancel the startup slide-in so it cannot fight drag/dock anims."""
        aid = self._open_anim_after
        if aid is None:
            return
        try:
            self.root.after_cancel(aid)
        except tk.TclError:
            pass
        self._open_anim_after = None
        try:
            self.root.attributes("-alpha", self._glass_alpha())
        except tk.TclError:
            pass

    # ── glass + hand-drawn icons ────────────────────────────────────────

    def _bg(self, t: dict) -> str:
        """Uniform surface color (glass = slight whole-window translucency)."""
        return t["bg"]

    def _glass_alpha(self) -> float:
        return 0.93 if self.glass else 1.0

    def _work_area(self) -> tuple[int, int, int, int]:
        l, t = 0, 0
        r = self.root.winfo_screenwidth()
        b = self.root.winfo_screenheight()
        if sys.platform == "win32":
            try:
                import ctypes

                class RECT(ctypes.Structure):
                    _fields_ = [
                        ("l", ctypes.c_long),
                        ("t", ctypes.c_long),
                        ("r", ctypes.c_long),
                        ("b", ctypes.c_long),
                    ]

                rect = RECT()
                # SPI_GETWORKAREA = 0x0030 (excludes the taskbar).
                if ctypes.windll.user32.SystemParametersInfoW(
                    0x0030, 0, ctypes.byref(rect), 0
                ):
                    l, t, r, b = rect.l, rect.t, rect.r, rect.b
            except Exception:
                pass
        return l, t, r, b

    def _top_hwnd(self) -> int | None:
        """Top-level wrapper hwnd (GA_ROOT). At init GetParent(winfo_id()) can
        still be 0 (wrapper not created yet), which used to leave the region and
        DWM attributes stranded on the child window -> square wrapper background
        bleeding through the clipped corners as a black frame."""
        if sys.platform != "win32":
            return None
        import ctypes

        try:
            wid = self.root.winfo_id()
            hwnd = ctypes.windll.user32.GetAncestor(wid, 2)  # GA_ROOT
            if not hwnd:
                hwnd = ctypes.windll.user32.GetParent(wid) or wid
            return int(hwnd)
        except Exception:
            return None

    def _apply_round_region(self) -> None:
        """Clip the window (wrapper + child) to rounded corners."""
        if sys.platform != "win32":
            return
        import ctypes

        try:
            # Self-heal: if the wrapper appeared after _enable_glass ran,
            # re-target the accent to the real top-level window.
            top = self._top_hwnd()
            if top and top != self._hwnd:
                self._hwnd = top
                self._update_glass_color()
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            if w < 40 or h < 40:
                return
            r = 18
            targets = []
            if top:
                targets.append(top)
            try:
                child = int(self.root.winfo_id())
                if child not in targets:
                    targets.append(child)
            except Exception:
                pass
            sig = (tuple(targets), w, h)
            if sig == self._rgn_sig:
                return  # idempotent guard: SetWindowRgn storms freeze the loop
            self._rgn_sig = sig
            # Setting a region on the wrapper discards the pending wm
            # placement (position resets to 0,0); snapshot and restore it.
            pending = self.root.geometry()
            applied = False
            for hwnd in targets:
                region = ctypes.windll.gdi32.CreateRoundRectRgn(
                    0, 0, w + 1, h + 1, r * 2, r * 2
                )
                if region:
                    ok = ctypes.windll.user32.SetWindowRgn(
                        ctypes.c_void_p(hwnd), region, 1
                    )
                    if not ok:
                        ctypes.windll.gdi32.DeleteObject(region)
                    else:
                        applied = True
            if applied and pending != self.root.geometry():
                try:
                    self.root.geometry(pending)
                except tk.TclError:
                    pass
        except Exception:
            pass

    def _enable_glass(self) -> None:
        """Rounded corners (region is applied once the size is known) + optional
        whole-window translucency for the liquid-glass feel."""
        if sys.platform != "win32":
            return
        import ctypes

        class ACCENT_POLICY(ctypes.Structure):
            _fields_ = [
                ("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId", ctypes.c_uint),
            ]

        class WCA_DATA(ctypes.Structure):
            _fields_ = [
                ("Attribute", ctypes.c_ulong),
                ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t),
            ]

        self._accent_cls = ACCENT_POLICY
        self._wca_cls = WCA_DATA
        try:
            hwnd = self._top_hwnd()
            if not hwnd:
                hwnd = self.root.winfo_id()
            self._hwnd = int(hwnd)
            # NOTE: DWMWA_WINDOW_CORNER_PREFERENCE (33) / system backdrop (38)
            # are deliberately NOT set: when applied to the top-level window
            # they make DWM take over composition and ignore the GDI region,
            # which used to bleed the square wrapper background through the
            # rounded corners as a black frame. The r18 region set on both the
            # wrapper and the child is the single source of truth for rounding.
            # The DWM acrylic accent (SetWindowCompositionAttribute) is also
            # never enabled: its backdrop is drawn over the whole rect and
            # ignores SetWindowRgn, which put a dark square patch back into
            # the clipped corners (the reported black frame). The liquid-glass
            # feel comes from the Tk whole-window alpha instead.
            self._update_glass_color()
        except Exception:
            self._hwnd = None

    def _update_glass_color(self) -> None:
        """Never enable the DWM acrylic accent (it ignores SetWindowRgn and
        leaves a dark square patch in the rounded corners); only clear any
        leftover backdrop when glass is off."""
        if not self._hwnd:
            return
        import ctypes

        try:
            acc = self._accent_cls()
            acc.AccentState = 0  # ACCENT_ENABLE_NONE
            acc.AccentFlags = 2
            acc.GradientColor = THEMES[self.theme_name]["glass"]
            data = self._wca_cls()
            data.Attribute = 19  # WCA_ACCENT_POLICY
            data.Data = ctypes.addressof(acc)  # c_void_p needs a plain address
            data.SizeOfData = ctypes.sizeof(acc)
            user32 = ctypes.windll.user32
            user32.SetWindowCompositionAttribute.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            user32.SetWindowCompositionAttribute(
                ctypes.c_void_p(self._hwnd), ctypes.byref(data)
            )
        except Exception:
            pass

    def _apply_glass_mode(self) -> None:
        """Runtime toggle of the glass (state.glass live-read)."""
        try:
            self.root.attributes("-alpha", self._glass_alpha())
        except tk.TclError:
            pass
        self._update_glass_color()
        self._apply_theme()
        self._apply_round_region()

    def _icon_button(self, parent: tk.Widget, draw_fn, on_click, side: str) -> tk.Canvas:
        t = self._theme()
        cv = tk.Canvas(
            parent,
            width=22,
            height=22,
            bg=self._bg(t),
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        cv.pack(side=side, padx=3, pady=2)
        cv.bind("<Button-1>", lambda _e: on_click())
        self._icons.append((cv, draw_fn))
        draw_fn(cv, t)
        return cv

    def _draw_panel_icon(self, cv: tk.Canvas, t: dict) -> None:
        # rounded panel frame + back chevron = "return to the plugin panel"
        c = t["sub"]
        self._round_rect(
            cv, 2.5, 4.5, 19.5, 17.5, 3.5, outline=c, width=1.4, fill=""
        )
        cv.create_line(
            13, 7.5, 8, 11, 13, 14.5,
            fill=c, width=1.5, capstyle="round", joinstyle="round",
        )

    def _draw_theme_icon(self, cv: tk.Canvas, t: dict) -> None:
        # half-filled circle = light/dark theme toggle
        c = t["sub"]
        cv.create_oval(3.5, 3.5, 18.5, 18.5, outline=c, width=1.4)
        cv.create_arc(
            3.5, 3.5, 18.5, 18.5, start=90, extent=180, fill=c, outline=c, width=1.4
        )

    def _draw_snap_icon(self, cv: tk.Canvas, t: dict) -> None:
        # horseshoe magnet (poles down) = edge snapping; slash when disabled
        c = t["sub"]
        cv.create_arc(
            4.5, 4, 17.5, 17, start=0, extent=180, style="arc",
            outline=c, width=2.2,
        )
        cv.create_line(4.5, 10.5, 4.5, 15.5, fill=c, width=2.2, capstyle="round")
        cv.create_line(17.5, 10.5, 17.5, 15.5, fill=c, width=2.2, capstyle="round")
        cv.create_line(3, 16.8, 6, 16.8, fill=c, width=1.5, capstyle="round")
        cv.create_line(16, 16.8, 19, 16.8, fill=c, width=1.5, capstyle="round")
        if not self.snap:
            cv.create_line(3.5, 19, 19, 3.5, fill=c, width=1.6, capstyle="round")

    def _repaint_snap_icon(self) -> None:
        cv = getattr(self, "snap_btn", None)
        if cv is not None and cv.winfo_exists():
            cv.delete("all")
            self._draw_snap_icon(cv, self._theme())

    def _toggle_snap(self) -> None:
        # Flip locally for instant feedback; the server reply persists the
        # plugin config and is the source of truth (polls also reconcile).
        self.snap = not self.snap
        self._repaint_snap_icon()
        resp = self._request("/action", {"op": "toggle_snap"})
        if resp and resp.get("snap") is not None and bool(resp["snap"]) != self.snap:
            self.snap = bool(resp["snap"])
            self._repaint_snap_icon()

    def _draw_close_icon(self, cv: tk.Canvas, t: dict) -> None:
        c = t["sub"]
        cv.create_line(6.5, 6.5, 15.5, 15.5, fill=c, width=1.5, capstyle="round")
        cv.create_line(15.5, 6.5, 6.5, 15.5, fill=c, width=1.5, capstyle="round")


    # ── theming ─────────────────────────────────────────────────────────

    def _theme(self) -> dict:
        """Active theme with custom_colors.primary overlaid on accent."""
        t = THEMES[self.theme_name]
        if self.custom_colors:
            primary = _norm_hex(self.custom_colors.get("primary"))
            if primary:
                dark_bg = sum(_hex_to_rgb(str(t["bg"]))) < 380
                t = dict(t)
                t["accent"] = _fit_accent(primary, dark_bg)
        return t

    def _apply_theme(self) -> None:
        t = self._theme()
        bg = self._bg(t)  # one uniform surface for every container + text
        for w in (self.root, self.outer, self.topbar, self.cards_frame, self.dig_frame):
            w.configure(bg=bg)
        self.day_lbl.configure(bg=bg, fg=t["text"])
        self.sub_lbl.configure(bg=bg, fg=t["sub"])
        self.status_lbl.configure(bg=bg, fg=t["sub"])
        for cv, draw in self._icons:
            cv.configure(bg=bg)
            cv.delete("all")
            draw(cv, t)
        if self.empty_lbl is not None and self.empty_lbl.winfo_exists():
            self.empty_lbl.configure(bg=bg, fg=t["sub"])
        if self.more_lbl is not None and self.more_lbl.winfo_exists():
            self.more_lbl.configure(bg=bg, fg=t["sub"])
        self._update_glass_color()
        self._repaint_pills()
        if (
            self._detail_alive()
            and self.detail_ev is not None
        ):
            self._paint_detail(self.detail_ev)

    # ── data ────────────────────────────────────────────────────────────

    def _request(
        self, path: str, payload: dict | None = None, params: dict | None = None
    ) -> dict | None:
        q = {"token": self.token}
        if params:
            q.update(params)
        url = f"{self.base}{path}?{urllib.parse.urlencode(q)}"
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                # Token rotated (plugin reloaded): this window is stale.
                self.root.after(0, self.root.destroy)
                return None
            return None
        except Exception:
            return None

    def _poll(self) -> None:
        state = self._request("/state")
        if state is None:
            if not self.ever_connected:
                # Never reached the loopback yet (plugin still starting, or this
                # window was pointed at a dead port after a reload). Retry within
                # the grace window so a slow start still connects; past it, give
                # up instead of lingering as a blank window.
                if int(time.monotonic() * 1000) - self.start_ms >= CONNECT_GRACE_MS:
                    self.root.destroy()
                    return
            else:
                self.failures += 1
                if self.failures >= FAIL_EXIT_AFTER:
                    self.root.destroy()
                    return
        else:
            self.failures = 0
            self.ever_connected = True
            self._render(state)
        self.root.after(POLL_MS, self._poll)

    def _render(self, state: dict) -> None:
        theme = state.get("theme")
        if theme in THEMES and theme != self.theme_name:
            self.theme_name = theme
            self._apply_theme()
        cc = state.get("custom_colors")
        if cc != self.custom_colors:
            self.custom_colors = cc if isinstance(cc, dict) else None
            self.last_state = None
            self._apply_theme()
        t = self._theme()
        bg = self._bg(t)
        if "snap" in state:
            new_snap = bool(state.get("snap"))
            if new_snap != self.snap:
                self.snap = new_snap
                self._repaint_snap_icon()
        if "glass" in state and bool(state.get("glass")) != self.glass:
            self.glass = bool(state.get("glass"))
            self._apply_glass_mode()

        digest = state.get("digest") or {}
        cards = state.get("cards") or []
        serial = json.dumps(
            {"d": digest, "c": cards, "t": self.theme_name}, ensure_ascii=False
        )
        if serial == self.last_state:
            return
        self.last_state = serial

        self.day_lbl.configure(text=str(digest.get("day", "")))
        self.sub_lbl.configure(
            text=f"{digest.get('weekday', '')} · {digest.get('lunar', '')}"
        )

        # cards
        for child in self.cards_frame.winfo_children():
            child.destroy()
        for card in cards[:3]:
            card_title = str(card.get("title", ""))
            card_time = str(card.get("time", ""))
            key = card.get("key")
            wrap = tk.Frame(self.cards_frame, bg=bg)
            wrap.pack(fill="x", pady=(0, 3))
            cv = tk.Canvas(wrap, bg=bg, highlightthickness=0, height=40)
            cv.pack(fill="x")
            inner = tk.Frame(cv, bg=t["card"], padx=10, pady=7)
            # Actions are packed FIRST (right side) so a long title can never
            # push them out of the window; info is packed last and takes rest.
            del_btn = tk.Label(
                inner,
                text="✕",
                font=("Segoe UI", 10),
                bg=t["card"],
                fg="#E5484D",
                padx=3,
                cursor="hand2",
            )
            del_btn.pack(side="right")
            del_btn.bind(
                "<Button-1>", lambda _e, k=key: self._card_action(k, "card_delete")
            )
            snz_btn = tk.Label(
                inner,
                text="5分",
                font=("Microsoft YaHei UI", 8),
                bg=t["card"],
                fg=t["sub"],
                padx=4,
                cursor="hand2",
            )
            snz_btn.pack(side="right")
            snz_btn.bind(
                "<Button-1>", lambda _e, k=key: self._card_action(k, "snooze")
            )
            done_cv = tk.Canvas(
                inner,
                width=20,
                height=20,
                bg=t["card"],
                highlightthickness=0,
                cursor="hand2",
            )
            done_cv.pack(side="right", padx=(5, 2))
            done_cv.bind("<Configure>", lambda _e, c=done_cv: self._paint_done(c))
            done_cv.bind(
                "<Button-1>", lambda _e, k=key: self._card_action(k, "done")
            )
            info = tk.Frame(inner, bg=t["card"])
            info.pack(side="left", fill="x", expand=True)
            tk.Label(
                info,
                text=card_time,
                font=("Microsoft YaHei UI", 9, "bold"),
                bg=t["card"],
                fg=t["accent"],
            ).pack(anchor="w")
            title_lbl = tk.Label(
                info,
                text=card_title,
                font=("Microsoft YaHei UI", 9),
                bg=t["card"],
                fg=t["text"],
                anchor="w",
            )
            title_lbl.pack(anchor="w", fill="x")
            win = cv.create_window(0, 0, window=inner, anchor="nw")

            def _fit_card(
                _e=None,
                cv=cv,
                inner=inner,
                win=win,
                info=info,
                tl=title_lbl,
                card_title=card_title,
                t=t,
            ):
                cw = cv.winfo_width()
                if cw <= 4:
                    return
                cv.itemconfigure(win, width=cw)
                ih = inner.winfo_height()
                cv.configure(height=ih)
                cv.coords(win, 0, 0)
                cv.delete("cardbg")
                self._round_rect(
                    cv,
                    0.5,
                    0.5,
                    cw - 0.5,
                    max(1.5, ih - 0.5),
                    10,
                    fill=t["card"],
                    outline="",
                    tags="cardbg",
                )
                cv.tag_lower("cardbg")
                avail = max(60, info.winfo_width() - 6)
                tl.configure(text=self._trim_to_width(card_title, avail))

            inner.bind("<Configure>", _fit_card)
            cv.bind("<Configure>", _fit_card)
        if len(cards) > 3:
            tk.Label(
                self.cards_frame,
                text=f"还有 {len(cards) - 3} 条提醒…",
                font=("Microsoft YaHei UI", 8),
                bg=bg,
                fg=t["sub"],
                anchor="w",
            ).pack(fill="x")

        # digest pills (per sketch: capsule rows + centered "+N")
        for child in self.dig_frame.winfo_children():
            child.destroy()
        self.empty_lbl = None  # destroyed above; _apply_theme must not touch it
        self.more_lbl = None
        self.pills = []
        lines = digest.get("lines") or []
        total = int(digest.get("total") or len(lines))
        shown = lines[:3]
        keys = {str(l.get("key") or "") for l in shown}
        if not shown:
            self.empty_lbl = tk.Label(
                self.dig_frame,
                text="今天暂无安排",
                font=("Microsoft YaHei UI", 9),
                bg=bg,
                fg=t["sub"],
                anchor="center",
            )
            self.empty_lbl.pack(fill="x", pady=(2, 0))
        for ln in shown:
            cv = tk.Canvas(
                self.dig_frame,
                height=26,
                bg=bg,
                highlightthickness=0,
                cursor="hand2",
            )
            cv.pack(fill="x", pady=3)
            cv.bind("<Configure>", lambda _e, c=cv, l=ln: self._paint_pill(c, l))
            cv.bind(
                "<Button-1>",
                lambda e, c=cv, l=ln: self._on_pill_click(c, l, e),
            )
            self.pills.append((cv, ln))
        more_n = max(0, total - len(shown))
        if more_n:
            self.more_lbl = tk.Label(
                self.dig_frame,
                text=f"+{more_n}",
                font=("Microsoft YaHei UI", 10, "bold"),
                bg=bg,
                fg=t["sub"],
                anchor="center",
            )
            self.more_lbl.pack(fill="x", pady=(2, 0))
        self._repaint_pills()
        self.status_lbl.configure(text=self._status_text())
        self._fit()

    # ── pill painting ───────────────────────────────────────────────────

    @staticmethod
    def _round_rect(cv: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
                    r: float, **kw) -> int:
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return cv.create_polygon(points, smooth=True, **kw)

    def _paint_done(self, cv: tk.Canvas) -> None:
        w, h = cv.winfo_width(), cv.winfo_height()
        if w <= 2 or h <= 2:
            return
        t = self._theme()
        cv.delete("all")
        cv.create_oval(1, 1, w - 2, h - 2, fill=t["accent"], outline="")
        cv.create_text(
            w / 2,
            h / 2,
            text="✓",
            fill="#FFFFFF",
            font=("Segoe UI", 10, "bold"),
        )

    def _paint_pill(self, cv: tk.Canvas, ln: dict) -> None:
        w = cv.winfo_width()
        h = 26
        if w <= 4:
            return
        t = self._theme()
        key = str(ln.get("key") or "")
        sel = bool(key) and key == self.selected
        cv.configure(bg=self._bg(t))
        cv.delete("all")
        self._round_rect(
            cv, 1.5, 1.5, w - 1.5, h - 1.5, h / 2 - 1.5,
            fill=t["chip"],
            outline=t["accent"] if sel else t["border"],
            width=1.5 if sel else 1,
        )
        cy = h / 2 + 1
        time_s = str(ln.get("time", ""))
        tid = cv.create_text(
            14, cy, text=time_s, anchor="w",
            font=("Microsoft YaHei UI", 9, "bold"), fill=t["accent"],
        )
        bb = cv.bbox(tid)
        tx = (bb[2] + 12) if bb else 70
        avail = w - tx - 14
        if avail >= 12:
            title = self._trim_to_width(str(ln.get("title", "")), avail)
            if ln.get("done"):
                cv.create_text(
                    tx, cy, text=title, anchor="w",
                    font=("Microsoft YaHei UI", 9), fill=t["sub"],
                )
                cv.create_line(
                    tx, cy, tx + min(avail, self._f9.measure(title)), cy,
                    fill=t["sub"],
                )
            else:
                cv.create_text(
                    tx, cy, text=title, anchor="w",
                    font=("Microsoft YaHei UI", 9), fill=t["text"],
                )
    def _status_text(self) -> str:
        if self.selected and self._detail_alive():
            return "详情可完成或删除"
        return "点击日程查看详情"

    def _on_pill_click(self, cv: tk.Canvas, ln: dict, event: tk.Event) -> None:
        key = str(ln.get("key") or "")
        if not key:
            return
        if (self.selected == key and self._detail_alive()):
            self._close_detail()  # clicking the same pill again closes the overlay
            return
        self.selected = key
        self._open_detail(key)
        self.status_lbl.configure(text=self._status_text())
        self._repaint_pills()

    def _trim_to_width(self, text: str, maxw: int) -> str:
        if self._f9.measure(text) <= maxw:
            return text
        while text and self._f9.measure(text + "…") > maxw:
            text = text[:-1]
        return text + ("…" if text else "")

    def _repaint_pills(self) -> None:
        for cv, ln in self.pills:
            if cv.winfo_exists():
                self._paint_pill(cv, ln)

    # ── task detail overlay (in-window, panel-modal style) ─────────────────

    @staticmethod
    def _detail_mix(c1: str, c2: str, ratio: float) -> str:
        """Blend two #rrggbb colours (ratio = weight of c1)."""
        a, b = _hex_to_rgb(c1), _hex_to_rgb(c2)
        return "#%02x%02x%02x" % tuple(
            max(0, min(255, int(a[i] * ratio + b[i] * (1 - ratio))))
            for i in range(3)
        )

    def _detail_alive(self) -> bool:
        return self.detail_layer is not None and self.detail_layer.winfo_exists()

    def _detail_escape(self, _event: tk.Event | None = None) -> str | None:
        if self._detail_alive():
            self._close_detail()
            return "break"
        return None

    def _open_detail(self, key: str) -> None:
        resp = self._request("/detail", params={"key": key})
        ev = (resp or {}).get("event")
        if not ev:
            self._close_detail()  # restores default status text
            if resp and resp.get("error"):
                self.status_lbl.configure(text=str(resp["error"])[:24])
            return
        self._detail_del_armed = False
        self._detail_hover = None
        self.detail_key = key
        self.detail_ev = ev
        self.detail_serial = json.dumps(ev, ensure_ascii=False, sort_keys=True)
        self._build_detail_layer()
        self._paint_detail(ev)

    def _build_detail_layer(self) -> None:
        if self._detail_alive():
            self.detail_layer.lift()
            return
        layer = tk.Frame(self.root, bg=self._bg(self._theme()))
        self.detail_layer = layer
        layer.place(relx=0, rely=0, relwidth=1, relheight=1)
        layer.lift()

    # painting -------------------------------------------------------------

    def _paint_detail(self, ev: dict) -> None:
        layer = self.detail_layer
        if layer is None or not layer.winfo_exists():
            return
        t = self._theme()
        bg = self._bg(t)
        # claim the current size first: update_idletasks below may fire
        # <Configure>, which must not re-enter this painter
        self._detail_size_sig = (
            self.root.winfo_width(), self.root.winfo_height()
        )
        layer.configure(bg=bg)
        for child in layer.winfo_children():
            child.destroy()
        self.detail_btn_cv = None
        pad = 14
        inner = max(120, self.root.winfo_width() - pad * 2)
        self._detail_inner = inner

        head = tk.Frame(layer, bg=bg)
        head.pack(fill="x", padx=pad, pady=(13, 0))
        tk.Label(
            head, text=str(ev.get("title") or ""), bg=bg, fg=t["text"],
            font=("Microsoft YaHei UI", 12, "bold"),
            wraplength=max(60, inner - 26), justify="left", anchor="w",
        ).pack(side="left", fill="x", expand=True)
        x_btn = tk.Label(
            head, text="✕", font=("Segoe UI", 10), bg=bg, fg=t["sub"],
            cursor="hand2", padx=2,
        )
        x_btn.pack(side="right")
        x_btn.bind("<Button-1>", self._detail_x_click)
        x_btn.bind("<Enter>", lambda _e: x_btn.configure(fg="#E5484D"))
        x_btn.bind("<Leave>", lambda _e: x_btn.configure(fg=t["sub"]))

        tk.Label(
            layer, text="时间", bg=bg, fg=t["sub"],
            font=("Microsoft YaHei UI", 9), anchor="w",
        ).pack(fill="x", padx=pad, pady=(12, 0))
        time_cv = tk.Canvas(layer, height=26, bg=bg, highlightthickness=0)
        time_cv.pack(fill="x", padx=pad, pady=(3, 0))
        self._draw_field(time_cv, inner, 26)
        time_cv.create_text(
            10, 13, anchor="w",
            text=self._trim_to_width(str(ev.get("dt") or "—"), inner - 26),
            font=("Microsoft YaHei UI", 9), fill=t["text"],
        )

        tk.Label(
            layer, text="备注", bg=bg, fg=t["sub"],
            font=("Microsoft YaHei UI", 9), anchor="w",
        ).pack(fill="x", padx=pad, pady=(11, 0))
        note = str(ev.get("note") or "").strip()
        note_cv = tk.Canvas(layer, height=30, bg=bg, highlightthickness=0)
        note_cv.pack(fill="x", padx=pad, pady=(3, 0))

        foot = tk.Canvas(layer, height=30, bg=bg, highlightthickness=0)
        foot.pack(fill="x", padx=pad, pady=(12, 13))
        foot.bind("<Button-1>", self._detail_btn_click)
        foot.bind("<Motion>", self._detail_btn_motion)
        foot.bind("<Leave>", lambda _e: self._detail_hover_set(None))
        self.detail_btn_cv = foot

        # size the note box to its text, capped so the footer stays visible
        try:
            layer.update_idletasks()
            avail = self.root.winfo_height() - foot.winfo_y() - 30 - 13 - 8
        except tk.TclError:
            avail = 60
        self._paint_field_text(note_cv, inner, note, avail)
        self._paint_detail_btns(bool(ev.get("done")))

    def _draw_field(self, cv: tk.Canvas, w: int, h: int) -> None:
        """Panel input-box look: card fill + hairline border, r8."""
        t = self._theme()
        rid = self._round_rect(
            cv, 0.5, 0.5, w - 0.5, h - 0.5, 8,
            fill=t["card"], outline=t["border"], width=1,
        )
        cv.tag_lower(rid)

    def _paint_field_text(
        self, cv: tk.Canvas, inner: int, note: str, avail: int
    ) -> None:
        t = self._theme()
        text = note or "无备注"
        tid = cv.create_text(
            10, 8, text=text, anchor="nw", width=inner - 26,
            font=("Microsoft YaHei UI", 9),
            fill=t["text"] if note else t["sub"],
        )
        bbox = cv.bbox(tid)
        content = (bbox[3] - bbox[1] + 16) if bbox else 30
        h = max(30, min(content, max(30, int(avail))))
        cv.configure(height=h)
        self._draw_field(cv, inner, h)
        # wheel-scrolling state: overflow is reachable by scrolling
        self._note_cv = cv
        self._note_tid = tid
        self._note_view_h = h
        self._note_scroll = 0
        self._note_scroll_max = max(0, content - h)
        self._note_thumb_id = None
        if self._note_scroll_max > 0:
            self._draw_note_thumb(cv, inner, h, self._bg(t), t)
        cv.bind("<MouseWheel>", self._detail_note_wheel)

    def _draw_note_thumb(
        self, cv: tk.Canvas, inner: int, h: int, bg: str, t: dict
    ) -> None:
        """Slim scrollbar thumb on the right edge (only when scrollable)."""
        if self._note_scroll_max <= 0 or h <= 8:
            return
        top, bot = 3.0, h - 3.0
        track = bot - top
        thumb_h = max(12.0, track * h / (h + self._note_scroll_max))
        span = track - thumb_h
        y0 = top + span * (self._note_scroll / self._note_scroll_max)
        col = self._detail_mix(t["sub"], bg, 0.5)
        self._note_thumb_id = self._round_rect(
            cv, inner - 8, y0, inner - 5, y0 + thumb_h, 1.5,
            fill=col, outline="",
        )

    def _detail_note_wheel(self, event: tk.Event) -> str | None:
        """Scroll the note box with the mouse wheel (delta 120/notch)."""
        cv = self._note_cv
        if (
            cv is None
            or not cv.winfo_exists()
            or self._note_scroll_max <= 0
            or self._note_tid is None
        ):
            return None
        step = 24
        direction = -1 if getattr(event, "delta", 0) > 0 else 1
        new = max(0, min(self._note_scroll_max, self._note_scroll + direction * step))
        if new == self._note_scroll:
            return "break"
        self._note_scroll = new
        try:
            cv.coords(self._note_tid, 10, 8 - new)
        except tk.TclError:
            return None
        t = self._theme()
        if self._note_thumb_id is not None:
            try:
                cv.delete(self._note_thumb_id)
            except tk.TclError:
                pass
            self._note_thumb_id = None
        self._draw_note_thumb(cv, self._detail_inner, self._note_view_h, self._bg(t), t)
        return "break"

    def _detail_hover_set(self, which: str | None) -> None:
        if self._detail_hover == which:
            return
        self._detail_hover = which
        self._paint_detail_btns(bool((self.detail_ev or {}).get("done")))

    def _detail_btn_motion(self, event: tk.Event) -> None:
        """Hover feedback: tk only tracks the canvas 'current' tag on motion,
        so enter/leave between the two pills must be derived from it."""
        cv = self.detail_btn_cv
        if cv is None or not cv.winfo_exists():
            return
        which = None
        for iid in cv.find_withtag("current"):
            tags = cv.gettags(iid)
            if "pd_done" in tags:
                which = "done"
                break
            if "pd_del" in tags:
                which = "del"
                break
        self._detail_hover_set(which)
        try:
            cv.configure(cursor="hand2" if which else "")
        except tk.TclError:
            pass

    def _detail_x_click(self, _event: tk.Event | None = None):
        self._close_detail()
        return "break"

    def _paint_detail_btns(self, done: bool) -> None:
        cv = self.detail_btn_cv
        if cv is None or not cv.winfo_exists():
            return
        t = self._theme()
        bg = self._bg(t)
        inner = self._detail_inner or max(120, self.root.winfo_width() - 28)
        cv.configure(bg=bg)
        cv.delete("all")
        if self._f9b is None:
            self._f9b = tkfont.Font(
                family="Microsoft YaHei UI", size=9, weight="bold"
            )
        danger = "#E5484D"
        armed = self._detail_del_armed
        dtext = "确认删除？" if armed else "删除"
        dpw = self._f9b.measure(dtext) + 26
        if armed:
            dfill = danger
            if self._detail_hover == "del":
                dfill = self._detail_mix(danger, "#FFFFFF", 0.90)
            doutline, dcol = "", "#FFFFFF"
        else:
            dfill = ""
            if self._detail_hover == "del":
                dfill = self._detail_mix(danger, bg, 0.12)
            doutline, dcol = danger, danger
        self._round_rect(
            cv, 1.5, 1.5, dpw, 28.5, 13.5,
            fill=dfill, outline=doutline, width=1.2, tags="pd_del",
        )
        cv.create_text(
            dpw / 2, 15, text=dtext, anchor="center", font=self._f9b,
            fill=dcol, tags="pd_del",
        )
        label = "↩ 撤销完成" if done else "✓ 完成"
        pw = self._f9b.measure(label) + 26
        x2 = inner - 1.5
        x1 = x2 - pw
        fill = t["accent"]
        if self._detail_hover == "done":
            fill = self._detail_mix(t["accent"], bg, 0.85)
        self._round_rect(
            cv, x1, 1.5, x2, 28.5, 13.5,
            fill=fill, outline="", tags="pd_done",
        )
        cv.create_text(
            (x1 + x2) / 2, 15, text=label, font=self._f9b, fill="#FFFFFF",
            tags="pd_done",
        )

    def _detail_btn_click(self, event: tk.Event):
        cv = self.detail_btn_cv
        if cv is None or not cv.winfo_exists():
            return "break"
        tags: set = set()
        for iid in cv.find_withtag("current"):
            tags.update(cv.gettags(iid))
        if "pd_done" in tags:
            self._detail_toggle_done()
            return "break"
        if "pd_del" in tags:
            self._detail_delete()
            return "break"
        return None  # empty footer area: let the window drag take over

    def _detail_toggle_done(self) -> None:
        key = self.detail_key
        if not key:
            return
        if self._request("/action", {"op": "ev_done", "key": key}) is None:
            return
        self.last_state = None
        self._poll_once_now()  # pills, popup and status refresh via /state

    def _detail_delete(self) -> None:
        key = self.detail_key
        if not key:
            return
        if not self._detail_del_armed:
            self._detail_del_armed = True
            self._paint_detail_btns(bool((self.detail_ev or {}).get("done")))
            self.status_lbl.configure(text="再点一次确认删除")
            self.root.after(4000, self._detail_disarm)
            return
        if self._request("/action", {"op": "ev_delete", "key": key}) is None:
            return
        self._detail_del_armed = False
        self._close_detail()
        self.status_lbl.configure(text="已删除")
        self.last_state = None
        self._poll_once_now()

    def _detail_disarm(self) -> None:
        if not self._detail_del_armed:
            return
        self._detail_del_armed = False
        self._paint_detail_btns(bool((self.detail_ev or {}).get("done")))
        self.status_lbl.configure(text=self._status_text())

    def _sync_detail(self) -> None:
        """Every poll: re-fetch overlay data, repaint on change, close if gone."""
        if not self._detail_alive():
            return
        key = self.detail_key
        if not key:
            self._close_detail()
            return
        resp = self._request("/detail", params={"key": key})
        if resp is None:
            return  # transient error: keep showing current content
        ev = resp.get("event")
        if not ev:
            self._close_detail()  # event deleted remotely
            self.status_lbl.configure(text="日程已删除")
            return
        sig = json.dumps(ev, ensure_ascii=False, sort_keys=True)
        if sig != self.detail_serial:
            prev = self.detail_ev or {}
            self.detail_serial = sig
            self.detail_ev = ev
            # only the done flag flipped → patch the footer in place
            # (no full rebuild, so the overlay doesn't flicker)
            others_same = (
                {k: v for k, v in prev.items() if k != "done"}
                == {k: v for k, v in ev.items() if k != "done"}
            )
            if others_same:
                self._paint_detail_btns(bool(ev.get("done")))
            else:
                self._paint_detail(ev)

    def _close_detail(self) -> None:
        self._detail_del_armed = False
        self._detail_hover = None
        self.detail_key = None
        self.detail_ev = None
        self.detail_serial = None
        self.detail_btn_cv = None
        self._note_cv = None
        self._note_tid = None
        self._note_scroll = 0
        self._note_scroll_max = 0
        self._note_view_h = 0
        self._note_thumb_id = None
        layer = self.detail_layer
        self.detail_layer = None
        if layer is not None:
            try:
                if layer.winfo_exists():
                    layer.destroy()
            except tk.TclError:
                pass
        if self.selected:
            self.selected = None
            self.status_lbl.configure(text=self._status_text())
            self._repaint_pills()

    def _on_root_moved(self, _event: tk.Event | None = None) -> None:
        self._apply_round_region()
        if not self._detail_alive():
            return
        sig = (self.root.winfo_width(), self.root.winfo_height())
        if sig != self._detail_size_sig:
            self._detail_size_sig = sig
            if self.detail_ev is not None:
                self._paint_detail(self.detail_ev)

    def _fit(self) -> None:
        """Resize to content while keeping the window fully inside the work area."""
        self.root.update_idletasks()
        wa_l, wa_t, wa_r, wa_b = self._work_area()
        w = max(240, int(self.root.winfo_screenwidth() * self.ratio / 100))
        h = self.root.winfo_reqheight()
        x = min(max(wa_l, self.root.winfo_x()), max(wa_l, wa_r - w))
        y = self.root.winfo_y()
        if y + h > wa_b - 8:  # content grew past the work-area bottom edge
            y = max(wa_t, wa_b - h - 40)
        self.root.geometry(f"{w}x{h}+{int(x)}+{int(y)}")
        self._apply_round_region()

    # ── actions ─────────────────────────────────────────────────────────

    def _card_action(self, key: str | None, op: str) -> None:
        if not key:
            return
        if self._request("/action", {"op": op, "key": key}) is not None:
            self.last_state = None
            self._poll_once_now()

    def _poll_once_now(self) -> None:
        state = self._request("/state")
        if state is not None:
            self.failures = 0
            self._render(state)
            self._sync_detail()

    def _cycle_theme(self) -> None:
        # Paint the next theme locally first so the click feels instant;
        # the server reply is the source of truth and reconciles any drift.
        try:
            nxt = THEME_ORDER[(THEME_ORDER.index(self.theme_name) + 1) % len(THEME_ORDER)]
        except ValueError:
            nxt = THEME_ORDER[0]
        self.theme_name = nxt
        self._apply_theme()
        resp = self._request("/action", {"op": "cycle_theme"})
        if resp and resp.get("theme") in THEMES and resp["theme"] != self.theme_name:
            self.theme_name = resp["theme"]
            self._apply_theme()
        self.last_state = None
        self._poll_once_now()

    def _open_panel(self) -> None:
        if self.panel_url:
            webbrowser.open(self.panel_url)

    def _back_to_panel(self) -> None:
        """Open the plugin panel in the browser and close this window."""
        self._open_panel()
        self._request("/action", {"op": "quit", "panel": 1})
        self.root.destroy()

    def _quit(self) -> None:
        self._request("/action", {"op": "quit"})
        self.root.destroy()

    # ── dragging ────────────────────────────────────────────────────────

    def _drag_start(self, event: tk.Event) -> None:
        self._stop_open_anim()  # startup slide-in must not fight the user
        self._stop_snap_anim()  # user grabbed the window: cancel any dock anim
        self.drag_origin = (event.x_root, event.y_root)
        self.drag_press = (event.x_root, event.y_root, time.time())
        self._drag_moved_far = False

    def _drag_move(self, event: tk.Event) -> None:
        if self.drag_origin is None or self.drag_press is None:
            return
        if abs(event.x_root - self.drag_press[0]) + abs(
            event.y_root - self.drag_press[1]
        ) >= 8:
            self._drag_moved_far = True
        dx = event.x_root - self.drag_origin[0]
        dy = event.y_root - self.drag_origin[1]
        if abs(dx) + abs(dy) < 3:
            return
        x = self.root.winfo_x() + dx
        y = self.root.winfo_y() + dy
        self.root.geometry(f"+{int(x)}+{int(y)}")
        self.drag_origin = (event.x_root, event.y_root)

    def _drag_end(self, _event: tk.Event) -> None:
        if self.drag_origin is None:
            return
        self.drag_origin = None
        # Click-vs-drag (desktop_pet semantics): a press/release that moved
        # <8px within 0.25s is a click (e.g. hitting a topbar button) and must
        # NOT trigger docking.
        held = time.time() - self.drag_press[2] if self.drag_press else 99.0
        moved_far = self._drag_moved_far
        self.drag_press = None
        self._drag_moved_far = False
        if held < 0.25 and not moved_far:
            return
        self._snap_release()

    def _snap_release(self) -> None:
        """Dock toward the nearest work-area edge with a glide animation.

        There is deliberately NO distance threshold (desktop_pet behavior):
        while the toggle is on, releasing the window always magnet-docks it.
        """
        if not self.snap:
            return
        wa_l, wa_t, wa_r, wa_b = self._work_area()
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        # nearest edge by the smallest remaining gap (negative = already past)
        gaps = (
            (x - wa_l, wa_l, y),
            (wa_r - w - x, wa_r - w, y),
            (y - wa_t, x, wa_t),
            (wa_b - h - y, x, wa_b - h),
        )
        _gap, tx, ty = min(gaps, key=lambda g: g[0])
        self._animate_snap_to(int(tx), int(ty))

    def _animate_snap_to(self, tx: int, ty: int) -> None:
        """320ms OutCubic glide to (tx, ty), then persist the position."""
        self._stop_open_anim()
        self._stop_snap_anim()
        x0, y0 = self.root.winfo_x(), self.root.winfo_y()
        if x0 == tx and y0 == ty:
            self._send_pos()
            return
        self._snap_anim = {
            "x0": x0, "y0": y0, "tx": tx, "ty": ty,
            "t0": time.time(), "step": None,
        }
        self._anim_tick()

    def _anim_tick(self) -> None:
        st = self._snap_anim
        if st is None:
            return
        p = min(1.0, (time.time() - st["t0"]) / 0.32)
        e = 1.0 - (1.0 - p) ** 3  # easeOutCubic
        x = st["x0"] + (st["tx"] - st["x0"]) * e
        y = st["y0"] + (st["ty"] - st["y0"]) * e
        self.root.geometry(f"+{int(x)}+{int(y)}")
        if p < 1.0:
            st["step"] = self.root.after(16, self._anim_tick)
        else:
            self.root.geometry(f"+{st['tx']}+{st['ty']}")
            self._snap_anim = None
            self._send_pos()

    def _stop_snap_anim(self) -> None:
        st = self._snap_anim
        if st is None:
            return
        if st.get("step") is not None:
            try:
                self.root.after_cancel(st["step"])
            except tk.TclError:
                pass
        self._snap_anim = None

    def _send_pos(self) -> None:
        self._request(
            "/action",
            {"op": "pos", "x": self.root.winfo_x(), "y": self.root.winfo_y()},
        )

    # ── system tray ──────────────────────────────────────────────────────

    def _tray_image(self):
        # hand-composed 32x32 calendar glyph, no external assets
        img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((2, 3, 29, 29), radius=7, fill="#2563EB")
        d.rounded_rectangle((9, 1, 12, 6), radius=1.5, fill="#1D4ED8")
        d.rounded_rectangle((19, 1, 22, 6), radius=1.5, fill="#1D4ED8")
        d.rounded_rectangle((5, 9, 26, 26), radius=4, fill="#FFFFFF")
        d.rectangle((5, 9, 26, 14), fill="#2563EB")
        d.ellipse((13, 17, 19, 23), fill="#2563EB")
        return img

    def _start_tray(self) -> None:
        if not _HAS_TRAY:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem(
                    "打开完整面板",
                    lambda: self.tray_queue.put("panel"),
                    default=True,
                ),
                pystray.MenuItem("退出", lambda: self.tray_queue.put("quit")),
            )
            self.tray = pystray.Icon(
                "astrbot_calendar", self._tray_image(), "日历日程", menu
            )
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception as exc:
            self.tray = None
            print(f"[calendar] tray disabled: {exc}", flush=True)

    def _drain_tray(self) -> None:
        # Tray menu callbacks run on the pystray thread; marshal to Tk here.
        try:
            while True:
                act = self.tray_queue.get_nowait()
                if act == "panel":
                    self._open_panel()
                elif act == "quit":
                    try:
                        self._quit()
                    except tk.TclError:
                        pass
                    return
        except queue.Empty:
            pass
        try:
            if self.root.winfo_exists():
                self.root.after(250, self._drain_tray)
        except tk.TclError:
            pass

    def run(self) -> None:
        self._start_tray()
        try:
            self.root.mainloop()
        finally:
            if self.tray is not None:
                try:
                    self.tray.stop()
                except Exception:
                    pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Calendar mini window")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--ratio", type=int, default=12)
    parser.add_argument("--theme", default="light")
    parser.add_argument("--panel-url", default="")
    parser.add_argument("--snap", choices=("on", "off"), default="on")
    parser.add_argument("--glass", choices=("on", "off"), default="on")
    parser.add_argument("--x", type=int, default=None)
    parser.add_argument("--y", type=int, default=None)
    args = parser.parse_args()

    if sys.platform == "win32":  # crisp rendering on high-DPI screens
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    win = CalendarWindow(args)
    win.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
