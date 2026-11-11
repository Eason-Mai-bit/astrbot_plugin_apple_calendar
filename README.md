# 日历日程（astrbot_plugin_calendar）

Apple Calendar 风格的日程日历面板 + 桌面右下角日程小窗 + 提醒卡片 + 自然语言增删查改日程，另内置倒计时、每日日记与白板便签三大页内模块，纯本地运行，内置农历 / 节气 / 休班角标数据。

- 版本：0.4.0
- 作者：Eason-Mai-bit
- 要求：AstrBot >= 4.5.7（AI 工具需 >= 4.5.1 的 FunctionTool SDK）
- 平台：面板全平台可用；桌面小窗面向 Windows（圆角 / 液态玻璃使用 Win32 API）

## 更新日志

### 0.4.0（2026-11-11）

本版本新增**白板便签**模块，日历加入**周视图**，AI 工具由 7 个扩充到 10 个。

- 🆕 **白板便签**：邮票齿孔形状贴纸（自选颜色的浅色版），大 / 小便签两种类型；小便签可挂到大便签下（卡内下划线行 + 完成按钮），也可独立存在
- 🆕 **白板交互**：便签自由拖动并固定坐标（拖到大便签上=挂靠、拖出=独立）；Ctrl+滚轮独立缩放每张便签
- 🆕 **便签字段**：可设日程（日期起止，可跨天、可省略到天/小时/分钟）、颜色、标签、备注；📅 在日历里点选起止，🕘 快速沿用已有时间
- 🆕 **便签工具栏**：标题/备注/#标签 搜索、标签 chip、日期高亮筛选（📅 点选起止，命中高亮、其余置灰）；双击标题行内快改；✎ 完整编辑；子行可拖动调序
- 🆕 **AI 工具 ×3**：`add_goal`（建便签）、`list_goals`（列便签与进度）、`update_goal`（改便签，需确认）
- 🆕 **周视图**：月视图双击某天进入；左上「‹」按 年 ← 月 ← 周 ← 日 分级返回；周视图内 ‹ › 翻周，点周头跳到当天日视图
- 🆕 **HTTP API ×6**：`/goals`（增 / 查 / 改 / 删 / 调序）、`/colors`
- 🔧 日历视图由 年 / 月 / 日 三种扩展为 **年 / 月 / 周 / 日 四种**
- 🔧 数据新增 `goals.json`（白板便签），原子写入（临时文件 + 替换）持久化

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

- 年 / 月 / **周** / 日四种视图，月视图统一「日期在上、日程在下」，超出单元格高度自动折叠为 `+N`
- **周视图**：月视图双击某天进入；左上「‹」按 年 ← 月 ← 周 ← 日 分级返回；周视图内 ‹ › 翻周，点周头跳到当天日视图
- 事件胶囊按颜色分组（blue / teal / indigo / slate），支持全天日程、跨天重复（每天 / 每周 / 每月），可标注「谁的日程」归属人并按颜色 / 归属人过滤（☰）
- 农历日期、干支、节气、传统节日自动显示；休 / 班角标来自 `chinese-calendar`
- 关键词搜索、点击日期进入日视图、今天快捷返回
- 打开 / 切换视图与弹窗均有 ≤200ms 的 opacity/transform 动画

### 倒计时（页内标签 ⏳）

- 网格方块展示「距离下一个待办还有多久」，每页 8 个，支持翻页与空位占位
- 单位（周 / 天 / 小时）在插件设置 `countdown_unit` 中切换，单单位取整
- 数据来自日程与全天提醒时刻；已过截止的显示「进行中」；点方块可完成 / 删除 / **跳到日历定位**

### 每日日记（页内标签 📝）

- 邮箱式布局：左列表（置顶 + 按月分组，滚动懒加载）+ 右阅读 / 编辑区
- `#标签` 自动从标题与正文提取；顶部搜索（标题/正文/标签）与标签 chip 多选过滤
- 一天多条、日期可改（默认今天）、置顶 / 编辑 / 删除（二次确认）、导出 Markdown / 文本
- 分栏视图右侧栏同步显示选中日期的日记（紫色系，与日程颜色区分）

### 白板便签（页内标签 🎯）

- 邮票齿孔形状贴纸（自选颜色的浅色版），大 / 小便签两种类型；**小便签可挂到大便签下**（卡内下划线行 + 完成按钮），也可独立存在
- 白板**自由拖动**并固定坐标（拖到大便签上=挂靠、拖出=独立）；**Ctrl+滚轮**独立缩放每张便签
- 每张便签可设**日程（日期起止，可跨天、可省略到天/小时/分钟）**、颜色、标签、备注；📅 在日历里点选起止，🕘 快速沿用已有时间
- 顶部工具栏：标题/备注/#标签 搜索、标签 chip、日期高亮筛选（📅 点选起止，命中高亮、其余置灰）
- 双击标题**行内快改**；✎ 打开完整编辑；子行可**拖动调序**
- 数据存 `goals.json`，无周期概念（时间全部挂在便签自身）

### 桌面小窗（右下角常驻）

- 置顶圆角卡片，液态玻璃（可关）、4 色主题循环、拖动松手自动磁吸到最近屏幕边（320ms 滑动动画，开关可关），位置自动记忆
- 居中大日期 + 当日日程胶囊（只统计当天未结束条目）；点选胶囊出现详情：完成 / 删除
- 顶栏手绘按钮：回面板、吸附开关、换主题、关闭；提醒卡片三键「完成 / 推迟 / 删除」
- 系统托盘常驻图标：双击打开完整面板，右键菜单「打开完整面板 / 退出」（依赖 `pillow` + `pystray`，缺库时小窗照常运行，仅无托盘）

### 提醒

- 按 `remind_default_minutes` 提前提醒；全天日程按 `all_day_remind_time` 提醒
- 提醒卡片在面板与小窗同步呈现；小窗未运行时可自动拉起（`auto_show_window_on_remind`），提醒不丢失

### AI 自然语言（增删查改）

注册 10 个 LLM 工具，任何会话均可操作共享日历、日记与便签：

| 工具 | 作用 | 关键参数 |
| --- | --- | --- |
| `list_events` | **查**：按日期范围 / 关键词列日程，返回 id | `from_date` `to_date` `query` |
| `create_event` | **增**：创建日程 | `title` `start`（必填）、`end` `all_day` `repeat` `remind_minutes` `note` `color` |
| `update_event` | **改**：按 id 修改 | `id`（必填）+ 任意要改的字段 |
| `delete_event` | **删**：按 id 删除 | `id`（必填） |
| `list_countdown` | 查最近待办倒计时 | `limit` |
| `add_diary` | 增：写日记（#标签自动识别，直接写入） | `title`（必填）、`content` `date` |
| `list_diary` | 查：检索日记 | `query` `tag` `date` `limit` |
| `add_goal` | 增：建便签（大 / 小，直接创建） | `title`（必填）、`parent` `tags` `start` `end` `color` `note` |
| `list_goals` | 查：列便签与进度 | `query` `tag` |
| `update_goal` | 改：修改便签（需确认） | `id`（必填）+ 任意字段 |

日程写操作遵循「问齐缺失字段 → 复述摘要 → 等用户确认 → 再执行」协议；`on_llm_request` 会注入当前时间与使用指引。

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
| `enable_ai_tools` | 注册日程 / 日记 / 便签 AI 工具并注入提示词 | `true` |
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
| `countdown_unit` | 倒计时页显示单位（周 / 天 / 小时，单单位取整） | `天` |

## 面板入口

- WebUI 侧边栏 → 插件页面 →「日历日程」
- 直达地址：`http://127.0.0.1:6185/#/plugin-page/astrbot_plugin_calendar/calendar`（按实际端口）
- 页内顶栏图标：⏳ 倒计时、📝 日记、🎯 白板便签（互斥切换，左上「‹」返回月历）

显示模式（面板 / 桌面小窗）通过面板顶栏 ⧉ 按钮切换，状态持久化在 `state.json`。

## HTTP API

面板前端经 `window.AstrBotPluginPage` 桥接调用，路由由 AstrBot 插件扩展端点承载（`/api/v1/plugins/extensions/<路由>`）：

| 方法 | 路由 | 说明 |
| --- | --- | --- |
| GET | `/astrbot_plugin_calendar/events` | 按 `from`/`to` 列日程，或 `q` 搜索 |
| POST | `/astrbot_plugin_calendar/events` | 创建日程 |
| POST | `/astrbot_plugin_calendar/events/update` | 修改日程 |
| POST | `/astrbot_plugin_calendar/events/delete` | 删除日程 |
| GET | `/astrbot_plugin_calendar/countdown` | 倒计时数据（剩余秒、单单位、进行中态） |
| GET | `/astrbot_plugin_calendar/diary` | 日记检索（`q` / `tag` / `date`） |
| POST | `/astrbot_plugin_calendar/diary` | 写入日记 |
| POST | `/astrbot_plugin_calendar/diary/update` | 修改日记（`id`） |
| POST | `/astrbot_plugin_calendar/diary/delete` | 删除日记（`id`） |
| GET | `/astrbot_plugin_calendar/diary/tags` | 日记标签与计数 |
| GET | `/astrbot_plugin_calendar/goals` | 便签列表（`q` / `tag` 检索，返回标签计数） |
| POST | `/astrbot_plugin_calendar/goals` | 创建便签（`title` 必填，`parent_id` 挂靠） |
| POST | `/astrbot_plugin_calendar/goals/update` | 修改便签（`id`，含 `start`/`end`/`tags`/`scale`/`x`/`y`） |
| POST | `/astrbot_plugin_calendar/goals/delete` | 删除便签（级联子便签） |
| POST | `/astrbot_plugin_calendar/goals/order` | 同组便签拖动调序（`parent_id` + `ordered_ids`） |
| GET | `/astrbot_plugin_calendar/digest` | 当日摘要（小窗用，含 `key`/`total`） |
| GET | `/astrbot_plugin_calendar/reminders` | 待处理提醒卡片 |
| POST | `/astrbot_plugin_calendar/reminders` | 提醒卡片 done / snooze |
| GET | `/astrbot_plugin_calendar/display` | 显示模式状态 |
| POST | `/astrbot_plugin_calendar/display/mode` | 切换 `panel` / `window` |
| GET | `/astrbot_plugin_calendar/config` | 前端配置 |
| POST | `/astrbot_plugin_calendar/demo` | （重新）写入示例数据 |
| POST | `/astrbot_plugin_calendar/colors` 等 | 自定义配色 / 面板背景 |

桌面小窗走独立的本机通道（`127.0.0.1` 随机端口 + 运行期令牌）：`GET /state` 拉取摘要与配置，`POST /action` 回传 `pos`（位置）、`cycle_theme`、`toggle_snap`（吸附开关）、`done`/`snooze`（提醒卡）、`ev_done`/`ev_delete`（胶囊操作）、`card_done`（完成提醒背后的任务）、`quit`（可带 `panel` 同时回面板）。

## 数据与历法来源

- 数据全部存本地：插件目录下 `events.json`（日程）、`diary.json`（日记）、`goals.json`（白板便签）、`state.json`（提醒状态、显示模式、小窗位置）
- 日记与便签均按原子写入（临时文件 + 替换）持久化，长期保留
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
├── pages/countdown/      # 倒计时独立页（与页内标签同接口）
├── window/calendar_window.py  # 桌面小窗（tkinter，独立子进程）
├── data/                 # t2i 模板等运行资源
├── _conf_schema.json     # 配置结构
├── requirements.txt
└── metadata.yaml
```

## 常见问题

- **改了配置没生效？** 面板 / 小窗类配置热生效或约 2 秒内生效；改动代码需在 WebUI 重载插件。
- **AI 没有工具？** 确认 `enable_ai_tools` 为 true 且 AstrBot 版本支持 FunctionTool SDK，然后重载插件。
- **农历 / 休班显示为空？** 依赖缺失或年份超出 `chinese-calendar` 覆盖范围，按上文安装依赖或配置兜底 JSON。
- **小窗打不开？** 小窗依赖 Windows 图形环境；面板与 AI 功能不受影响，可将显示模式保持为 `panel`。
- **#标签 没被识别？** `#` 前必须是空格或行首（如「今天 #学习」），词中的 `#`（如 C#）不会提取。
