# 0005 — 分支/worktree 命名收敛为 `sNNN-<word>`（≤16 字符）

- 状态：已采纳（2026-07-22）
- **修订** [0001 分支策略](./0001-fork-branching-strategy.md) 的「One branch per
  investigation」命名条款（`sNNN-slug`，与 spec 文件名一致）。0001 的其余部分不变；
  按 0001 自己定的规矩，ADR 不改写正文，用新记录取代。

## 1. 背景

0001 定的规则是**分支名照抄 spec 文件名的 slug**，理由是让两者一眼对应。实践下来这个
对应关系的收益远小于它的代价——现有分支名：

| 分支 | 长度 |
|---|---|
| `s017-balanced-bounded-three-arm-hostseconds` | 43 |
| `s019-avgstor-crossdomain-tick-underflow` | 39 |
| `s018-o3-cpu-restore-balanced-checkpoint` | 39 |
| `s020-numa-node-offset-serial-arm`（占号时拟定） | 32 |

这些名字每天要出现在 `git worktree add`、`cd /workspace/gem5-wt/<branch>`、
`ln -s /workspace/shm/gem5/<branch>/build`、`git merge --no-ff <branch>`、以及每一份
交接文档里，还要在会话之间靠人复述。太长导致的不是美观问题，是**引用摩擦**：命令行里
要么反复 tab 补全、要么复制粘贴，口头/文字引用时几乎必然被截断成「s019 那个分支」——
既然实际引用时用的就是编号，那名字里那一长串 slug 就没有在承担识别功能。

## 2. 决策

**分支名 = `sNNN-<word>`，总长 ≤16 字符。**

- `<word>`：**一个**小写 ASCII 单词，不含连字符或下划线（三位编号下即 ≤11 字符）。
- 该词必须取自对应 spec 的 slug（或其显而易见的缩写），使人看到分支名能对上 spec。
- worktree 路径 `/workspace/gem5-wt/sNNN-<word>/`，tmpfs
  `/workspace/shm/gem5/sNNN-<word>/build/`——与分支同名，这一点不变。

**spec 文件名不变**，继续用长的描述性 slug（`S-020-numa-node-offset-serial-arm.md`）。
两者不再逐字对应：**连接键是编号 `NNN`**。编号本来就是全局唯一的，而且所有交叉引用
（`S-017 §6`、`S-012 §19.4`）用的一直是编号，不是 slug——所以这次解耦并没有削弱任何
实际在用的对应关系。

示例：`s019-avgstor`（12）· `s020-numa`（9）· `s018-o3`（7）。

## 3. 备选方案

- **A：spec 文件名也一起缩短**（`S-020-numa.md`）。否决：`docs/specs/` 的文件列表是
  人浏览这批研究的主要入口，长 slug 在那里是**有用**的——它承担的是目录功能，不是引用
  功能。两者的最优长度本来就不同，强行统一是问题的来源而不是解法。
- **B：保持长名，靠补全/别名缓解**。否决：git 不缩写分支名；worktree 路径会原样出现在
  每一条 `git worktree list`、每一份 spec 和交接文档里，补全帮不到这些地方。
- **C：只用编号 `s019`**。否决：`git branch -a` / `git worktree list` 里一排纯编号完全
  不提示内容。一个词只花 11 个字符，换来的可读性是划算的。

## 4. 后果与已知代价

- **0001 里「分支名与 spec 文件名一致」这条不变式作废**。按 slug 全名 grep 分支的做法
  要改成按 `sNNN` grep。`sNNN-*` 这个 glob（0001 用于「push 活跃分支」「force-delete
  废弃分支」）**仍然有效**，不受影响。
- **不做机制强制，是有意的**。角色门可以在 `git worktree add`/`checkout -b` 上校验名字
  形状，但这棵树里合法存在着非 `sNNN` 的 worktree——`baseline-prefork-13462eed1b`
  （三臂的 Baseline 臂）和 S-016 用过的 `bisect-*` 四个二分树。一个形状门会把这些正当
  用法一并拦掉，需要的例外清单比它防住的错误更麻烦。本条靠 review 兜住。
- **≤16 是约定不是校验**，超一两个字符不会有任何东西报错。真正的约束是「一个词」——
  这条比字符数更容易自查。
- **已结题的分支不改名**（S-017、S-018 及更早）：它们的名字已经写进 `INDEX.md` 行、
  spec 正文和 `--no-ff` 合并提交的消息里，改名会让那些记录与仓库现状对不上。研究记录
  的一致性优先于命名整齐。

## 5. 何时应重新审视

- 若某个 S-NNN 实在找不出一个能概括它的词，导致命名反复纠结，说明「一个词」这条太紧，
  可放宽到「两个词、总长仍 ≤16」。
- 若将来非 `sNNN` 的 worktree 类型固定下来（例如 baseline 臂成为常设），可以给它们也
  定一个形状，届时机制强制才重新变得可行。
