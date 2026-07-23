# 0009 — Researcher 独写 spec；分支内交接改走 docs/worktree/

- 状态：已采纳（2026-07-23）
- 范围/Scope: 协议
- 相关：[0001 分支策略](./0001-fork-branching-strategy.md)（生命周期第 2 步被本记录修订）、
  [0005 短分支名](./0005-short-branch-names.md)、
  [0007 写权矩阵默认拒绝](./0007-writable-matrix-default-deny-and-scratch-outside-repo.md)
  （写权矩阵的具体角色归属被本记录修订）、
  `CLAUDE.md`「Research role workflow」、`docs/roles/**`、
  `.claude/hooks/role-gate.py`、`util/roles/use-role`

## 1. 背景

原分工里，Architect 在主树写每个 S-NNN 的 spec 初版（背景/研究点/设计/验收标准），
然后「交棒即止」——分支建好后不再碰该 spec，此后 spec 由 worktree 里的
Researcher/Experimenter/Implementor/Debugger 四个角色**共同追加**。两个后果：

1. **Architect 卡在每次调查的关键路径上**，而它的另一半职责（角色体系/协议本身）与具体
   调查无关。「交棒即止」这条硬规则存在的唯一理由，就是躲开同一 spec 文件在主树与分支
   双写、`--no-ff` 合并冲突。
2. **单一 spec 文件承载了全部跨角色沟通**：实验计划、实现变更记录、调试证据链、结果，
   全是它的「章节」。日久 spec 膨胀到难读（S-012 达 123 KB，`## 11 实现记录` …
   `## 19 有界重跑` 把设计、实现日志、调试根因、结果交织在一处）。设计与结论被过程淹没。

## 2. 决策

**(a) spec 起草与机制设计下放给 Researcher，Architect 退出调查关键路径。**
Architect 不再写任何 `docs/specs/S-*.md`，职权收窄为角色体系/协议优化、门与生成机制、
仓库级配置，以及**协议层面**的 ADR。Researcher 成为**分支内的架构师**：起草并**独占**本
Spec 的正本，做机制选型与研究设计，但仅限本 Spec 相关文档。

**(b) spec 正本单写者 = Researcher，内容收窄为「目标 + 最终结果 + 结论」。**
Experimenter/Implementor/Debugger 不再有 spec 写权。逐步过程移出 spec。

**(c) 分支内改走逐消费者交接文档，留痕在 `docs/worktree/sNNN-word/`：**
`experiment-*.md`（Researcher 出任务书、Experimenter 回填执行结果）、
`impl-*.md`（Researcher 出任务书、Implementor 回填变更记录）、
`debug-*.md`（撞见失败者起头、Debugger 续写证据链与修复）。门按**子树**授权给四个
worktree 角色，具体哪个文件归谁由各角色 PROTOCOL 散文划分——与 `docs/specs/` 一向
按分节划分同一模式。结题时 Researcher 把最终结果表 + 结论从这些文件蒸馏进 spec。

**(d) PI 获得 ADR 写权，用于研究方向决策。** ADR 分范围：Architect 写协议/机制 ADR，
PI 写研究方向 ADR，各在 ADR 内以 `范围/Scope:` 一行标明。门对 `docs/decisions/**` 在
主树同时对 pi/architect 开放，范围区分靠约定不靠门。

**(e) 生命周期简化**（修订 0001）：PI 占号 → PI **随即**建分支+worktree（中间不再有
Architect 环节）→ Researcher 起草 spec + 出交接文档 → 下游执行、回填交接文档 →
Researcher 蒸馏结论进 spec → PI 审计/合并。原 Architect 分支前的设计评审，由 Researcher
交接前的 Checkpoint 2 设计评审点承接。

三处可执行副本（`CLAUDE.md` 矩阵、`use-role`、`role-gate.py:MAIN_AREAS/WT_AREAS`）与
`test-role-gate.py` 同步更新，回归测试通过。

## 3. 否决的备选

- **保持 Architect 写 spec + 单文件交接**（现状）。否决：上面两个后果不解决——spec 持续
  膨胀，Architect 持续卡在关键路径。
- **拆交接文档，但门按文件名/子目录逐角色上锁**（`experiment-*.md` 只给 experimenter…）。
  否决：门复杂度与命名契约的维护成本，换来的隔离价值有限——`docs/specs/` 由多个角色按
  分节共写多年，从没靠门做分节级上锁，散文划分足够。本 ADR 要的是文档组织，不是门粒度。
- **spec 仍由多角色共写结果章节**（只把过程挪走）。否决：Researcher = 分支内架构师这一
  定位要求 spec 单写者；多写者会重新引入本要消除的协调面，且结论的综合需要一个统一的
  收束者。
- **PI 的研究方向决策记进 ROADMAP/OPEN-ISSUES 而非 ADR**。否决：ROADMAP 是活文档、
  OPEN-ISSUES 是未决索引，都不承载「已决、含否决备选、不再重写」的定向决策——这正是 ADR
  的形态。给 PI ADR 写权补上这个缺口。

## 4. 后果与已知代价

- **每次调查现在都以一次 Researcher 会话收束**：结果由 Experimenter 写进 experiment 文档，
  但 spec 的最终结果表 + 结论必须由 Researcher 蒸馏——多一次会话跳转。这是单写者模型的
  代价，已接受。
- **「结果与既有结论矛盾当场订正」的执行方式变了**：Experimenter/Debugger 不能直接改
  spec，只能在自己的交接文档里记清并路由 Researcher。同一会话内订正的是它有写权的交接
  文档；spec 的订正落在 Researcher 收束时。
- **PI 写权变宽了一点**（多了 `docs/decisions/**`）。与「PI 写权最窄」的老表述有张力，但
  研究方向 ADR 本就是「定方向」职责的一部分，是刻意的扩权。
- **`docs/worktree/` 是要合并进主干的研究记录**，不是 scratch：临时脚本与中间数据仍一律
  落 `/tmp/`（决策 0007 不变）。

## 5. 何时应重新审视

- 若 `docs/worktree/` 的散文划分被反复违反（角色写错文件），说明需要把门升级到逐文件
  上锁——那时重开 §3 第二个备选。
- 若 Researcher 的收束会话成为瓶颈，考虑让 Experimenter 直接写 spec 的结果表（§3 第三个
  备选的弱化版）。
- 若 PI 的研究方向 ADR 与 Architect 的协议 ADR 在实践中反复混淆，考虑给两者分号段或
  分目录。
