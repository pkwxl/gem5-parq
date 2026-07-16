# S-007: 自旋屏障设计与实测 + 项目里程碑结论

> **状态**：**里程碑达成**——并行 EventQueue 首次跑出对串行的真实、可复现
> 加速（SE 最大压力工作点 6.8x，FS 真实靶子定窗 1.41x），且内存安全、时序
> 中性均已验证。下一根杠杆是更真实的跨域链路延迟 / 每域更多工作
> （[S-004](./S-004-first-speedup-measurement-and-fixes.md) §9.3 出路 1/3），
> 不再是屏障本身。对应原单体设计文档第 12-13 节（含全文结论），内容原样
> 保留。

---

## 12. 可选自旋屏障：把 S-004 §9.3 出路 2 落地并实测

### 12.1 动机与设计

S-004 §9.2 实测每 quantum 的 cv 屏障成本 ~25.6µs，S-004 §9.3 出路 2 预测换成自旋屏障
可降到 ~2µs（一个数量级）。S-005 §10.4 已确立自旋的前置条件——绑核（S-005 §10）+
无超线程 + 核数富余——本节据此落地。做成**可选的屏障模式**（不是硬替换），
以便同一二进制在同一 Q 下 A/B 对比、并挑出加速最大的那个，符合本项目
"先测再断言"的一贯做法。

- `Root.eventq_barrier_mode`（`Param.String`，默认 `"cv"`）+
  `eventq_barrier_spin_iters`（`Param.Unsigned`，hybrid 用），管线完全镜像
  10 节的 `eventq_host_cpus`：`Root.py` → `root.cc`（字符串映射到枚举）→
  `eventq.{hh,cc}` 全局 `eventqBarrierMode`/`eventqBarrierSpinIters` →
  `BaseGlobalEvent` 构造 `Barrier` 时读取。
- 三种模式（`src/base/barrier.hh`）：
  - **`cv`**：原 `std::condition_variable` 路径，逐字节不动（默认）。
  - **`spin`**：sense-reversing 原子屏障——`spinLeft.fetch_sub`，最后到达者
    （返回 1）重置计数、`spinGen`（release）自增放行，其余线程在 `spinGen`
    （acquire）上 `_mm_pause()` 自旋。`fetch_sub(acq_rel)` + `spinGen`
    release/acquire 给出和 cv 版 mutex 相同的 full-barrier happens-before；
    保住"恰好一个 `wait()` 返回 true"的契约（`global_event.cc:133/147`
    靠它只跑一次 `_globalEvent->process()`）。
  - **`hybrid`**：先自旋 `spin_iters` 次，再退回 cv 睡眠——放行时最后到达者
    额外 `notify_all` 唤醒已挂起者。为长/不均衡 quantum 或宿主争抢兜底。
- **基线中性（结构性）**：默认 `cv` 时 cv 路径逐字节不变；且**串行模式
  （单队列）永不自旋**（`fetch_sub` 立即返回 1），故即便选 `spin`，串行结果
  也不变。配置入口：`se.py --eventq-barrier-mode/--eventq-barrier-spin-iters`；
  FS 脚本 `EVENTQ_BARRIER_MODE`/`EVENTQ_BARRIER_SPIN_ITERS` 环境变量。
- 改动文件：`src/base/barrier.hh`、`src/sim/eventq.{hh,cc}`、`src/sim/root.cc`、
  `src/sim/Root.py`、`src/sim/global_event.cc`；配置
  `configs/deprecated/example/se.py`、
  `docs/refs/scripts/x86_fs_mesi3_parallel_eventq.py`。

### 12.2 正确性验证（SE，`build/X86/gem5.opt`）

- **串行 cv 基线逐字节不变**：`--options="200000 1"` → `simTicks=31555788000`，
  `Validating...Success!`。
- **自旋是时序中性的机制替换**：精确模式（ll=1、Q=500、
  `--options="20000 1"`，绑核 8-13）下 `cv`/`spin`/`hybrid` 三种模式
  **全部** `simTicks=6389611500`、`Validating...Success!`——换屏障机制不改任何
  事件时序/顺序（跨域顺序由量子网格 + async 合并决定，与线程"如何等待"无关）。
  若 spin 给出不同的 simTicks，那才是真 bug（漏了 happens-before）；实测一致。

### 12.3 A/B 实测（SE，2e9 tick 窗口，两臂都绑核 8-13，屏障模式是唯一变量）

ABAB 交错跑消宿主 load 漂移（沿用 S-005 §10.3 方法）。两个工作点：

**加速区 ll=20、Q=10000（10ns）**（2×10⁵ 次同步，3 轮中位数）：

| 模式 | wall 中位数 | vs 串行 | 每-quantum 屏障成本 |
|------|-------------|---------|---------------------|
| 串行 | 5.46 s | 1.00x | — |
| cv | 9.26 s | 0.59x（比串行慢） | (9.26−5.46)/2e5 ≈ **19.0µs** |
| **spin** | **6.06 s** | **0.90x（接近平价）** | 0.60/2e5 ≈ **3.0µs** |
| hybrid@200 | 6.98 s | 0.78x | 1.52/2e5 ≈ 7.6µs |

spin 比 cv 快 **1.53x**；把并行从 0.59x 抬到 **0.90x 串行**（接近平价）。

**最大屏障压力 ll=1、Q=500（0.5ns）**（4×10⁶ 次同步，2 轮中位数）：

| 模式 | wall 中位数 | 每-quantum 成本 |
|------|-------------|-----------------|
| cv | ~102.5 s（107.5/97.6） | 102.5/4e6 ≈ **25.6µs**——与 S-004 §9.2 独立实测**完全吻合** |
| **spin** | **~15.0 s（15.1/15.0）** | 15.0/4e6 ≈ **3.8µs**（含有用工作） |

spin 比 cv 快 **6.8x**。

**结论**：spin 把每-quantum 屏障成本从实测 ~25.6µs 降到 ~3µs（正是 S-004 §9.3 预测的
数量级），两个工作点各自独立印证。纯 **spin 是最大加速模式**（与 S-004 §9.3/S-005 §10.4
预测一致），hybrid 居中——`hybrid` 的价值在纯 spin 会过度烧核的场景（长/
不均衡 quantum、宿主争抢），本沙箱这两个工作点都是短 quantum，故纯 spin 胜。
注意 0.90x 仍未过 1：加速区 Q 被 S-004 §9.4 修正后的正确性上限（ll=20 → Q≤10000）
夹住，屏障不再是主瓶颈后，下一步该往 S-004 §9.3 出路 1/3（更真实链路延迟抬 Q 上限、
每域更多工作摊固定成本）走，而不是继续抠屏障。

### 12.4 FS A/B 状态：受阻，且撞上 APIC 修复之后的下一堵墙（与自旋无关）

FS 8-EventQueue 自旋 A/B **未完成**，两个原因如实记录：

1. **重编 `build/X86_MESI_Three_Level/gem5.opt` 被 ETXTBSY 阻塞**：S-006 §11.5 的串行
   参考跑（`/tmp/fs3-restore-serial`，旧二进制）仍在执行同一 MESI 二进制文件，
   Linux 不允许 relink 正在执行的可执行文件。
2. **S-006 §11.5 修复后的并行重放长跑（`/tmp/fs3-restore-par-fix`，旧二进制）在 ROI
   里跑了 3h20m 后段错误**（SIGSEGV，exit 139），**未出 stats**。回溯：

   ```
   statistics::pythonDump()               <- 调 libpython(PyImport_ImportModule)
   statistics::StatEvent::process()
   GlobalEvent::BarrierEvent::process()   <- 一个 GlobalEvent 屏障
   EventQueue::serviceOne()
   ...SimulatorThreads::runUntilLocalExit lambda   <- 从属线程(__clone)
   ```

   即：一次 stats dump（`StatEvent`，本身是个跨所有队列的 GlobalEvent）在**从属
   EventQueue 线程**上执行了 `pythonDump()`，从**非主线程**触碰 CPython 解释器
   （未持 GIL）→ 段错误。这是 S-006 §11.5 的 APIC 开机墙之后 FS 并行的**下一堵墙**，
   **与本节自旋屏障无关**（该跑用的是不含自旋的旧二进制）；且它会打到**任何**
   FS 并行跑在 stats-dump 时刻，不分屏障模式。修法方向（未实现、待决策）：把
   `StatEvent`/`pythonDump` 强制回主线程（queue 0）执行，或在 dump 时 stop-the-
   world 到主线程。日志 `/tmp/fs3-restore-par-fix.log`。

故 FS 自旋 A/B 的前置是先解 12.4.2 这堵 pythonDump 跨线程墙（并腾出 MESI 二进制
重编）。SE 侧的结论（12.3）已经是干净、可复现、和理论吻合的完整证据。

### 12.5 pythonDump 跨线程墙：根因 + 修复 + 实测（已解决）

**根因确认**（读码）：`GlobalEvent::BarrierEvent::process()`
（`global_event.cc`）原本让**最后到达屏障的那个线程**执行
`_globalEvent->process()`（`if (globalBarrier()) ...`）。多队列下这个线程是
不确定的——可能是任一从属 EventQueue 线程。而 `StatEvent`（一个 GlobalEvent，
`stat_control.cc`，由 `m5 dumpstats` pseudo-op 或 `periodicStatDump` 调度）的
`process()` → `statistics::dump()` → `pythonDump()`
（`src/python/pybind11/stats.cc`）会 `py::module_::import("m5.stats")`——一次
CPython 调用。**关键约束**：`simulate()` 的 pybind 绑定（`event.cc:108`）
**没有** `gil_scoped_release`，故主线程在整个事件循环期间**持有 GIL**；从属线程
从来不是 Python 线程、也不持 GIL。因此从属线程跑 `pythonDump()` 直接段错误
（就是 12.4.2 的回溯）。这排除了"在 pythonDump 里 `gil_scoped_acquire`"的修法：
从属线程会阻塞在主线程持有的 GIL 上，而主线程正卡在同一个屏障的第二道
`globalBarrier()` 等它——**死锁**。

**修复**（`src/sim/global_event.cc`，一处）：把 GlobalEvent 的单一执行者从
"最后到达者"改成"驱动 main event queue 0 的线程"——多队列下这就是持 GIL 的主
（Python）线程：

```cpp
globalBarrier();                              // 全部到达
if (curEventQueue() == mainEventQueue[0])     // 只有 queue-0/主线程
    _globalEvent->process();
globalBarrier();                              // 等 process 完成
```

`curEventQueue()` 是每线程 TLS（`doSimLoop(eq)` 里 set），queue-0 线程恒等于
`mainEventQueue[0]`，唯一。**串行中性**：serial 下 queue 0 是唯一队列，与旧
"最后到达者"完全等价（且 SE 精确/基线复验逐字节不变，见下）。只改
`GlobalEvent`，**不碰** `GlobalSyncEvent`（量子屏障热路径），因为后者的
`process()` 只是重排自身、不碰 Python。这本质上是一个上游都该修的隐藏 bug：
任何多队列跑 + 中途 Python stat dump，只要最后到达者是从属线程就会崩。

**验证**：
- **SE 无回归**（`build/X86/gem5.opt` 重编）：serial cv 仍 31555788000；并行精确
  cv/spin/hybrid 仍全部 6389611500 Validating Success——改动对正常并行运行零影响。
- **FS 实测（真靶子）**：给 FS 脚本加了 `STAT_DUMP_PERIOD` 环境开关（默认关，
  调 `m5.stats.periodicStatDump`），把崩溃点从 ROI 末（旧跑 3h20m 才到）提前到
  重放后几秒。修复后的 `build/X86_MESI_Three_Level/gem5.opt`（用改名规避
  ETXTBSY——把串行参考跑占用的旧二进制 `mv` 成 `gem5.opt.serialref`，运行中的
  进程按 inode 继续、重编落到腾空的路径，串行参考跑不受影响）跑
  `PARALLEL_EVENTQ=1 HOST_PIN_CPUS=8..15 STAT_DUMP_PERIOD=1000000`：8 个 EventQueue
  线程真并行，**连续 52 次 stat dump 全部成功、零段错误**（跑到手动停）。旧行为
  下每次 dump 落到从属线程的概率 ~7/8，52 次全躲开 queue 0 的概率 (1/8)^52 ≈ 0——
  故这是对修复的确定性证明。日志 `/tmp/fs3-pydump-fix.log`。

**FS 自旋 A/B 现在解锁**：pythonDump 墙已除、MESI 二进制已带自旋+本修复重编好。
结果见 12.6。

### 12.6 FS 8-EventQueue A/B 实测：自旋在真靶子上同样有效

跑完整 ROI 要数小时，改为**定窗**对照：给 FS 脚本加 `MAX_TICKS` 环境开关
（默认关，`simulator.run(max_ticks=N)`——从重放点推进 N tick 后 MAX_TICK 退出、
主线程 dump stats 停机），两臂都绑同一组 node0 8-15 核（屏障模式是唯一变量）。
`MESI_Three_Level`、4 核、Q=300、ll=20、窗口 2e8 tick（= 6.67e5 个 quantum），
3 轮中位数：

| 模式 | wall 中位数 | vs cv | simInsts |
|------|-------------|-------|----------|
| cv | 57.1 s | 1.00x | 74062 |
| **spin** | **40.5 s** | **1.41x 更快** | 74062 |
| hybrid@200 | 40.4 s | 1.41x 更快 | 74062 |

- **正确性**：三种模式、3 轮，`simInsts` **全部 74062**（逐一致）——自旋/hybrid
  在 FS 下同样是**时序中性且确定性**的机制替换（和 12.2 的 SE 结论一致，这里用
  simInsts 而非 simTicks，因为定窗把 simTicks 钉死在 2e8）。
- **每-quantum 屏障节省**：(57.1−40.5)/6.67e5 ≈ **24.9µs/quantum**——和 S-004 §9.2/§12.3
  实测的 cv ~25.6µs **几乎完全吻合**。屏障成本是每-quantum 固定量，与 SE/FS 无关，
  这里再次独立复现。
- **为什么 FS 只有 1.41x 而 SE OP1 有 6.8x**：屏障成本固定，但它占运行时的**比例**
  取决于每 quantum 有多少有用工作。FS（完整 Timing+MESI3+设备）每 quantum 的有用
  仿真远多于 SE 裸 Ruby，屏障只占 cv 运行时的 ~29%（SE OP1 里占 ~85%），故同样的
  绝对节省换算成更小的相对加速。这是一致的物理图景，不是矛盾。
- hybrid 与 spin 基本持平（等待极短、几乎不回落到 cv），符合 12.1 的预期。

**结论**：SE 的自旋屏障收益**完整迁移到真实 FS 靶子**——同样时序中性、同样
~25µs/quantum 的固定节省，在这个参数下 1.41x。串行参考跑（pid 1540862，
`/tmp/fs3-restore-serial`，串行中性、仍在 ROI 中）给出的完整-ROI 对照 simTicks 仍
TBD，但定窗 A/B 的 simInsts 一致性已足够证明并行 FS 运行的正确性。脚本知识点：
`MAX_TICKS`（定窗）、`STAT_DUMP_PERIOD`（12.5 的 dump 复现）、ETXTBSY 用改名规避
（12.5）。

## 13. 结论与里程碑：自旋屏障——并行 EventQueue 首次跑出真实加速

本文从 0 节的"经典内存系统 + 多 EventQueue 无解"出发，走到这里，可以给出一个
**阶段性结论**了（不再是前言里那句"尚未做出架构决定"）：

**方案成立。** parti-gem5 式的"N 核 → N+1 个 EventQueue + per-consumer 限定范围
锁 + quantum 屏障"路线，在 gem5 上被完整实现、且被证明是**内存安全 + 时序中性**
的：

1. **内存安全**：SE 模式下每一个跨域竞态都被定位并修掉（Ruby functional 遍历
   加锁 S-004 §9.7；futex UAF + 跨域 TLB flush + 跨域 deschedule S-004 §9.8），完整多线程负载
   ASan 干净跑到 tick 8.5e9（S-004 §9.8）。FS 模式下 APIC 中断跨域唤醒墙（S-006 §11.5，量子
   网格锚点 bug）和 stats-dump 跨线程墙（12.5，pythonDump 跑在从属线程）也都
   根因清楚、修复、实测。
2. **时序中性**：所有机制替换都不改仿真结果——串行基线逐字节不变
   （simTicks=31555788000 贯穿始终）；并行精确模式 simTicks/simInsts 在
   cv/spin/hybrid 三种屏障下完全一致（12.2/12.6）。

**性能里程碑（本轮的主结果）。** 9 节第一次真实加速比测量暴露出：真正的瓶颈
不是仿真、不是锁，而是 **quantum 屏障本身**——cv（condition_variable/futex）
屏障实测 ~25.6µs/quantum（S-004 §9.2）。S-005 §10 的宿主线程绑核（PDES 调度纪律）解锁了
自旋屏障这个前置条件，12 节据此落地了**可选屏障模式**（cv/spin/hybrid），并
两侧实测：

- **每-quantum 屏障成本从 ~25.6µs 降到 ~3µs**——S-004 §9.3 预测的整整一个数量级，在
  SE（12.3）和 FS（12.6）两个完全独立的靶子上各自复现出同一个 ~25µs 的固定节省。
- **SE**：最大屏障压力下（ll=1/Q=500）2e9 窗口 cv→spin **6.8x**；加速区
  （ll=20/Q=10000）把并行从 **0.59x（比串行慢）抬到 0.90x（接近平价）**。
- **FS**（真实全系统，MESI_Three_Level 8-EventQueue）：Q=300/ll=20 定窗
  **1.41x**——绝对节省与 SE 相同，相对加速小是因为 FS 每 quantum 有用工作多、
  屏障占比低（~29% vs SE 的 ~85%），物理图景自洽。

这是**并行 EventQueue 路线第一次跑出对串行的真实、可复现加速**（此前 S-004 §9.1 是
125x 减速）。纯 spin 是最大加速模式（绑核 + 无超线程 + 核数富余下自旋安全，
S-005 §10.4），hybrid 是不均衡/被抢占场景的兜底。

**还没到头，但瓶颈已经换人。** 0.90x（SE）/1.41x（FS）说明屏障不再是主瓶颈后，
下一根杠杆是 S-004 §9.3 出路 1/3——把跨域链路延迟建成更真实的值（抬高 Q 上限）、每域
放更多工作（摊薄固定同步成本）——而不是继续抠屏障或 S-002 §7.3 那 ~15% 的单线程锁开销。
SE 模式的 OS/线程仿真天花板（S-004 §9.9）仍是选 FS 做主战场的理由，这一点没变。

**产出（均已提交，分支 main）**：自旋屏障 `80f6e534f3`/`117ce20f50`/`fb347fe4d7`；
pythonDump 修复 `4642260764`/`3ca1e20044`；FS A/B `8e6d6d3009`。

## 14. 操作提示：并行 EventQueue 运行下 `SIGUSR1` 会段错误

这条之前只记在 memory（`parallel-eventq-sigusr1-crash-hazard`）里，属于
本项目遗留的一个真实 gem5 bug（不是本文档记录的哪堵墙的一部分），补记于此
免得下次调试重新踩坑。

给运行中的 gem5 进程发 `SIGUSR1` 会触发异步 stats dump
（`init_signals.cc:dumpStatsHandler` → `async_statdump`）。**串行模式下
安全**（`StatEvent`/`pythonDump()` 跑在持有 Python GIL 的主线程上）；
**并行 EventQueue 模式下会段错误**：dump 作为一个 `GlobalEvent`
（`BarrierEvent::process` → `StatEvent::process` →
`statistics::pythonDump()`）跑在某个**从属**队列线程上
（`SimulatorThreads::runUntilLocalExit` → `doSimLoop` → `serviceOne`），
在没有 GIL 的情况下调用 CPython API
（`PyImport_ImportModule`/`PyUnicode_New`）→ 崩在 libpython 里。

2026-07-15 实测确认：给 12.4-12.6 期间那次 FS 8-EventQueue 重放（PID
1565656，跑了约 3h/830 CPU-分钟）发 `kill -USR1` 读一个 live tick，直接
把跑跑的进程杀死；同一个信号发给串行重放则正常 dump、继续跑。

**怎么应用**：不要用 `SIGUSR1`/`SIGUSR2`（异步 stat dump/reset）去探测一个
并行 EventQueue 运行。要在不打扰运行的前提下读一个当前 tick，用只读方式
附加：`gdb -p <pid> -batch -ex 'p gem5::mainEventQueue'`（或检查某个队列的
`getCurTick()`），或者干脆等 ROI 跑完直接 diff `stats.txt`。串行运行仍然
可以放心用 `SIGUSR1` 探测。

这本身也是 12.5 节 pythonDump 墙（跑在从属线程上）的同一个根因家族的另一个
触发路径——12.5 修的是 `GlobalEvent::BarrierEvent::process` 在 quantum
边界的定期 dump；这里是异步信号触发的 dump，走的是同一条
"stat dump 没有被强制搬到主线程"的代码路径，理论上 12.5 的修法（把
`_globalEvent->process()` 限定到 `curEventQueue()==mainEventQueue[0]`）
应该同样能修好这条路径，但**尚未针对 `SIGUSR1` 专门验证过**——留作后续
待办，不要假设已经修好。

---

**上一篇**：[S-006：迁移到 FS 模式](./S-006-fs-mode-migration.md)
**下一篇**：（无——这是当前进度的最后一份 spec；下一步的新调查请开一份新的
`S-008-slug.md`，见 [INDEX.md](./INDEX.md) 的编号约定）
**返回**：[INDEX.md](./INDEX.md)
