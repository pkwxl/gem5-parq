# S-006: 迁移到 FS 模式——3-level 检查点重放与 APIC 中断跨域唤醒墙

> **状态**：APIC 中断跨域唤醒墙已根因定位并修复（`simQuantumStart` 量子网格
> 锚点修正，提交 `2273b39c1f`/`284b291f46`/`9d25761024`）。并行重放验证越过
> 该墙进入 ROI；串行参考跑与后续 FS 长跑/pythonDump 墙见
> [S-007](./S-007-spin-barrier-and-milestone.md) §12.4-12.6。11.1-11.5 对应原
> 单体设计文档第 11 节，内容原样保留；11.6-11.8 是后补的内容（此前只存在于
> memory，未落盘），补齐了 2-level FS 首次尝试、3-level 域布线校验依据、以及
> "Ruby 检查点无法在多 EventQueue 下重放"这条 load-bearing 结论。相关 memory:
> `parallel-eventq-fs-device-wall`、`parallel-eventq-l2-project-status`。

---

## 11. 迁移到 FS 模式：3-level 检查点重放，撞上 APIC 中断跨域唤醒墙

### 11.1 背景与做法

S-004 §9.9 的结论是 SE 模式对多线程客户机的 OS/线程语义仿真不足，正是
parti-gem5 选 FS 模式的理由。本节起把并行 EventQueue 切分搬到 FS
模式（真实客户机内核负责线程生命周期/arena/TLS，gem5 不再仿真这些）。

为避开"Atomic-on-Ruby 跑 Timing 速度、开机要几小时"的问题，采用
**跨层次检查点**：

- `docs/refs/scripts/x86_fs_classic_save_ckpt.py`：在 **classic
  NoCache Atomic**（真原子访存、无 Ruby 状态机）上开机，把 `threads`
  基准 base64 塞进 `/root/threads`，在其计时区（ROI）**之前**的
  `m5 exit` 处存检查点。检查点只带物理内存（`.pmem`）+ CPU 架构态
  + 设备态，全部与缓存层次无关，因此可跨层次重放。
- `docs/refs/scripts/x86_fs_mesi3_parallel_eventq.py`：用
  `CHECKPOINT_DIR=...` 把该检查点重放进 **MESI_Three_Level** 并行
  配置，直接落到 ROI，免重开机。`PARALLEL_EVENTQ=1` 开切分。

**域映射（4 核、1 个共享 L3、2 个 DDR 通道 → 8 个 EventQueue）**：
域 0 = DMA + 所有设备 + uncore；域 i+1（i=0..3）= 核 i + 其私有 L1
（含 sequencer/L1 缓存/**local APIC**）+ 其私有 L2 + 对应路由器；
域 5 = 共享 L3；域 6..7 = 各 directory + 其下游 DRAM MemCtrl + 路由器。
`SIM_QUANTUM_TICKS=300`（正确性优先：卡在最小 *classic* 跨域边，即
iobus/IOXBar 的 `forward_latency`=1 板周期=333 tick @3GHz 之下），
`LINK_LATENCY=20` ruby 周期。

**里程碑（已达成）**：classic 检查点成功生成于
`/workspace/gem5-ckpt/x86-threads3-roi-classic`（`m5.cpt` 373KB +
`board.physmem.store0.pmem` 87MB + IDE cow 3.6MB）。开机到 login 在
本沙箱实际耗时约 5.5 小时（脚本注释预期"几分钟"，严重偏离——疑似
宿主争抢 / Atomic-on-FS 比注释假设慢；但确实跑通、无错）。

### 11.2 并行重放：`activateContext` 跨域调度 assert（新墙）

`PARALLEL_EVENTQ=1` 重放**几乎立刻崩溃**（tick 5305999202821，
客户机一行都没跑：`stats.txt` 空、console 无输出、未到
`m5 resetstats`），退出码 134（SIGABRT）：

```
eventq.hh:759: EventQueue::schedule(...): Assertion `when >= getCurTick()' failed.
```

**回溯（自底向上）**：

```
doSimLoop → EventQueue::serviceOne              ← 某个设备/uncore 域线程
NoncoherentXBar::recvTimingReq                   ← PIO xbar（域 0）
RubyPort::PioResponsePort::recvTimingReq
X86ISA::Interrupts::recvMessage                  ← APIC 投递一个中断
BaseSimpleCPU::wakeup
TimingSimpleCPU::activateContext                 ← 调度该 CPU 的 fetch 事件
   → EventQueue::schedule(...)  ✗ when < 目标队列 curTick
```

**诊断——APIC 中断投递路径跨域**：中断消息在 PIO-xbar 线程
（**域 0**）上被 service，却要唤醒 **核 i**（其 fetch 事件在
**域 i+1**）。域 0 用**自己的** TLS `curTick` 算出 `when`，调度到
域 i+1 的队列，而后者已推进过了那个 tick（最多领先一个 quantum）→
`when < 目标 curTick`，正是跨域"调度到过去"。

这与 S-004 §9.6 修过的 SE 模式 `activateContext` 跨域墙**同一家族**，但
**触发路径不同**：S-004 §9.6 是 `clone3 → initState → activateContext`，
S-004 §9.6 的 quantum 边界 snap 没覆盖到**中断投递**这条路；而且这里是
**PIO/xbar 这条 classic 边**在做跨域调用（与 parallel-eventq-fs-
device-wall 记录的 FS 设备墙一致——Ruby int_link 的修复都不触及它）。

### 11.3 串行重放对照（进行中 / 待补）

同配置 `PARALLEL_EVENTQ=0` 单 EventQueue 重放**不可能触发**该 assert
（单队列无跨域调度）。其作用：(1) 证明 classic→MESI_Three_Level
跨层次检查点重放本身正确；(2) 产出并行跑必须逐字节匹配的参考
`stats.txt`。本次运行**尚在 ROI 中**（`threads 200000 1` 在 Timing
CPU + 详细 MESI-3 Ruby 上很慢，且 `chunk_size=1` 是最坏共享），
结果 simTicks 待补。输出目录 `/tmp/fs3-restore-serial`。

### 11.4 下一步候选（待抉择，留给新会话调试）

1. **把 S-004 §9.6 的激活 snap 延伸到中断路径**：让
   `TimingSimpleCPU::activateContext`（或 `BaseSimpleCPU::wakeup`）
   在被跨域调用时 snap 到 quantum 边界 / 改由属主线程调度——S-004 §9.6 修法
   的直接类比，只是对象换成 APIC 驱动的唤醒。属核心调度改动，需重做
   正确性论证。
2. **把 local APIC / 中断控制器放到与其核同一个域**，使
   `recvMessage → wakeup` 不再跨域。需先确认 PIO xbar 那条边能否
   根本不跨域（它是设备侧 classic port，可能只是把墙挪个位置）。
3. 其它：stop-the-world quantum 边界、或把中断投递降级为经属主
   队列的 one-shot kick 事件（S-003 §8.8 kick 机制的复用）。

崩溃日志 `/tmp/fs3-restore-par.log`。参见 memory
[[parallel-eventq-fs-device-wall]]、[[parallel-eventq-l2-project-status]]。

### 11.5 根因：不是"未覆盖的新路径"，是 S-004 §9.6 snap 的量子网格锚点 bug

新会话逐层核对 11.4 的三个候选后，发现 **11.2 对这堵墙的定性是错的**，
三个候选没有一个正是对的修法：

- **候选 2 直接死路**：`config.ini` 证实 local APIC（`interrupts`）**本来
  就**和它的核同域（核 i → `eventq_index=i+1`）。崩溃与 APIC 自己的
  `eventq_index` 无关——`recvMessage` 是被 PIO xbar（域 0）的
  `serviceOne` **同步**调用的，跑在**域 0 的线程**上，改 APIC 的域改
  不了这一点。
- **候选 1 的前提（"S-004 §9.6 的 snap 没覆盖中断路径"）也是错的**：中断路径
  最终走到的正是 S-004 §9.6 已经装好的那个 `activateContext` snap
  （`timing.cc`），而且这次的二进制**包含**该 snap（07-14 23:44 编译，
  晚于 S-004 §9.6 的 `fd31250bf8` @09:55）。崩溃时 snap 分支**确实被走到**
  （`curEventQueue()`=域 0 xbar 线程 ≠ `eventQueue()`=核的域 i+1）。

**真正的 bug**：S-004 §9.6 的 snap 算的是

```cpp
when = divCeil(curTick() + 1, simQuantum) * simQuantum;   // 网格：Q 的整数倍，锚在 0
```

但 barrier 网格并不锚在 0，而锚在**并行模式开始的那个 tick**：
`simulate.cc` 在 `curTick() + simQuantum` 建 `GlobalSyncEvent`，
`global_event.cc:161` 每次在 `curTick() + repeat` 重排——barrier 落在
`startTick + k*Q`。**SE 模式仿真从 tick 0 起，两套网格重合**，所以每一次
SE 跑都对；**FS 重放的 startTick 是检查点 tick（5305999202821 ≡ 121
(mod 300)）**，两套网格错位 121 tick。于是在一个量子窗口
`[base, base+300)`（base ≡ 121 mod 300）内：若中断在域 0 处于窗口前段
（`a ≤ 178`）时投递，而目标核的域已推进过 `base+179`（`b > 179`），
snap 会向下取整到最近的 300 的整数倍 `base+179`，落在目标队列
`_curTick` 之后……不，之**前** → 目标队列上 `assert(when >= getCurTick())`
失败。正是那个开机即崩、tick 卡在 5305999202821 的现象。全树只有
`timing.cc` 这一个量子 snap 点（grep 确认）。

**修法（候选 1 的正确、且更小的形态）**：把 snap 对齐到 **barrier
网格**——这本就是 S-004 §9.6 注释写的原意（"不早于目标合并这次插入的那个
barrier"）。新增全局锚点 `simQuantumStart`（`eventq.{hh,cc}`），在
`simulate.cc` 建 `GlobalSyncEvent` 处置为 `curTick()`；snap 改为

```cpp
when = simQuantumStart +
       divCeil(curTick() + 1 - simQuantumStart, simQuantum) * simQuantum;
```

**基线中性（可证，无需复测）**：当 `simQuantumStart == 0`（串行；以及从
tick 0 起跑的 SE 模式）该式**逐字节退化回**旧式 `divCeil(curTick()+1,
Q)*Q`，故所有既往 SE/串行结果不变；只有 startTick≠0（即检查点重放）
时行为才不同。串行参考跑（旧二进制，`/tmp/fs3-restore-serial`）因此仍
有效。

**验证**：重编 `build/X86_MESI_Three_Level/gem5.opt`，同一并行重放命令
（输出 `/tmp/fs3-restore-par-fix`）——**开机即崩的 assert 消失**：进程
起了全部 8 个 EventQueue 线程、~366% CPU 真并行、越过 tick
5305999202821 的中断投递墙、日志零 assert/abort，进入 ROI 继续跑。
后续是否撞上更下一层的墙（真实客户机 ROI 在 Timing+MESI-3 上很慢）
待这次长跑给出——串行参考 `simTicks` 也仍在 ROI 中、待补。

改动 4 文件：`src/sim/eventq.{hh,cc}`（`simQuantumStart` 全局）、
`src/sim/simulate.cc`（建 barrier 处置锚点）、`src/cpu/simple/timing.cc`
（snap 对齐到锚点）。崩溃日志见 `/tmp/fs3-restore-par.log`；修复后日志
`/tmp/fs3-restore-par-fix.log`。

### 11.6 补记：FS 模式的第一次尝试（2-level，早于本节的 3-level pivot）

以下内容原来只存在于 memory（`parallel-eventq-fs-device-wall`），据 CLAUDE.md
"不隐藏不利结果" 的约定补记到设计文档：11.1-11.5 的 3-level 工作**不是**
FS 迁移的第一次尝试，前面还有一轮 2-level（`MESI_Two_Level`）的探路，产出
了两个后续一直在用的结论。

**第一堵墙（先于 11.2，且是不同的边）**：`X86Board` stdlib + Ruby
`MESI_Two_Level` + `SimplePt2Pt` + 4 个 Timing 核，域映射
`backend=域0 / 核 i -> 域 i+1`（N+1 个 EventQueue），一路正确跑过整个
Ruby 缓存/一致性网络到 tick ~7.08e10（深入 Linux 内核初始化），才崩：

```
EventQueue::schedule: assert(when >= getCurTick())
NoncoherentXBar::recvTimingReq → BridgeBase::schedTimingReq  (board.iobus, eq=0)
```

根因：核域（i+1）里的 CPU 发出一次 MMIO/APIC 访问，走**经典**内存系统的
PIO 路径（`RubyPort` pio → bridge → iobus，全在后端域 0），设备访问延迟
小于 `sim_quantum`。这条路径用的是原始 `EventQueue::schedule`，**不经过**
Ruby 的 `Consumer::commitTick`，所以 per-consumer 锁 + 到达 tick 机制
（S-003 §8.x）根本碰不到它——这正是 11.2 那堵新墙（3-level 下同一条 classic
边、不同触发者）的**前身**：`apicbridge`（`X86Board.py:168`，经 iobus）是
中断投递撞上这条边的一个实例。

**在此之前先修的一个配置 bug**：`SimplePt2Pt` 的路由器是 `ext_links` 上的
**交叉引用**，不是子节点，所以必须直接给它们赋 `eventq_index`
（`el.int_node`），不能靠 `el.descendants()` 遍历到；给 `Switch` 赋值同时
也把它的 `PerfectSwitch` 和出端口 `Throttle` 一起放进该域（两者都以
`Switch` 作为 Consumer 的 `em`）。

**"quantum 必须小于最小 PIO 延迟"被验证为可行方案（选项 a）**：iobus
（`IOXBar`）跑在板时钟上，`forward_latency=1` 周期=333 tick；把
`SIM_QUANTUM_TICKS=300`（<333）后并行跑越过了 tick 7.08e10 这堵墙，开进
客户机内核（控制台打出 "Linux version 6.8.0..." + e820 内存表，无崩溃，
5 线程 ~265% CPU）。代价符合预期：约 1h17m 只跑到内核控制台早期
（对应 S-004 §9.1 的 ~125x 减速区间）——**只验证了正确性，没有加速**，真正
的加速仍需要"把跨域 classic 边推迟到 barrier"（选项 b，未做）或换个不经过
这条边的拓扑。此 2-level 配置对（`x86_fs_mesi_parallel_eventq.py`）后来被
11.1 起的 3-level 配置对取代，但发现的两条结论（SimplePt2Pt 分域方式、
quantum-below-min-PIO-latency 规则）原样延续到了 3-level 版本
（`SIM_QUANTUM_TICKS=300` 沿用同一个 333-tick 上界）。

### 11.7 三级 pivot 的域布线校验

11.1 给出的域映射（域 0=设备、域 i+1=核+私有 L1/L2、域 5=共享 L3、域
6-7=directory+DRAM）不是随手划的，落地前对照 SLICC 生成的实际连线核对过：

- `L1_i <-> L2_i` 是直连的 `bufferFromL0`/`bufferToL0` `MessageBuffer`
  （域内消息，安全）。
- `L2_i <-> L3`、`L3 <-> directory`、`DMA <-> directory` 都是网络
  `int_link`（走 `LINK_LATENCY`，是 per-consumer 锁机制覆盖的路径）。
- **关键约束：directory j 与它下游的 DRAM `MemCtrl` j 必须同域**——
  directory→DRAM 这条边是 classic port（原始 `EventQueue::schedule`），
  不是 Ruby `int_link`，拆开会重现 11.6 那堵 iobus 墙的同一类问题；
  `get_mem_ports()`/`get_memory_controllers()` 按相同顺序枚举，按下标
  配对即可。
- 拆完之后**唯一剩下的跨域 classic 边**是 CPU-sequencer↔iobus
  （`connectIOPorts`），仍然由 `SIM_QUANTUM_TICKS=300` 卡住（11.6 的
  quantum-below-min-PIO-latency 规则在 3-level 下继续成立）。

### 11.8 LOAD-BEARING：Ruby 检查点与并行重放根本不兼容——为什么必须用 classic 检查点

11.1 说"跨层次检查点"时只交代了"避免 Atomic-on-Ruby 太慢"这一个动机；
实际还有一个**更硬的正确性理由**，是后来才发现的：**带 Ruby 状态的检查点
根本无法在多 EventQueue 下重放**，不是快不快的问题。

用 `x86_fs_mesi3_save_ckpt.py`（在 Ruby 图上存档）产出的检查点做并行重放，
在 **tick 0、`RubySystem::startup()` 内部**（不是运行过程中）就 abort：

```
eventq.hh:759 assert(when >= getCurTick()) failed; Program aborted at tick 0
BaseGlobalEvent::schedule ← GlobalSimLoopExitEvent ← set_max_tick
                          ← simulate() ← RubySystem::startup()
```

**根因**（`RubySystem.cc:435-485` + `unserialize` @403-426）：如果检查点带
Ruby 缓存 trace（`*.ruby_system.cache.gz`），`unserialize` 会置位
`m_warmup_enabled=true`；`startup()` 据此重放这个 trace 的方式是**在它
自己的队列上** `setCurTick(0)`，再嵌套调用一次 `simulate()`。这个嵌套
`simulate()` 会建一个 `GlobalSimLoopExit`（`GlobalEvent`），
`BaseGlobalEvent::schedule` 把它**扇出到全部 N 个 EventQueue**——但其余
7 个队列还停在检查点的 tick（远大于 0），于是 `when(≈0) >=
getCurTick()(巨大)` 失败。Ruby 的 warmup 重放按设计**只支持单队列**，与
多 EventQueue 重放根本不兼容（串行重放同一个检查点没事——只有 1 个队列，
reset 到 0，全局事件也落在 0，天然一致）。

**修法 / 为什么必须在非-Ruby board 上开机存档**：在 **classic NoCache**
上存的检查点（`x86_fs_classic_save_ckpt.py`）**没有** `ruby_system`
这个 section，所以 `RubySystem::unserialize` 根本不会跑，
`m_warmup_enabled` 保持 false，`startup()` 直接跳过重放（只做
`resetStats`）——并行重放就以冷 Ruby 缓存启动，不崩。所以
`x86_fs_classic_save_ckpt.py`（`build/X86/gem5.opt`，NoCache+Atomic，
存档到 `/workspace/gem5-ckpt/x86-threads3-roi-classic`）是并行重放**必须**
的存档路径，不只是"更快的那个"；Ruby 存档脚本
（`x86_fs_mesi3_save_ckpt.py`）只能算串行专用的备选。跨层次重放本身没问题：
检查点只带物理内存 + CPU 架构态 + 设备态（都与缓存层次无关）；缓存从冷启动；
Atomic→Timing 的切换本来就是预期行为；board/内存大小/核数须一致。

（另外顺带校准一个耗时数字：无 KVM、`AtomicSimpleCPU` 的 FS 开机
+ systemd 即使在 classic NoCache 上也是"几小时"量级，不是"几分钟"；
Atomic-on-Ruby 更慢——因为 Ruby 忽略 atomic 访问、每次访存都要走一遍
SLICC。11.1 记的 "5.5 小时" 与此一致，不是偶然的宿主争抢。）

---

**上一篇**：[S-005：宿主机线程绑核](./S-005-host-thread-cpu-affinity.md)
**下一篇**：[S-007：自旋屏障设计与实测 + 项目里程碑](./S-007-spin-barrier-and-milestone.md)
**返回**：[INDEX.md](./INDEX.md)
