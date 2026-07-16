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
| S-009 | [S-009-raise-fs-quantum-past-iobus-edge-design.md](./S-009-raise-fs-quantum-past-iobus-edge-design.md) | **加锁范围已实现+TSan 验证；§19.2 snap + `BridgeBase` 同构修法已编译+功能/性能验证通过（§27）；实测 Q=6660 加速比 ~0.91x，与 S-008 §15.4 投影吻合；两处 snap 改动的 TSan 验证仍是可选待办** | 精确定位卡住 Q=300 的是 `PacketQueue::schedSendEvent`（`RubyPort.cc` 的 PIO 转发 + 每个经典 PIO 设备共享），不是 IOXBar 参数；设计在这一个公共点套用 S-006 §11.5 的 grid-anchored snap（§19，**仍未实现**，一次改动覆盖所有调用点）；审计**确认**这条经典 Port 路径存在真实的跨域数据竞争（§18），§23 逐设备排查发现竞争有两种形状（核 vs 核、核 vs 域0自己的线程），找到两个通用加锁落点（`BaseXBar`/`PioDevice`）+ PIT/RTC/IDE 的手工接线；两个通用点已实现+TSan 扩时长 A/B 干净（§24）；**§25 把 PIT/RTC/IDE 也做完**：`PioDevice` 加公开的 `getPioLock()` 访问器给非 `PioDevice` 调用者用，PIT/RTC 走可选的 `crossDomainLock` 参数（RTC 比设计稿多锁了 `RTCTickEvent::process()` 一个点），IDE 发现 `IdeController` 本来就是 `PioDevice`（经 `PciEndpoint→PciDevice→DmaDevice`），复用同一把锁即可、不需要新设计，只是 `IdeDisk` 的 6 个 DMA 状态机函数会互相同步调用，锁必须挂在 `EventFunctionWrapper` lambda 这一层而非逐函数、否则自锁死；非 TSan 正确性 A/B + TSan 扩时长 A/B（MAX_TICKS=1.3e9）均干净，且 §24.5 的 `AddrRangeMap` 竞争这次不再出现（S-010 修复生效的交叉验证）；§25.2 记录了一个环境不一致——本次会话沙盒的 cgroup 拿不到 CLAUDE.md 记录的 `isolcpus=54-55,92-111`，需要用户在容器/编排层核实 |
| S-010 | [S-010-addr-range-map-cache-race.md](./S-010-addr-range-map-cache-race.md) | **已实现+TSan 验证；性能 A/B 待做** | 起点是 S-009 §24.5 TSan A/B 报告数量最多的一类（1500+ 次）：`AddrRangeMap<AbstractMemory*,1>`（`PhysicalMemory::addrMap`）的 LRU 查找缓存在 `find()`/`addNewEntryToCache()` 间无锁跨域读写，和 S-004 §9.8 X86 TLB 竞争同一个模式；全仓库 8 处 `AddrRangeMap` 实例化排查（§7）额外发现 `BaseXBar::portMap` 也有同一种竞争、且被 S-009 §24.1 的 `layerLock` 漏保护了三个调用入口（§7.2）；修法是在 `AddrRangeMap` 内部加 `cacheLock`（容器级，不是逐调用点，§10），非 TSan 正确性 A/B + TSan 扩时长 A/B（Q=300 小窗口 + MAX_TICKS=1.3e9 serial×2/spin×2，§11）均干净——四份 stats.txt 逐字节相同，76 次 TSan 警告的 `#0` 帧全部落在已知背景噪声（`_curTick`/`Event::Flags`/`Consumer::lock`），无一落在 `AddrRangeMap`/`portMap`；仍未测热路径开销 |
| S-011 | [S-011-consumer-lock-owner-race-audit.md](./S-011-consumer-lock-owner-race-audit.md) | **已实现+已编译+TSan/正确性验证通过** | S-010 §11.2 TSan A/B 里第二多的报告类别（66 次）：`Consumer::lock()`/`unlock()`（S-002 引入的 per-consumer 可重入锁）自己的记账字段 `m_wakeup_mutex_owner` 无同步读写；跟同一轮报告里的 `_curTick`/`Event::Flags` 不是一回事——那两个是项目自己"放宽跨域时序"的设计取舍、陈旧读可容忍，这里读的是"我现在持锁吗"，需要精确答案；§5 确认了可达性（L3-miss 高频路径 + 每 controller 私有终生 router + 域→线程终生绑定三者叠加，是稳态常见路径非边角情形）；§6 设计了一份压力测试来实测触发概率，但§7 用户决定不实现/不跑它；**§8 设计了修法**：把 `m_wakeup_mutex_owner` 从无同步的 `std::thread::id` 换成 `std::atomic<EventQueue*>`（复用 `Consumer.cc:65/118` 已有的 `curEventQueue()` 域身份 idiom），论证原子对象的单点一致性保证本身就足以排除 §3.2 的误判窗口；**§10（用户明确要求"go ahead and implement §8"后本会话做的）把修法实现、编译（非 TSan + TSan 两个 build）并验证**：TSan A/B（`MAX_TICKS=1.3e9`，serial×2+spin×2）里 `Consumer::lock()`/`unlock()` 的报告从 66 次/37-38 次降到 **0**，spin 两轮各剩 5 次已知背景噪声（`_curTick`/`Flags`）；正确性验证改用"同一模式下改动前 vs 改动后"对照（而不是原计划的"serial vs spin"对照），因为验证过程中发现 serial 和 spin 在这个窗口下本来就不是逐字节相同（跟这次修法无关，改动前后这个分歧数字完全一样），这次修法本身在同一模式下不改变任何可观测行为（`stats.txt` 逐字节相同）；**副产品**：serial/spin 在 `MAX_TICKS=1.3e9` 非 TSan build 下的既有分歧（`simInsts` 926690 vs 926608）是一个新发现的、独立于 S-011 的待办项，如实记录在 §10.5，未解决，需要用户决定是否单独立文调查 |
| S-012 | [S-012-eventq-critical-path-instrumentation-design.md](./S-012-eventq-critical-path-instrumentation-design.md) | **用户确认按 5 步实现；Step 1（脚手架）已落地+编译+验证（§11），Step 2-5 未做** | 出路 3（每域塞更多工作）立项前缺一份关键路径数据——把这个缺口拆成三个可测的未知数：哪个域最后到达 quantum 屏障（谁在关键路径上）、每个线程墙钟时间构成（处理事件 vs 等屏障 vs 阻塞在跨域锁上）、各域每 quantum 工作量是否不均衡；设计了两类打点（`Barrier::wait()` 到达/离开时刻 + `UncontendedMutex` 慢路径等锁时长，四个已知跨域锁 `layerLock`/`pioLock`/`cacheLock`/`m_wakeup_mutex` 打标签）加一个可选第三类（每域每 quantum 事件数，用来区分"真的多干活"和"卡在锁上")；关联键初稿误用了 `simQuantumStart`（它是运行期常量——S-009 §22 论证其跨线程读安全的根据恰恰是"不变"，按它分组会把整个运行归成一组），评审后修正为各域自己队列在打点处的本地 `curTick()`（同一 quantum 边界各域相同、纯线程本地读，§3.4）；域号通过线程入口的 `thread_local` 变量落地（`simulate.cc` 的 `thread_main`），不做每次打点查表；关闭时对已验证的快路径行为无改动（`UncontendedMutex` 多一次可预测分支），打开时是独立诊断跑、不产出可比性能数字；**本文档不包含任何实现**，按 CLAUDE.md 的 checkpoint 约定，落地前需要用户过一遍设计再决定 |

## 如何新增一份 spec

1. 取下一个未使用的编号（当前最大是 `S-012`，下一份是 `S-013`），**不要**
   回填或重排已有编号，哪怕新主题在逻辑上是某个旧编号的延续。
2. 命名 `S-NNN-slug.md`，独立成文——不要把新主题塞进某个已有 `S-NNN`
   文件的正文里，哪怕它是那份文档里某个未解决问题的直接后续（用交叉引用
   `S-NNN §X.Y` 链接过去即可，同 S-001..S-007 互相引用的方式）。
3. 写完后只在这个表格里加一行——不要因为新增一份 spec 就去改写旧
   `S-NNN` 文件的正文内容。
4. 如果这份新 spec 明显解决/推进了上面"现状"段落提到的某个悬而未决项，
   顺手更新那一段（这是本索引里唯一预期会被频繁编辑的部分）。
