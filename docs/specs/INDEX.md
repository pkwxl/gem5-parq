# docs/specs 索引

`docs/specs/` 是这个 gem5 fork 的设计/问题记录目录，按 `S-NNN-slug.md`
编号，编号**按目录统一分配**（不分项目），新主题接着往后取号，不回填、
不重排已有编号。

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
这条工作量最大、最不确定）。**§24 已经实现并验证了两个通用点**：TSan
（本沙盒的限制已解除，§24.2）扩时长 A/B（MAX_TICKS=1.3e9）显示两把新锁
干净，四份 stats.txt 逐字节相同；PIT/RTC/IDE 仍按计划留到下一轮。**§24.5
额外发现一个不在 S-009 范围内、量级可能更大的独立竞争**——
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
几个数字做定量结论，应重新跑一次。

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
| S-009 | [S-009-raise-fs-quantum-past-iobus-edge-design.md](./S-009-raise-fs-quantum-past-iobus-edge-design.md) | **设计稿，未实现** | 精确定位卡住 Q=300 的是 `PacketQueue::schedSendEvent`（`RubyPort.cc` 的 PIO 转发 + 每个经典 PIO 设备共享），不是 IOXBar 参数；设计在这一个公共点套用 S-006 §11.5 的 grid-anchored snap（§19，一次改动覆盖所有调用点）；审计**确认**这条经典 Port 路径存在真实的跨域数据竞争（§18），§23 逐设备排查发现竞争有两种形状（核 vs 核、核 vs 域0自己的线程），找到两个通用加锁落点（`BaseXBar`/`PioDevice`）+ PIT/RTC/IDE 的手工接线；两个通用点已实现+TSan 扩时长 A/B 干净（§24），PIT/RTC/IDE 仍留待下一轮；§24.5 发现的 `AddrRangeMap` 竞争已拆分单独立文，见 S-010 |
| S-010 | [S-010-addr-range-map-cache-race.md](./S-010-addr-range-map-cache-race.md) | **已实现+TSan 验证；性能 A/B 待做** | 起点是 S-009 §24.5 TSan A/B 报告数量最多的一类（1500+ 次）：`AddrRangeMap<AbstractMemory*,1>`（`PhysicalMemory::addrMap`）的 LRU 查找缓存在 `find()`/`addNewEntryToCache()` 间无锁跨域读写，和 S-004 §9.8 X86 TLB 竞争同一个模式；全仓库 8 处 `AddrRangeMap` 实例化排查（§7）额外发现 `BaseXBar::portMap` 也有同一种竞争、且被 S-009 §24.1 的 `layerLock` 漏保护了三个调用入口（§7.2）；修法是在 `AddrRangeMap` 内部加 `cacheLock`（容器级，不是逐调用点，§10），非 TSan 正确性 A/B + TSan 扩时长 A/B（Q=300 小窗口 + MAX_TICKS=1.3e9 serial×2/spin×2，§11）均干净——四份 stats.txt 逐字节相同，76 次 TSan 警告的 `#0` 帧全部落在已知背景噪声（`_curTick`/`Event::Flags`/`Consumer::lock`），无一落在 `AddrRangeMap`/`portMap`；仍未测热路径开销 |

## 如何新增一份 spec

1. 取下一个未使用的编号（当前最大是 `S-007`，下一份是 `S-008`），**不要**
   回填或重排已有编号，哪怕新主题在逻辑上是某个旧编号的延续。
2. 命名 `S-NNN-slug.md`，独立成文——不要把新主题塞进某个已有 `S-NNN`
   文件的正文里，哪怕它是那份文档里某个未解决问题的直接后续（用交叉引用
   `S-NNN §X.Y` 链接过去即可，同 S-001..S-007 互相引用的方式）。
3. 写完后只在这个表格里加一行——不要因为新增一份 spec 就去改写旧
   `S-NNN` 文件的正文内容。
4. 如果这份新 spec 明显解决/推进了上面"现状"段落提到的某个悬而未决项，
   顺手更新那一段（这是本索引里唯一预期会被频繁编辑的部分）。
