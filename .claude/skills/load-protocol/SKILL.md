---
name: load-protocol
description: 会话起手加载当前研究角色及其完整 PROTOCOL.md。读 `.active-role` → Read `docs/roles/<role>/PROTOCOL.md` 全文 → 只输出角色名一行，进入 Phase A。每个新会话的第一步。
---

# load-protocol — 研究角色协议加载

每个新会话起手运行一次，把当前角色身份与完整协议带入上下文。

## 步骤

1. **确认工作树** — 当前工作目录属于主树 `/workspace/gem5` 还是某个
   worktree `/workspace/gem5-wt/<branch>/`。主树只跑 `pi`/`architect`，
   worktree 只跑 `researcher`/`experimenter`/`implementor`/`debugger`。

2. **读角色** — Read 工作树根的 `.active-role`（单行，角色名）。
   - 文件缺失或为空 → 输出：`没有激活角色。请先运行 util/roles/use-role <role>。`
     然后停止，不要自行推断角色。

3. **读协议** — Read `docs/roles/<role>/PROTOCOL.md` **全文**。
   - 文件缺失 → 输出：`找不到 docs/roles/<role>/PROTOCOL.md，请运行
     util/roles/use-role <role> 确认角色已配置。` 然后停止。

4. **宣告角色** — 只输出角色名一行（如 `researcher`），不复述职权、不分步
   播报，然后进入协议的 Phase A。

`.active-role` 是权限凭证，只能由 `util/roles/use-role` 写入。任何直接改写它
的做法等于自选角色，是协议违规。
