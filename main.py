"""AstrBot calendar / schedule plugin (Apple Calendar style).

Backend: storage, Web API (panel bridge), LLM tools, reminder scheduler and
the desktop mini-window process manager. Frontend lives in pages/calendar/.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

from . import lunar
from . import recurrence as rec
from .recurrence import (
    COLOR_CHOICES,
    REPEAT_CHOICES,
    dt_str,
    load_json_file,
    occ_key,
    occurrence_end,
    occurrence_starts,
    parse_date,
    parse_datetime,
    reminder_fire_time,
)

try:  # FunctionTool SDK (>= 4.5.1)
    from pydantic import Field
    from pydantic.dataclasses import dataclass as pyd_dataclass

    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.tool import FunctionTool, ToolExecResult
    from astrbot.core.astr_agent_context import AstrAgentContext

    _HAS_TOOLS = True
except Exception:  # pragma: no cover - depends on host version
    _HAS_TOOLS = False

PLUGIN_NAME = "astrbot_plugin_calendar"
PANEL_PATH = f"/plugin-page/{PLUGIN_NAME}/calendar"
THEME_ORDER = ["dark", "light", "aurora", "sakura"]
TICK_SECONDS = 15
REMINDER_FIRE_GRACE = dt.timedelta(minutes=10)
REMINDER_CARD_MAX_AGE = dt.timedelta(days=7)

PROTOCOL = (
    "仅在调用本工具时遵循以下流程：1) 只问缺失字段，用户已说清的绝不再问；"
    "2) 起止时间缺失必须询问，颜色/备注/提醒/重复/谁的日程等可选字段未说明则用默认值，"
    "并用一句话说明「未说明则用默认」，不要逐项追问；3) 所有缺失字段在同一条消息里一次问完；"
    "4) 问齐后复述摘要并等用户确认（例：「→ 明早8:00 交作业，每天重复，提前15分钟提醒，确认添加？」），"
    "用户确认后才调用本工具；5) 时间一律写成绝对时间 YYYY-MM-DD HH:MM，"
    "根据当前时间解析「明天/下周三」等相对表述；"
    "6) 用户说「每天/每周…持续到/直到/截止到某日」时，必须把该日解析为 repeat_until 参数传入"
    "（这时不再询问），绝不能把终止日期只写进 note；"
    "用户要求重复但没说持续多久时，在摘要里附一句「未加终止日期，会一直重复」。"
    "日历为全局单一日历，所有会话共享。"
)


def _weekday_cn(idx: int) -> str:
    return "一二三四五六日"[idx % 7]


_DIARY_TAG_TAIL = "，。！？、；：,.!?;:\"'）)】]》…"


def extract_tags(text: str) -> list[str]:
    """从标题/正文中提取 #标签（支持中文）；#前须为空白或行首，剔除结尾标点。"""
    out: list[str] = []
    for m in re.finditer(r"(?:^|\s)#([^\s#]{1,30})", text or ""):
        tag = m.group(1).strip(_DIARY_TAG_TAIL)
        if tag and tag not in out:
            out.append(tag)
    return out


MAX_BG_BYTES = 8 * 1024 * 1024
BG_FILE_NAME = "panel_bg.img"
BG_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

AI_SYS_PROMPT = (
    "你是日历日程助手，负责把用户的自然语言变成日程，或回答日历相关问题。\n"
    "当前时间：{now}（时区 {tz}，周{wd}）。\n"
    "只输出一个 JSON 对象，不要输出 JSON 之外的任何文字，不要 markdown 代码块。\n"
    '格式：{"reply":"给用户的简短中文回复", "events":[日程对象数组]}\n'
    "events 规则：不需要创建日程时为 []；需要创建时每个元素为 "
    '{"title":"标题(必填,≤120字)", "start":"YYYY-MM-DD HH:MM", '
    '"end":"YYYY-MM-DD HH:MM", "all_day":false, "repeat":"none|daily|weekly|monthly", '
    '"repeat_until":"重复终止日期 YYYY-MM-DD（仅 repeat 非 none 且用户说了持续期限时才给）", '
    '"remind_minutes":0, "color":"blue|teal|indigo|slate", "note":""}。\n'
    "start 必须早于 end；end 缺省为 start 后 1 小时；用户没说年份就按当前时间推断；"
    "没说提醒用 0；没说颜色用 blue；没说重复用 none；"
    "用户说「持续到/直到/截止到某日」的重复必须解析出该日期填入 repeat_until，不能只写进 note。"
    "一次最多创建 3 个日程。\n"
    "reply 要自然友好、一句话说明创建结果或回答内容。"
)


def _detect_image_mime(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def _norm_opacity(value, default: float = 0.3) -> float:
    try:
        op = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(op, 0.05), 0.9)


_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _norm_hex(value) -> str | None:
    """Normalize to lowercase #rrggbb, or None when not a valid hex color."""
    s = str(value or "").strip()
    if len(s) == 4 and s.startswith("#"):  # #rgb -> #rrggbb
        s = "#" + "".join(ch * 2 for ch in s[1:])
    return s.lower() if _HEX_RE.match(s) else None


def _parse_ai_json(raw: str) -> dict | None:
    s = (raw or "").strip()
    if s.startswith("```"):
        nl = s.find("\n")
        s = s[nl + 1:] if nl > 0 else s
        tail = s.rstrip()
        if tail.endswith("```"):
            s = tail[:-3]
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        obj = json.loads(s[i:j + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


# ---------------------------------------------------------------------------
# LLM tools (registered only when enable_ai_tools is on)
# ---------------------------------------------------------------------------

if _HAS_TOOLS:

    @pyd_dataclass
    class CreateEventTool(FunctionTool[AstrAgentContext]):
        """Create a calendar event after the confirmation protocol."""

        name: str = "create_event"
        description: str = "创建日程/提醒。" + PROTOCOL
        parameters: dict = Field(
            default_factory=lambda: {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "日程标题"},
                    "start": {
                        "type": "string",
                        "description": "开始时间，绝对格式 YYYY-MM-DD HH:MM",
                    },
                    "end": {
                        "type": "string",
                        "description": "结束时间 YYYY-MM-DD HH:MM；缺省为开始后 1 小时",
                    },
                    "all_day": {
                        "type": "boolean",
                        "description": "是否全天日程；缺省 false",
                    },
                    "repeat": {
                        "type": "string",
                        "enum": list(REPEAT_CHOICES),
                        "description": "重复规则；缺省 none",
                    },
                    "repeat_until": {
                        "type": "string",
                        "description": (
                            "重复终止日期 YYYY-MM-DD 或 YYYY-MM-DD HH:MM；仅 repeat 非 none 时使用。"
                            "用户说「持续到/直到/截止到某日」时必须解析为该参数传入，"
                            "不能只写进 note；未说持续期限则不传（会一直重复）"
                        ),
                    },
                    "remind_minutes": {
                        "type": "integer",
                        "description": "提前提醒分钟数，0 表示不提醒；缺省用默认值",
                    },
                    "note": {"type": "string", "description": "备注；缺省空字符串"},
                    "color": {
                        "type": "string",
                        "enum": list(COLOR_CHOICES),
                        "description": "颜色分组；缺省 blue",
                    },
                    "owner": {
                        "type": "string",
                        "description": (
                            "这条日程是谁的（人名，如：我/室友/全家）；"
                            "缺省空字符串"
                        ),
                    },
                },
                "required": ["title", "start"],
            }
        )
        plugin: Any = None

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return self.plugin.tool_create_event(**kwargs)

    @pyd_dataclass
    class ListEventsTool(FunctionTool[AstrAgentContext]):
        """List events in a date range or by keyword."""

        name: str = "list_events"
        description: str = (
            "查询日程：按日期范围或关键词列出已有日程，返回每条的 id，"
            "可用于后续 update_event / delete_event。仅查询，无需用户确认。"
        )
        parameters: dict = Field(
            default_factory=lambda: {
                "type": "object",
                "properties": {
                    "from_date": {
                        "type": "string",
                        "description": "起始日期 YYYY-MM-DD；缺省为今天",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "结束日期 YYYY-MM-DD；缺省为起始日期 +7 天",
                    },
                    "query": {
                        "type": "string",
                        "description": "关键词，匹配标题或备注；缺省不过滤",
                    },
                },
                "required": [],
            }
        )
        plugin: Any = None

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return self.plugin.tool_list_events(**kwargs)

    @pyd_dataclass
    class ListCountdownTool(FunctionTool[AstrAgentContext]):
        """Read-only countdown list for upcoming todos."""

        name: str = "list_countdown"
        description: str = (
            "查询最近待办的倒计时：未来最近的未完成日程及距开始还有多久"
            "（全天日程按全天提醒时刻计，单位取插件设置的周/天/小时）。"
            "仅查询，无需用户确认。"
        )
        parameters: dict = Field(
            default_factory=lambda: {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "条数，缺省 8，最大 20",
                    },
                },
                "required": [],
            }
        )
        plugin: Any = None

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return self.plugin.tool_list_countdown(**kwargs)

    @pyd_dataclass
    class AddDiaryTool(FunctionTool[AstrAgentContext]):
        """Write one daily diary entry (plain text)."""

        name: str = "add_diary"
        description: str = (
            "写入一篇每日日记（纯文本：标题+正文）。在标题或正文里写 #标签 "
            "会自动成为标签；同一天可写多条。直接写入，无需用户确认。"
        )
        parameters: dict = Field(
            default_factory=lambda: {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "日记标题（必填，≤200字）"},
                    "content": {"type": "string", "description": "日记正文（纯文本，可多行；用 #xxx 标注标签）"},
                    "date": {
                        "type": "string",
                        "description": "归属日期 YYYY-MM-DD；缺省为今天",
                    },
                },
                "required": ["title"],
            }
        )
        plugin: Any = None

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return self.plugin.tool_add_diary(**kwargs)

    @pyd_dataclass
    class ListDiaryTool(FunctionTool[AstrAgentContext]):
        """Search diary entries by keyword or tag."""

        name: str = "list_diary"
        description: str = (
            "检索日记：按关键词（匹配标题与正文）或 #标签 查询，"
            "返回每条的 id、日期、标题、标签与正文摘要。仅查询，无需确认。"
        )
        parameters: dict = Field(
            default_factory=lambda: {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "关键词，匹配标题或正文；缺省不过滤"},
                    "tag": {"type": "string", "description": "按标签过滤（不含 # 号）"},
                    "date": {"type": "string", "description": "只看某天 YYYY-MM-DD；缺省全部"},
                    "limit": {"type": "integer", "description": "条数，缺省 10，最大 30"},
                },
                "required": [],
            }
        )
        plugin: Any = None

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return self.plugin.tool_list_diary(**kwargs)

    @pyd_dataclass
    class UpdateEventTool(FunctionTool[AstrAgentContext]):
        """Update an existing event after user confirmation."""

        name: str = "update_event"
        description: str = (
            "修改已有日程（先 list_events 拿 id）。改动内容需先向用户复述并确认后再调用。"
            + PROTOCOL
        )
        parameters: dict = Field(
            default_factory=lambda: {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "日程 id（来自 list_events）"},
                    "title": {"type": "string", "description": "新标题"},
                    "start": {"type": "string", "description": "新开始时间 YYYY-MM-DD HH:MM"},
                    "end": {"type": "string", "description": "新结束时间 YYYY-MM-DD HH:MM"},
                    "all_day": {"type": "boolean", "description": "是否全天"},
                    "repeat": {
                        "type": "string",
                        "enum": list(REPEAT_CHOICES),
                        "description": "重复规则",
                    },
                    "repeat_until": {
                        "type": "string",
                        "description": (
                            "重复终止日期；设置或修改重复终止条件时传入。"
                            "清除终止限制传空字符串"
                        ),
                    },
                    "remind_minutes": {"type": "integer", "description": "提前提醒分钟数，0=不提醒"},
                    "note": {"type": "string", "description": "备注"},
                    "color": {"type": "string", "enum": list(COLOR_CHOICES), "description": "颜色分组"},
                    "owner": {"type": "string", "description": "归属人（人名）；清空传空字符串"},
                },
                "required": ["id"],
            }
        )
        plugin: Any = None

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return self.plugin.tool_update_event(**kwargs)

    @pyd_dataclass
    class DeleteEventTool(FunctionTool[AstrAgentContext]):
        """Delete an event after user confirmation."""

        name: str = "delete_event"
        description: str = (
            "删除已有日程（先 list_events 拿 id）。删除前必须向用户复述目标日程并确认。"
        )
        parameters: dict = Field(
            default_factory=lambda: {
                "type": "object",
                "properties": {"id": {"type": "string", "description": "日程 id（来自 list_events）"}},
                "required": ["id"],
            }
        )
        plugin: Any = None

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return self.plugin.tool_delete_event(**kwargs)

else:  # pragma: no cover
    CreateEventTool = ListEventsTool = UpdateEventTool = DeleteEventTool = None


# ---------------------------------------------------------------------------
# plugin
# ---------------------------------------------------------------------------


class CalendarPlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config
        self._lock = threading.RLock()
        self._events: list[dict] = []
        self._diary: list[dict] = []
        self._state: dict = {
            "display_mode": "panel",
            "theme": "light",
            "window_pos": None,
            "reminders": {},
            "dismissed": {},
        }
        self._data_dir: Path = Path(__file__).resolve().parent
        self._holiday_override: dict[str, str] = {}
        self._win_proc: Optional[subprocess.Popen] = None
        self._loop_task: Optional[asyncio.Task] = None
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._port = 0
        self._token = secrets.token_urlsafe(18)
        self._tools: list[Any] = []
        self._tool_base: dict[str, str] = {}
        self._apply_config()
        if self._ai_enabled:
            self._register_tools()

    # ── config ───────────────────────────────────────────────────────────

    _CONFIG_KEYS = (
        "enable_ai_tools",
        "timezone",
        "day_start_hour",
        "remind_default_minutes",
        "all_day_remind_time",
        "auto_show_window_on_remind",
        "window_width_ratio",
        "window_snap",
        "window_glass",
        "snooze_minutes",
        "holiday_override_path",
        "demo_on_start",
        "webui_base_url",
        "countdown_unit",
    )

    def _apply_config(self) -> None:
        """Read _conf_schema.json values with safe defaults on any problem."""
        raw: dict[str, Any] = {}
        try:
            injected = getattr(self, "config", None)
            if injected is not None:
                raw = {k: injected.get(k) for k in self._CONFIG_KEYS if k in injected}
        except Exception as exc:
            logger.debug("[calendar] injected config unusable: %s", exc)

        def as_int(key: str, fallback: int) -> int:
            try:
                return int(raw.get(key, fallback))
            except (TypeError, ValueError):
                return fallback

        def as_bool(key: str, fallback: bool) -> bool:
            v = raw.get(key, fallback)
            return bool(v)

        self._ai_enabled = as_bool("enable_ai_tools", True)
        self._tz = str(raw.get("timezone") or "Asia/Shanghai")
        self._day_start_hour = min(12, max(0, as_int("day_start_hour", 6)))
        self._remind_default = as_int("remind_default_minutes", 15)
        self._all_day_remind = str(raw.get("all_day_remind_time") or "09:00")
        self._auto_show_window = as_bool("auto_show_window_on_remind", True)
        self._width_ratio = min(18, max(8, as_int("window_width_ratio", 12)))
        self._snap = as_bool("window_snap", True)
        self._glass = as_bool("window_glass", True)
        self._snooze_minutes = max(1, as_int("snooze_minutes", 5))
        self._holiday_override_path = str(raw.get("holiday_override_path") or "")
        self._demo_on_start = as_bool("demo_on_start", True)
        self._webui_base = str(raw.get("webui_base_url") or "http://127.0.0.1:6185").rstrip("/")
        unit_raw = str(raw.get("countdown_unit") or "天").strip()
        self._countdown_unit = {
            "周": "week", "天": "day", "小时": "hour",
            "week": "week", "day": "day", "hour": "hour",
            "0": "week", "1": "day", "2": "hour",
        }.get(unit_raw, "day")

    # ── lifecycle ────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        lunar.set_logger(logger.warning)
        self._data_dir = self._resolve_data_dir()
        self._load_events()
        self._load_diary()
        self._load_state()
        self._holiday_override = lunar.load_holiday_override(self._holiday_override_path)
        self._register_apis()
        self._start_loopback()
        if self._state.get("display_mode") == "window":
            self._spawn_window()
        self._loop_task = asyncio.create_task(self._tick_loop())
        threading.Thread(target=self._warm_lunar_cache, daemon=True).start()
        logger.info(
            "[calendar] ready: %d event(s), store=%s, loopback=127.0.0.1:%d, ai_tools=%s",
            len(self._events),
            self._data_dir,
            self._port,
            self._ai_enabled,
        )

    def _warm_lunar_cache(self) -> None:
        """Pre-compute lunar/festival lookups for the surrounding years.

        Building a Lunar object costs ~1.2ms per day; warming lazily in the
        background keeps the first year-view switch responsive.
        """
        try:
            today = dt.date.today()
            for year in (today.year - 1, today.year, today.year + 1):
                day = dt.date(year, 1, 1)
                while day.year == year:
                    lunar.festivals_and_terms(day)
                    lunar.lunar_text(day)
                    lunar.day_header_text(day)
                    lunar.holiday_badge(day, self._holiday_override)
                    day += dt.timedelta(days=1)
        except Exception as exc:
            logger.debug("[calendar] lunar cache warm failed: %s", exc)

    async def terminate(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None
        self._stop_loopback()
        self._kill_window()
        with self._lock:
            self._save_events()
            self._save_state()
            self._save_diary()

    def _resolve_data_dir(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            base = Path(get_astrbot_data_path())
        except Exception:
            base = Path(__file__).resolve().parent
        path = base / "plugin_data" / PLUGIN_NAME
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("[calendar] cannot create data dir %s: %s", path, exc)
        return path

    # ── storage ──────────────────────────────────────────────────────────

    @property
    def _events_path(self) -> Path:
        return self._data_dir / "events.json"

    @property
    def _state_path(self) -> Path:
        return self._data_dir / "state.json"

    @property
    def _diary_path(self) -> Path:
        return self._data_dir / "diary.json"

    def _load_diary(self) -> None:
        data = rec.load_json_file(str(self._diary_path), {"version": 1, "entries": []})
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            entries = []
        self._diary = [
            e for e in entries if isinstance(e, dict) and e.get("id")
        ]

    def _save_diary(self) -> None:
        try:
            rec.atomic_write_json(
                str(self._diary_path),
                {"version": 1, "entries": self._diary},
            )
        except OSError as exc:
            logger.error("[calendar] saving diary failed: %s", exc)

    def _load_events(self) -> None:
        data = rec.load_json_file(str(self._events_path), {"version": 1, "events": []})
        events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(events, list):
            events = []
        self._events = [e for e in events if isinstance(e, dict) and e.get("id") and e.get("start")]

    def _save_events(self) -> None:
        try:
            rec.atomic_write_json(
                str(self._events_path), {"version": 1, "events": self._events}
            )
        except OSError as exc:
            logger.error("[calendar] saving events failed: %s", exc)

    def _load_state(self) -> None:
        data = rec.load_json_file(str(self._state_path), {})
        if isinstance(data, dict):
            for key in (
                "display_mode",
                "theme",
                "window_pos",
                "reminders",
                "dismissed",
                "panel_bg",
                "custom_colors",
            ):
                if key in data:
                    self._state[key] = data[key]
        if self._state.get("theme") not in THEME_ORDER:
            self._state["theme"] = "light"
        if self._state.get("display_mode") not in ("panel", "window"):
            self._state["display_mode"] = "panel"
        if not isinstance(self._state.get("reminders"), dict):
            self._state["reminders"] = {}
        if not isinstance(self._state.get("dismissed"), dict):
            self._state["dismissed"] = {}
        if not self._valid_custom_colors(self._state.get("custom_colors")):
            self._state.pop("custom_colors", None)

    def _save_state(self) -> None:
        try:
            rec.atomic_write_json(str(self._state_path), self._state)
        except OSError as exc:
            logger.error("[calendar] saving state failed: %s", exc)

    @staticmethod
    def _valid_custom_colors(value) -> bool:
        """custom_colors must be {"primary","secondary"} of valid hex colors."""
        if not isinstance(value, dict):
            return False
        return bool(_norm_hex(value.get("primary"))) and bool(
            _norm_hex(value.get("secondary"))
        )

    # ── event CRUD core ──────────────────────────────────────────────────

    def _find_event(self, event_id: str) -> Optional[dict]:
        for ev in self._events:
            if ev.get("id") == event_id:
                return ev
        return None

    def _validate(self, data: dict, partial: bool = False) -> dict:
        """Validate/normalize an event payload. Raises ValueError on bad input."""
        out: dict[str, Any] = {}
        if not partial or "title" in data:
            title = str(data.get("title") or "").strip()
            if not title:
                raise ValueError("标题不能为空")
            if len(title) > 120:
                raise ValueError("标题最长 120 字")
            out["title"] = title
        if not partial or "all_day" in data:
            out["all_day"] = bool(data.get("all_day"))
        all_day = out.get("all_day", bool(data.get("all_day")))

        if not partial or "start" in data:
            start = parse_datetime(data.get("start"))
            out["start"] = dt_str(start)
        if not partial or "end" in data or "start" in data:
            raw_end = data.get("end")
            if raw_end:
                end = parse_datetime(raw_end)
            elif "start" in data or not partial:
                st = parse_datetime(out.get("start") or data.get("start"))
                end = st.replace(hour=23, minute=59, second=0) if all_day else st + dt.timedelta(hours=1)
            else:
                end = None
            if end is not None:
                if all_day and end.date() < parse_datetime(out.get("start") or data.get("start")).date():
                    raise ValueError("全天日程的结束日期不能早于开始日期")
                if end <= parse_datetime(out.get("start") or data.get("start")):
                    if not (all_day and end.date() == parse_datetime(out.get("start") or data.get("start")).date()):
                        raise ValueError("结束时间必须晚于开始时间")
                out["end"] = dt_str(end)
        if "color" in data:
            color = str(data.get("color") or "blue")
            out["color"] = color if color in COLOR_CHOICES else "blue"
        if "repeat" in data:
            rep = str(data.get("repeat") or "none")
            out["repeat"] = rep if rep in REPEAT_CHOICES else "none"
        if "repeat_until" in data:
            ru = data.get("repeat_until")
            s = "" if ru is None else str(ru).strip()
            if not s or s.lower() in ("none", "null"):
                out["repeat_until"] = None
            else:
                parse_datetime(s)  # raises ValueError on bad format
                out["repeat_until"] = s
                ref_start = out.get("start") or data.get("start")
                if ref_start:
                    if parse_datetime(s).date() < parse_datetime(ref_start).date():
                        raise ValueError("repeat_until 不能早于日程开始日期")
            if (out.get("repeat") or str(data.get("repeat") or "none")) == "none":
                out["repeat_until"] = None
        if "remind_minutes" in data:
            rm = data.get("remind_minutes")
            if rm is None or rm == "":
                out["remind_minutes"] = None
            else:
                try:
                    rm = int(rm)
                except (TypeError, ValueError):
                    raise ValueError(f"remind_minutes 必须是整数，收到: {rm!r}")
                if rm < 0:
                    raise ValueError("remind_minutes 不能为负")
                out["remind_minutes"] = rm
        if "note" in data:
            note = str(data.get("note") or "")
            if len(note) > 2000:
                raise ValueError("备注最长 2000 字")
            out["note"] = note
        if not partial or "owner" in data:
            owner = str(data.get("owner") or "").strip()
            if len(owner) > 32:
                raise ValueError("「谁的日程」最长 32 字")
            out["owner"] = owner
        if "done" in data:
            out["done"] = bool(data.get("done"))
        return out

    def create_event(self, payload: dict, created_by: str = "panel") -> dict:
        with self._lock:
            fields = self._validate(payload, partial=False)
            now = dt_str(dt.datetime.now())
            ev = {
                "id": uuid.uuid4().hex,
                "title": fields["title"],
                "all_day": fields.get("all_day", False),
                "start": fields["start"],
                "end": fields.get("end"),
                "color": fields.get("color", "blue"),
                "repeat": fields.get("repeat", "none"),
                "remind_minutes": fields.get("remind_minutes", self._remind_default),
                "note": fields.get("note", ""),
                "owner": fields.get("owner", ""),
                "done": fields.get("done", False),
                "demo": bool(payload.get("demo")),
                "created_by": created_by,
                "created_at": now,
                "updated_at": now,
            }
            if fields.get("repeat_until"):
                ev["repeat_until"] = fields["repeat_until"]
            self._events.append(ev)
            self._save_events()
            return ev

    def update_event(self, event_id: str, payload: dict) -> dict:
        with self._lock:
            ev = self._find_event(event_id)
            if ev is None:
                raise KeyError(f"未找到日程 {event_id}")
            merged = {**ev, **payload}
            fields = self._validate(merged, partial=False)
            allowed = (
                "title",
                "all_day",
                "start",
                "end",
                "color",
                "repeat",
                "repeat_until",
                "remind_minutes",
                "note",
                "owner",
                "done",
            )
            for key in allowed:
                if key in fields:
                    ev[key] = fields[key]
            if "repeat_until" in fields and not fields["repeat_until"]:
                ev.pop("repeat_until", None)
            ev["updated_at"] = dt_str(dt.datetime.now())
            self._save_events()
            return ev

    def delete_event(self, event_id: str) -> None:
        with self._lock:
            before = len(self._events)
            self._events = [e for e in self._events if e.get("id") != event_id]
            if len(self._events) == before:
                raise KeyError(f"未找到日程 {event_id}")
            # Drop reminder bookkeeping for this event.
            self._state["reminders"] = {
                k: v
                for k, v in self._state["reminders"].items()
                if not k.startswith(event_id + "|")
            }
            self._state["dismissed"] = {
                k: v
                for k, v in self._state["dismissed"].items()
                if not k.startswith(event_id + "|")
            }
            self._save_events()
            self._save_state()

    # ── range building (events + festival blocks + per-date meta) ───────

    def build_range(self, dfrom: dt.date, dto: dt.date) -> dict:
        with self._lock:
            events_src = list(self._events)
            rem = dict(self._state["reminders"])
        out_events: list[dict] = []
        for ev in events_src:
            try:
                occs = occurrence_starts(ev, dfrom, dto)
            except Exception as exc:
                logger.warning("[calendar] expand %s failed: %s", ev.get("id"), exc)
                continue
            for os_ in occs:
                oe = occurrence_end(ev, os_)
                if oe.date() < dfrom or os_.date() > dto:
                    continue
                key = occ_key(ev["id"], os_)
                st = rem.get(key, {})
                out_events.append(
                    {
                        "key": key,
                        "id": ev["id"],
                        "title": ev.get("title", ""),
                        "start": dt_str(os_),
                        "end": dt_str(oe),
                        "all_day": bool(ev.get("all_day")),
                        "color": ev.get("color", "blue"),
                        "repeat": ev.get("repeat", "none"),
                        "repeat_until": ev.get("repeat_until"),
                        "remind_minutes": ev.get("remind_minutes"),
                        "note": ev.get("note", ""),
                        "owner": ev.get("owner", ""),
                        "done": bool(ev.get("done")) or bool(st.get("completed")),
                        "category": "user",
                    }
                )
        out_events.sort(key=lambda x: (not x["all_day"], x["start"]))

        festival: list[dict] = []
        meta: dict[str, dict] = {}
        day = dfrom
        while day <= dto:
            names = lunar.festivals_and_terms(day)
            meta[str(day)] = {
                "lunar": lunar.lunar_text(day),
                "badge": lunar.holiday_badge(day, self._holiday_override),
                "header": lunar.day_header_text(day),
                "festivals": names,
            }
            for name in names:
                festival.append(
                    {
                        "key": f"fest|{day}|{name}",
                        "id": None,
                        "title": name,
                        "start": f"{day}T00:00:00",
                        "end": f"{day}T23:59:59",
                        "all_day": True,
                        "color": "",
                        "category": "festival",
                        "done": False,
                    }
                )
            day += dt.timedelta(days=1)
        return {"events": out_events, "festival": festival, "meta": meta}

    def search_events(self, q: str, limit: int = 30) -> list[dict]:
        ql = q.strip().lower()
        if not ql:
            return []
        today = dt.datetime.now().date()
        horizon = today + dt.timedelta(days=120)
        results: list[dict] = []
        with self._lock:
            src = list(self._events)
        for ev in src:
            if ql not in str(ev.get("title", "")).lower() and ql not in str(
                ev.get("note", "")
            ).lower():
                continue
            try:
                occs = occurrence_starts(ev, today, horizon)
                nxt = occs[0] if occs else parse_datetime(ev["start"])
                if not occs:
                    base = parse_datetime(ev["start"])
                    nxt = base if base.date() >= today else base
            except Exception:
                nxt = dt.datetime.now()
            results.append(
                {
                    "id": ev["id"],
                    "title": ev.get("title", ""),
                    "all_day": bool(ev.get("all_day")),
                    "start": dt_str(parse_datetime(ev["start"])),
                    "next": dt_str(nxt),
                    "color": ev.get("color", "blue"),
                    "repeat": ev.get("repeat", "none"),
                    "repeat_until": ev.get("repeat_until"),
                    "note": ev.get("note", ""),
                    "owner": ev.get("owner", ""),
                }
            )
        results.sort(key=lambda r: r["next"])
        return results[:limit]

    # ── digest / reminders ───────────────────────────────────────────────

    def build_digest(self) -> dict:
        """Today's digest: only occurrences covering today that have not ended.

        The expansion window is wider than one day so multi-day / repeating
        events overlapping today are found; everything else (finished items,
        future days, expanded repeats) is filtered out afterwards.
        """
        now = dt.datetime.now()
        today = now.date()
        lo, hi = today - dt.timedelta(days=1), today + dt.timedelta(days=7)
        with self._lock:
            src = list(self._events)
        occs: list[tuple[dt.datetime, dt.datetime, dict]] = []
        for ev in src:
            for os_ in occurrence_starts(ev, lo, hi):
                oe = occurrence_end(ev, os_)
                if oe < now:
                    continue
                if os_.date() > today or oe.date() < today:
                    continue  # keep only occurrences that cover today
                occs.append((os_, oe, ev))
        occs.sort(key=lambda t: (t[0].date() != today, t[0]))
        lines = []
        for os_, oe, ev in occs[:3]:
            lines.append(
                {
                    "key": ev.get("id", ""),
                    "time": "全天" if ev.get("all_day") else os_.strftime("%H:%M"),
                    "title": ev.get("title", ""),
                    "done": bool(ev.get("done")),
                }
            )
        return {
            "date": str(today),
            "day": str(today.day),
            "weekday": "周" + _weekday_cn(today.weekday()),
            "lunar": lunar.lunar_md(today),
            "lines": lines,
            "total": len(occs),
        }

    def _all_day_remind_clock(self) -> dt.time:
        """全天日程的倒计时锚点时刻（设置里的全天提醒时间，非法则 09:00）。"""
        try:
            hh, mm = str(self._all_day_remind or "09:00").split(":")[:2]
            return dt.time(max(0, min(23, int(hh))), max(0, min(59, int(mm))))
        except (TypeError, ValueError):
            return dt.time(9, 0)

    def build_countdown(self, limit: int | None = None) -> dict:
        """未来待办倒计时（只读）。

        规则（已与用户确认）：
        - 只取尚未结束的 occurrence；done=True 的事件跳过。
        - 计时锚点 = 事件开始时间；全天事件用全天提醒时刻。
        - 锚点已过 → remaining=0、status="ongoing"，仍排最前。
        - 重复日程通过 occurrence_starts 展开取下一次出现。
        - items 按 (remaining, start) 升序；截断到 200 条返回。
        """
        now = dt.datetime.now()
        today = now.date()
        horizon = today + dt.timedelta(days=400)
        clock = self._all_day_remind_clock()
        with self._lock:
            src = list(self._events)
        items: list[dict] = []
        for ev in src:
            if ev.get("done"):
                continue
            try:
                starts = occurrence_starts(ev, today, horizon)
            except Exception as exc:
                logger.warning(
                    "[calendar] countdown expand %s failed: %s", ev.get("id"), exc
                )
                continue
            for os_ in starts:
                oe = occurrence_end(ev, os_)
                if oe < now:
                    continue
                anchor = (
                    dt.datetime.combine(os_.date(), clock)
                    if ev.get("all_day")
                    else os_
                )
                remaining = max(0, int((anchor - now).total_seconds()))
                items.append(
                    {
                        "key": occ_key(ev["id"], os_),
                        "id": ev["id"],
                        "title": ev.get("title", ""),
                        "start": dt_str(os_),
                        "end": dt_str(oe),
                        "target": dt_str(anchor),
                        "remaining": remaining,
                        "status": "ongoing" if remaining == 0 else "upcoming",
                        "all_day": bool(ev.get("all_day")),
                        "color": ev.get("color", "blue"),
                        "owner": ev.get("owner", ""),
                        "note": ev.get("note", ""),
                        "repeat": ev.get("repeat", "none"),
                        "repeat_until": ev.get("repeat_until"),
                    }
                )
        items.sort(key=lambda x: (x["remaining"], x["start"]))
        total = len(items)
        items = items[:200]
        if limit is not None:
            items = items[: max(0, int(limit))]
        return {
            "unit": self._countdown_unit,
            "now": dt_str(now),
            "total": total,
            "items": items,
        }

    def _pending_cards(self) -> list[dict]:
        now = dt.datetime.now()
        with self._lock:
            rem = dict(self._state["reminders"])
        cards = []
        for key, st in rem.items():
            if not isinstance(st, dict) or st.get("completed"):
                continue
            if st.get("snooze_until"):
                try:
                    if parse_datetime(st["snooze_until"]) > now:
                        continue
                except ValueError:
                    pass
            if not st.get("fired_at"):
                continue
            try:
                event_id, os_text = key.split("|", 1)
                occ_start = parse_datetime(os_text)
            except ValueError:
                continue
            if now - occ_start > REMINDER_CARD_MAX_AGE:
                continue
            # The mini window always renders today's date block; surfacing a
            # leftover occurrence from a previous day under today's date reads
            # as a mismatch. A due snooze is exempt: the snooze time, not the
            # occurrence day, is what the user asked to be reminded at.
            snoozed_due = False
            if st.get("snooze_until"):
                try:
                    snoozed_due = (
                        parse_datetime(st["snooze_until"]).date() == now.date()
                    )
                except ValueError:
                    pass
            if occ_start.date() < now.date() and not snoozed_due:
                continue
            ev = self._find_event(event_id)
            if ev is None:
                continue
            cards.append(
                {
                    "key": key,
                    "title": ev.get("title", ""),
                    "note": ev.get("note", ""),
                    "time": "全天" if ev.get("all_day") else occ_start.strftime("%H:%M"),
                    "occurrence": os_text,
                    "done": bool(ev.get("done")),
                }
            )
        cards.sort(key=lambda c: c["occurrence"])
        return cards

    def _fire_due_reminders(self) -> None:
        now = dt.datetime.now()
        lo, hi = now.date() - dt.timedelta(days=1), now.date() + dt.timedelta(days=1)
        changed = False
        with self._lock:
            rem = self._state["reminders"]
            for ev in list(self._events):
                if ev.get("done"):
                    continue
                try:
                    occs = occurrence_starts(ev, lo, hi)
                except Exception:
                    continue
                for os_ in occs:
                    fire = reminder_fire_time(ev, os_, self._all_day_remind)
                    if fire is None or fire > now:
                        continue
                    key = occ_key(ev["id"], os_)
                    st = rem.get(key)
                    if st is not None and st.get("fired_at"):
                        continue
                    if now - fire > REMINDER_FIRE_GRACE:
                        # Missed the normal fire window: still surface the
                        # reminder for the whole life of the occurrence so an
                        # in-progress or just-finished event can be checked or
                        # removed from the mini window; long-past stays silent.
                        oe = None
                        try:
                            oe = occurrence_end(ev, os_)
                        except Exception:
                            oe = None
                        if oe is None:
                            continue
                        oe_local = oe if oe.tzinfo is None else oe.replace(tzinfo=None)
                        if oe_local <= now:
                            continue
                    if st is None:
                        rem[key] = {"fired_at": dt_str(now), "completed": False, "snooze_until": None}
                        changed = True
                    elif not st.get("fired_at"):
                        st["fired_at"] = dt_str(now)
                        changed = True
            if changed:
                self._save_state()

    # ── main tick ────────────────────────────────────────────────────────

    async def _tick_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(TICK_SECONDS)
                self._refresh_tool_time()
                self._fire_due_reminders()
                self._maybe_auto_show_window()
                self._reap_window()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[calendar] tick failed")

    def _maybe_auto_show_window(self) -> None:
        if not self._auto_show_window:
            return
        if self._win_proc is not None and self._win_proc.poll() is None:
            return
        now = dt.datetime.now()
        with self._lock:
            dismissed = set(self._state.get("dismissed", {}))
        fresh = [
            c
            for c in self._pending_cards()
            if c["key"] not in dismissed
            and now - parse_datetime(c["occurrence"]) <= dt.timedelta(days=1)
        ]
        if fresh:
            logger.info("[calendar] pending reminder without window; launching mini window")
            self._spawn_window()

    # ── desktop mini window process ──────────────────────────────────────

    @property
    def _panel_url(self) -> str:
        # Vue uses hash history; a path-style URL hits the backend's
        # index whitelist, falls through to the 404 "WebUI missing" page.
        return f"{self._webui_base}/#{PANEL_PATH}"

    def _spawn_window(self) -> None:
        if self._win_proc is not None and self._win_proc.poll() is None:
            return
        script = Path(__file__).resolve().parent / "window" / "calendar_window.py"
        if not script.is_file():
            logger.error("[calendar] window script missing: %s", script)
            return
        pos = self._state.get("window_pos") or {}
        args = [
            sys.executable,
            "-u",
            str(script),
            "--port",
            str(self._port),
            "--token",
            self._token,
            "--ratio",
            str(self._width_ratio),
            "--theme",
            str(self._state.get("theme", "light")),
            "--panel-url",
            self._panel_url,
            "--snap",
            "on" if getattr(self, "_snap", True) else "off",
            "--glass",
            "on" if getattr(self, "_glass", True) else "off",
        ]
        if isinstance(pos, dict) and pos.get("x") is not None and pos.get("y") is not None:
            args += ["--x", str(int(pos["x"])), "--y", str(int(pos["y"]))]
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._win_proc = subprocess.Popen(
                args, cwd=str(script.parent), **kwargs
            )
            logger.info("[calendar] mini window launched (pid=%s)", self._win_proc.pid)
        except Exception as exc:
            self._win_proc = None
            logger.error("[calendar] mini window launch failed: %s", exc)

    def _kill_window(self) -> None:
        proc = self._win_proc
        self._win_proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
        except Exception as exc:
            logger.debug("[calendar] killing window: %s", exc)

    def _reap_window(self) -> None:
        if self._win_proc is not None and self._win_proc.poll() is not None:
            logger.info("[calendar] mini window exited (code=%s)", self._win_proc.returncode)
            self._win_proc = None

    # ── loopback HTTP (window <-> plugin, 127.0.0.1 only) ───────────────

    def _start_loopback(self) -> None:
        plugin = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102
                return

            def _send(self, code: int, obj: Any) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth(self) -> bool:
                return f"token={plugin._token}" in self.path

            def do_GET(self) -> None:  # noqa: N802
                if not self._auth():
                    self._send(401, {"error": "unauthorized"})
                    return
                if self.path.startswith("/state"):
                    try:
                        self._send(200, plugin._window_state())
                    except Exception:
                        logger.exception("[calendar] window state build failed")
                        self._send(500, {"error": "state build failed"})
                    return
                if self.path.startswith("/detail"):
                    qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    key = (qs.get("key") or [""])[0]
                    try:
                        self._send(200, plugin._window_detail(key))
                    except Exception:
                        logger.exception("[calendar] window detail build failed")
                        self._send(500, {"error": "detail build failed"})
                    return
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                if not self._auth():
                    self._send(401, {"error": "unauthorized"})
                    return
                if not self.path.startswith("/action"):
                    self._send(404, {"error": "not found"})
                    return
                try:
                    length = int(self.headers.get("content-length") or 0)
                    raw = self.rfile.read(length) if length else b"{}"
                    payload = json.loads(raw or b"{}")
                except Exception:
                    self._send(400, {"error": "bad json"})
                    return
                try:
                    self._send(200, plugin._window_action(payload or {}))
                except Exception as exc:
                    logger.exception("[calendar] window action failed")
                    self._send(500, {"error": str(exc)})

        try:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self._port = self._httpd.server_address[1]
            threading.Thread(
                target=self._httpd.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True
            ).start()
        except OSError as exc:
            self._httpd = None
            self._port = 0
            logger.error("[calendar] loopback server failed: %s", exc)

    def _stop_loopback(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None

    def _window_state(self) -> dict:
        with self._lock:
            mode = self._state.get("display_mode", "panel")
            theme = self._state.get("theme", "light")
            custom = self._state.get("custom_colors")
            if not self._valid_custom_colors(custom):
                custom = None
        # Live-read so toggling window_snap in settings applies within one poll.
        snap = getattr(self, "_snap", True)
        try:
            cfg = getattr(self, "config", None)
            if cfg is not None and cfg.get("window_snap") is not None:
                snap = bool(cfg.get("window_snap"))
        except Exception:
            pass
        glass = getattr(self, "_glass", True)
        try:
            cfg = getattr(self, "config", None)
            if cfg is not None and cfg.get("window_glass") is not None:
                glass = bool(cfg.get("window_glass"))
        except Exception:
            pass
        return {
            "ok": True,
            "digest": self.build_digest(),
            "cards": self._pending_cards(),
            "theme": theme,
            "snap": snap,
            "glass": glass,
            "panel_url": self._panel_url,
            "display_mode": mode,
            "custom_colors": custom,
        }

    def _window_detail(self, key: str) -> dict:
        """One event's full popup content, pre-formatted for the mini-window."""
        key = str(key or "")
        if not key:
            return {"error": "缺少 key"}
        with self._lock:
            ev = self._find_event(key)
            remind_default = self._remind_default
        if ev is None:
            return {"error": "日程不存在"}
        try:
            s = dt.datetime.fromisoformat(str(ev.get("start") or ""))
            e_ = dt.datetime.fromisoformat(str(ev.get("end") or ""))
            multi = e_.date() > s.date()
            if ev.get("all_day"):
                dt_txt = f"{s:%m-%d} 全天"
                if multi:
                    dt_txt = f"{s:%m-%d} → {e_:%m-%d} 全天"
            elif multi:
                dt_txt = f"{s:%m-%d %H:%M} → {e_:%m-%d %H:%M}"
            else:
                dt_txt = f"{s:%m-%d %H:%M} ~ {e_:%H:%M}"
        except (TypeError, ValueError):
            dt_txt = f"{ev.get('start')} ~ {ev.get('end')}"
        rep = str(ev.get("repeat") or "none")
        rep_txt = {"none": "单次", "daily": "每天", "weekly": "每周", "monthly": "每月"}.get(rep, rep)
        if rep != "none":
            until = str(ev.get("repeat_until") or "")
            if until:
                try:
                    rep_txt += f" · 至 {dt.datetime.fromisoformat(until):%Y-%m-%d}"
                except (TypeError, ValueError):
                    rep_txt += f" · 至 {until}"
            else:
                rep_txt += " · 无终止日期"
        rm = ev.get("remind_minutes")
        if rm is None:
            rm_txt = f"默认 · 提前 {remind_default} 分钟"
        elif not rm:
            rm_txt = "已关闭"
        else:
            rm_txt = f"提前 {rm} 分钟"
        return {
            "event": {
                "id": key,
                "title": str(ev.get("title") or ""),
                "dt": dt_txt,
                "repeat": rep_txt,
                "remind": rm_txt,
                "note": str(ev.get("note") or ""),
                "done": bool(ev.get("done")),
            }
        }

    def _window_action(self, payload: dict) -> dict:
        op = str(payload.get("op") or "")
        with self._lock:
            if op == "pos":
                try:
                    self._state["window_pos"] = {
                        "x": int(payload.get("x")),
                        "y": int(payload.get("y")),
                    }
                    self._save_state()
                except (TypeError, ValueError):
                    pass
                return {"ok": True}
            if op == "cycle_theme":
                cur = self._state.get("theme", "light")
                nxt = THEME_ORDER[(THEME_ORDER.index(cur) + 1) % len(THEME_ORDER)]
                self._state["theme"] = nxt  # silent switch: no chat announcement
                self._save_state()
                return {"ok": True, "theme": nxt}
            if op == "toggle_snap":
                new_snap = not bool(getattr(self, "_snap", True))
                self._snap = new_snap
                cfg = getattr(self, "config", None)
                if cfg is not None:
                    try:
                        cfg["window_snap"] = new_snap
                        save = getattr(cfg, "save_config", None)
                        if callable(save):
                            save()
                    except Exception as exc:
                        logger.debug("[calendar] persist window_snap failed: %s", exc)
                return {"ok": True, "snap": new_snap}
            if op == "done":
                key = str(payload.get("key") or "")
                st = self._state["reminders"].get(key)
                if st is None:
                    st = {"fired_at": dt_str(dt.datetime.now()), "completed": False, "snooze_until": None}
                    self._state["reminders"][key] = st
                st["completed"] = True
                st["snooze_until"] = None
                self._state.get("dismissed", {}).pop(key, None)
                # A completed whole event keeps its struck-through style too.
                self._save_state()
                return {"ok": True}
            if op == "card_done":
                # 完成提醒卡片背后的任务：勾选事件完成，并关掉卡片。
                key = str(payload.get("key") or "")
                event_id = key.split("|", 1)[0]
                ev = self._find_event(event_id)
                if ev is None:
                    return {"error": f"未找到日程 {event_id}"}
                try:
                    self.update_event(event_id, {"done": True})
                except (KeyError, ValueError, TypeError) as exc:
                    return {"error": str(exc)}
                with self._lock:
                    st = self._state["reminders"].get(key)
                    if st is None:
                        st = {
                            "fired_at": dt_str(dt.datetime.now()),
                            "completed": False,
                            "snooze_until": None,
                        }
                        self._state["reminders"][key] = st
                    st["completed"] = True
                    st["snooze_until"] = None
                    self._state.get("dismissed", {}).pop(key, None)
                    self._save_state()
                return {"ok": True, "done": True}
            if op == "snooze":
                key = str(payload.get("key") or "")
                st = self._state["reminders"].get(key)
                if st is None:
                    st = {"fired_at": dt_str(dt.datetime.now()), "completed": False, "snooze_until": None}
                    self._state["reminders"][key] = st
                st["snooze_until"] = dt_str(
                    dt.datetime.now() + dt.timedelta(minutes=self._snooze_minutes)
                )
                st["completed"] = False
                self._state.get("dismissed", {}).pop(key, None)
                self._save_state()
                return {"ok": True, "snooze_until": st["snooze_until"]}
            if op == "ev_done":
                key = str(payload.get("key") or "")
                ev = self._find_event(key)
                if ev is None:
                    return {"error": f"未找到日程 {key}"}
                new_done = not bool(ev.get("done"))
                try:
                    self.update_event(key, {"done": new_done})
                except (KeyError, ValueError, TypeError) as exc:
                    return {"error": str(exc)}
                return {"ok": True, "done": new_done}
            if op == "ev_delete":
                key = str(payload.get("key") or "")
                try:
                    self.delete_event(key)
                except KeyError as exc:
                    return {"error": str(exc)}
                return {"ok": True}
            if op == "card_delete":
                # Remove the whole event behind a reminder card and close the
                # card itself so it cannot resurface from state.
                key = str(payload.get("key") or "")
                event_id = key.split("|", 1)[0]
                try:
                    self.delete_event(event_id)
                except KeyError as exc:
                    return {"error": str(exc)}
                with self._lock:
                    st = self._state["reminders"].get(key)
                    if st is not None:
                        st["completed"] = True
                        st["snooze_until"] = None
                    else:
                        self._state["reminders"][key] = {
                            "fired_at": dt_str(dt.datetime.now()),
                            "completed": True,
                            "snooze_until": None,
                        }
                    self._save_state()
                return {"ok": True}
            if op == "quit":
                # User explicitly closed the window: stop auto-reviving the
                # current cards, but keep them pending for later viewing.
                for card in self._pending_cards():
                    self._state.setdefault("dismissed", {})[card["key"]] = True
                if payload.get("panel"):
                    # "回面板": also switch back to panel display mode so the
                    # window is not respawned on the next plugin load.
                    self._state["display_mode"] = "panel"
                self._save_state()
                return {"ok": True}
        return {"error": f"unknown op {op!r}"}

    # ── diary (每日日记) ───────────────────────────────────────────────────

    def _validate_diary(self, data: dict, partial: bool = False) -> dict:
        """校验日记字段；tags 由 title+content 的 #语法派生，不接受外部传入。"""
        out: dict[str, Any] = {}
        if not partial or "title" in data:
            title = str(data.get("title") or "").strip()
            if len(title) > 200:
                raise ValueError("日记标题最长 200 字")
            out["title"] = title
        if not partial or "content" in data:
            content = str(data.get("content") or "")
            if len(content) > 20000:
                raise ValueError("日记正文最长 20000 字")
            out["content"] = content
        if not partial or "date" in data:
            raw = str(data.get("date") or "").strip()
            if raw:
                out["date"] = str(parse_date(raw))  # ValueError 可读
            elif not partial:
                out["date"] = str(dt.date.today())
        if not partial or "pinned" in data:
            out["pinned"] = bool(data.get("pinned"))
        return out

    def create_diary(self, payload: dict) -> dict:
        with self._lock:
            fields = self._validate_diary(payload, partial=False)
            now = dt_str(dt.datetime.now())
            entry = {
                "id": uuid.uuid4().hex,
                "date": fields.get("date") or str(dt.date.today()),
                "title": fields.get("title", ""),
                "content": fields.get("content", ""),
                "tags": extract_tags(
                    fields.get("title", "") + "\n" + fields.get("content", "")
                ),
                "pinned": fields.get("pinned", False),
                "created_at": now,
                "updated_at": now,
            }
            self._diary.append(entry)
            self._save_diary()
            return entry

    def update_diary(self, entry_id: str, payload: dict) -> dict:
        with self._lock:
            ent = next((e for e in self._diary if e.get("id") == entry_id), None)
            if ent is None:
                raise KeyError(f"未找到日记 {entry_id}")
            fields = self._validate_diary(payload, partial=True)
            for key in ("title", "content", "date", "pinned"):
                if key in fields:
                    ent[key] = fields[key]
            if "title" in fields or "content" in fields:
                ent["tags"] = extract_tags(
                    str(ent.get("title", "")) + "\n" + str(ent.get("content", ""))
                )
            ent["updated_at"] = dt_str(dt.datetime.now())
            self._save_diary()
            return ent

    def delete_diary(self, entry_id: str) -> None:
        with self._lock:
            before = len(self._diary)
            self._diary = [e for e in self._diary if e.get("id") != entry_id]
            if len(self._diary) == before:
                raise KeyError(f"未找到日记 {entry_id}")
            self._save_diary()

    @staticmethod
    def _diary_date_num(value: str) -> int:
        try:
            return int(str(value or "").replace("-", "") or 0)
        except (TypeError, ValueError):
            return 0

    def list_diary(
        self,
        q: str = "",
        tag: str = "",
        date: str = "",
        limit: int = 0,
    ) -> list[dict]:
        """检索日记：关键词(标题+正文) / 逗号分隔多标签(AND) / 指定日期。"""
        ql = str(q or "").strip().lower()
        wanted = [t.strip() for t in str(tag or "").split(",") if t.strip()]
        with self._lock:
            entries = list(self._diary)
        out = []
        for ent in entries:
            if date and ent.get("date") != date:
                continue
            if wanted and not all(t in (ent.get("tags") or []) for t in wanted):
                continue
            if ql:
                hay = (
                    str(ent.get("title", "")) + "\n" + str(ent.get("content", ""))
                ).lower()
                if ql not in hay:
                    continue
            out.append(ent)
        # 新的在前、置顶最前：稳定排序两趟
        out.sort(key=lambda e: str(e.get("updated_at", "")), reverse=True)
        out.sort(
            key=lambda e: (
                0 if e.get("pinned") else 1,
                -self._diary_date_num(e.get("date")),
            )
        )
        if limit and limit > 0:
            out = out[:limit]
        return out

    def diary_tags(self) -> list[dict]:
        counts: dict[str, int] = {}
        with self._lock:
            for ent in self._diary:
                for t in ent.get("tags") or []:
                    counts[str(t)] = counts.get(str(t), 0) + 1
        return sorted(
            ({"tag": k, "count": v} for k, v in counts.items()),
            key=lambda x: (-x["count"], x["tag"]),
        )

    # ── panel Web API ────────────────────────────────────────────────────

    def _register_apis(self) -> None:
        reg = self.context.register_web_api
        reg(f"/{PLUGIN_NAME}/events", self._api_events, ["GET"], "List/search events")
        reg(f"/{PLUGIN_NAME}/events", self._api_event_create, ["POST"], "Create event")
        # Panel bridge only supports GET/POST, so mutations use flat POST routes.
        reg(f"/{PLUGIN_NAME}/events/update", self._api_event_update, ["POST"], "Update event")
        reg(f"/{PLUGIN_NAME}/events/delete", self._api_event_delete, ["POST"], "Delete event")
        reg(f"/{PLUGIN_NAME}/digest", self._api_digest, ["GET"], "Today digest")
        reg(f"/{PLUGIN_NAME}/countdown", self._api_countdown, ["GET"], "Upcoming countdowns")
        reg(f"/{PLUGIN_NAME}/diary", self._api_diary_list, ["GET"], "Search diary entries")
        reg(f"/{PLUGIN_NAME}/diary", self._api_diary_create, ["POST"], "Create diary entry")
        reg(f"/{PLUGIN_NAME}/diary/update", self._api_diary_update, ["POST"], "Update diary entry")
        reg(f"/{PLUGIN_NAME}/diary/delete", self._api_diary_delete, ["POST"], "Delete diary entry")
        reg(f"/{PLUGIN_NAME}/diary/tags", self._api_diary_tags, ["GET"], "Diary tag counts")
        reg(f"/{PLUGIN_NAME}/reminders", self._api_reminders, ["GET"], "Pending reminder cards")
        reg(f"/{PLUGIN_NAME}/reminders", self._api_reminder_action, ["POST"], "done/snooze card")
        reg(f"/{PLUGIN_NAME}/display", self._api_display, ["GET"], "Display mode status")
        reg(f"/{PLUGIN_NAME}/display/mode", self._api_display_mode, ["POST"], "panel/window switch")
        reg(f"/{PLUGIN_NAME}/config", self._api_config, ["GET"], "Frontend config")
        reg(f"/{PLUGIN_NAME}/ai", self._api_ai, ["POST"], "AI create events from natural language")
        reg(f"/{PLUGIN_NAME}/bg", self._api_bg, ["GET"], "Panel background image")
        reg(f"/{PLUGIN_NAME}/bg", self._api_bg_set, ["POST"], "Set panel background")
        reg(f"/{PLUGIN_NAME}/bg/clear", self._api_bg_clear, ["POST"], "Remove panel background")
        reg(f"/{PLUGIN_NAME}/colors", self._api_colors_set, ["POST"], "Set custom primary/secondary colors")
        reg(f"/{PLUGIN_NAME}/colors/clear", self._api_colors_clear, ["POST"], "Reset custom colors to default")

    async def _api_events(self):
        q = request.query.get("q", "") or ""
        if q:
            return json_response({"events": self.search_events(q), "search": True})
        try:
            dfrom = parse_date(request.query.get("from", ""))
            dto = parse_date(request.query.get("to", ""))
        except ValueError as exc:
            return error_response(str(exc))
        if dfrom > dto:
            dfrom, dto = dto, dfrom
        if (dto - dfrom).days > 400:
            return error_response("查询范围过大（上限 400 天）")
        return json_response(self.build_range(dfrom, dto))

    async def _api_event_create(self):
        try:
            body = await request.json({}) or {}
        except Exception:
            return error_response("请求体必须是 JSON")
        try:
            ev = self.create_event(body, created_by=request.username or "panel")
        except (ValueError, TypeError) as exc:
            return error_response(str(exc))
        return json_response({"ok": True, "event": ev})

    async def _api_event_update(self):
        try:
            body = await request.json({}) or {}
        except Exception:
            return error_response("请求体必须是 JSON")
        event_id = str(body.pop("id", "") or "")
        if not event_id:
            return error_response("缺少 id")
        try:
            ev = self.update_event(event_id, body)
        except KeyError as exc:
            return error_response(str(exc), status_code=404)
        except (ValueError, TypeError) as exc:
            return error_response(str(exc))
        return json_response({"ok": True, "event": ev})

    async def _api_event_delete(self):
        try:
            body = await request.json({}) or {}
        except Exception:
            return error_response("请求体必须是 JSON")
        event_id = str(body.get("id", "") or "")
        if not event_id:
            return error_response("缺少 id")
        try:
            self.delete_event(event_id)
        except KeyError as exc:
            return error_response(str(exc), status_code=404)
        return json_response({"ok": True})

    async def _api_digest(self):
        return json_response(self.build_digest())

    async def _api_countdown(self):
        return json_response(self.build_countdown())

    async def _api_diary_list(self):
        q = request.query.get("q", "") or ""
        tag = request.query.get("tag", "") or ""
        date = request.query.get("date", "") or ""
        try:
            limit = int(request.query.get("limit") or 0)
        except ValueError:
            limit = 0
        return json_response(
            {
                "entries": self.list_diary(
                    q=q, tag=tag, date=date, limit=limit
                )
            }
        )

    async def _api_diary_create(self):
        try:
            body = await request.json({}) or {}
        except Exception:
            return error_response("请求体必须是 JSON")
        try:
            entry = self.create_diary(body)
        except (ValueError, TypeError) as exc:
            return error_response(str(exc))
        return json_response({"ok": True, "entry": entry})

    async def _api_diary_update(self):
        try:
            body = await request.json({}) or {}
        except Exception:
            return error_response("请求体必须是 JSON")
        entry_id = str(body.pop("id", "") or "")
        if not entry_id:
            return error_response("缺少 id")
        try:
            entry = self.update_diary(entry_id, body)
        except KeyError as exc:
            return error_response(str(exc), status_code=404)
        except (ValueError, TypeError) as exc:
            return error_response(str(exc))
        return json_response({"ok": True, "entry": entry})

    async def _api_diary_delete(self):
        try:
            body = await request.json({}) or {}
        except Exception:
            return error_response("请求体必须是 JSON")
        entry_id = str(body.get("id", "") or "")
        if not entry_id:
            return error_response("缺少 id")
        try:
            self.delete_diary(entry_id)
        except KeyError as exc:
            return error_response(str(exc), status_code=404)
        return json_response({"ok": True})

    async def _api_diary_tags(self):
        return json_response({"tags": self.diary_tags()})

    async def _api_reminders(self):
        return json_response({"cards": self._pending_cards()})

    async def _api_reminder_action(self):
        try:
            body = await request.json({}) or {}
        except Exception:
            return error_response("请求体必须是 JSON")
        return json_response(self._window_action(body))

    async def _api_display(self):
        with self._lock:
            mode = self._state.get("display_mode", "panel")
        running = self._win_proc is not None and self._win_proc.poll() is None
        return json_response({"mode": mode, "window_running": running})

    async def _api_display_mode(self):
        try:
            body = await request.json({}) or {}
        except Exception:
            return error_response("请求体必须是 JSON")
        mode = str(body.get("mode") or "")
        if mode not in ("panel", "window"):
            return error_response("mode 必须是 panel 或 window")
        with self._lock:
            self._state["display_mode"] = mode
            self._save_state()
        if mode == "window":
            self._spawn_window()
        else:
            self._kill_window()
        return json_response({"ok": True, "mode": mode})

    async def _api_config(self):
        with self._lock:
            mode = self._state.get("display_mode", "panel")
            custom = self._state.get("custom_colors")
        return json_response(
            {
                "day_start_hour": self._day_start_hour,
                "remind_default_minutes": self._remind_default,
                "countdown_unit": self._countdown_unit,
                "colors": list(COLOR_CHOICES),
                "repeats": list(REPEAT_CHOICES),
                "display_mode": mode,
                "window_running": self._win_proc is not None and self._win_proc.poll() is None,
                "timezone": self._tz,
                "custom_colors": custom if self._valid_custom_colors(custom) else None,
            }
        )

    # ── AI dialog ─────────────────────────────────────────────────────────

    async def _api_ai(self):
        try:
            body = await request.json({}) or {}
        except Exception:
            return error_response("请求体必须是 JSON")
        text = str(body.get("text") or "").strip()
        if not text:
            return error_response("内容不能为空")
        if len(text) > 2000:
            return error_response("输入过长（上限 2000 字）")
        contexts: list[dict] = []
        hist = body.get("history")
        if isinstance(hist, list):
            for m in hist[-10:]:
                if not isinstance(m, dict):
                    continue
                role = m.get("role")
                content = m.get("content")
                if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                    contexts.append({"role": role, "content": content[:4000]})
        contexts.append({"role": "user", "content": text})
        try:
            prov = await self.context.get_using_provider_async()
        except Exception as exc:
            logger.warning("[calendar] provider lookup failed: %s", exc)
            prov = None
        if prov is None:
            return error_response("尚未配置对话模型：请在控制台「供应商」中启用一个 LLM")
        now = dt.datetime.now()
        sys_prompt = (
            AI_SYS_PROMPT.replace("{now}", now.strftime("%Y-%m-%d %H:%M"))
            .replace("{tz}", self._tz)
            .replace("{wd}", _weekday_cn(now.weekday()))
        )
        try:
            resp = await prov.text_chat(contexts=contexts, system_prompt=sys_prompt)
        except Exception as exc:
            logger.error("[calendar] ai text_chat failed: %s", exc)
            return error_response(f"AI 调用失败：{exc}")
        raw = (getattr(resp, "completion_text", "") or "").strip()
        obj = _parse_ai_json(raw)
        if obj is None:
            reply = raw or "（模型无回复）"
            return json_response({"reply": reply, "events": [], "errors": []})
        reply = str(obj.get("reply") or "").strip() or "好的"
        created: list[dict] = []
        errors: list[str] = []
        evs = obj.get("events")
        if isinstance(evs, list):
            for ev in evs[:3]:
                if not isinstance(ev, dict):
                    continue
                try:
                    created.append(self.create_event(ev, created_by="ai"))
                except (ValueError, TypeError) as exc:
                    errors.append(str(exc))
        return json_response({"reply": reply, "events": created, "errors": errors})

    # ── panel background image ────────────────────────────────────────────

    def _bg_file(self) -> Path:
        return self._data_dir / BG_FILE_NAME

    def _bg_state(self) -> dict:
        with self._lock:
            info = self._state.get("panel_bg")
            return dict(info) if isinstance(info, dict) else {}

    def _bg_store(self, info: dict) -> None:
        with self._lock:
            self._state["panel_bg"] = info
            self._save_state()

    def _bg_data_url(self) -> str:
        p = self._bg_file()
        try:
            raw = p.read_bytes()
        except OSError:
            return ""
        if not raw or len(raw) > MAX_BG_BYTES:
            return ""
        mime = _detect_image_mime(raw)
        if not mime:
            return ""
        return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")

    async def _api_bg(self):
        info = self._bg_state()
        image = self._bg_data_url() if info.get("enabled") else ""
        return json_response({"image": image, "opacity": _norm_opacity(info.get("opacity"))})

    async def _api_bg_set(self):
        try:
            body = await request.json({}) or {}
        except Exception:
            return error_response("请求体必须是 JSON")
        info = self._bg_state()
        if "opacity" in body:
            try:
                op = float(body.get("opacity"))
            except (TypeError, ValueError):
                return error_response("opacity 必须是数字")
            info["opacity"] = _norm_opacity(op)
        image = body.get("image")
        if image:
            if not isinstance(image, str) or not image.startswith("data:image/") or "," not in image:
                return error_response("图片必须是 data:image/...;base64 格式")
            try:
                raw = base64.b64decode(image.split(",", 1)[1], validate=False)
            except Exception:
                return error_response("图片解码失败")
            if not raw:
                return error_response("图片内容为空")
            if len(raw) > MAX_BG_BYTES:
                return error_response("图片不能超过 8MB")
            if _detect_image_mime(raw) is None:
                return error_response("仅支持 PNG/JPEG/WebP/GIF 图片")
            p = self._bg_file()
            tmp = p.with_name(BG_FILE_NAME + ".tmp")
            try:
                tmp.write_bytes(raw)
                os.replace(tmp, p)
            except OSError as exc:
                logger.error("[calendar] bg write failed: %s", exc)
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                return error_response("保存图片失败")
            info["enabled"] = True
            self._bg_store(info)
            return json_response(
                {"image": image, "opacity": _norm_opacity(info.get("opacity")), "enabled": True}
            )
        self._bg_store(info)
        return json_response(
            {"opacity": _norm_opacity(info.get("opacity")), "enabled": bool(info.get("enabled"))}
        )

    async def _api_bg_clear(self):
        info = self._bg_state()
        info["enabled"] = False
        self._bg_store(info)
        return json_response({"ok": True})

    async def _api_colors_set(self):
        try:
            body = await request.json({}) or {}
        except Exception:
            return error_response("请求体必须是 JSON")
        primary = _norm_hex(body.get("primary"))
        secondary = _norm_hex(body.get("secondary"))
        if not primary or not secondary:
            return error_response("primary/secondary 必须是合法颜色（#rrggbb）")
        with self._lock:
            self._state["custom_colors"] = {"primary": primary, "secondary": secondary}
            self._save_state()
        return json_response({"ok": True, "custom_colors": self._state["custom_colors"]})

    async def _api_colors_clear(self):
        with self._lock:
            self._state.pop("custom_colors", None)
            self._save_state()
        return json_response({"ok": True, "custom_colors": None})

    # ── LLM tools ────────────────────────────────────────────────────────

    def _register_tools(self) -> None:
        if not _HAS_TOOLS:
            logger.warning("[calendar] FunctionTool SDK unavailable; AI tools disabled")
            self._ai_enabled = False
            return
        self._tools = [
            CreateEventTool(plugin=self),
            ListEventsTool(plugin=self),
            ListCountdownTool(plugin=self),
            AddDiaryTool(plugin=self),
            ListDiaryTool(plugin=self),
            UpdateEventTool(plugin=self),
            DeleteEventTool(plugin=self),
        ]
        for tool in self._tools:
            self._tool_base[tool.name] = tool.description
        self.context.add_llm_tools(*self._tools)
        self._refresh_tool_time()

    # Tools that resolve relative time ("明天"/"下周三") or list by date need the
    # current clock; mutation-by-id tools do not, so we skip the suffix there to
    # avoid signalling calendar relevance on every turn.
    _TIME_SENSITIVE_TOOLS = frozenset({"create_event", "list_events", "list_countdown", "add_diary"})

    def _refresh_tool_time(self) -> None:
        if not self._tools:
            return
        now = dt.datetime.now()
        suffix = (
            f"（当前时间 {now:%Y-%m-%d %H:%M} 周{_weekday_cn(now.weekday())}，时区 {self._tz}）"
        )
        for tool in self._tools:
            if tool.name not in self._TIME_SENSITIVE_TOOLS:
                continue
            base = self._tool_base.get(tool.name, tool.description)
            try:
                tool.description = base + suffix
            except Exception:
                pass

    _REPEAT_UNTIL_WORDS = ("持续到", "截止到", "截至到", "直到", "结束于", "到……为止")

    def tool_create_event(self, **kwargs) -> str:
        rep = str(kwargs.get("repeat") or "none")
        note = str(kwargs.get("note") or "")
        until = str(kwargs.get("repeat_until") or "").strip()
        if (
            rep in ("daily", "weekly", "monthly")
            and not until
            and any(w in note for w in self._REPEAT_UNTIL_WORDS)
        ):
            return (
                "创建失败：备注提到「持续到/直到/截止到某日」但缺少 repeat_until 参数。"
                "请把终止日期解析为 repeat_until（YYYY-MM-DD）传入后重试；"
                "解析不出时先向用户澄清，禁止创建不终止的重复日程。"
            )
        try:
            ev = self.create_event(kwargs, created_by="llm")
        except (ValueError, TypeError) as exc:
            return f"创建失败：{exc}。请向用户说明原因，修正后重新走「复述确认」流程再调用。"
        if ev.get("repeat", "none") != "none" and not ev.get("repeat_until"):
            logger.warning(
                "[calendar] repeating event created without repeat_until: %s (%s)",
                ev.get("title"),
                ev.get("id"),
            )
        start = parse_datetime(ev["start"])
        when = f"{start:%Y-%m-%d} 全天" if ev.get("all_day") else f"{start:%Y-%m-%d %H:%M}"
        rep_out = ev.get("repeat", "none")
        rep = "" if rep_out == "none" else f"，{rep_out} 重复"
        if rep_out != "none":
            rep += (
                f"，截止 {ev['repeat_until'][:10]}"
                if ev.get("repeat_until")
                else "，无终止日期将一直重复"
            )
        rm = ev.get("remind_minutes")
        rm_txt = "" if not rm else f"，提前 {rm} 分钟提醒"
        return f"已创建：「{ev['title']}」{when}{rep}{rm_txt}（id={ev['id']}）"

    def tool_list_events(self, **kwargs) -> str:
        try:
            today = dt.date.today()
            dfrom = parse_date(kwargs.get("from_date")) if kwargs.get("from_date") else today
            dto = (
                parse_date(kwargs.get("to_date"))
                if kwargs.get("to_date")
                else dfrom + dt.timedelta(days=7)
            )
        except ValueError as exc:
            return f"查询失败：{exc}"
        if dfrom > dto:
            dfrom, dto = dto, dfrom
        if (dto - dfrom).days > 366:
            return "查询失败：范围过大（上限一年）"
        data = self.build_range(dfrom, dto)
        rows = []
        for ev in data["events"]:
            start = parse_datetime(ev["start"])
            end = parse_datetime(ev["end"])
            if ev["all_day"]:
                span = f"{start:%m-%d} 全天"
            else:
                span = f"{start:%m-%d %H:%M}-{end:%H:%M}"
            rp = ""
            rep_out = ev.get("repeat", "none")
            if rep_out != "none":
                rp = (
                    f" ·{rep_out}重复·截止{ev['repeat_until'][:10]}"
                    if ev.get("repeat_until")
                    else f" ·{rep_out}重复·无终止"
                )
            rows.append(
                f"[{ev['id']}] {span} {ev['title']}{rp}"
                + (f" ·@{ev['owner']}" if ev.get("owner") else "")
            )
        if kwargs.get("query"):
            ql = str(kwargs["query"]).lower()
            rows = [r for r in rows if ql in r.lower()]
        if not rows:
            return f"{dfrom:%Y-%m-%d} ~ {dto:%Y-%m-%d} 没有匹配的日程。"
        return f"{dfrom:%Y-%m-%d} ~ {dto:%Y-%m-%d} 共 {len(rows)} 条：\n" + "\n".join(rows)

    def tool_list_countdown(self, **kwargs) -> str:
        try:
            limit = int(kwargs.get("limit") or 8)
        except (TypeError, ValueError):
            limit = 8
        limit = max(1, min(20, limit))
        data = self.build_countdown(limit=limit)

        def fmt(sec: int) -> str:
            if sec <= 0:
                return "进行中"
            unit = data["unit"]
            if unit == "week":
                return f"{sec // 604800}周"
            if unit == "hour":
                return f"{sec // 3600}小时"
            return f"{sec // 86400}天"

        rows = []
        for it in data["items"]:
            when = str(it["target"])[:16].replace("T", " ")
            owner = f" @{it['owner']}" if it.get("owner") else ""
            rows.append(
                f"[{it['id']}] {when} · {fmt(it['remaining'])} · {it['title']}{owner}"
            )
        if not rows:
            return "未来没有待办。"
        return f"最近 {len(rows)} 条待办：\n" + "\n".join(rows)

    def tool_add_diary(self, **kwargs) -> str:
        title = str(kwargs.get("title") or "").strip()
        if not title:
            return "写入失败：日记标题不能为空。"
        payload = {
            "title": title,
            "content": str(kwargs.get("content") or ""),
        }
        date = kwargs.get("date")
        if date:
            payload["date"] = str(date)
        try:
            entry = self.create_diary(payload)
        except (ValueError, TypeError) as exc:
            return f"写入失败：{exc}"
        tags = "、".join(f"#{t}" for t in entry.get("tags") or []) or "无标签"
        return f"已写入日记《{entry['title']}》（{entry['date']}，{tags}）。"

    def tool_list_diary(self, **kwargs) -> str:
        try:
            limit = int(kwargs.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(30, limit))
        entries = self.list_diary(
            q=str(kwargs.get("query") or ""),
            tag=str(kwargs.get("tag") or ""),
            date=str(kwargs.get("date") or ""),
            limit=limit,
        )
        if not entries:
            return "没有匹配的日记。"
        rows = []
        for ent in entries:
            tags = " ".join(f"#{t}" for t in ent.get("tags") or [])
            snippet = str(ent.get("content", "")).replace("\n", " ")
            if len(snippet) > 60:
                snippet = snippet[:60] + "…"
            rows.append(
                f"[{ent['id']}] {ent.get('date', '')} 《{ent.get('title', '')}》"
                f" {tags} — {snippet}"
            )
        return f"共 {len(rows)} 篇：\n" + "\n".join(rows)

    def tool_update_event(self, **kwargs) -> str:
        event_id = str(kwargs.pop("id", "") or "")
        if not event_id:
            return "更新失败：缺少 id（先用 list_events 查询）。"
        rep = str(kwargs.get("repeat") or "none")
        note = str(kwargs.get("note") or "")
        until = str(kwargs.get("repeat_until") or "").strip()
        if (
            rep in ("daily", "weekly", "monthly")
            and not until
            and any(w in note for w in self._REPEAT_UNTIL_WORDS)
        ):
            return (
                "更新失败：备注提到「持续到/直到/截止到某日」但缺少 repeat_until 参数。"
                "请把终止日期解析为 repeat_until 传入；解析不出时先向用户澄清。"
            )
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        if not kwargs:
            return "更新失败：没有要修改的字段。"
        try:
            ev = self.update_event(event_id, kwargs)
        except KeyError as exc:
            return f"更新失败：{exc}"
        except (ValueError, TypeError) as exc:
            return f"更新失败：{exc}。请修正后重新确认再调用。"
        rep_out = ev.get("repeat", "none")
        rep_txt = ""
        if rep_out != "none":
            rep_txt = (
                f"（{rep_out} 重复，截止 {ev['repeat_until'][:10]}）"
                if ev.get("repeat_until")
                else f"（{rep_out} 重复，无终止日期将一直重复）"
            )
        return f"已更新：「{ev['title']}」{ev['start']}{rep_txt}（id={ev['id']}）"

    def tool_delete_event(self, **kwargs) -> str:
        event_id = str(kwargs.get("id") or "")
        if not event_id:
            return "删除失败：缺少 id（先用 list_events 查询）。"
        try:
            self.delete_event(event_id)
        except KeyError as exc:
            return f"删除失败：{exc}"
        return f"已删除日程 id={event_id}"

    # ── demo data (spec §8) ──────────────────────────────────────────────

    def _seed_demo(self, replace: bool = False) -> int:
        """Insert the spec's real schedule. Idempotent by demo id."""
        year = dt.date.today().year

        def d(month: int, day: int) -> str:
            return f"{year}-{month:02d}-{day:02d}"

        rows: list[dict] = [
            dict(
                id="demo_national_vacation",
                title="国庆假期",
                all_day=True,
                start=f"{d(10, 1)}T00:00:00",
                end=f"{d(10, 7)}T23:59:00",
                color="indigo",
            ),
            dict(id="demo_annual_day1", title="国庆节·挑战班年会Day1", start=f"{d(10, 8)}T09:00:00", end=f"{d(10, 8)}T12:00:00", color="blue", note="挑战班年会 第一天"),
            dict(id="demo_annual_day2", title="挑战班年会Day2", start=f"{d(10, 9)}T09:00:00", end=f"{d(10, 9)}T12:00:00", color="blue", note="挑战班年会 第二天"),
            dict(id="demo_thought_report", title="提交思想报告", start=f"{d(10, 9)}T18:00:00", end=f"{d(10, 9)}T19:00:00", color="slate"),
            dict(id="demo_self_study", title="个人学习总结", start=f"{d(10, 10)}T14:00:00", end=f"{d(10, 10)}T16:00:00", color="teal"),
            dict(id="demo_hdp_closing", title="高党结业仪式", start=f"{d(10, 11)}T09:00:00", end=f"{d(10, 11)}T11:00:00", color="indigo"),
            dict(id="demo_hdp_discuss", title="高党集体讨论", start=f"{d(10, 11)}T14:00:00", end=f"{d(10, 11)}T16:00:00", color="indigo"),
            dict(id="demo_writing_a1", title="写作Assignment1", all_day=True, start=f"{d(10, 12)}T00:00:00", end=f"{d(10, 12)}T23:59:00", color="slate"),
            dict(id="demo_bio_midterm", title="生化期中", start=f"{d(10, 14)}T09:00:00", end=f"{d(10, 14)}T11:00:00", color="blue", remind_minutes=15, note="带学生证"),
            dict(id="demo_writing_a2", title="写作Assignment2", all_day=True, start=f"{d(10, 19)}T00:00:00", end=f"{d(10, 19)}T23:59:00", color="slate"),
            dict(id="demo_physio_report", title="生理学读书报告", start=f"{d(10, 20)}T14:00:00", end=f"{d(10, 20)}T16:00:00", color="teal"),
            dict(id="demo_bio_pre", title="生化讨论pre", start=f"{d(10, 21)}T15:00:00", end=f"{d(10, 21)}T17:00:00", color="blue", remind_minutes=30),
            dict(id="demo_bio_handout", title="生化实验讲义", all_day=True, start=f"{d(11, 1)}T00:00:00", end=f"{d(11, 1)}T23:59:00", color="slate"),
            dict(id="demo_physics_lab", title="普物实验", start=f"{d(11, 2)}T14:00:00", end=f"{d(11, 2)}T17:00:00", color="teal"),
            dict(id="demo_marx_pre", title="马原哲学pre", start=f"{d(11, 3)}T09:00:00", end=f"{d(11, 3)}T10:30:00", color="indigo"),
            dict(id="demo_bio_midterm2", title="生化期中", start=f"{d(11, 6)}T09:00:00", end=f"{d(11, 6)}T11:00:00", color="blue", remind_minutes=15),
            dict(id="demo_physio_lit", title="生理学问题文献讲述", start=f"{d(11, 10)}T09:00:00", end=f"{d(11, 10)}T10:00:00", color="teal"),
        ]
        now = dt_str(dt.datetime.now())
        count = 0
        with self._lock:
            if replace:
                self._events = [e for e in self._events if not e.get("demo")]
                self._save_events()
            already = {e.get("id") for e in self._events}
            for row in rows:
                if row["id"] in already:
                    continue
                ev = dict(row)
                ev.setdefault("all_day", False)
                ev.setdefault("repeat", "none")
                ev.setdefault("remind_minutes", None)  # demo stays quiet unless specified
                ev.setdefault("note", "")
                ev["done"] = False
                ev["demo"] = True
                ev["created_by"] = "demo"
                ev["created_at"] = now
                ev["updated_at"] = now
                self._events.append(ev)
                count += 1
            if count:
                self._save_events()
        if count:
            logger.info("[calendar] seeded %d demo event(s)", count)
        return count
