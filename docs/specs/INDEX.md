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

**现状（截至 S-007）**：**里程碑已达成**——自旋屏障让并行 EventQueue 第一次
跑出对串行的真实、可复现加速（SE 模式最大屏障压力工作点 6.8x；FS 真实靶子
定窗测量 1.41x），且已验证内存安全、时序中性。下一步杠杆是让跨域链路延迟
更贴近真实值、每域分摊更多工作（S-004 §9.3 出路 1/3），屏障本身已经不是
主瓶颈。仍然悬而未决、不阻塞当前结论的项：FS 完整 ROI 的串行参考 `simTicks`
（S-007 §12.6 提到仍在跑）、TLB 加锁后的热路径开销未测量、精确模式下一个
恒定的一 ruby 周期偏差未查明根因（S-006 §9.6 附近提到）。

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
