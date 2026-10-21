# 日历日程（astrbot_plugin_calendar）

Apple Calendar 风格的日程日历面板 + 倒计时清单 + 日记本 + 桌面右下角日程小窗 + 提醒卡片 + 自然语言增删查改，纯本地运行，内置农历 / 节气 / 休班角标数据。

- 版本：**0.3.0**
- 作者：Eason-Mai-bit
- 要求：AstrBot >= 4.5.7（AI 工具需 >= 4.5.1 的 FunctionTool SDK）
- 平台：面板全平台可用；桌面小窗面向 Windows（圆角 / 液态玻璃使用 Win32 API）

## 更新日志

### 0.3.0（2026-10-21）

本版本新增**倒计时页**与**日记本**两大模块，AI 工具由 4 个扩充到 7 个。

- 🆕 **倒计时页**：未来待办按「距开始还有多久」排序，支持周 / 天 / 小时三种显示单位，卡片网格 + 分页浏览，点开可看详情并直接标记完成或删除
- 🆕 **日记本**：邮箱式三栏布局（列表 / 阅读 / 编辑），`#标签` 自动提取、按标签筛选、关键词搜索，支持导出 Markdown 与 TXT
- 🆕 **AI 工具 ×3**：`list_countdown`（查倒计时）、`add_diary`（写日记）、`list_diary`（查日记）
- 🆕 **配置项** `countdown_unit`：倒计时页显示单位（周 / 天 / 小时）
- 🆕 **HTTP API ×6**：`/countdown`、`/diary`（增 / 查 / 改 / 删）、`/diary/tags`
- 🔧 桌面小窗按钮改为中文文字（删除 / 推迟 / 完成），带鼠标悬停着色，布局自适应
- 🔧 面板顶栏新增「倒计时」「日记」两个入口按钮（标签互斥，不会与日历视图叠加）

### 0.2.5

- 重复日程支持 `repeat_until` 终止日期（AI 解析规则同步）
- 新增 `owner` 归属人字段：面板显示人员圆点，支持按人筛选，小窗同步
- 新增自定义主题色对话框，HSL 渲染同步到桌面小窗

### 0.2.4

- 过期事件过滤，snooze 状态豁免
- 小窗连接宽限期，避免启动瞬间误判离线
- 运行期数据加入 `.gitignore`

### 0.2.1

- 分栏视图下新增桌面风格「当日侧栏」
- 新增 `display` / `show` API

### 0.2.0

- 日历面板、桌面小窗、重复规则首轮功能更新

### 0.1.0

- 首个发布版本

## 功能

### 日历面板

- 年 / 月 / 日三种视图，月视图统一「日期在上、日程在下」，超出单元格高度自动折叠为 `+N`
- 事件胶囊按颜色分组（blue / teal / indigo / slate），支持全天日程、跨天重复（每天 / 每周 / 每月，可设终止日期）
- 农历日期、干支、节气、传统节日自动显示；休 / 班角标来自 `chinese-calendar`
- 关键词搜索、按日历显隐过滤（☰）、按归属人筛选、点击日期进入日视图、今天快捷返回
- 打开 / 切换视图与弹窗均有 ≤200ms 的 opacity/transform 动画

### 倒计时页（0.3.0 新增）

从面板顶栏「倒计时」按钮进入，是一个独立的页内标签视图。

- 列出**未来最近的未完成日程**，按距开始的时间由近到远排序
- 显示单位可配置：**周 / 天 / 小时**，单单位取整（如「剩 3 天」而非「3 天 7 小时」）
- 卡片网格布局 + 分页导航（`上一页` / `下一页` / `1 / N` 页码指示）
- 顶栏显示当前单位徽章，并给出下次刷新的提示
- 点开任意卡片弹出详情框：标题、时间、归属人、备注；底部可**直接标记完成**或**删除**
- 仅统计未完成项，已完成日程不会出现在倒计时列表中

### 日记本（0.3.0 新增）

从面板顶栏「日记」按钮进入，采用邮箱式三栏布局。

- **三栏结构**：左侧条目列表 → 中间阅读 → 右侧编辑，互不打断
- **标签系统**：在标题或正文里写 `#标签` 即自动成为标签，无需单独维护；标签栏显示各标签条目数，点击即可筛选
- **搜索**：关键词匹配标题与正文
- **导出**：单篇可导出 Markdown 或 TXT，带标题、日期与标签行
- **日期归属**：每篇日记归属到具体某一天，同一天可写多篇
- 数据与日程完全分离存放，互不影响

### 桌面小窗（右下角常驻）

- 置顶圆角卡片，液态玻璃（可关）、4 色主题循环、拖动松手自动磁吸到最近屏幕边（320ms 滑动动画，开关可关），位置自动记忆
- 居中大日期 + 当日日程胶囊；点选胶囊后出现操作按钮：**完成** / **推迟** / **删除**（0.3.0 起改为中文文字按钮，带悬停着色，删除仍需二次确认）
- 顶栏手绘按钮：左侧回面板，右侧吸附开关（磁铁图标，斜杠 = 关闭）/ 换主题 / 关闭；吸附开关即点即存，WebUI 同步
- 系统托盘常驻图标：双击打开完整面板，右键菜单「打开完整面板 / 退出」（依赖 `pillow` + `pystray`，缺库时小窗照常运行，仅无托盘）
- 提醒到期时显示提醒卡片，可「完成」或固定推迟 `snooze_minutes`

### 提醒

- 按 `remind_default_minutes` 提前提醒；全天日程按 `all_day_remind_time` 提醒
- 提醒卡片在面板与小窗同步呈现；小窗未运行时可自动拉起（`auto_show_window_on_remind`），提醒不丢失

### AI 自然语言（增删查改）

注册 **7 个** LLM 工具，任何会话均可操作共享日历与日记：

| 工具 | 作用 | 关键参数 |
| --- | --- | --- |
| `list_events` | **查日程**：按日期范围 / 关键词列出，返回 id | `from_date` `to_date` `query` |
| `create_event` | **增日程**：创建 | `title` `start`（必填）、`end` `all_day` `repeat` `repeat_until` `remind_minutes` `note` `color` `owner` |
| `update_event` | **改日程**：按 id 修改 | `id`（必填）+ 任意要改的字段 |
| `delete_event` | **删日程**：按 id 删除 | `id`（必填） |
| `list_countdown` | **查倒计时**（0.3.0）：列出最近的未完成日程及剩余时间 | `limit`（缺省 8，最大 20） |
| `add_diary` | **写日记**（0.3.0）：标题 + 正文，`#标签` 自动识别 | `title`（必填）、`content`、`date` |
| `list_diary` | **查日记**（0.3.0）：按关键词或标签检索 | `query` `tag` `date` `limit` |

写操作遵循「问齐缺失字段 → 复述摘要 → 等用户确认 → 再执行」协议；`on_llm_request` 会注入当前时间与使用指引。`list_countdown`、`add_diary` 等时间敏感工具会实时取当前时间，避免长会话中出现时间漂移。

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
| `enable_ai_tools` | 注册全部 7 个 AI 工具并注入提示词 | `true` |
| `timezone` | IANA 时区，用于当前时间注入与提醒计算 | `Asia/Shanghai` |
| `day_start_hour` | 日视图时间轴起始小时（0/4/6/8） | `6` |
| `remind_default_minutes` | AI 创建未说明时的默认提前提醒分钟 | `15` |
| `all_day_remind_time` | 全天日程的默认提醒时刻 | `09:00` |
| `auto_show_window_on_remind` | 提醒到期且小窗未运行时自动拉起小窗 | `true` |
| `window_width_ratio` | 小窗宽度占屏幕百分比（8-18） | `12` |
| `window_snap` | 开启后拖动松手自动磁吸到最近屏幕边（点击不触发）；关闭则自由放置，约 2 秒内热生效 | `true` |
| `window_glass` | 小窗液态玻璃半透明效果（约 2 秒内热生效） | `true` |
| `snooze_minutes` | 提醒卡片固定推迟分钟数 | `5` |
| **`countdown_unit`** | **倒计时页显示单位（周 / 天 / 小时，单单位取整）** | **`天`** |
| `holiday_override_path` | 休 / 班兜底 JSON 路径（库未覆盖的年份用） | 空 |
| `demo_on_start` | 数据为空时自动填入示例日程 | `true` |
| `webui_base_url` | WebUI 根地址（小窗「回面板」使用） | `http://127.0.0.1:6185` |

## 面板入口

- WebUI 侧边栏 → 插件页面 →「日历日程」
- 直达地址：`http://127.0.0.1:6185/#/plugin-page/astrbot_plugin_calendar/calendar`（按实际端口）

面板内三个视图由顶栏按钮切换：**日历** / **倒计时** / **日记**。倒计时与日记是标签互斥的页内视图，打开时会暂停日历重绘，关闭即恢复。

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
| **GET** | **`/astrbot_plugin_calendar/countdown`** | **倒计时列表（0.3.0）** |
| **GET** | **`/astrbot_plugin_calendar/diary`** | **查询日记（支持关键词 / 标签 / 日期）** |
| **POST** | **`/astrbot_plugin_calendar/diary`** | **新建日记** |
| **POST** | **`/astrbot_plugin_calendar/diary/update`** | **修改日记** |
| **POST** | **`/astrbot_plugin_calendar/diary/delete`** | **删除日记** |
| **GET** | **`/astrbot_plugin_calendar/diary/tags`** | **标签及各自条目数** |

桌面小窗走独立的本机通道（`127.0.0.1` 随机端口 + 运行期令牌）：`GET /state` 拉取摘要与配置，`POST /action` 回传 `pos`（位置）、`cycle_theme`、`toggle_snap`（吸附开关）、`done`/`snooze`（提醒卡）、`card_done`/`ev_done`/`ev_delete`（胶囊操作）、`quit`（可带 `panel` 同时回面板）。

## 数据与历法来源

- 数据全部存本地，插件目录下：
  - `events.json` — 日程
  - `state.json` — 提醒状态、显示模式、小窗位置
  - **`diary.json` — 日记（0.3.0 新增，与日程分离存放）**
- 农历 / 干支 / 节气 / 传统节日由 `lunar_python` 运行时推算，任意年份可用
- 休 / 班角标由 `chinese-calendar` 提供（上游收录国务院办公厅放假安排）；新公告已出而库未更新时，可写一个 `{"2026-10-01": "休"}` 形式的 JSON 指到 `holiday_override_path`，优先生效
- 无任何硬编码历法表；缺库仅对应显示为空并告警一次

## 目录结构

```
astrbot_plugin_calendar/
├── main.py                     # 存储、Web API、LLM 工具、提醒调度、小窗进程管理
├── lunar.py                    # 农历 / 节日 / 休班 封装（全部可降级）
├── recurrence.py               # 重复规则、时间解析、提醒时刻计算
├── pages/
│   ├── calendar/               # 日历面板前端（index.html / app.js / style.css）
│   └── countdown/              # 倒计时页资源（0.3.0 新增）
├── window/calendar_window.py   # 桌面小窗（tkinter，独立子进程）
├── data/                       # t2i 模板等运行资源
├── _conf_schema.json           # 配置结构
├── requirements.txt
└── metadata.yaml
```

## 常见问题

- **改了配置没生效？** 面板 / 小窗类配置热生效或约 2 秒内生效；改动代码需在 WebUI 重载插件。
- **AI 没有日程工具？** 确认 `enable_ai_tools` 为 true 且 AstrBot 版本支持 FunctionTool SDK，然后重载插件。
- **农历 / 休班显示为空？** 依赖缺失或年份超出 `chinese-calendar` 覆盖范围，按上文安装依赖或配置兜底 JSON。
- **小窗打不开？** 小窗依赖 Windows 图形环境；面板与 AI 功能不受影响，可将显示模式保持为 `panel`。
- **倒计时数字看着不对？** 检查 `countdown_unit`。它是**单单位取整**——选「周」时，不足一周的会显示为「不到 1 周」，不会给出「0 周」这种读数。想看得细一些就切到「天」或「小时」。
- **倒计时列表是空的？** 它只列**未完成且尚未结束**的日程：已标记完成的、结束时刻已过的都不会出现。反过来说，**正在进行中**的日程不会消失，会以「进行中」字样排在列表最前。
- **日记里的 `#标签` 没生效？** 标签需满足两点：① `#` 前是行首或空格（紧贴在文字中间不识别）② `#` 与标签文字之间不能有空格。标签在保存时自动从标题与正文提取，无需手动维护。

## 许可证

MIT
