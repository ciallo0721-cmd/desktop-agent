# skills/

技能包目录。每个子文件夹 = 一个技能，模型按需加载，不乱吃上下文。

```
skills/
  file-tidy/              示例技能，可以直接抄结构
    SKILL.md              必需
    scripts/tidy.py       技能自带的脚本，用 skill_run 调用
    references/by-type.md 附件，正文里写了才按需读
```

## SKILL.md 格式

```
---
name: 文件整理
description: 一句话说明干什么 + 什么时候该用（这句会进系统提示词）
when: 更具体的触发条件，可以留空
enabled: true
---
正文：分步骤写清楚怎么做，命令原样贴出来。
```

## 三条硬规矩

1. **`description` 决定模型会不会用这个技能。** 必须写清「干什么」和「什么时候用」，
   带上用户可能说的原话（「下载目录太乱」「按类型归类」）。写成广告词 = 永远不触发。
2. **正文只写模型猜不到的东西。** 通用常识别抄，抄了白烧 token。
   具体路径、命令、参数、踩过的坑，这些才值钱。
3. **写了的脚本必须真能跑。** 先用 `python scripts/xxx.py --help` 之类验证过再放进来。

## 模型怎么用它

1. 系统提示词里只有 `id · 名字 · 一句描述`，正文不进。
2. 模型自己判断命中，输出 `{"tool": "skill", "name": "file-tidy"}` 把说明书读进来。
3. 附件：`{"tool": "skill", "name": "file-tidy", "file": "references/by-type.md"}`。
4. 脚本：`{"tool": "skill_run", "name": "file-tidy", "cmd": "python scripts/tidy.py workspace"}`，
   工作目录自动钉在技能文件夹里，所以命令里的相对路径都相对这里。

## 开关

- 界面：GUI 左下角「🧩 技能」，Web 顶栏「技能」，CLI 敲 `/skill`。
- 存储：`data/config.json` 里的 `skills_enabled`（总开关）和 `skills_disabled`（名单）。
- 关掉的技能不会出现在系统提示词里，模型看不见它，也就不会去调。

## 安全

`skill_run` 跟 `bash` 走同一套高危检测，`rm -r`、`format`、`shutdown` 这类照样弹窗问用户。
技能不是权限升级，只是「说明书」。
