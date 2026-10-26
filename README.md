# PawPilot

跑在自己 Windows 电脑上的桌面 Agent（原名 Desktop Agent）。能执行命令、截屏看图、点鼠标打字、联网搜索，还能接 Telegram、跑定时任务、进真沙箱。
界面用 customtkinter，模型走任何 OpenAI 兼容接口。

## 它和普通聊天框的区别

说的每一句关于"电脑上发生了什么"的话，都来自工具的真实返回，不是编的。

## 能力

| 工具 | 作用 |
|---|---|
| `bash` | 执行 shell 命令（Git Bash / cmd），可串联 |
| `screenshot` | 截全屏或指定区域，图片**直接**给多模态模型看 |
| `click` / `move` / `type` / `key` / `scroll` | 鼠标键盘操作，坐标按原图像素 |
| `copy_region` / `clipboard` | 区域截图入剪贴板、读写剪贴板文字 |
| `web_search` | 联网搜索（默认抓 360 / Bing，填 Key 可走 Tavily / Serper） |
| `web_fetch` | 打开网页抓正文纯文本 |
| `skill` / `skill_run` | 加载技能说明书、在技能目录里跑技能脚本 |
| `ask` / `wait` / `done` | 反问用户、等待、收尾 |

## 快速开始

```
双击 first.bat      # 第一次：问 API、装依赖、建 venv、启动
双击 start.bat      # 之后：直接进 GUI
start.bat -debug    # 带控制台，看报错
```

## 发图片

用户可以直接把图片发给 agent，模型会真的看到它（不是 OCR 转文字再传话）。

| 前端 | 怎么发 |
|---|---|
| **GUI** | 点「图片」按钮选文件（可多选）；或 `Ctrl+V` 直接粘截图；输入框上方会出现待发缩略图，点 × 撤掉 |
| **CLI** | `/img C:\path\pic.png` 附加；`/img` 单独敲 = 从剪贴板取；`/img -` 清空 |
| **TUI** | `Ctrl+G` 收剪贴板里的图；或输入 `/img C:\path\pic.png` |
| **Web** | 点「图片」选文件；或在输入框里直接 `Ctrl+V` 粘截图 |

带图消息可以只有图、不写字。

落盘在 `data/images/<对话id>/`，长边超过 2048 会等比缩小，带透明的存 PNG、其余存 JPEG(q88)，
单张原图上限 24MB。对话里只存相对路径，整个项目文件夹可以随便搬。
历史里只有**最近一条**带图消息会真的把原图发出去，更早的降级成「这里原本有图片，已经省略」——
否则聊上几轮，每次请求体都是十几 MB。

## Skill 技能包

预置「怎么做某件事」的说明书，模型用到才读，不占用日常上下文。

```
skills/
  file-tidy/              一个技能 = 一个文件夹
    SKILL.md              必需：元信息 + 正文
    scripts/tidy.py       可选：技能自带脚本，用 skill_run 跑
    references/by-type.md 可选：附件，正文里点名了才按需读
```

`SKILL.md` 长这样：

```
---
name: 文件整理
description: 一句话说清干什么 + 什么时候该用（这句进系统提示词）
when: 更具体的触发条件，可留空
enabled: true
---
正文：分步骤写怎么做，命令原样贴。
```

工作方式（渐进式披露）：

1. 系统提示词里只放 `id · 名字 · 一句描述`，**正文不进**。
2. 模型判断命中 → `{"tool": "skill", "name": "file-tidy"}` 把正文读进来。
3. 附件 → `{"tool": "skill", "name": "file-tidy", "file": "references/by-type.md"}`。
4. 跑脚本 → `{"tool": "skill_run", "name": "file-tidy", "cmd": "python scripts/tidy.py workspace"}`，
   cwd 自动钉在技能文件夹里，相对路径都相对它。

管理入口：

| 前端 | 在哪 |
|---|---|
| **GUI** | 左下角「🧩 技能」：逐个开关、新建模板、直接打开 SKILL.md |
| **Web** | 顶栏「技能」面板：勾选开关 |
| **CLI** | `/skill` 列表，`/skill on\|off <id>`，`/skill cat <id>` 看正文 |
| **配置文件** | `skills_enabled`（总开关）、`skills_disabled`（关闭名单） |

`skill_run` 和 `bash` 走同一套高危检测，`rm -r` / `format` / `shutdown` 照样弹窗确认。
技能只是说明书，**不是权限升级**。

仓里自带一个 `file-tidy` 当模板：默认 dry-run 只打印计划，加 `--apply` 才真挪文件。
自己加技能就照抄它的目录结构，`skills/README.md` 里有完整规矩。

## 四种前端

同一套对话、同一套工具，只是壳不一样。

| 模式 | 启动 | 说明 |
|---|---|---|
| **GUI** | `start.bat` 或 `python agent.py` | customtkinter 窗口，默认 |
| **CLI** | `start.bat cli` 或 `python agent.py cli` | 命令行 REPL，流式打字机 |
| **TUI** | `start.bat tui` 或 `python agent.py tui` | 命令行里的窗口，ASCII 边框 + 方向键 |
| **Web** | `start.bat web` 或 `python agent.py web` | 浏览器开 http://127.0.0.1:16800 |

### CLI 常用命令

```
/help  /new  /list  /use N  /del N  /stop  /mode bash|screen|both  /img 路径
/skill  /skill on|off|cat <id>  /clear  /exit
```

Ctrl+C 打断当前回复（不会退出程序）。

### TUI 按键

```
↑ ↓          在会话列表里选
← →          切换焦点（列表 / 输入框）
Enter        发送
PgUp PgDn    聊天区翻页
Ctrl+N       新对话
Ctrl+C       复制最后一条 AI 回复
Ctrl+V       粘贴（终端自己处理）
Ctrl+G       把剪贴板里的图片收进待发列表
/skill       列出技能；/skill on|off <id> 开关，/skill cat <id> 看正文
Ctrl+End     停止正在跑的这一轮（Ctrl+X 也行）
Ctrl+Q       退出（连按两次）
```

TUI 只用 ANSI 转义 + `msvcrt`，不需要装 `windows-curses`。

### Web

只用标准库 `http.server`，**不依赖 Flask**。SSE 长连接推流，
只监听 `127.0.0.1`，不对外网开放。会话列表、切换、新建、高危确认都能用。

`first.bat` 会依次问：

1. 主对话模型 —— base_url / api_key / model
2. 识图模型 —— 可以不填，不填就只能命令行模式
3. 记忆总结模型 —— 不填复用主模型
4. 称呼

然后自检 Python、cmd、git、Git Bash、WSL，缺 git 会用 winget 装。

## 目录

```
agent.py            入口（认 cli / tui / web 参数）
setup_env.py        首装向导 + 环境自检
first.bat           首装 / 重配
start.bat           日常启动
app/
  config.py         路径、配置读写、开机自启
  prompts.py        提示词拼装
  llm.py            OpenAI 兼容的流式 / 一次性调用
  tools.py          bash、截屏、鼠标键盘、剪贴板
  websearch.py      联网搜索与网页抓取
  memory.py         长期记忆读写
  images.py         用户发的图片：校验、缩放、落盘、转 data URI
  skills.py         技能扫描、frontmatter 解析、按需加载、附件越界拦截
  sandbox.py        Windows Sandbox 真隔离执行通道（.wsb 生成 + 输出回传）
  scheduler.py      定时任务调度 + 任务管理 API（data/schedule.json，改完即生效）
  channel_telegram.py  Telegram 远程通道（长轮询 + 白名单）
  market.py         技能市场：注册表 / 一键安装 / sha256 签名校验
  notify.py         通知推送（Windows 系统通知 + TG）
  usage.py          token 花费统计（data/usage.jsonl + Web 面板汇总）
  agent_core.py     动作解析、工具分发、FC 双轨主循环
  session.py        四种前端共用的会话服务
  ui_main.py        GUI 主窗口
  ui_settings.py    GUI 设置窗口
  ui_skills.py      GUI 技能管理窗口
  ui_cli.py         CLI 模式
  ui_tui.py         TUI 模式
  ui_web.py         Web 模式（标准库 HTTP + SSE）
prompt/
  systemprompt.md   系统提示（可改）
  roleprompt.md     角色与工作方式（可改）
  memory.md         长期记忆（自动维护，也可手改）
  人设.md           人格（可改）
skills/             技能包（见上文，自带 file-tidy 示例）
data/               配置与对话存档（不进 git）
workspace/          agent 的默认工作目录
```

## 提示词顺序

```
systemprompt.md → memory.md → 是否新对话 → 人设.md → roleprompt.md
→ 底线（硬编码）→ 当前环境 → 图片说明 → 技能清单（有启用的技能才出现）
→ 工具协议（硬编码）
```

用户消息统一包成 `userinput:内容`。

## 工具协议

不用 function calling 也能用，任何 OpenAI 兼容接口都能用。模型输出一个代码块：

````
```action
{"tool": "bash", "cmd": "ls -la"}
```
````

**双轨协议**（`native_tools`，默认 `auto`）：请求同时带 OpenAI tools 定义，
模型会原生 function calling 就走 `tool_calls`（一轮可执行多个调用，参数是结构化的，不再靠猜）；
只会吐文本就自动退回上面的 ```action 解析。接口不支持 tools 参数会自动拉黑并降级，
不用改配置。设 `off` 强制只用文本轨。

解析层同时兼容 DeepSeek 的 DSML 语法（`invoke name=` / `parameter name=`）作为兜底，
所以模型就算不守规矩也照样能动。

### 推理模型注意（deepseek-flash / reasoner 这类）

- **思考 token 也算进 `max_tokens`**。给太小（比如 100）会全部烧在 `reasoning_content` 上，
  `content` 是空串、`finish_reason=length`。`chat_once` 遇到这种会自动把额度乘 4 重试一次，
  还空就抛明确错误，不再静默返回空字符串。
- **原生 FC 轨正文常常是空的**，只有 `tool_calls`。主循环判定「空回复失败」时会先看有没有
  tool_calls，有就照常执行，不会误报「模型返回了空内容」。
- 实测 deepseek-flash：流式正常、原生 FC 正常、接口回传完整 usage（含 `reasoning_tokens`），
  统计面板因此能拿到精确 token 数，不再是字符估算。附带代价是 prompt 不复用——每轮都全量
  重发系统提示，一步 6k 上下的 prompt token。

## Agent 自建定时任务 / 技能市场

两个新工具：

- `schedule`：让 agent 自己管理定时任务（list / add / remove / toggle）。
  直接说「每天早上八点半搜新闻发我」，它就会建任务。
- `market`：技能市场注册表在 `data/market.json`，条目带 `source`（SKILL.md raw 链接 /
  .zip / git 仓库）和可选 `sha256` 签名。`{"tool":"market","op":"install","id":"..."}`
  一键安装；登记了 sha256 的安装时自动校验，对不上拒装——防供应链投毒。
  只装注册表里有的来源，模型报的 URL 一律不认。

## 通知 / 统计 / 截图缓存

- 定时任务跑完自动推送：Windows 右下角系统通知 + Telegram（`notify` 配置可分别开关）。
- token 花费统计：每次请求记一行到 `data/usage.jsonl`（字符数估算，非精确计费），
  Web 界面右上角「统计」面板看今天 / 7 天 / 按模型。
- 截图缓存：画面一个字节没变就复用上一张文件，并且**不再重复发同一张图给模型**
  （一屏截图上万 token，重复发纯烧钱）。

## 安全

- 高危命令（`rm -r`、`format`、`shutdown`、`diskpart`…）不直接执行，先弹窗让用户点头，
  拒绝后把结果回传给模型让它换方案。
- 隔离分两档（配置 `sandbox_mode`）：
  - `off`（默认）：工作目录钉在 `workspace/` + 高危正则。快，但不是真隔离。
  - `wsb`：命令整体丢进 **Windows Sandbox** 虚拟机执行（见下节），真隔离。
    需要 Win10/11 专业版并启用「Windows 沙盒」功能；每条命令有 10~20s 启动开销，
    沙箱内用 CMD 语法，项目目录只读映射，可配置禁网。

## 真隔离（Windows Sandbox 模式）

`data/config.json` 里：

```json
{
  "sandbox_mode": "wsb",
  "sandbox_network": false
}
```

开启后所有 `bash` 工具的命令都会走沙箱：生成 `.wsb` → 启动 Windows Sandbox →
自动执行 → 输出回传 → 自动关闭。宿主机零接触（除了读写 `data/sandbox/` 交换目录）。
高危确认弹窗在进沙箱之前照常生效。

## 定时任务

任务列表在 `data/schedule.json`（改完保存即生效，不用重启），启动时自动加载：

```json
[
  {
    "id": "daily-brief",
    "name": "每日早报",
    "prompt": "联网搜今天的科技新闻，整理成 5 条摘要",
    "kind": "daily",
    "time": "08:30",
    "interval_min": 120,
    "enabled": true
  }
]
```

`kind` 填 `daily`（每天定点，用 `time`）或 `interval`（间隔循环，用 `interval_min`）。
到点 agent 会自己跑这个 prompt，结果追加写进 `data/schedule_log.jsonl`。
无人值守环境下高危命令没有弹窗确认，会直接拒绝——这是刻意的。

## Telegram 远程遥控

`data/config.json`：

```json
{
  "telegram": {
    "enabled": true,
    "bot_token": "@BotFather 建机器人拿到的 token",
    "allowed_users": [123456789],
    "proxy": "http://127.0.0.1:7890"
  }
}
```

只依赖 requests + 长轮询，不需要公网 IP。`allowed_users` 是硬门槛，名单外的人说什么都不理。
`/new` 新对话、`/stop` 停止、其他文字直接发给 agent。高危命令远程一律自动拒绝。

## Web 远程访问

默认只监听 `127.0.0.1`。想用手机/别的电脑访问，在 `data/config.json`：

```json
{
  "web_remote": true,
  "web_token": "编一个足够长的随机串"
}
```

然后 `start.bat web`，控制台会打印带 token 的局域网地址。没设 token 时远程模式拒绝启动，防止裸奔。

## 已知限制

- 截图只覆盖主屏之外的扩展桌面按虚拟桌面坐标，副屏偏移由程序换算。
- 推理型模型识图很费 token，建议单独配一个真正的视觉模型。
- Windows 专用。Linux / macOS 未适配。

## 配置字段

`data/config.json`，也可在界面「设置」里改。参考 `data/config.example.json`。

和技能相关的三个：

| 字段 | 默认 | 作用 |
|---|---|---|
| `skills_enabled` | `true` | 总开关，关掉后技能清单不进提示词，`skill` 工具也拒绝 |
| `skills_disabled` | `[]` | 被关掉的技能 id，覆盖 SKILL.md 里的 `enabled` |
| `skill_timeout` | `120` | `skill_run` 单次执行超时（秒） |
| `sandbox_mode` | `off` | `off` = 钉目录+高危拦截；`wsb` = Windows Sandbox 真隔离 |
| `sandbox_network` | `false` | wsb 模式里沙箱是否允许联网 |
| `web_remote` / `web_token` | `false` / 空 | Web 远程监听开关 + 访问令牌（远程必须配 token） |
| `telegram` | 关闭 | Telegram 通道配置（enabled / bot_token / allowed_users / proxy） |
| `scheduler_enabled` | `true` | 定时任务总开关，任务列表在 `data/schedule.json` |
| `native_tools` | `auto` | FC 双轨：`auto` 自动适配 / `on` 强制 / `off` 只用文本协议 |
| `notify` | 全开 | 任务完成通知（task_done / windows / telegram） |
| `temperature` | `0.7` | 采样温度 0~2，Web 界面「参数」面板可实时调 |
| `show_reasoning` | `true` | 显示推理模型的思考过程（`reasoning_content`） |
| `extra_params` | `{}` | 自定义请求参数，原样合并进 payload |

## 思考过程 / 自定义参数

**显示思考**：推理模型（deepseek-flash、各种 reasoner）会把思考单独放在
`reasoning_content` 里。开着 `show_reasoning` 时：

- Web：正文上方出现灰色可折叠块，流式滚动更新，收尾自动折叠成「思考过程 · N 字」
- GUI：聊天气泡里以「〈思考〉」开头，超过 800 字自动折叠
- TUI：一行跟随刷新的「思考 › …」（只显示尾部 400 字）

关掉就完全看不到思考，只留正文，界面干净也省一些传输。

**自定义温度**：Web「参数」面板有滑条（0~2，含 0/0.3/0.7/1.0/1.5 预设），
GUI 在「设置 → 模型参数」。改完立刻对下一条消息生效，不用重启。

**自定义参数**：填一个 JSON 对象，原样合并进 `/chat/completions` 的 payload：

```json
{"top_p": 0.9, "frequency_penalty": 0.2, "max_tokens": 4096}
```

`model` / `messages` / `stream` / `tools` 是协议保留字段，填了会被忽略；
`null` 值的键直接跳过。JSON 写错、或塞了保留字段，保存时会直接报错而不是静默失败。

## License

MIT
