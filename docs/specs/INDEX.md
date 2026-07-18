# docs/specs 索引

`docs/specs/` 是这个 gem5 fork 的设计/问题记录目录，按 `S-NNN-slug.md`
编号，编号**按目录统一分配**（不分项目），新主题接着往后取号，不回填、
不重排已有编号。

> 跨 S-001..S-011 尚未解决/尚未验证/需要用户拍板的条目，汇总在
> [OPEN-ISSUES.md](./OPEN-ISSUES.md)，不要在这里重复维护。

## 项目：并行 EventQueue + 共享 L2/L3 无锁加速（parallel-EventQueue）

**目标**：把 gem5 单一的 `EventQueue` 按缓存/一致性域拆分成多个
（每核私有 L1/L2 一个域 + 共享 LLC/目录一个域），各跑在自己的宿主线程上，
用 quantum 屏障同步——沿用 parti-gem5 的思路（放宽跨域时序，不是无锁结构）。
目标是在保持时序精度（或者精确量化过、经过深思熟虑的放宽）的前提下，
拿到相对现有单 `EventQueue` 串行仿真器的真实 wall-clock 加速。这是本 fork
当前的主要研究目标，详见 `CLAUDE.md` 的"Primary research goal"一节。

**现状（截至 S-009）**：S-007 的"里程碑"是**并行屏障模式之间**的加速
（spin 比 cv 快 1.41x），**不是相对串行的加速**——S-008 首次把串行臂补进
FS 定窗 A/B，测出在当前工作点（Q=300/ll=20）并行**比串行慢 ~3x**
（0.33x）。缺口算术投影：单靠抬高 Q（出路 1，S-004 §9.3）大约只能追到
~0.92x，接近打平但到不了 1x，与 SE 侧"0.90x 封顶"的既有结论一致；要拿到
项目目标的真实加速，出路 1 和出路 3（每域塞更多工作）大概率要一起做。
S-009 把"抬高 Q"这一步的设计做完了（**未实现**）：精确定位到卡住 Q=300 的
是 `PacketQueue::schedSendEvent`（`RubyPort.cc` 的 PIO 转发和每一个经典
PIO 设备共享的同一个调度入口）里跨域调用时用错线程 curTick() 的问题，不是
IOXBar 的参数；设计是在这一个共享点套用 S-006 §11.5 已经验证过的
grid-anchored snap，一次改动覆盖所有调用点（§19）。审计还确认了一个和 Q
大小无关、独立的真实问题——这条经典 Port 路径（IOXBar 的
`reqLayers`/`respLayers`/`routeTo`、下游设备状态）完全没做跨域线程安全
保护，多核并发 PIO 会产生真实数据竞争（不是理论风险，§18），修起来和
S-002/S-003 给 Ruby 加锁的工作量相当。§23 逐设备排查发现竞争其实有**两种
形状**：核域线程 vs 核域线程（§18.2 原有的），以及新发现的核域线程 vs
**域 0 自己的 EventQueue 线程**（PIT/RTC/IDE 磁盘各自的周期性自调度事件，
不需要两个核同时做 PIO 就会触发）——后者推翻了"只在 RubyPort 跨域入口
加一把锁"的简化方案。找到两个通用加锁落点（`BaseXBar`、`PioDevice`），
外加 PIT/RTC 各一处、IDE 一条 controller↔disk 共享锁路径的手工接线（IDE
这条工作量最大、最不确定）。**§24 实现并验证了两个通用点**：TSan
（本沙盒的限制已解除，§24.2）扩时长 A/B（MAX_TICKS=1.3e9）显示两把新锁
干净，四份 stats.txt 逐字节相同。**§25 把 PIT/RTC/IDE 的手工加锁也做完
了**：IDE 那条路径没有像 23.2 担心的那样需要新架构——`IdeController`
本来就是 `PioDevice`，复用同一把 `pioLock` 即可，只需注意 `IdeDisk` 的
6 个 DMA 状态机函数会互相同步调用，锁只能加在真正的异步入口
（`EventFunctionWrapper` lambda）而不是逐函数加，否则会自锁死；RTC 比
设计稿多锁了一个点（`RTCTickEvent::process()`，不只是 `RTCEvent::
process()`）。非 TSan 正确性 A/B + TSan 扩时长 A/B（同样 MAX_TICKS=1.3e9）
均干净，且这次 TSan 报告里已经不再出现 §24.5 提到的 `AddrRangeMap`
竞争（S-010 修复生效的交叉验证）。§25.2 额外记了一个环境不一致：这次
会话的沙盒 cgroup（`cpuset.cpus=0-53,56-91`）拿不到 CLAUDE.md 记录的
`isolcpus=54-55,92-111` 隔离核，本轮验证改用非隔离核，`hostSeconds`
数字仅供参考、不能当新基准——需要用户在容器/编排层核实这段隔离核的
分配。**§26（新会话）把 §19.2 的 grid-anchored snap 写进了
`PacketQueue::schedSendEvent`**；实现前按 §22 待办做的 grep 审计还发现
一个真实缺口：`BridgeBase`（`src/mem/bridge.cc`，`X86Board` 标准建的
`bridge`/`apicbridge` 都是这个类）用手写调度路径、完全绕开
`PacketQueue`，S-006 §11.6 已经实测记录过这条边会在 Q 抬过其 50ns 延迟
窗口时崩溃，19.3"一次改动覆盖所有调用点"这句结论对它不成立——用户当场
决定同一会话内也做，§26.5 给 `BridgeBase` 加了同构的
`crossDomainSnap()`（两处 `schedTimingReq`/`schedTimingResp` 的
`bridge.schedule()` 调用点)。**两处改动都完全未编译、未验证**——本轮
沙盒连 `scons`/`pip`/`clang-format` 都没有，比 §25.2 记的隔离核缺口更
严重，用户已知晓、会之后自己装；下一次有工具链的会话要把两处一起编译、
一起跑 §20 验证协议，不能只验证一处就当整体完成。

**§27（新会话）把 §20 的验证协议跑完了——S-009 的标题任务到这里功能上
完成**：这次沙盒的 `scons`/工具链缺口（§26 记的）和 25.2 记的隔离核缺口
**都不在了**（`python3-dev` 缺失导致的 `python-config` 报错装包后解决；
`isolcpus=54-55,92-111` 这次能从容器内直接拿到）——`packet_queue.cc`
（§19.2/§26.1）和 `bridge.{hh,cc}`（§26.5）两处改动一起编译进
`build/X86_MESI_Three_Level/gem5.opt`，按 §20 协议跑 Q=300（确认新代码
在旧安全值下惰性，1 轮）→ Q=1000（小步试探，2 轮）→ Q=6660（S-008
§15.4 的目标值，2 轮），三个台阶全部通过：`assert(when >=
getCurTick())` 全程不再触发，`simInsts` 与既有基线一致，每个台阶
serial/spin 的完整 `stats.txt` 两两逐字节相同。**Q=6660 实测加速比
~0.91x，和 S-008 §15.4 的 ~0.92x 投影几乎完全吻合**——这是这条投影第一次
被真实测量确认，不是估算。结论和 S-008/项目现状段第二句说的完全一致：
出路 1（抬高 Q）单独做只能到接近打平，够不到 >1x 的目标，下一步要做的是
出路 3（每域塞更多工作、摊薄固定同步成本），是 S-009 范围之外的新设计，
需要单独立项；`packet_queue.cc`/`bridge.{hh,cc}` 这两处改动的 TSan
扩时长验证（沿用 §24.5/§25.4 的方法）还没做，是可选的下一步，不阻塞
"S-009 标题任务已完成"这个结论（§27.5）。

**§24.5（历史）额外发现一个不在 S-009 范围内、量级可能更大的独立
竞争**——
`AddrRangeMap<AbstractMemory*,1>` 物理内存地址查找缓存（每核每次内存
访问都会命中，和 S-004 §9.8 的 X86 TLB 竞争同一个模式）——已拆分单独
立文并**做完**（S-010）：全仓库 8 处 `AddrRangeMap` 实例化审计额外发现
`BaseXBar::portMap`（S-009 §24.1 的 `layerLock` 没盖住的三个入口）也有
同一种竞争，容器内部加了 `cacheLock` 一次性覆盖两处，TSan 扩时长 A/B
（MAX_TICKS=1.3e9，serial×2+spin×2）干净，四份 stats.txt 逐字节相同，
两个原竞争在 76 次 TSan 报告里完全消失；热路径开销还没测。仍然
悬而未决、不阻塞当前结论的项：TLB 加锁后的热路径开销未测量、精确模式下
一个恒定的一 ruby 周期偏差未查明根因（S-006 §9.6 附近提到）。FS 完整 ROI
串行参考的 `simTicks` 曾经跑完过，但原始 `stats.txt` 已随一次宿主机重启从
`/tmp` 丢失，只剩交接文档里未核实的转述数字（S-008 §16）——如果需要拿这
几个数字做定量结论，应重新跑一次。**S-011（已实现+已验证）**记录了同一轮
TSan A/B 里第二多的报告类别——S-002 引入的 per-consumer 锁自己的 owner
记账字段无同步读写，跟 `_curTick` 那种"设计上接受的陈旧读"不是一回事，
论证了一个具体可能的误判窗口；确认了这个窗口可达（不是纯理论）——本 fork
实际 FS 拓扑给每个 controller 一个私有终生不变的 router/`Switch`、域→
宿主线程终生绑定（S-005），加上触发窗口的 SLICC 动作是普通 L3-miss 高频
路径，三者叠加意味着同一个跨域宿主线程会对同一个 consumer 反复加锁解锁，
是稳态常见模式而非边角情形；设计了一份压力测试用来实测触发概率（只设计），
但用户看过后决定不实现/不跑它；转而设计了修法（§8）：把 owner 字段从无
同步的 `std::thread::id` 换成 `std::atomic<EventQueue*>`（复用
`Consumer.cc` 已有的 `curEventQueue()` 域身份 idiom），论证了"原子对象的
单点一致性保证"本身就足以排除 §3 描述的误判窗口；备选的
`std::recursive_mutex` 方案标准保证正确但会引入 `UncontendedMutex` 专门
要避免的每次调用开销，与本 fork 性能目标冲突，不作首选。**用户明确要求
"go ahead and implement §8"后，本会话把修法实现、编译（非 TSan+TSan 两个
build）并验证**（§10）：TSan A/B 里 `Consumer::lock()`/`unlock()` 的报告
从 66 次/37-38 次降到 0；正确性验证发现原计划"serial vs spin 逐字节相同"
这个假设在这个窗口/build 组合下本来就不成立（跟这次修法无关，改动前后
分歧数字完全一样），改用"同一模式下改动前后对照"确认这次修法不改变任何
可观测行为。**副产品**：serial/spin 在 `MAX_TICKS=1.3e9` 非 TSan build 下
的既有分歧是一个新发现、独立于 S-011 的待办项，未解决，需要用户决定是否
单独立文调查。**S-012（设计，未实现）**把"出路 3 该往哪塞工作"这个悬而
未决的方向性问题，拆成了一份具体的关键路径插桩设计——三个可测的未知数
（谁最后到达 quantum 屏障、每线程墙钟时间构成、域间工作量是否不均衡）、
两类打点（屏障到达/离开 + 跨域锁慢路径等待时长）、一个复用 S-009 §22
已验证读取安全性的关联键（`simQuantumStart`）；设计完成但**一行代码都
没写**，落地实现需要用户先过一遍再决定（CLAUDE.md 的 checkpoint 约定）。
Step 1-3 已落地：Step 3 打开插桩跑了第一次真实窗口，发现域 1（核 0 私有
L1+L2）是压倒性的最后到达者（50.4%），远超直觉预期的域 5/6-7（共享
L3/目录）——但"为什么是域 1"本身留白，见 **S-013**。**S-013（新会话，
负面结果，未解决）**读代码定位到一个真实根因（`threads` 基准的数组
初始化+结果校验全程串行、落在主线程所在的核上，外加没有显式线程绑核），
写了一份三阶段全并行+显式绑核的新基准 `threads_balanced.cpp`，跑通了新的
classic NoCache Atomic 开机+存检查点（~5.7 小时，与 S-006 §11.1 同量级），
用 S-012 §13 同一套关键路径协议验证——**但没有达到均衡**：不均衡以几乎
同样的幅度持续存在，只是从域 1 换成了域 3（且时间序列显示这个偏斜是
逐渐"滚雪球"变大的，不是从头到尾恒定）。重跑同一检查点做确定性检验——
域 3 两次都赢（51.2%/52.6%，几乎同一个数字）——**排除了纯随机/混沌的
解释**，说明这是某种结构性、可复现的原因，但具体是什么还没定位（候选：
guest 调度器对某个 vCPU 的系统性偏好、或 `pthread_setaffinity_np` 的
返回值目前没检查、可能悄悄失败了）。需要用户决定下一步（给
`threads_balanced` 加诊断打印+重新开机验证绑核是否真的生效、换更大的
`chunk_size` 建新检查点、或者搁置四核均衡这个目标直接推进 S-012
Step 4）。**追问"窗口是不是太短"（用户提出）牵出一个更大的问题**：
`MAX_TICKS=2e8` 这个窗口只够跑 74588 条指令，大概率还在客户机线程启动
阶段，S-012/S-013 至今为止所有关键路径数据可能都没真正测到基准的并行
计算阶段；去掉 tick 上限重跑复现了一个和四核均衡无关、严重得多的问题——
**S-014**：`BaseXBar::Layer::occupyLayer` 有一个 S-009 grid-anchored
snap 没盖到的跨域调度崩溃点，两份检查点都在差不多的 tick 量级崩溃，
和 S-013 的基准改动无关；更要紧的是，**S-009 §27 验证 Q=6660/0.91x
加速比用的正是这同一个偏小窗口**，这次崩溃发生在窗口之外，说明 0.91x
这个项目现在最核心的结论从未在足够长/足够接近真实工作负载的窗口上
验证过——需要用户决定这件事的优先级和 0.91x 结论要不要先加一条限定
说明。

**S-015（同会话，S-014 修法验证过程中意外发现，负面/未解决）**：为确认
S-014 §8 的 `occupyLayer` 修法真的扛得住原来的崩溃点，用户选择"跑到明显
超过原崩溃点"（`MAX_TICKS=2e9`，约为原崩溃点 646M tick 的 3 倍）——串行臂
全程干净；并行/spin 臂跑了 4 次完全相同的命令，**1 次崩溃、3 次干净跑完**
（其中一次干净的还是特意开了 `--debug-flags`/`--debug-start` 想在崩溃点
附近抓现场，结果反而没崩）。崩溃的 assert 和位置都不一样——不是
`occupyLayer` 或 `EventQueue::schedule` 的 `assert(when >=
getCurTick())`，而是 `packet_queue.cc:219`
`PacketQueue::sendDeferredPacket()` 的 `assert(deferredPacketReady())`，
调用栈是 `BaseXBar::Layer<RequestPort,ResponsePort>::retryWaiting()`
（`respLayers`）直接调用 `PacketQueue::retry()`——这条路径完全不经过
`occupyLayer`,所以跟这次的修法无关,不是回归。更关键的区别：S-009/S-014
之前修的全部三处（`schedSendEvent`/`BridgeBase`/`occupyLayer`）都是
**确定性**的 quantum 网格算术问题（同检查点+同构建+同 quantum 每次都在
同一个 tick 崩)，这次 4 次里只崩 1 次,是非确定性的,更像是真实的数据竞争
而不是算术错误。读代码提出一个**未经验证的假设**——`PacketQueue`自己的
`transmitList`/`waitingOnRetry`可能从来没有得到过和`BaseXBar`的
`layerLock`/`PioDevice`的`pioLock`同等级别的跨域互斥保护,`retryWaiting`
在 domain 0 自己的线程上调用`retry()`,可能和同一个设备的 PIO
响应生成路径（可能经由跨域调用,在别的域的线程上执行）在同一个
`PacketQueue`上产生真实竞争——但这只是从代码形状推测出来的假设,没有
用 TSan 或任何工具验证过。未尝试修复,已按惯例独立立文（S-015)、不
往 S-014 里塞新主题；这是继续验证 S-014 §6 第 3 步（无上限窗口复现)
之前需要先解决的新阻塞项,同样需要用户先决定优先级和范围。

## Specs 列表

| # | 文件 | 状态 | 摘要 |
|---|---|---|---|
| S-001 | [S-001-design-background-and-proposal.md](./S-001-design-background-and-proposal.md) | 设计定稿 | 背景、提案复述、parti-gem5 论文对照（拓扑/锁模型/死锁风险/精度放宽）、A-H 开放问题确认、Throttle 死锁规避机制设计 |
| S-002 | [S-002-v1-per-consumer-lock.md](./S-002-v1-per-consumer-lock.md) | 已实现+验证 | v1：per-consumer 共享 wakeup mutex 实现，单队列下正确性验证，发现 ~15% 单线程开销 |
| S-003 | [S-003-first-multithreaded-ruby-crashes.md](./S-003-first-multithreaded-ruby-crashes.md) | 已修复 | gem5 首次多线程 Ruby 实测撞到的两次真实崩溃；根因链（同域快速路径绕过锁 → `Event::Scheduled` 时序窗口）；Consumer 自持状态机修复（8.7/8.8） |
| S-004 | [S-004-first-speedup-measurement-and-fixes.md](./S-004-first-speedup-measurement-and-fixes.md) | 追平串行；SE 路线在 9.9 停止 | 第一次真实加速比测量（125x 减速）；瓶颈算术分解；CPU 入域；Ruby functional 加锁；ASan 揪出的 3 个真实并发 bug；最终撞上客户机线程退出的 SE 模式天花板 |
| S-005 | [S-005-host-thread-cpu-affinity.md](./S-005-host-thread-cpu-affinity.md) | 已实现+A/B（指向性） | 宿主线程绑核（PDES 调度纪律），是自旋屏障的前置条件 |
| S-006 | [S-006-fs-mode-migration.md](./S-006-fs-mode-migration.md) | APIC 唤醒墙已修复 | 迁移到 FS 模式，跨层次检查点重放，APIC 中断跨域唤醒墙的根因（量子网格锚点 bug）与修复；2-level 首次尝试/域布线依据/Ruby 检查点与并行重放不兼容（§11.6-11.8，补记） |
| S-007 | [S-007-spin-barrier-and-milestone.md](./S-007-spin-barrier-and-milestone.md) | **里程碑达成** | 自旋/混合屏障设计与 SE+FS 双靶实测；FS pythonDump 跨线程墙（已修复）；项目阶段性结论；并行运行下 `SIGUSR1` 崩溃的操作提示（§14） |
| S-008 | [S-008-fs-serial-vs-parallel-current-position.md](./S-008-fs-serial-vs-parallel-current-position.md) | 测量完成；结论待 S-009 验证 | FS 定窗首次补齐串行臂：当前工作点并行比串行慢 ~3x（0.33x）；抬高 Q 的缺口算术投影只到 ~0.92x；完整 ROI 串行参考数字因宿主重启丢失、按未核实转述记录（§16） |
| S-009 | [S-009-raise-fs-quantum-past-iobus-edge-design.md](./S-009-raise-fs-quantum-past-iobus-edge-design.md) | **加锁范围已实现+TSan 验证；§19.2 snap + `BridgeBase` 同构修法已编译+功能/性能验证通过（§27）；实测 Q=6660 加速比 ~0.91x，与 S-008 §15.4 投影吻合；两处 snap 改动的 TSan 验证仍是可选待办；⚠️ **该 0.91x 验证用的窗口（`MAX_TICKS=2e8`）事后发现严重偏小（S-013 §7，只到 74588 条指令，还在客户机线程启动阶段），拉长窗口后在 `BaseXBar::Layer::occupyLayer` 撞上 grid-anchored snap 没盖到的第三处跨域调度崩溃点，两份检查点都复现，见 [S-014](./S-014-occupylayer-crossdomain-crash-beyond-tested-window.md)——0.91x 这个结论需要补一条"仅在未验证过的短窗口内成立"的说明** | 精确定位卡住 Q=300 的是 `PacketQueue::schedSendEvent`（`RubyPort.cc` 的 PIO 转发 + 每个经典 PIO 设备共享），不是 IOXBar 参数；设计在这一个公共点套用 S-006 §11.5 的 grid-anchored snap（§19，**仍未实现**，一次改动覆盖所有调用点）；审计**确认**这条经典 Port 路径存在真实的跨域数据竞争（§18），§23 逐设备排查发现竞争有两种形状（核 vs 核、核 vs 域0自己的线程），找到两个通用加锁落点（`BaseXBar`/`PioDevice`）+ PIT/RTC/IDE 的手工接线；两个通用点已实现+TSan 扩时长 A/B 干净（§24）；**§25 把 PIT/RTC/IDE 也做完**：`PioDevice` 加公开的 `getPioLock()` 访问器给非 `PioDevice` 调用者用，PIT/RTC 走可选的 `crossDomainLock` 参数（RTC 比设计稿多锁了 `RTCTickEvent::process()` 一个点），IDE 发现 `IdeController` 本来就是 `PioDevice`（经 `PciEndpoint→PciDevice→DmaDevice`），复用同一把锁即可、不需要新设计，只是 `IdeDisk` 的 6 个 DMA 状态机函数会互相同步调用，锁必须挂在 `EventFunctionWrapper` lambda 这一层而非逐函数、否则自锁死；非 TSan 正确性 A/B + TSan 扩时长 A/B（MAX_TICKS=1.3e9）均干净，且 §24.5 的 `AddrRangeMap` 竞争这次不再出现（S-010 修复生效的交叉验证）；§25.2 记录了一个环境不一致——本次会话沙盒的 cgroup 拿不到 CLAUDE.md 记录的 `isolcpus=54-55,92-111`，需要用户在容器/编排层核实 |
| S-010 | [S-010-addr-range-map-cache-race.md](./S-010-addr-range-map-cache-race.md) | **已实现+TSan 验证；性能 A/B 待做** | 起点是 S-009 §24.5 TSan A/B 报告数量最多的一类（1500+ 次）：`AddrRangeMap<AbstractMemory*,1>`（`PhysicalMemory::addrMap`）的 LRU 查找缓存在 `find()`/`addNewEntryToCache()` 间无锁跨域读写，和 S-004 §9.8 X86 TLB 竞争同一个模式；全仓库 8 处 `AddrRangeMap` 实例化排查（§7）额外发现 `BaseXBar::portMap` 也有同一种竞争、且被 S-009 §24.1 的 `layerLock` 漏保护了三个调用入口（§7.2）；修法是在 `AddrRangeMap` 内部加 `cacheLock`（容器级，不是逐调用点，§10），非 TSan 正确性 A/B + TSan 扩时长 A/B（Q=300 小窗口 + MAX_TICKS=1.3e9 serial×2/spin×2，§11）均干净——四份 stats.txt 逐字节相同，76 次 TSan 警告的 `#0` 帧全部落在已知背景噪声（`_curTick`/`Event::Flags`/`Consumer::lock`），无一落在 `AddrRangeMap`/`portMap`；仍未测热路径开销 |
| S-011 | [S-011-consumer-lock-owner-race-audit.md](./S-011-consumer-lock-owner-race-audit.md) | **已实现+已编译+TSan/正确性验证通过** | S-010 §11.2 TSan A/B 里第二多的报告类别（66 次）：`Consumer::lock()`/`unlock()`（S-002 引入的 per-consumer 可重入锁）自己的记账字段 `m_wakeup_mutex_owner` 无同步读写；跟同一轮报告里的 `_curTick`/`Event::Flags` 不是一回事——那两个是项目自己"放宽跨域时序"的设计取舍、陈旧读可容忍，这里读的是"我现在持锁吗"，需要精确答案；§5 确认了可达性（L3-miss 高频路径 + 每 controller 私有终生 router + 域→线程终生绑定三者叠加，是稳态常见路径非边角情形）；§6 设计了一份压力测试来实测触发概率，但§7 用户决定不实现/不跑它；**§8 设计了修法**：把 `m_wakeup_mutex_owner` 从无同步的 `std::thread::id` 换成 `std::atomic<EventQueue*>`（复用 `Consumer.cc:65/118` 已有的 `curEventQueue()` 域身份 idiom），论证原子对象的单点一致性保证本身就足以排除 §3.2 的误判窗口；**§10（用户明确要求"go ahead and implement §8"后本会话做的）把修法实现、编译（非 TSan + TSan 两个 build）并验证**：TSan A/B（`MAX_TICKS=1.3e9`，serial×2+spin×2）里 `Consumer::lock()`/`unlock()` 的报告从 66 次/37-38 次降到 **0**，spin 两轮各剩 5 次已知背景噪声（`_curTick`/`Flags`）；正确性验证改用"同一模式下改动前 vs 改动后"对照（而不是原计划的"serial vs spin"对照），因为验证过程中发现 serial 和 spin 在这个窗口下本来就不是逐字节相同（跟这次修法无关，改动前后这个分歧数字完全一样），这次修法本身在同一模式下不改变任何可观测行为（`stats.txt` 逐字节相同）；**副产品**：serial/spin 在 `MAX_TICKS=1.3e9` 非 TSan build 下的既有分歧（`simInsts` 926690 vs 926608）是一个新发现的、独立于 S-011 的待办项，如实记录在 §10.5，未解决，需要用户决定是否单独立文调查 |
| S-012 | [S-012-eventq-critical-path-instrumentation-design.md](./S-012-eventq-critical-path-instrumentation-design.md) | **用户确认按 5 步实现；Step 1-4 已落地（脚手架+屏障计时/事件计数+第一次真实打点窗口+锁等待打点），Step 5 未做；⚠️ §15（新会话）在长窗口（`MAX_TICKS=2e9`）下发现 `critPathFlush()` 段错误——`OutputDirectory::files` 是无锁 `std::map`，8 个域线程在同一 quantum 屏障后几乎同时并发调用 `simout.create()`，是未定义行为；根因已读代码确认，未修复、未复现调试，域 1-7 CSV 缺失的原因也未查明；此前所有插桩跑（本文 §13、S-013 §7）都只验证过 `MAX_TICKS=2e8` 短窗口，从未触发这条并发路径** | 出路 3（每域塞更多工作）立项前缺一份关键路径数据——把这个缺口拆成三个可测的未知数：哪个域最后到达 quantum 屏障（谁在关键路径上）、每个线程墙钟时间构成（处理事件 vs 等屏障 vs 阻塞在跨域锁上）、各域每 quantum 工作量是否不均衡；设计了两类打点（`Barrier::wait()` 到达/离开时刻 + `UncontendedMutex` 慢路径等锁时长，四个已知跨域锁 `layerLock`/`pioLock`/`cacheLock`/`m_wakeup_mutex` 打标签）加一个可选第三类（每域每 quantum 事件数，用来区分"真的多干活"和"卡在锁上")；关联键初稿误用了 `simQuantumStart`（它是运行期常量——S-009 §22 论证其跨线程读安全的根据恰恰是"不变"，按它分组会把整个运行归成一组），评审后修正为各域自己队列在打点处的本地 `curTick()`（同一 quantum 边界各域相同、纯线程本地读，§3.4）；域号通过线程入口的 `thread_local` 变量落地（`simulate.cc` 的 `thread_main`），不做每次打点查表；关闭时对已验证的快路径行为无改动（`UncontendedMutex` 多一次可预测分支），打开时是独立诊断跑、不产出可比性能数字；**Step 3（§13）第一次打开插桩跑真实窗口**：域 1（核 0 私有 L1+L2）是压倒性最后到达者（50.4%），推翻了"共享 L3/目录域大概率更重"的原始直觉；"为什么是域 1"留白，由 **S-013** 接手 |
| S-013 | [S-013-balanced-four-core-workload-checkpoint.md](./S-013-balanced-four-core-workload-checkpoint.md) | **负面结果——新检查点已建+已验证，但四核并未变均衡；未解决，待用户决策；§10（新会话，`s013-balanced-four-core-workload-checkpoint` 分支）在 S-014/S-015/S-016 合并后尝试重跑 §9 被挡住的长窗口校验，仿真本身跑到了 `MAX_TICKS=2e9`（`occupyLayer` 崩溃没有再出现），但退出阶段撞上了 S-012 自己插桩基础设施里一个新的、无关的段错误——详见 [S-012 §15](./S-012-eventq-critical-path-instrumentation-design.md#15-新发现的问题未修复长窗口下-critpathflush-段错误outputdirectory-非线程安全)；S-013 本身"更长窗口下四核是否依然不均衡"这个问题依然没有答案，现在挡路的是这个新问题** | S-012 §13.5 留白的"为什么域 1 是关键路径"：读 `threads.cpp` 源码定位到一个真实根因——基准的数组初始化+结果校验是全串行的（40 万元素操作），全部落在主线程所在的核上，且从无显式线程绑核；写了 `threads_balanced.cpp`（三阶段全并行+每线程显式绑 vCPU，`chunk_size`/`num_values` 参数不变以控制变量），原生沙箱内验证正确性；新增 `x86_fs_classic_save_ckpt_balanced.py`，跑通新检查点（~5.7 小时，同 S-006 §11.1 量级）；用 S-012 §13 同一套关键路径协议验证——**不均衡以几乎同样的幅度持续存在，只是从域 1 换成了域 3**（时间序列显示是逐渐"滚雪球"变大，不是恒定），推翻了"平摊三阶段负载就能均衡"的假设；重跑同一检查点做确定性检验，域 3 两次都赢（51.2%/52.6%），**排除了纯随机/混沌解释，指向某种未定位的结构性原因**；验证过程中还发现并妥善处理了一个待决问题——工作树里有一份未提交、形状与 S-012 Step 4（锁打标签）一致的改动，用路径级 `git stash` 隔离出干净的 Step-3 build 做对照，验证完后已还原 WIP；**§9 追问"窗口是不是太短"发现 `MAX_TICKS=2e8` 只够跑 74588 条指令、大概率还在客户机线程启动阶段**，拉长窗口（去掉 tick 上限）复现了一个和本文主题无关但严重得多的问题，见 **S-014** |
| S-014 | [S-014-occupylayer-crossdomain-crash-beyond-tested-window.md](./S-014-occupylayer-crossdomain-crash-beyond-tested-window.md) | **已复现+已定位根因+已修复+已确认（§10）：`crossDomainSnap()` 修法已合并进 `main`；挡路的 S-015 已修复+合并（`--no-ff`，`1bacbb0a7c`）；§6 第 3 步（长窗口确认）用 S-015 §11.3 同一构建上的 20 次 `MAX_TICKS=2e9` 批次复用为确认证据（20/20 干净，两种崩溃都没再出现），没有重跑——S-015 的修法基于 S-014 自己的修法之上，同一份 build 已经同时验证了两者；剩余待办：§6 第 4 步（0.91x 结论要不要加限定说明）和 `CoherentXBar` 调用点审计（当前拓扑下是死代码，优先级低）** | 把 S-013 的检查点跑到无 tick 上限（到客户机自己的 `m5 exit` 为止）撞上 `assert(when >= getCurTick())` 崩溃，栈顶在 `BaseXBar::Layer::occupyLayer`（`NoncoherentXBar::recvTimingReq` 调用）；换回原始检查点（`x86-threads3-roi-classic`）同样崩在几乎同一个 tick 量级——**和 S-013 的基准改动无关，是 Q=6660 这个工作点本身在长窗口下的问题**；读源码确认根因——`occupyLayer` 直接调 `xbar.schedule(releaseEvent, until)`，`until` 由跨域调用方的 `clockEdge()` 算出，跟 S-009 §19.2 修的 `PacketQueue::schedSendEvent`是同一类跨域调度竞争，但 `occupyLayer` 这个调用点从来没套 `crossDomainSnap()`——是 grid-anchored snap 覆盖范围里第三个被漏掉的点（继 `BridgeBase`，S-009 §26.5，之后又一个）；**§7（新会话，只读审计）**用本项目实际拓扑（`MESIThreeLevelCacheHierarchy`，只建 `IOXBar`/`NoncoherentXBar`，`CoherentXBar` 全系在这个 fork 里从未被实例化）逐条梳理了 `occupyLayer` 的四条调用链——`recvTimingReq→succeededTiming`（`reqLayers`，跨域，即崩溃现场本身）和 `recvTimingReq→failedTiming`（同一 `reqLayers`，同一跨域调用栈的失败分支，共享一模一样的隐患、只是这次没被触发）需要修；`recvTimingResp→succeededTiming` 和 `recvRetry→retryWaiting`（都在 `respLayers`/走 mem_side）在当前拓扑下因为 `iobus.mem_side_ports` 挂的全是域 0 自己的设备而域内安全，但这只是这份配置的巧合、不是 `xbar.cc` 结构上的保证；审计末尾留了一个未决的设计取舍给下一步——只补两个当前可达的调用点（精确但可能重蹈 S-009 两次漏补同一类洞的覆辙）还是把 snap 直接包进 `occupyLayer` 本身（一次性覆盖全部四条链和未审计的 `CoherentXBar`，代价是给不需要的两条路径也加一次冗余 snap）；**S-009 §27 验证 0.91x 用的正是同一个偏小的 `MAX_TICKS=2e8` 窗口**，这次崩溃发生在窗口之外，意味着 0.91x 这个结论从未在真正跑得够长/够接近真实工作负载的窗口上验证过；**§8（同会话，用户明确要求"把修法包进 occupyLayer 本身，覆盖全部四条链"后做的）实现了修法**：新增 `Layer<SrcType, DstType>::crossDomainSnap()`（`xbar.hh`/`xbar.cc`），跟 `BridgeBase::crossDomainSnap()`/`PacketQueue::schedSendEvent` 同一套 grid-anchored 手法，`occupyLayer()` 调度前统一套一遍——一次性覆盖全部四条调用链（§7 表格）+ 未审计的 `CoherentXBar`，代价是给当前拓扑下本不需要的两条路径也加一次无害的冗余检查；`build/X86_MESI_Three_Level/gem5.opt` 编译干净（`taskset -c 0-53,56-91`，避开 CLAUDE.md 记录的隔离核）；照搬 S-009 §27 短窗口协议（同检查点、`SIM_QUANTUM_TICKS=6660`、`MAX_TICKS=2e8`）跑了一轮 serial/spin 回归对照，无 assert/abort/panic，`simInsts=74062` 与 S-009 §27 参考值一致，两份 `stats.txt`（排除 `host*`）逐字节相同——证明修法在已验证过的短窗口内是惰性的（符合预期，因为这个窗口本来就跑不到触发 snap 生效的条件）。**这次会话没有做的**：重新跑 §1 那个无 `MAX_TICKS` 上限的复现，去确认修法真的能撑过原来的崩溃点——这一步时长未知，按 CLAUDE.md 的 checkpoint 约定，属于该先跟用户核实范围/时长再启动、不该自行开跑的新阶段；`CoherentXBar` 调用点的审计/验证（§7 里记的、这个 fork 当前拓扑下的死代码）和 0.91x 结论要不要加限定说明（§6 第 4 步）也都还没做；**§9（同会话，用户选择"跑到明显超过原崩溃点"，`MAX_TICKS=2e9`）**：串行臂干净跑完；并行/spin 臂跑了 4 次完全相同的命令，原始的 `occupyLayer`/`assert(when >= getCurTick())` 崩溃一次都没有再出现——但 4 次里有 1 次撞上了一个完全不同的新 assert，详见 **S-015**（独立立文，不是这次修法的回归） |
| S-015 | [S-015-packetqueue-retry-race-beyond-occupylayer-fix.md](./S-015-packetqueue-retry-race-beyond-occupylayer-fix.md) | **已修复，已合并进 `main`（`--no-ff`，`1bacbb0a7c`；分支 `s015-packetqueue-retry-race` 保留未删除）：§8 `pqLock` 混合方案单独实现后被 §10 的崩溃确认批次证伪（2/11 崩溃，同一 tick）；根因是跨对象的 `Layer`↔`PacketQueue` 顺序问题，纯对象锁修不了；§11 在混合方案之上加了"容忍伪重试"语义改动（不再 assert，改为重新调度未就绪的包）——这才是真正生效的修法，`MAX_TICKS=2e9` 批次 20/20 干净（对比修前 ~15-18%），4 次容忍事件全部落在历史崩溃 tick 上，20 次结果 outcome 完全一致（simInsts 相同）；§12 补跑的 TSan A/B（flat 启动方式，非 §7.4 的 wrapper 脚本）确认 §7 那四处竞争在 spin 臂上完全消失（两条全窗口跑清、零 race 报告），但两条 serial 臂在 TSan 下挂起（`futex_wait_queue`，无崩溃标记）——这次证明挂起和 wrapper 脚本无关（flat 方式一样挂），推翻了 §7.4 的主要猜测；**§13（2026-07-17，另一会话新发现）：这个挂起既不是 TSan 专属、也不是 S-015 引入的**——继续 bisect/排查后发现和 S-009 至 S-015 整条线改动、和检查点内容都无关，已独立立文 **[S-016](./S-016-unbounded-serial-run-hang.md)**，本文 §13 保留最初发现时的记录、不再在这里追加新内容。** 以下为历史记录：第一次修法（把 `layerLock` 套进 `releaseLayer()`）已实现+编译+验证——验证失败（验证批次头 8 次里 5 次照样崩，跟修之前同一个量级甚至更高）；**根因诊断不完整**：真正更直接、大概率是主因的漏洞是 `qport.hh` 里通用的 `recvRespRetry`/`recvReqRetry`——这是对端跨域线程可以直接调用的、完全没有任何锁、也无法感知 `layerLock` 存在的框架级代码；§1c 修法代码保留（无害但不够）；**根因已获 TSan 工具级确认（§7，两次独立复现在历史崩溃 tick `5305999323366` 上，TSan 直接报出 `PacketQueue::retry()`/`deferredPacketReady()` 等同一实例四处状态的跨域数据竞争，standalone 复现率 ~67%）**；修法设计已完整勾勒（§8：`pqLock` 叶子锁 + owner 线程 `sendEvent` 调度接力的**混合方案**，含锁序证明——纯锁不够，因为 `EventQueue::reschedule()` 断言禁止跨域调用）；**hybrid-vs-全量 defer 的设计决策已拍板（§9，2026-07-17，用户委托）：选 §8 混合方案，全量 5b defer 被否决为本 bug 的修法**（确认的竞争经由返回同步 bool 的 `recvTimingResp` 进入，不重设计流控协议就无法 defer，所以锁无论如何都需要；全量 defer 留作未来独立架构调查方向）；**实现尚未开始，未修复** | 验证 S-014 §8 修法时意外撞上的新问题：`packet_queue.cc:219` `PacketQueue::sendDeferredPacket()` 的 `assert(deferredPacketReady())`，调用栈 `BaseXBar::Layer<RequestPort,ResponsePort>::retryWaiting()`→`PacketQueue::retry()`，和 `occupyLayer` 完全不是同一段代码；关键区别——S-009/S-014 之前修的三处都是**确定性**的 quantum 网格算术问题（同配置每次崩在同一个 tick），这次同一条命令重复跑只有部分次数崩溃，是**非确定性**的，更像真实数据竞争而非算术错误；读代码提出一个**未验证的假设**——`PacketQueue` 自己的 `transmitList`/`waitingOnRetry` 可能从未获得过和 `BaseXBar` 的 `layerLock`/`PioDevice` 的 `pioLock` 同等级别的跨域互斥保护；**§1a（后续会话，16 次补测）**：又撞了 2 次，累计 3/20（~15%），足以判定不需要上 TSan 就能继续追——**新发现：3 次崩溃的 tick 全部落在同一个 `SIM_QUANTUM_TICKS`（6660）范围内、同一个约 91% 窗口进度点上**，其中一次的 assert 甚至换成了 `doSimLoop` 的 `"event scheduled in the past"`（不是 `PacketQueue` 那个 assert）——说明这大概率是**同一个根因在同一个确定性的模拟时间点上触发的竞争**，只是竞争结果依赖宿主线程调度的非确定性；**§1b（用户要求"试试窄范围 --debug-start"，同会话）**：在崩溃 tick 附近开一个窄的 `--debug-start` 追踪窗口跑了 8 次，撞了 4 次（50%，比裸跑的 15% 更高，说明加追踪开销反而让竞争更容易触发），4 次崩溃日志逐字节一致，**首次锁定具体热点对象**：`board.iobus.respLayer8` / `board.iobus.cpu_side_ports[8]-RespPacketQueue`，还发现了**第三种 assert 表现形式**（`eventq.cc:223` 的 `panic("event not found!")`，经 `PacketQueue::schedSendEvent` 内联的 `reschedule()` 触发）；**读代码找到一个具体、可验证的根因候选**：`layerLock`（S-009 §24 加的）只包住了 `NoncoherentXBar` 的三个同步入口（`recvTimingReq`/`recvTimingResp`/`recvReqRetry`），但 `Layer::releaseEvent` 的回调链（`releaseLayer`→`retryWaiting`→`occupyLayer`/`sendRetry`→对端 `PacketQueue::retry`）是直接由本域自己的 `EventQueue::serviceOne` 派发的，从来没有拿过 `layerLock`——而 `recvTimingReq` 恰恰可能在**另一个核心域的线程**上执行，`releaseEvent` 回调则永远在本域自己线程上执行，两者touch 同一份 `Layer`/`PacketQueue` 状态却没有互斥；这是和 S-009（`BridgeBase`）、S-014（`occupyLayer`）同一个模式的"漏保护点"，不是全新的机制问题；仍未经 TSan confirm，也还没写修法；需要用户先决定是否现在就按这个思路试着修（把 `layerLock` 也套到 `releaseEvent` 路径上）还是先上 TSan 求证|
| S-016 | [S-016-unbounded-serial-run-hang.md](./S-016-unbounded-serial-run-hang.md) | **已修复，已合并进 `main`**（`--no-ff`；分支 `s016-unbounded-serial-run-hang` 保留未删除）：根因（`NoncoherentXBar::recvReqRetry` 在 `layerLock` 临界区内调用 `retryWaiting()→sendRetry()`，同步回调到对端 `PacketQueue::sendDeferredPacket()`，绕回同一线程再次调用 `recvTimingReq()`，对同一把非重入 `UncontendedMutex` 二次加锁自锁死）已通过读代码定位；**§8**：把 S-015 的叶子锁模式套用到 `xbar.cc`/`xbar.hh`/`noncoherent_xbar.cc`（`layerLock` 收窄到各方法自己管理，只包住真正touch的状态，`retryWaiting`/`sendRetry` 及下游 `sendTiming` 调用链不再持锁）；构建干净；无上限串行验证跑（同一 checkpoint/命令）监控 20+ 分钟，`utime` 持续上涨到 13 万+ tick 仍在涨、`state=R`/`wchan=0`，日志已越过历史上每次卡死都停在的 `interrupts.cc:530` 行（验证跑本身已在合并前主动停止，未等它跑到自然结束）；有界（`MAX_TICKS=2e9`）parallel/spin 健全性跑约 53 秒干净跑完，`simInsts=1475264` 与项目既有的 serial 参考数字逐位相同，未见正确性回归；用于诊断的四个 bisection worktree（`bisect-*`）已在合并后清理 | 检查另一会话跑的 S-014/S-015 无上限 serial/spin A/B 时意外发现串行臂挂起（`utime` 冻结、`wchan=futex_wait_queue`、`stats.txt` 空，日志最后一行永远是 `interrupts.cc:530` 那条只触发一次的广播 IPI `hack_once`，后来确认是启动阶段巧合、非因果）；最初四次复现被读成"排除了 S-009 至 S-015 整条线的跨域锁改动"——**这个结论是错的**：用作"pre-S-009"对照的 `3a030687a6` 实际上是 `94a0365951`（`layerLock`/`pioLock`）和 `3f493c8838`（`AddrRangeMap` 锁）的**后代**（`git merge-base --is-ancestor` 确认），误判源于该提交的 commit message"docs: add pre-S-008 session handoff note"描述的是内容而非提交本身在历史中的位置；同一会话当场改正：在真正的锁之前的提交 `796f662040` 重新构建+跑，**不挂**（连续监控 10 分钟零卡顿）；再在 `94a0365951`（只有 `layerLock`+`pioLock`）构建+跑，**挂了**，同一信号，两次 stall 检查点确认；干净的二分法把根因锁定在 `layerLock`/`pioLock` 本身；随后逐行读 `94a0365951` 的 `NoncoherentXBar`/`BaseXBar::Layer`/`PacketQueue` 代码，找到上面摘要的完整重入链条；四个 bisection worktree（`bisect-a3e325a-serial-hang`、`bisect-3a03068-pre-s009-serial-hang`、`bisect-796f662-true-pre-s009-serial-hang`、`bisect-94a0365-layerlock-pio-only`）留存未清理 |

## 如何新增一份 spec

1. 取下一个未使用的编号（当前最大是 `S-015`，下一份是 `S-016`），**不要**
   回填或重排已有编号，哪怕新主题在逻辑上是某个旧编号的延续。
   **并行开发下先在 `main` 上占号**：编号是全局分配的，两个并行调查不能
   同时抢同一个 `S-NNN`。开分支前，先在 `main` 上给下面的表格加一行
   （状态填 `进行中`，附上分支名 `sNNN-slug`），提交——这一步在主干上把
   编号原子地占住，也让在途的调查可见。然后再
   `git worktree add /workspace/gem5-wt/sNNN-slug -b sNNN-slug main`。
   完整的分支/worktree/tmpfs 约定见
   [../decisions/0001-fork-branching-strategy.md](../decisions/0001-fork-branching-strategy.md)。
2. 命名 `S-NNN-slug.md`，独立成文——不要把新主题塞进某个已有 `S-NNN`
   文件的正文里，哪怕它是那份文档里某个未解决问题的直接后续（用交叉引用
   `S-NNN §X.Y` 链接过去即可，同 S-001..S-007 互相引用的方式）。
3. 写完后把占号时那一行的状态从 `进行中` 更新为最终状态——不要因为新增
   一份 spec 就去改写旧 `S-NNN` 文件的正文内容。这个表格**只增不改别人的
   行**：并行分支合回 `main` 时，这里几乎必然冲突，按"两行都保留"机械解决，
   第二个合并的人负责解冲突、不改写别人那一行（见
   [../decisions/0001-fork-branching-strategy.md](../decisions/0001-fork-branching-strategy.md)）。
4. 如果这份新 spec 明显解决/推进了上面"现状"段落提到的某个悬而未决项，
   顺手更新那一段（这是本索引里唯一预期会被频繁编辑的部分）。
