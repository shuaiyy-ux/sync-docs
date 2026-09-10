---
name: kb
description: |
  跨项目知识库（@KB_HOME@/）的唯一入口。三件事：检索、重建索引、把项目接进来。
  简写 /kb。模式在读完本文后选，不用在调用前猜。

  **检索**——默认动作，占九成。描述性查找走这里：过往教训、类似 bug、跨项目模式。
  写新功能 / 改契约 / 改 UI / 调试陌生子系统前主动调，不用等用户开口。
  用户说「之前怎么处理的」「有没有类似的」「have we done this before」时调。
  对 registry.md 做描述性 grep 返回 0 条时升级到这里，不要直接回「没找到」。

  **重建索引**——用户说「同步文档 / 重建知识库 / 更新知识库 / sync docs / rescan」时调；
  某项目写完成规模的文档、需要传播到中央索引时也调。全量重建，不便宜，单项目文档小改别调。
  这是新知识进入检索索引的唯一途径。

  **接项目**——新项目起步、或老项目的 CLAUDE.md 写于 KB 之前；
  用户说「我这个项目接一下 KB」「set up KB for this project」「更新一下 KB 协议」时调。
  只改项目自己的文件，先出 diff 再改，不静默写入。

  不适用：精确 identifier / 文件名 / 路径片段 / 错误原文——用 grep 查 registry.md,
  更快且确定。例：`start.sh`、`DispatchQueue.main.asyncAfter`、`api_health_bad`。
---

# kb · 跨项目知识库

KB 位置 `@KB_HOME@/`,脚本在 `@SKILL_HOME@/scripts/`。

## 先选模式

| 用户要什么 | 模式 | 读哪份正文 |
|---|---|---|
| 找过往经验、类似 bug、跨项目模式 | **检索** | `references/search.md` |
| 把新写的文档送进索引、重建 KB | **重建** | `references/rebuild.md` |
| 让某个项目能用上 KB | **接项目** | `references/wire-project.md` |

选定后**完整读那一份**再动手，里面是完整流程，本文只做分流。

## 三条贯穿规则

**1 · 精确串用 grep,别用语义检索。**
知道字面量（文件名、符号名、错误原文）就直接：

```bash
grep "<literal>" @KB_HOME@/registry.md @KB_HOME@/context.md
```

语义检索是给「只能用话描述的现象」用的。反过来也成立：描述性 grep 落空，要升级到检索模式，不许回「没找到」。

**2 · KB 是假设，代码是事实。**
任何一条 KB 结论在采纳前都要回原文件核对，并 grep 现有代码确认那个模式还在。
标了 `[unverified]`（超 180 天）或 `kind: project-specific`（而你在别的项目）的，默认不采纳。
KB 与代码打架，代码赢——把过期情况告诉用户，别闷头照旧建议办。

**3 · 经验往哪写。**
有归属项目的写进该项目自己的 `docs/`,再跑**重建**模式才进索引。
无归属的通用方法论写 `claude-knowledge/policy/`（该目录不被扫描，靠 `@`-import 触达）。
**别手写进 `claude-knowledge/` 根目录**——那是生成物，写进去检索不到，下次重建还会被覆盖。

## 常见故障

| 症状 | 处置 |
|---|---|
| `kb-search.py` 报 no hashes.json | KB 还没建，先跑**重建**模式 |
| 首次检索卡 30 秒以上 | 在自举 `.venv` 并下载 ~80MB MiniLM 模型，属正常，跟用户说一声 |
| 检索无 `score > 0.15` 的命中 | 如实报「KB 对 <query> 没有强匹配」，可建议换个说法重试 |
| 在 `claude-knowledge/` 目录里跑**接项目** | 拒绝——这里是模板本身，没什么可接的 |
