# 0003 — 角色门的三处裁定：experimenter 的构建权、merge/rebase 的按树判定、仓库级配置的归属

- 状态：已采纳（2026-07-22）
- 相关：[0001 分支策略](./0001-fork-branching-strategy.md)、`CLAUDE.md`
  「Research role workflow」、`.claude/hooks/role-gate.py`、`util/roles/use-role`

## 1. 背景

六角色工作流上线后的第一批真实会话（S-019）暴露了三处缺口。前两处是**下游可执行副本
与散文正本漂移**，第三处是**正本本身没写全**。三处都是在 S-019 的分支上被撞到的：
Experimenter 会话无法执行 E0（`docs/specs/S-019-*.md` §6 记录，按协议「只记不改」），
分支无法取得 `main` 上的更新，而三个改 `.qwen/**` 和 `.gitignore` 的提交则从一个
无人认领的洞里进了分支。

`CLAUDE.md` 已经写明：散文正本与 `use-role`/`role-gate.py` 冲突时**正本胜**。本记录
按这条原则逐一裁定，并同步三份下游副本。

## 2. 缺口 1 —— experimenter 的 `scons` 被误拒

**事实**：`role-gate.py` 对 `researcher` 与 `experimenter` 一律拒绝 `scons`。而
`CLAUDE.md` 角色表写 experimenter 的职责是「Execute a plan faithfully: **build**, run,
measure」，`docs/roles/experimenter/PROTOCOL.md` §0/§1/§2 三处预设它构建——§1 那条
「构建不得占用保留核」是专门写给这个角色的纪律，若它根本不能构建，该条即为空文。
hook 自身也不自洽：保留核检查（`check_discipline`）把 `experimenter` 单列为唯一可用
隔离核的角色，`scons -j` 的 deny 文案又建议 `taskset -c 0-53,56-91 scons -j80`，两者
都预设了它会跑那条被拒的命令。

**备选方案 A（否决）**：把构建改派 Implementor，即 spec 的实验计划里凡涉及建二进制
的步骤都要求先 `ROLE SWITCH: implementor`。否决理由：三臂方法论要求每次测量各自建
Baseline / Current Serial / Current Parallel 三个二进制，这会让一次普通的实验至少多
出两次角色切换，且切回来之后 Experimenter 仍要自己核对「二进制是不是预期 commit」
（其 §2 Phase A 第 2 条），责任被切碎而没有换来任何隔离收益——Implementor 的隔离价值
在于**改代码**与**设计/测量**分离，不在于谁敲 `scons`。

**决策**：`scons` 只对 `researcher` 拒绝。`experimenter`/`implementor`/`debugger` 放行，
仍受既有两条纪律约束（`-j` 必须绑核；不得占用 `54-55`/`92-111`）。researcher 保持不
构建，与其 PROTOCOL §4「绝不跑 gem5 实验或构建；只读探查不算实验」一致。

## 3. 缺口 2 —— 分支没有合法途径取得 `main` 的更新

**事实**：`PI_ONLY_GIT` 匹配 `git merge` 时不分工作树，而 `check_path` 又禁止跨工作树
写入、PI 只在主树工作。两条规则叠加的结果是：**没有任何角色能把 `main` 拉进一个
`sNNN` 分支**。`git rebase` 则完全不在管辖内——它当时能用，是漏网而不是设计。

这条规则的立法意图是生命周期第 5 步：「合并回主干」是 PI 的结题动作。把 `main` 拉进
分支方向相反，是分支卫生。两者被同一条正则不加区分地拦住，属于机制过宽。

**备选方案 B（否决）**：由 PI 代为同步。否决理由：PI 的工作树被红线限定为主树，跨树
写入又被 hook 拒绝——这条路在机制上根本不通，除非同时放宽两条更重要的红线。

**决策**：按树判定。

- 主树：`git merge`、`git rebase` 都是 `pi` 专属（改写的是研究主干）。
- worktree：`git merge`、`git rebase` 对该分支的四个角色放行。
- 造分支/worktree（`worktree add`、`checkout -b`、`switch -c`、`branch <new>`）在
  **任何**树上仍是 `pi` 专属，不受本条影响。

## 4. 缺口 3 —— 仓库级配置与 agent 指令文件无归属

**事实**：`check_path` 对未列入 `MAIN_AREAS`/`WT_AREAS` 的仓库内路径 fall-through
**放行**。仓库根实际跟踪着 `.gitignore`、`.pre-commit-config.yaml`、`.clang-format`、
`pyproject.toml`、`SConstruct`、`AGENTS.md`、`QWEN.md`、`.qwen/**`——全部无归属，任何树
的任何角色都能改。S-019 分支上三个与该 spec 主题无关的提交正是从这里进去的。

**备选方案 C（否决）**：把 fall-through 反转成默认 deny。否决理由：误杀面过大——
`docs/refs/**`（非 scripts 部分）、各类一次性工作文件都会被拦，且与 hook 自己
「护栏不是沙箱，假想敌是漂移和手滑」的定位冲突。默认 deny 的门需要一份穷举白名单才
好用，而这个仓库是上游 gem5 的 fork，白名单会持续与上游变动打架。

**决策**：显式列名，保留 fall-through 放行。

| 路径 | 主树 | worktree | 理由 |
|---|---|---|---|
| `AGENTS.md`、`QWEN.md`、`.qwen/**` | architect | 无人 | agent 指令文件与角色体系同源，同一 owner |
| `.gitignore`、`.pre-commit-config.yaml`、`.clang-format`、`pyproject.toml` | architect | 无人 | 仓库级配置不属于任何一次研究；在分支里改会随 `--no-ff` 把它带进主干 |
| `SConstruct` | 无人 | implementor | 构建系统是**代码**，不是仓库配置，与 `src/**` 同性质 |

配套内容决定（用户 2026-07-22 拍板）：`.qwen/**` 移出版本控制并加进 `.gitignore`
——它是另一个 agent 的本地会话状态（含 `welcome-back-state.json`），不是研究记录。

## 5. 后果与已知代价

- Experimenter 现在能自己建三臂，代价是「谁能启动构建」这条边界变宽了一格；构建仍
  受绑核纪律约束，而绑核纪律才是真正会作废别人数据的那条。
- worktree 里放开 `rebase` 意味着分支历史可被改写。`CLAUDE.md` 视逐步提交日志本身为
  研究记录，因此这条放宽只在「把 `main` 拉进分支」这个用途上是安全的；把一串研究提交
  压扁仍然违反「never squash」的约定，但**目前没有机制拦住 `git merge --squash`**
  ——本记录不扩大范围去补，如实记为遗留项。
- 显式列名的清单会随上游 gem5 新增根级配置文件而过时；漏列的新文件回落到 fall-through
  放行，是**静默**的宽松而不是报错。下次有人在根目录加配置文件时需要同步这张表。
- S-019 分支上那三个越界提交不因本记录而合法化。它们改的内容（`.qwen` 移出跟踪）已由
  主树按新归属重做，分支 rebase 到新 `main` 时应丢弃自己那三个。

## 6. 何时应重新审视

- 如果出现「Experimenter 为了让计划跑通而顺手改代码」的实例——那说明放开构建权确实
  松动了执行/实现的隔离，应回到备选 A。
- 如果某个分支被 `rebase` 弄丢了研究提交，应把 worktree 内的 `rebase` 收回、只留
  `merge`。
- 如果根级配置文件的漏列反复发生，说明白名单模式不成立，应重新考虑备选 C（默认 deny
  加穷举白名单）。
