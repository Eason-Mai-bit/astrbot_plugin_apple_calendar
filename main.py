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
    class AddGoalTool(FunctionTool[AstrAgentContext]):
        """Create a note (big note or small note) on the whiteboard."""

        name: str = "add_goal"
        description: str = (
            "创建便签（大便签，或挂在某大便签下的小便签）。parent 缺省=独立大便签；"
            "支持 #标签(tags)、时间与颜色。直接创建，无需用户确认。"
        )
        parameters: dict = Field(
            default_factory=lambda: {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "便签标题（必填，≤200字）"},
                    "parent": {"type": "string", "description": "大便签标题（挂为小便签）；缺省创建独立大便签"},
                    "note": {"type": "string", "description": "备注；缺省空"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "标签列表（不含 # 号），可缺省",
                    },
                    "start": {
                        "type": "string",
                        "description": "开始时间（YYYY-MM-DD HH:MM，可跨天）；可留空",
                    },
                    "end": {
                        "type": "string",
                        "description": "结束时间（YYYY-MM-DD HH:MM）；缺省与开始相同；可留空",
                    },
                    "color": {
                        "type": "string",
                        "enum": list(COLOR_CHOICES),
                        "description": "便签主颜色；缺省 blue",
                    },
                },
                "required": ["title"],
            }
        )
        plugin: Any = None

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return self.plugin.tool_add_goal(**kwargs)

    @pyd_dataclass
    class ListGoalsTool(FunctionTool[AstrAgentContext]):
        """List whiteboard notes with progress and tags."""

        name: str = "list_goals"
        description: str = (
            "列出白板便签与进度（大便签 + 其下小便签完成情况）。"
            "可按关键词或标签过滤。仅查询，无需确认。"
        )
        parameters: dict = Field(
            default_factory=lambda: {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "关键词，匹配标题、备注或标签；缺省不过滤"},
                    "tag": {"type": "string", "description": "按标签过滤（不含 # 号，逗号分隔为 AND）"},
                },
                "required": [],
            }
        )
        plugin: Any = None

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return self.plugin.tool_list_goals(**kwargs)

    @pyd_dataclass
    class UpdateGoalTool(FunctionTool[AstrAgentContext]):
        """Update an existing note (title / note / done / tags / time)."""

        name: str = "update_goal"
        description: str = (
            "修改已有便签（先 list_goals 拿 id）：可改标题、备注、标签、时间，"
            "或标记完成/未完成。改动需先向用户复述并确认后再调用。"
        )
        parameters: dict = Field(
            default_factory=lambda: {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "便签 id（来自 list_goals）"},
                    "title": {"type": "string", "description": "新标题"},
                    "note": {"type": "string", "description": "新备注"},
                    "done": {"type": "boolean", "description": "标记完成 true / 未完成 false"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "标签列表（整体替换，不含 # 号）",
                    },
                    "start": {
                        "type": "string",
                        "description": "开始时间 YYYY-MM-DD HH:MM；传空字符串清除",
                    },
                    "end": {
                        "type": "string",
                        "description": "结束时间 YYYY-MM-DD HH:MM；传空字符串清除",
                    },
                },
                "required": ["id"],
            }
        )
        plugin: Any = None

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return self.plugin.tool_update_goal(**kwargs)

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
        self._goals_items: list[dict] = []
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
        self._load_goals()
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
            self._save_goals()

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

    @property
    def _goals_path(self) -> Path:
        return self._data_dir / "goals.json"

    def _load_goals(self) -> None:
        data = rec.load_json_file(
            str(self._goals_path), {"version": 1, "goals": []}
        )
        if not isinstance(data, dict):
            data = {}
        goals = data.get("goals")
        self._goals_items = [
            g for g in goals if isinstance(g, dict) and g.get("id")
        ] if isinstance(goals, list) else []
        # 周期概念已取消：迁移旧数据（丢弃 cycle_id，补齐便签字段）
        for g in self._goals_items:
            g.pop("cycle_id", None)
            g.setdefault("kind", "small" if g.get("parent_id") else "big")
            g.setdefault("scale", 1.0)
            g.setdefault("tags", [])
            # 旧三件套 date/start_time/end_time → 可跨天 start/end
            if "date" in g:
                d = g.pop("date", None)
                t1 = g.pop("start_time", None)
                t2 = g.pop("end_time", None)
                if d:
                    g["start"] = f"{d} {t1 or '00:00'}"
                    g["end"] = f"{d} {t2 or '23:59'}"
            g.pop("start_time", None)
            g.pop("end_time", None)
            g.setdefault("start", None)
            g.setdefault("end", None)
        # 旧数据无坐标：加载时补白板落点（大便签错位排布，小便签挂父便签下）
        big_n = 0
        for g in self._goals_items:
            if g.get("parent_id"):
                continue
            if not isinstance(g.get("x"), (int, float)) or not isinstance(
                g.get("y"), (int, float)
            ):
                g["x"] = 24 + (big_n % 3) * 296
                g["y"] = 24 + (big_n // 3) * 270
            big_n += 1
        for g in self._goals_items:
            if not g.get("parent_id"):
                continue
            if not isinstance(g.get("x"), (int, float)) or not isinstance(
                g.get("y"), (int, float)
            ):
                parent = self._find_goal(self._goals_items, g.get("parent_id"))
                g["x"] = int((parent or {}).get("x") or 0) + 40
                g["y"] = int((parent or {}).get("y") or 0) + 270

    def _save_goals(self) -> None:
        try:
            rec.atomic_write_json(
                str(self._goals_path),
                {"version": 1, "goals": self._goals_items},
            )
        except OSError as exc:
            logger.error("[calendar] saving goals failed: %s", exc)

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

    # ── goals (白板便签 · 无周期) ──────────────────────────────────────────
    # 大便签(kind=big) / 小便签(kind=small)：小便签可挂大便签下(行内下划线行)
    # 或独立存在；每张便签独立设时间/颜色/标签/缩放(scale)与白板坐标(x,y)。

    @staticmethod
    def _find_goal(goals: list[dict], goal_id: str) -> Optional[dict]:
        return next((g for g in goals if g.get("id") == goal_id), None)

    @classmethod
    def _parse_when(cls, raw: str, end_side: bool = False) -> Optional[str]:
        """宽松解析便签时间：可省略天/小时/分钟。
        支持 2026-11-12 14:30 / 2026-11-12 14 / 2026-11-12 /
        11-12 14:00 / 11-12 / 14:30 / 14（今天）。返回 'YYYY-MM-DD HH:MM'。
        end_side=True 时“只到天”默认 23:59，否则 00:00。
        """
        s = str(raw or "").strip().replace("T", " ")
        if not s:
            return None
        today = dt.date.today()
        year = today.year
        day: Optional[dt.date] = None
        hh: Optional[int] = None
        mm: Optional[int] = None
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?: (\d{1,2})(?::(\d{1,2}))?)?$", s)
        if m:
            try:
                day = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                raise ValueError(f"日期无效: {raw}")
            if m.group(4) is not None:
                hh = int(m.group(4))
                mm = int(m.group(5) or 0)
        else:
            m = re.match(r"^(\d{1,2})-(\d{1,2})(?: (\d{1,2})(?::(\d{1,2}))?)?$", s)
            if m:
                mo, dd = int(m.group(1)), int(m.group(2))
                cand = None
                for y in (year, year + 1):
                    try:
                        cand = dt.date(y, mo, dd)
                        break
                    except ValueError:
                        continue
                if cand is None:
                    raise ValueError(f"日期无效: {raw}")
                if cand.year == year and (today - cand).days > 180:
                    try:
                        cand = dt.date(year + 1, mo, dd)
                    except ValueError:
                        pass
                day = cand
                if m.group(3) is not None:
                    hh = int(m.group(3))
                    mm = int(m.group(4) or 0)
            else:
                m = re.match(r"^(\d{1,2}):(\d{1,2})(?::\d{1,2})?$", s)
                if m:
                    day = today
                    hh = int(m.group(1))
                    mm = int(m.group(2))
                else:
                    m = re.match(r"^(\d{1,2})$", s)
                    if m:
                        day = today
                        hh = int(m.group(1))
                        mm = 0
                    else:
                        # 兜底：交给标准解析（给出可读错误）
                        return parse_datetime(s).strftime("%Y-%m-%d %H:%M")
        if hh is not None and hh > 23:
            raise ValueError(f"小时无效: {raw}")
        if mm is not None and mm > 59:
            raise ValueError(f"分钟无效: {raw}")
        if hh is None:
            hh, mm = (23, 59) if end_side else (0, 0)
        return f"{day:%Y-%m-%d} {hh:02d}:{int(mm or 0):02d}"

    @staticmethod
    def _check_goal_range(
        start: Optional[str], end: Optional[str]
    ) -> tuple[Optional[str], Optional[str]]:
        """日程范围：只给一端则另一端补齐（只到天 → 补整天）；结束不早于开始。"""
        if start and not end:
            end = start[:10] + " 23:59" if start.endswith("00:00") else start
        if end and not start:
            start = end[:10] + " 00:00" if end.endswith("23:59") else end
        if start and end and end < start:
            raise ValueError("结束时间不能早于开始时间")
        return start, end

    @staticmethod
    def _normalize_tags(tags: Any) -> list[str]:
        """显式标签：去 #、去重、单个 ≤24 字、最多 10 个。"""
        out: list[str] = []
        if isinstance(tags, list):
            for t in tags:
                s = str(t).strip().lstrip("#")
                if s and len(s) <= 24 and s not in out:
                    out.append(s)
        return out[:10]

    @staticmethod
    def _goal_tags(goal: dict) -> list[str]:
        """展示/检索用标签 = 显式 tags ∪ 标题与正文中的 #标签。"""
        out: list[str] = [str(t) for t in (goal.get("tags") or [])]
        for t in extract_tags(
            str(goal.get("title", "")) + "\n" + str(goal.get("note", ""))
        ):
            if t not in out:
                out.append(t)
        return out[:10]

    def _validate_goal(self, data: dict, partial: bool = False) -> dict:
        out: dict[str, Any] = {}
        if not partial or "title" in data:
            title = str(data.get("title") or "").strip()
            if not title:
                raise ValueError("目标标题不能为空")
            if len(title) > 200:
                raise ValueError("目标标题最长 200 字")
            out["title"] = title
        if "note" in data:
            note = str(data.get("note") or "")
            if len(note) > 2000:
                raise ValueError("备注最长 2000 字")
            out["note"] = note
        if "color" in data:
            color = str(data.get("color") or "blue")
            out["color"] = color if color in COLOR_CHOICES else "blue"
        if "done" in data:
            out["done"] = bool(data.get("done"))
        if "kind" in data:
            kind = str(data.get("kind") or "")
            if kind not in ("big", "small"):
                raise ValueError("类型只能是大便签或小便签")
            out["kind"] = kind
        if "tags" in data:
            out["tags"] = self._normalize_tags(data.get("tags"))
        if "scale" in data and data.get("scale") is not None:
            try:
                sc = float(data.get("scale"))
            except (TypeError, ValueError):
                raise ValueError("缩放必须是数字")
            if not 0.5 <= sc <= 2.0:
                raise ValueError("缩放范围 0.5 ~ 2.0")
            out["scale"] = round(sc, 2)
        for key in ("x", "y"):
            if key in data and data.get(key) is not None:
                try:
                    out[key] = max(0, min(100000, int(data.get(key))))
                except (TypeError, ValueError):
                    raise ValueError("便签坐标必须是整数")
        for key in ("start", "end"):
            if key in data:
                out[key] = self._parse_when(
                    str(data.get(key) or ""), end_side=(key == "end")
                )
        return out

    def _goal_has_children(self, goal_id: str) -> bool:
        return any(g.get("parent_id") == goal_id for g in self._goals_items)

    def create_goal(self, payload: dict) -> dict:
        with self._lock:
            fields = self._validate_goal(payload, partial=False)
            parent_id = payload.get("parent_id")
            parent_id = str(parent_id) if parent_id else None
            kind = fields.get("kind") or ("small" if parent_id else "big")
            parent = None
            if parent_id:
                parent = self._find_goal(self._goals_items, parent_id)
                if parent is None:
                    raise ValueError("父便签不存在")
                if parent.get("kind") != "big":
                    raise ValueError("只能挂到大便签下")
                if parent.get("parent_id"):
                    raise ValueError("便签只支持两层结构")
                if kind == "big":
                    raise ValueError("大便签不能挂到别的便签下")
            start, end = self._check_goal_range(
                fields.get("start"), fields.get("end")
            )
            same = [
                g
                for g in self._goals_items
                if (g.get("parent_id") or None) == parent_id
            ]
            order = max((int(g.get("order") or 0) for g in same), default=0) + 1
            x = fields.get("x")
            y = fields.get("y")
            if x is None or y is None:
                if parent is not None:
                    x = int(parent.get("x") or 0) + 40
                    y = int(parent.get("y") or 0) + 270
                else:
                    n = sum(
                        1 for g in self._goals_items if not g.get("parent_id")
                    )
                    x = 24 + (n % 3) * 296
                    y = 24 + (n // 3) * 270
            now = dt_str(dt.datetime.now())
            goal = {
                "id": uuid.uuid4().hex,
                "parent_id": parent_id,
                "kind": kind,
                "title": fields["title"],
                "note": fields.get("note", ""),
                "color": fields.get("color", "blue"),
                "done": fields.get("done", False),
                "tags": fields.get("tags", []),
                "scale": fields.get("scale", 1.0),
                "start": start,
                "end": end,
                "order": order,
                "x": int(x),
                "y": int(y),
                "created_at": now,
                "updated_at": now,
            }
            self._goals_items.append(goal)
            self._save_goals()
            return goal

    def update_goal(self, goal_id: str, payload: dict) -> dict:
        with self._lock:
            goal = self._find_goal(self._goals_items, goal_id)
            if goal is None:
                raise KeyError(f"未找到目标 {goal_id}")
            fields = self._validate_goal(payload, partial=True)
            m_start = fields["start"] if "start" in fields else goal.get("start")
            m_end = fields["end"] if "end" in fields else goal.get("end")
            m_start, m_end = self._check_goal_range(m_start, m_end)
            if "parent_id" in payload:
                parent_id = payload.get("parent_id")
                parent_id = str(parent_id) if parent_id else None
                if parent_id:
                    parent = self._find_goal(self._goals_items, parent_id)
                    if parent is None:
                        raise ValueError("父便签不存在")
                    if parent.get("id") == goal_id:
                        raise ValueError("便签不能挂到自己下面")
                    if parent.get("kind") != "big":
                        raise ValueError("只能挂到大便签下")
                    if parent.get("parent_id"):
                        raise ValueError("便签只支持两层结构")
                    if goal.get("kind") == "big":
                        raise ValueError("大便签不能挂到别的便签下")
                if parent_id != goal.get("parent_id"):
                    goal["parent_id"] = parent_id
                    goal["order"] = self._next_goal_order(
                        parent_id, exclude=goal_id
                    )
            if "kind" in fields:
                new_kind = fields["kind"]
                if new_kind == "big" and self._goal_has_children(goal_id):
                    raise ValueError("大便签下还有小便签，先移出小便签")
                if new_kind == "small" and goal.get("parent_id") and not (
                    payload.get("parent_id") is not None
                    or "parent_id" in payload
                ):
                    pass  # parent unchanged → stays a child row
                goal["kind"] = new_kind
            for key in (
                "title",
                "note",
                "color",
                "done",
                "tags",
                "scale",
                "x",
                "y",
            ):
                if key in fields:
                    goal[key] = fields[key]
            if "start" in fields or "end" in fields:
                goal["start"] = m_start
                goal["end"] = m_end
            goal["updated_at"] = dt_str(dt.datetime.now())
            self._save_goals()
            return goal

    def _next_goal_order(self, parent_id: Optional[str], exclude: str = "") -> int:
        same = [
            g
            for g in self._goals_items
            if g.get("id") != exclude
            and (g.get("parent_id") or None) == parent_id
        ]
        return max((int(g.get("order") or 0) for g in same), default=0) + 1

    def delete_goal(self, goal_id: str) -> int:
        with self._lock:
            goal = self._find_goal(self._goals_items, goal_id)
            if goal is None:
                raise KeyError(f"未找到目标 {goal_id}")
            ids = {goal_id}
            if not goal.get("parent_id"):
                ids |= {
                    g["id"]
                    for g in self._goals_items
                    if g.get("parent_id") == goal_id
                }
            self._goals_items = [g for g in self._goals_items if g["id"] not in ids]
            self._save_goals()
            return len(ids)

    def order_goals(self, parent_id: Optional[str], ordered_ids: list[str]) -> int:
        """同一父组内的便签排序（拖动调序）。"""
        with self._lock:
            group = [
                g
                for g in self._goals_items
                if (g.get("parent_id") or None) == parent_id
            ]
            group_ids = {g["id"] for g in group}
            given = [str(i) for i in ordered_ids]
            if set(given) != group_ids or len(given) != len(group_ids):
                raise ValueError("排序列表与该组目标不匹配")
            rank = {gid: i for i, gid in enumerate(given)}
            for g in group:
                g["order"] = rank[g["id"]] + 1
                g["updated_at"] = dt_str(dt.datetime.now())
            self._save_goals()
            return len(group)

    def goals_payload(self, query: str = "", tag: str = "") -> dict:
        """检索：query 匹配标题/备注/标签；tag 逗号分隔多标签(AND)。"""
        with self._lock:
            items = [dict(g) for g in self._goals_items]
        for g in items:
            g["tags"] = self._goal_tags(g)
        ql = str(query or "").strip().lower()
        wanted = [t.strip() for t in str(tag or "").split(",") if t.strip()]
        out = []
        for g in items:
            if wanted and not all(t in (g.get("tags") or []) for t in wanted):
                continue
            if ql:
                hay = (
                    str(g.get("title", ""))
                    + "\n"
                    + str(g.get("note", ""))
                    + "\n"
                    + " ".join(g.get("tags") or [])
                ).lower()
                if ql not in hay:
                    continue
            out.append(g)
        out.sort(key=lambda g: int(g.get("order") or 0))
        counts: dict[str, int] = {}
        for g in items:
            for t in g.get("tags") or []:
                counts[t] = counts.get(t, 0) + 1
        tags = sorted(
            ({"tag": k, "count": v} for k, v in counts.items()),
            key=lambda x: (-x["count"], x["tag"]),
        )
        return {"goals": out, "tags": tags}

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
        reg(f"/{PLUGIN_NAME}/goals", self._api_goals, ["GET"], "Cycles and goals")
        reg(f"/{PLUGIN_NAME}/goals", self._api_goal_create, ["POST"], "Create goal")
        reg(f"/{PLUGIN_NAME}/goals/update", self._api_goal_update, ["POST"], "Update goal")
        reg(f"/{PLUGIN_NAME}/goals/delete", self._api_goal_delete, ["POST"], "Delete goal")
        reg(f"/{PLUGIN_NAME}/goals/order", self._api_goal_order, ["POST"], "Reorder goals in a group")
        for _removed in (
            "/goals/reorder",
            "/goals/cycle",
            "/goals/cycle/update",
            "/goals/cycle/delete",
        ):
            reg(f"/{PLUGIN_NAME}{_removed}", self._api_goals_removed, ["POST"], "Removed endpoint")
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

    # ── goals API ────────────────────────────────────────────────────────

    async def _api_goals(self):
        query = request.query.get("q", "") or ""
        tag = request.query.get("tag", "") or ""
        return json_response(self.goals_payload(query=query, tag=tag))

    async def _api_goal_create(self):
        try:
            body = await request.json({}) or {}
        except Exception:
            return error_response("请求体必须是 JSON")
        try:
            goal = self.create_goal(body)
        except (ValueError, TypeError) as exc:
            return error_response(str(exc))
        return json_response({"ok": True, "goal": goal})

    async def _api_goal_update(self):
        try:
            body = await request.json({}) or {}
        except Exception:
            return error_response("请求体必须是 JSON")
        goal_id = str(body.pop("id", "") or "")
        if not goal_id:
            return error_response("缺少 id")
        try:
            goal = self.update_goal(goal_id, body)
        except KeyError as exc:
            return error_response(str(exc), status_code=404)
        except (ValueError, TypeError) as exc:
            return error_response(str(exc))
        return json_response({"ok": True, "goal": goal})

    async def _api_goal_delete(self):
        try:
            body = await request.json({}) or {}
        except Exception:
            return error_response("请求体必须是 JSON")
        goal_id = str(body.get("id", "") or "")
        if not goal_id:
            return error_response("缺少 id")
        try:
            removed = self.delete_goal(goal_id)
        except KeyError as exc:
            return error_response(str(exc), status_code=404)
        return json_response({"ok": True, "removed": removed})

    async def _api_goal_order(self):
        try:
            body = await request.json({}) or {}
        except Exception:
            return error_response("请求体必须是 JSON")
        parent_id = body.get("parent_id")
        parent_id = str(parent_id) if parent_id else None
        ordered = body.get("ordered_ids")
        if not isinstance(ordered, list):
            return error_response("缺少 ordered_ids")
        try:
            count = self.order_goals(parent_id, ordered)
        except ValueError as exc:
            return error_response(str(exc))
        return json_response({"ok": True, "count": count})

    async def _api_goals_removed(self):
        # 占位：仪表盘会跨重载保留旧实例的路由，用同路径覆盖使其失效
        return error_response("该接口已移除：便签已改为无周期白板")

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
            AddGoalTool(plugin=self),
            ListGoalsTool(plugin=self),
            UpdateGoalTool(plugin=self),
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
    _TIME_SENSITIVE_TOOLS = frozenset({"create_event", "list_events", "list_countdown", "add_diary", "add_goal"})

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

    def tool_add_goal(self, **kwargs) -> str:
        title = str(kwargs.get("title") or "").strip()
        if not title:
            return "创建失败：便签标题不能为空。"
        parent_id = None
        parent_title = str(kwargs.get("parent") or "").strip()
        if parent_title:
            with self._lock:
                tops = [
                    g
                    for g in self._goals_items
                    if not g.get("parent_id") and g.get("kind") == "big"
                ]
            hit = [g for g in tops if g.get("title") == parent_title]
            if not hit:
                hit = [g for g in tops if parent_title in str(g.get("title", ""))]
            if len(hit) != 1:
                names = "、".join(str(g.get("title", "")) for g in tops[:6]) or "（无大便签）"
                return (
                    f"创建失败：大便签「{parent_title}」"
                    f"{'匹配不唯一' if hit else '不存在'}，现有大便签：{names}"
                )
            parent_id = hit[0]["id"]
        payload: dict[str, Any] = {
            "title": title,
            "note": str(kwargs.get("note") or ""),
            "color": str(kwargs.get("color") or "blue"),
        }
        for key in ("start", "end"):
            if kwargs.get(key):
                payload[key] = str(kwargs[key])
        if kwargs.get("tags") is not None:
            payload["tags"] = kwargs.get("tags")
        if parent_id:
            payload["parent_id"] = parent_id
        try:
            goal = self.create_goal(payload)
        except (ValueError, TypeError) as exc:
            return f"创建失败：{exc}"
        kind = "小便签" if goal.get("kind") == "small" else "大便签"
        tags = "、".join(f"#{t}" for t in self._goal_tags(goal)) or "无标签"
        when = ""
        if goal.get("start"):
            when = f"，{goal['start']} → {goal.get('end') or goal['start']}"
        return f"已创建{kind}「{goal['title']}」（{tags}{when}，id {goal['id']}）。"

    def tool_list_goals(self, **kwargs) -> str:
        data = self.goals_payload(
            query=str(kwargs.get("query") or ""),
            tag=str(kwargs.get("tag") or ""),
        )
        goals = data["goals"]
        if not goals:
            return "没有匹配的便签。"
        tops = [g for g in goals if not g.get("parent_id")]
        rows = [f"白板共 {len(goals)} 张便签："]
        for top in tops:
            kids = [g for g in goals if g.get("parent_id") == top["id"]]
            done_n = sum(1 for k in kids if k.get("done"))
            mark = "✓" if top.get("done") else "○"
            line = f"  {mark} [{top['id']}] {top['title']}"
            tags = " ".join(f"#{t}" for t in top.get("tags") or [])
            if tags:
                line += f" {tags}"
            if top.get("start"):
                line += f"（{top['start']} → {top.get('end') or top['start']}）"
            if kids:
                line += f"（小便签 {done_n}/{len(kids)}）"
            rows.append(line)
            for k in kids:
                km = "✓" if k.get("done") else "○"
                ktags = " ".join(f"#{t}" for t in k.get("tags") or [])
                kline = f"     {km} [{k['id']}] {k['title']}"
                if ktags:
                    kline += f" {ktags}"
                if k.get("start"):
                    kline += f"（{k['start']} → {k.get('end') or k['start']}）"
                rows.append(kline)
        top_ids = {t["id"] for t in tops}
        for g in goals:
            if g.get("parent_id") and g.get("parent_id") not in top_ids:
                mark = "✓" if g.get("done") else "○"
                rows.append(f"  {mark} [{g['id']}] {g['title']}（独立小便签）")
        return "\n".join(rows[:60])

    def tool_update_goal(self, **kwargs) -> str:
        goal_id = str(kwargs.get("id") or "").strip()
        if not goal_id:
            return "修改失败：缺少 id。"
        payload: dict[str, Any] = {}
        for key in ("title", "note", "done", "start", "end", "tags"):
            if kwargs.get(key) is not None:
                payload[key] = kwargs[key]
        if not payload:
            return "修改失败：没有提供要改的字段。"
        try:
            goal = self.update_goal(goal_id, payload)
        except KeyError as exc:
            return f"修改失败：{exc}"
        except (ValueError, TypeError) as exc:
            return f"修改失败：{exc}"
        state = "已完成" if goal.get("done") else "未完成"
        return f"已更新便签「{goal['title']}」（{state}，id {goal['id']}）。"

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
