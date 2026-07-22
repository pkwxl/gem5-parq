# 0008 — AGENTS.md / QWEN.md 改为角色切换时生成的派生文件

- 状态：已采纳（2026-07-22）
- 相关：[0003 角色门三处裁定](./0003-role-gate-scons-merge-and-repo-config.md) §4、
  [0007 写权矩阵默认拒绝](./0007-writable-matrix-default-deny-and-scratch-outside-repo.md)、
  `util/roles/use-role`、`CLAUDE.md`「Research role workflow」

## 1. 背景 —— 手写的第二份指令文件必然漂移

仓库里有三份给 agent 看的指令文件：`CLAUDE.md`（正本，Claude Code 自动读）、
`AGENTS.md`（Codex / OpenCode 等自动读）、`QWEN.md`（Qwen Code 自动读）。后两份是
**人工摘写**的 `CLAUDE.md` 缩略版，各写各的。

它们已经漂了。2026-07-22 抽查 `QWEN.md`：

| 写的 | 实际 |
|---|---|
| `origin = pkwxl/gem5-gem5-parq` | `pkwxl/gem5-parq` |
| S-017「Spec drafted, not run」 | 早已跑完，real speedup ≈ 0.459x（ROADMAP 的基准数字） |
| 「Build uses: `0-53,56-91`」 | 硬编码保留核，**直接违反决策 0004**「核号只在 `util/roles/reserved-cores` 一处定义」 |
| 「Kill with SIGTERM/SIGINT only」 | 红线是「不发 SIGUSR1/2」，被改写成一条不同的指令 |

`AGENTS.md` 好一些，但同样是摘要，同样没有任何机制保证它跟得上 `CLAUDE.md`。

更根本的问题：**这两份文件里完全没有角色协议**。别的 CLI 不跑 `/load-protocol`，
拿不到 `docs/roles/<role>/PROTOCOL.md`，于是在那些会话里「一个会话一个角色」这套
东西根本不存在——它们看到的是一份缩水、过期、且不含角色边界的指令。

## 2. 决策

**`AGENTS.md` 由 `util/roles/use-role` 在每次角色切换时生成**，内容是
`CLAUDE.md` + `docs/roles/<当前角色>/PROTOCOL.md` 全文拼接，前面加一段
「生成物、勿手改、正本在哪」的抬头。**`QWEN.md` 是指向 `AGENTS.md` 的相对软链**。
两者都进 `.gitignore`，并从 index 里 `git rm --cached` 掉。

配套：

- 从 `use-role` 的 chmod 门里删掉 `gate_file AGENTS.md` / `gate_file QWEN.md`
  ——生成前 `rm -f`、生成后 `chmod a-w`，手改会失败，重新生成永远成功。
- `role-gate.py` 的 `MAIN_AREAS` 里两者的 owner 从 `{architect}` 改成 `set()`，
  并给专门的拒绝文案：改派生文件无效，正本是 `CLAUDE.md` 和角色协议。
- 文件名沿用**复数** `AGENTS.md`。`agents.md` 是 Codex / OpenCode 等会自动读取的
  生态约定名；单数 `AGENT.md` 没有任何工具认，凭空多一个文件。
- `.qwen/`（Qwen Code 的会话状态目录）不受影响，仍归 architect、仍 gitignore。

## 3. 为什么 QWEN.md 用软链，而不是删掉、也不是生成第二份

- **删掉**：最省事，但 Qwen Code 默认只读 `QWEN.md`，删了等于把这条工具链踢出
  角色体系——恰好与本记录要解决的问题相反。
- **生成两份真实文件**：两份内容可以各自失败、各自过期，正是 §1 要消灭的那种漂移。
- **软链**：`ls -l` 一眼看得出派生关系，内容不可能不一致，`rm -f` + `ln -sfn` 幂等。

代价：新克隆的仓库在第一次 `use-role` 之前，`QWEN.md` 是**悬空软链**（`AGENTS.md`
被 gitignore，不入库）。读它的工具会拿到 `ENOENT`。这可以接受——同样这份克隆里
`.active-role` 也不存在，角色体系本来就要求先跑一次 `use-role` 才算就位。

## 4. 一个必须一并处理的死锁

`use-role:63-69` 在**工作树不干净**时拒绝会话内角色切换。如果生成的
`AGENTS.md` 还是 git 跟踪文件，那么每一次 `use-role` 都会把工作树弄脏，
下一次 `use-role` 就会被自己刚制造的脏状态拒绝。所以「`git rm --cached` +
`.gitignore`」不是收尾的清洁工作，而是这个方案能不能成立的前提。

**过渡期**同样会踩到它：已存在的 `sNNN-*` 分支在 rebase 到 `main` 之前，
`AGENTS.md` 在那些树里仍受跟踪（`CLAUDE.md` 早已记过「未 rebase 的分支跑的是
陈旧的门和陈旧的 skill」）。因此生成函数先自查：

```bash
if git -C "$REPO_ROOT" ls-files --error-unmatch AGENTS.md >/dev/null 2>&1; then
    echo "警告：AGENTS.md 在本树仍受 git 跟踪（分支未 rebase 到 main），跳过生成。" >&2
    return 0
fi
```

跳过并出声，而不是弄脏工作树。

## 5. 后果与已知代价

- **红线在别的 CLI 里没有机制强制。** `role-gate.py` 是 Claude Code 的 `PreToolUse`
  钩子，Codex / Qwen Code 不会跑它。生成的 `AGENTS.md` 抬头因此必须**明说**这一点：
  文字齐全 ≠ 有人拦着。这是本方案最大的一处诚实边界——它解决的是「那些会话看不到
  规则」，不是「那些会话会被拦住」。
- 生成物体积约 `CLAUDE.md` + 协议 ≈ 400 行，比原来的手写摘要长。对只读一次的
  agent 来说这是好事（信息全），对上下文预算是成本。不做裁剪：任何裁剪逻辑都要
  维护，而维护漂移正是被淘汰的那个方案。
- `AGENTS.md` 不再入库，意味着 GitHub 上浏览这个 fork 的人看不到它。`CLAUDE.md`
  仍在库里且是正本，损失仅限于「少一份摘要」。
- 每次 `use-role` 多两次文件操作（`rm -f` + 拼接写入），毫秒量级，可忽略。

## 6. 何时应重新审视

- 若某个 CLI 开始支持读 `CLAUDE.md` 或支持指定上下文文件名，对应的生成物就该退役，
  而不是继续维护一份拼接。
- 若生成物长度成为真实困扰（例如某 CLI 有硬上下文上限而被截断），届时的正解是
  **拆分正本**（把 `CLAUDE.md` 里与角色无关的 gem5 通识独立成一节可选包含），
  而不是恢复手写摘要。
- 若出现第四个 agent CLI，按同样办法加一条软链；出现第三种**内容需求**（不只是
  换个文件名），才需要回头重审本记录。
