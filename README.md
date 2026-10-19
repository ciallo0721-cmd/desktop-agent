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
| `ask` / `wait` / `done` | 反问用户、等待、收尾 |

## 快速开始

```
双击 first.bat      # 第一次：问 API、装依赖、建 venv、启动
双击 start.bat      # 之后：直接进界面
start.bat -debug    # 带控制台，看报错
```

`first.bat` 会依次问：

1. 主对话模型 —— base_url / api_key / model
2. 识图模型 —— 可以不填，不填就只能命令行模式
3. 记忆总结模型 —— 不填复用主模型
4. 称呼

然后自检 Python、cmd、git、Git Bash、WSL，缺 git 会用 winget 装。

## 目录

```
agent.py            入口
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
  agent_core.py     动作解析、工具分发、多步循环
  ui_main.py        主窗口
  ui_settings.py    设置窗口
prompt/
  systemprompt.md   系统提示（可改）
  roleprompt.md     角色与工作方式（可改）
  memory.md         长期记忆（自动维护，也可手改）
  人设.md           人格（可改）
data/               配置与对话存档（不进 git）
workspace/          agent 的默认工作目录
```

## 提示词顺序

```
systemprompt.md → memory.md → 是否新对话 → 人设.md → roleprompt.md
→ 底线（硬编码）→ 当前环境 → 工具协议（硬编码）
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

## License

MIT
