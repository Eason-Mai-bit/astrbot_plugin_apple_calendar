# 日历日程（astrbot_plugin_calendar）

Apple Calendar 风格的日程日历面板 + 桌面右下角日程小窗 + 提醒卡片 + 自然语言增删查改日程，纯本地运行，内置农历 / 节气 / 休班角标数据。

- 版本：0.2.0
- 作者：Eason-Mai-bit
- 要求：AstrBot >= 4.5.7（AI 工具需 >= 4.5.1 的 FunctionTool SDK）
- 平台：面板全平台可用；桌面小窗面向 Windows（圆角 / 液态玻璃使用 Win32 API）

## 功能

### 日历面板

- 年 / 月 / 日三种视图，月视图统一「日期在上、日程在下」，超出单元格高度自动折叠为 `+N`
- 事件胶囊按颜色分组（blue / teal / indigo / slate），支持全天日程、跨天重复（每天 / 每周 / 每月）
- 农历日期、干支、节气、传统节日自动显示；休 / 班角标来自 `chinese-calendar`
- 关键词搜索、按日历显隐过滤（☰）、点击日期进入日视图、今天快捷返回
- 打开 / 切换视图与弹窗均有 ≤200ms 的 opacity/transform 动画

### 桌面小窗（右下角常驻）

- 置顶圆角卡片，液态玻璃（可关）、4 色主题循环、拖动松手自动磁吸到最近屏幕边（320ms 滑动动画，开关可关），位置自动记忆
- 居中大日期 + 当日日程胶囊；点选胶囊后出现手绘图标：✓ 标记完成、垃圾桶删除（二次确认）
- 顶栏手绘按钮：左侧回面板，右侧吸附开关（磁铁图标，斜杠 = 关闭）/ 换主题 / 关闭；吸附开关即点即存，WebUI 同步
- 系统托盘常驻图标：双击打开完整面板，右键菜单「打开完整面板 / 退出」（依赖 `pillow` + `pystray`，缺库时小窗照常运行，仅无托盘）
- 提醒到期时显示提醒卡片，可「完成」或固定推迟 `snooze_minutes`

### 提醒

- 按 `remind_default_minutes` 提前提醒；全天日程按 `all_day_remind_time` 提醒
- 提醒卡片在面板与小窗同步呈现；小窗未运行时可自动拉起（`auto_show_window_on_remind`），提醒不丢失

### AI 自然语言（增删查改）

注册 4 个 LLM 工具，任何会话均可操作共享日历：

| 工具 | 作用 | 关键参数 |
| --- | --- | --- |
| `list_events` | **查**：按日期范围 / 关键词列日程，返回 id | `from_date` `to_date` `query` |
| `create_event` | **增**：创建日程 | `title` `start`（必填）、`end` `all_day` `repeat` `remind_minutes` `note` `color` |
| `update_event` | **改**：按 id 修改 | `id`（必填）+ 任意要改的字段 |
| `delete_event` | **删**：按 id 删除 | `id`（必填） |

写操作遵循「问齐缺失字段 → 复述摘要 → 等用户确认 → 再执行」协议；`on_llm_request` 会注入当前时间与使用指引。

## 安装

1. 将本目录放入 `data/plugins/astrbot_plugin_calendar/`（或通过 WebUI 插件管理安装）
2. 安装依赖（WebUI 安装时会自动执行）：

   ```bash
   pip install -r requirements.txt
   ```

   依赖四项：`lunar_python`（农历 / 节气推算）、`chinese-calendar`（休 / 班安排）、`pillow` + `pystray`（系统托盘图标）。缺库时对应功能自动降级为空，插件不会报错退出。

3. 在 WebUI 重载插件

## 配置

在 WebUI 插件设置中修改（`_conf_schema.json`）：

| 键 | 说明 | 默认 |
| --- | --- | --- |
| `enable_ai_tools` | 注册 create/list/update/delete 四个 AI 工具并注入提示词 | `true` |
| `timezone` | IANA 时区，用于当前时间注入与提醒计算 | `Asia/Shanghai` |
| `day_start_hour` | 日视图时间轴起始小时（0/4/6/8） | `6` |
| `remind_default_minutes` | AI 创建未说明时的默认提前提醒分钟 | `15` |
| `all_day_remind_time` | 全天日程的默认提醒时刻 | `09:00` |
| `auto_show_window_on_remind` | 提醒到期且小窗未运行时自动拉起小窗 | `true` |
| `window_width_ratio` | 小窗宽度占屏幕百分比（8-18） | `12` |
| `window_snap` | 开启后拖动松手自动磁吸到最近屏幕边（点击不触发）；关闭则自由放置，约 2 秒内热生效 | `true` |
| `window_glass` | 小窗液态玻璃半透明效果（约 2 秒内热生效） | `true` |
| `snooze_minutes` | 提醒卡片固定推迟分钟数 | `5` |
| `holiday_override_path` | 休 / 班兜底 JSON 路径（库未覆盖的年份用） | 空 |
| `demo_on_start` | 数据为空时自动填入示例日程 | `true` |
| `webui_base_url` | WebUI 根地址（小窗「回面板」使用） | `http://127.0.0.1:6185` |

## 面板入口

- WebUI 侧边栏 → 插件页面 →「日历日程」
- 直达地址：`http://127.0.0.1:6185/#/plugin-page/astrbot_plugin_calendar/calendar`（按实际端口）

显示模式（面板 / 桌面小窗）通过面板顶栏 ⧉ 按钮切换，状态持久化在 `state.json`。

## HTTP API

面板前端经 `window.AstrBotPlugin` 桥接调用，路由由 AstrBot 插件扩展端点承载（`/api/plugins/extensions/<路由>`）：

| 方法 | 路由 | 说明 |
| --- | --- | --- |
| GET | `/astrbot_plugin_calendar/events` | 按 `from`/`to` 列日程，或 `q` 搜索 |
| POST | `/astrbot_plugin_calendar/events` | 创建日程 |
| POST | `/astrbot_plugin_calendar/events/update` | 修改日程 |
| POST | `/astrbot_plugin_calendar/events/delete` | 删除日程 |
| GET | `/astrbot_plugin_calendar/digest` | 当日摘要（小窗用，含 `key`/`total`） |
| GET | `/astrbot_plugin_calendar/reminders` | 待处理提醒卡片 |
| POST | `/astrbot_plugin_calendar/reminders` | 提醒卡片 done / snooze |
| GET | `/astrbot_plugin_calendar/display` | 显示模式状态 |
| POST | `/astrbot_plugin_calendar/display/mode` | 切换 `panel` / `window` |
| GET | `/astrbot_plugin_calendar/config` | 前端配置 |
| POST | `/astrbot_plugin_calendar/demo` | （重新）写入示例数据 |

桌面小窗走独立的本机通道（`127.0.0.1` 随机端口 + 运行期令牌）：`GET /state` 拉取摘要与配置，`POST /action` 回传 `pos`（位置）、`cycle_theme`、`toggle_snap`（吸附开关）、`done`/`snooze`（提醒卡）、`ev_done`/`ev_delete`（胶囊操作）、`quit`（可带 `panel` 同时回面板）。

## 数据与历法来源

- 数据全部存本地：插件目录下 `events.json`（日程）、`state.json`（提醒状态、显示模式、小窗位置）
- 农历 / 干支 / 节气 / 传统节日由 `lunar_python` 运行时推算，任意年份可用
- 休 / 班角标由 `chinese-calendar` 提供（上游收录国务院办公厅放假安排）；新公告已出而库未更新时，可写一个 `{"2026-10-01": "休"}` 形式的 JSON 指到 `holiday_override_path`，优先生效
- 无任何硬编码历法表；缺库仅对应显示为空并告警一次

## 目录结构

```
astrbot_plugin_calendar/
├── main.py               # 存储、Web API、LLM 工具、提醒调度、小窗进程管理
├── lunar.py              # 农历 / 节日 / 休班 封装（全部可降级）
├── recurrence.py         # 重复规则、时间解析、提醒时刻计算
├── pages/calendar/       # 面板前端（index.html / app.js / style.css）
├── window/calendar_window.py  # 桌面小窗（tkinter，独立子进程）
├── data/                 # t2i 模板等运行资源
├── _conf_schema.json     # 配置结构
├── requirements.txt
└── metadata.yaml
```

## 常见问题

- **改了配置没生效？** 面板 / 小窗类配置热生效或约 2 秒内生效；改动代码需在 WebUI 重载插件。
- **AI 没有日程工具？** 确认 `enable_ai_tools` 为 true 且 AstrBot 版本支持 FunctionTool SDK，然后重载插件。
- **农历 / 休班显示为空？** 依赖缺失或年份超出 `chinese-calendar` 覆盖范围，按上文安装依赖或配置兜底 JSON。
- **小窗打不开？** 小窗依赖 Windows 图形环境；面板与 AI 功能不受影响，可将显示模式保持为 `panel`。
