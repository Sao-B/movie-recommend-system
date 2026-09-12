# CLAUDE.md

> **本项目开发说明的唯一事实来源是 [`AGENTS.md`](./AGENTS.md)，请直接阅读该文件。**

本文件仅作为 Claude Code 的兼容入口保留（Claude Code 会自动加载 `CLAUDE.md`），**不再重复维护正文**。

> 历史原因：此前 `CLAUDE.md` 与 `AGENTS.md` 是两份几乎完全相同的副本（9380 B vs 9387 B，全文仅差 3 处工具名替换），导致同一份提示词被注入两遍，白白消耗约 4k–6k tokens 上下文。现已收敛为单一事实来源。

---

## Claude 环境独有补充

以下内容仅在 Claude 环境生效，因此不放进 `AGENTS.md`：

1. **技能目录**：Claude Code 读 `.claude/skills/`，DSH / Codex 读 `.agents/skills/`。
   两者已合并为**同一份物理副本** —— `.claude/skills` 是指向 `.agents/skills` 的目录联接（junction），内容自动同步，无需分别维护。
2. **权限配置**：`.claude/settings.local.json`（本机专用，不入 git）。
3. **上下文忽略清单**：见 `AGENTS.md` 的「上下文忽略清单」章节，完整清单在 `.aiignore`。

---

## 快速导航

| 需要什么 | 去哪里 |
|------|--------|
| 技术栈、目录结构、数据库表、ALS 参数、常用命令 | `AGENTS.md` |
| 项目说明与快速启动 | `README.md` |
| 全局配置（含凭据，不入 git） | `config.yaml` |
| 建表 DDL | `sql/schema.sql` |
