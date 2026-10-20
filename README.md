# Desktop Agent

跑在自己 Windows 电脑上的桌面 Agent。能执行命令、截屏看图、点鼠标打字、联网搜索。
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
  agent_core.py     动作解析、工具分发、多步循环
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

不用 function calling，任何 OpenAI 兼容接口都能用。模型输出一个代码块：

````
```action
{"tool": "bash", "cmd": "ls -la"}
```
````

解析层同时兼容 DeepSeek 的 DSML 语法（`invoke name=` / `parameter name=`）作为兜底，
所以模型就算不守规矩也照样能动。

## 安全

- 高危命令（`rm -r`、`format`、`shutdown`、`diskpart`…）不直接执行，先弹窗让用户点头，
  拒绝后把结果回传给模型让它换方案。
- 沙箱模式只是把工作目录钉在 `workspace/` 并匹配高危正则，**不是真隔离**。
  要真隔离请开虚拟机。

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

## License

MIT
