# S-012 — 并行 EventQueue 关键路径插桩：谁在阻塞谁（设计，未实现）

**状态：设计完成，未实现，未编译，未跑。** 本文档只做设计，不动代码。是否
按本设计动手实现，需要用户先过一遍再决定（见 §10）——这是一种全新的测量
类型（白盒、按线程打点），跟 S-009/S-010/S-011 一直在用的"改动前后
`stats.txt` 逐字节比对"黑盒方法论完全不同，属于 CLAUDE.md 里"新子阶段、
需要 checkpoint"的情形。

## 1. 这份设计要解决什么问题

[OPEN-ISSUES.md §A1](./OPEN-ISSUES.md) 把"出路 3（每域塞更多工作，摊薄
同步固定成本）"这个方向性问题拆成了三个具体的未知数：

1. 每个 quantum 屏障，哪个域的线程最后到达（谁在关键路径上）？固定
   一个域还是轮换？
2. 每个线程的墙钟时间构成：真正处理事件 vs 自旋等 barrier vs 阻塞在
   跨域锁上，各占多少？
3. 各域每 quantum 的工作量（事件数/耗时）是否严重不均衡？

S-009 §27.6/S-008 §15.4 已经确认出路 1（抬高 Q）单独做只能到 ~0.91x，
够不到项目 >1x 的目标；出路 3 需要和出路 1 一起做，但"往哪个域塞什么样
的工作"这句话在没有关键路径数据之前无法转成具体设计——本文档设计的就是
拿这份数据的插桩方案。

## 2. 现有代码结构复述（设计的地基，均已读代码核实）

### 2.1 quantum 屏障的调用路径

`GlobalSyncEvent::BarrierEvent::process()`（`src/sim/global_event.cc:153`）
是驱动每个 quantum 边界同步的入口，每个域的线程各跑一份：

```cpp
void
GlobalSyncEvent::BarrierEvent::process()
{
    if (globalBarrier()) {          // 第一道屏障
        _globalEvent->process();
    }
    globalBarrier();                // 第二道屏障
    curEventQueue()->handleAsyncInsertions();
}
```

`globalBarrier()`（`src/sim/global_event.hh:93`）释放本域 EventQueue 的
`service_mutex`（避免跨线程死锁）后调用 `_globalEvent->barrier.wait()`。
`BaseGlobalEvent::barrier` 是 `Barrier` 类型（`src/sim/global_event.hh:113`），
构造时传入 `numMainEventQueues`（参与线程数=域数）。

### 2.2 `Barrier::wait()` 的返回值已经是"谁最后到达"的现成信号

`src/base/barrier.hh:130-192`。`wait()` 对**恰好一个**调用者返回
`true`——那个把计数器减到零（cv 模式）或把 `spinLeft` 减到零（spin/hybrid
模式）的调用者，也就是最后到达的那个线程。这个"单一 true 返回"契约本来
就是 `GlobalEvent::process()`/`GlobalSyncEvent::process()` 依赖的机制
（只让一个线程跑真正的全局事件体），不需要新加逻辑去"发现"谁最后到达
——只需要在这个已有的返回点上记一笔。

### 2.3 域拓扑（3-level MESI FS 配置，S-009 §27 验证过的操作点）

`docs/refs/scripts/x86_fs_mesi3_parallel_eventq.py` 的域映射（4 核默认
配置，8 个 EventQueue，注释在文件头 `Domain map` 一节）：

| 域号 | 内容 |
|---|---|
| 0 | I/O：DMA 控制器、所有设备、uncore 剩余部分 |
| 1..N | 核 i 的私有 L1+L2、本地 APIC（i=0..N-1，N 默认 4） |
| N+1 | 共享 L3 控制器+路由器 |
| N+2+j | 目录 j + 其下游 DRAM 控制器 + 路由器（j 从 0 数） |

默认 N=4 时是 8 个域（0..7），S-007/S-009 反复用到的"FS 8-EventQueue"
就是这个配置。这份拓扑注释本身就是"域 1..4（核私有）大概率工作量偏轻、
域 5（共享 L3）和域 6-7（目录+DRAM）大概率工作量偏重"这个直觉的来源——
但这只是直觉，从未用数据验证过，正是本设计要填的空。

### 2.4 域→线程绑定与线程入口

`src/sim/simulate.cc`，`SimulatorThreads::runUntilLocalExit()`
（约 100-122 行）：域 0 由主 Python 线程驱动（在 `simulate.cc` 里直接调
`doSimLoop(mainEventQueue[0])`，约 305 行）；域 1..N-1 各自起一个
`std::thread`，入口是 `thread_main(EventQueue *queue)`（约 202 行），
绑核（`pinThread`/`pinSelf`）在 S-005 已经做过。**域→线程是终生绑定**
（S-005 结论，S-011 §5 也依赖了这个前提）——这意味着"域号"可以在线程
入口处确定一次、之后整个运行期间不变，不需要每次打点都重新查。

`thread_main` 当前签名只接收 `EventQueue *queue`，没有数值域号——各线程
自己的域号目前只能从 `queue` 反查 `mainEventQueue` 数组下标得到。§5.3 给
出改法。

### 2.5 现有跨域锁全部基于同一个 `UncontendedMutex` 类型

`src/base/uncontended_mutex.hh`。`lock()` 的结构（已读源码，逐行核实）：

```cpp
void lock() {
    while (!testAndSet(0, 1)) {   // 第一次调用成功 == 无竞争快路径
        std::unique_lock<std::mutex> ul(m);
        if (flag++ == 0)
            break;
        cv.wait(ul);               // 竞争慢路径：真等待发生在这里
    }
}
```

`while` 条件里第一次 `testAndSet(0,1)` 成功（`flag` 原来是 0）就直接
返回，全程没有真正阻塞——这是绝大多数调用的情况（无竞争）。只有第一次
失败才会进入循环体，走 `std::mutex`+`cv` 的真实等待路径。**这个快/慢
路径的区分本身就是插桩要不要计时的天然分界线**：给快路径加时间戳测量
会污染这个 fork 一直很在意的热路径开销（C2，`docs/specs/OPEN-ISSUES.md`），
但慢路径本来就已经在付futex 睡眠/唤醒的代价，加两次 `steady_clock::now()`
在这个量级上可以忽略。

当前四个跟"关键路径"直接相关的实例，全部是无参数默认构造
（核实过声明处）：

| 锁 | 声明位置 | 覆盖范围 |
|---|---|---|
| `BaseXBar::layerLock` | `src/mem/xbar.hh:338` | 跨域 classic Port（IOXBar 等）（S-009 §24） |
| `PioDevice::pioLock` | `src/dev/io_device.hh:122` | 跨域 PIO；**PIT/RTC/IDE 的 `crossDomainLock` 指针实际指向同一个实例**（`&_parent->getPioLock()`，`src/dev/x86/i8254.hh:60`、`src/dev/x86/cmos.hh:88`），不需要单独打标签 |
| `AddrRangeMap::cacheLock` | `src/base/addr_range_map.hh:366` | `PhysicalMemory::addrMap`（每次访存）+ `BaseXBar::portMap`（每次经典 Port 请求）两个跨域实例（S-010 §7），同一个模板类还被 RISC-V/GPU-compute 等域私有场景复用（S-010 §7 表格），后者不需要打标签 |
| `Consumer::m_wakeup_mutex` | `src/mem/ruby/common/Consumer.hh:155` | 每个 controller 一个实例（S-011），跨域 wakeup 路径 |

### 2.6 `simQuantumStart` 是运行期常量——不能当按 quantum 变化的键用

`simQuantumStart`（`src/sim/eventq.cc:50`，`extern Tick simQuantumStart`
声明于 `eventq.hh:79`）是 quantum 网格的**锚点**：只在进入并行模式时
写一次（`simulate.cc:295`，`simQuantumStart = curTick();`），此后整个
并行运行期间不变。S-009 §22 论证它可以被跨域线程安全读取的根据**恰恰
就是这个"运行期间不变"**——grid-anchored snap 的 `crossDomainSnap()`
把它当常量锚点用，不是当"当前 quantum 的起点"用。

这意味着它**不能**充当"这条记录属于哪个 quantum"的分组键（本设计初稿
曾这么设计，评审时发现是错的——按一个运行期常量分组会把整个运行的所有
记录归进同一组）。正确的关联键见 §3.4，完全不需要新的跨线程读取。

### 2.7 Python 侧参数已有先例可循

`eventq_barrier_mode`/`eventq_host_cpus` 走的是标准 SimObject 参数管线：
`src/sim/Root.py` 声明 `Param`，`src/sim/root.cc` 的 `Root` 构造函数读
`p.eventq_barrier_mode` 设 C++ 全局（`root.cc:194-202`），驱动脚本
（`docs/refs/scripts/x86_fs_mesi3_parallel_eventq.py:83`）从环境变量
`EVENTQ_BARRIER_MODE` 读值、赋给 `root.eventq_barrier_mode`。§5.6 的开关
沿用同一条路径，不发明新机制。

## 3. 总体设计：两类打点 + 一个可选的第三类 + 一个关联键

### 3.1 打点类型一：quantum 屏障到达/离开（回答未知数 1、部分回答 3）

在 `Barrier::wait()`（`src/base/barrier.hh`）里，记录：
- 进入 `wait()` 的时刻 `tEnter`
- 是否是最后到达者（已有的返回值，无需新算）
- 如果不是最后到达者：离开 `wait()`（即被释放）的时刻 `tExit`；
  `tExit - tEnter` 就是这个域在这一次屏障上被阻塞的时长
- 如果是最后到达者：`tEnter` 本身就有意义——它是"这个域跑完这个
  quantum 的工作、到达屏障"的时刻，跟其他域的 `tEnter` 一比较，最大值
  与最小值之差就是这个 quantum 里各域工作量的时间差（直接回答未知数 3
  的"是否不均衡"，不需要单独再测事件数）

一次 quantum 边界会调用两次 `globalBarrier()`（S-009 结构，见 §2.1）——
两次都要打点，用调用顺序位（第一道/第二道）区分，因为第一道屏障前是
"跑完这个 quantum 的域内事件"，第二道屏障前是"跑 `_globalEvent->process()`
本体"（只有域 0 跑），语义不同，混在一起会让"域 X 是不是关键路径"的
结论失真。

### 3.2 打点类型二：跨域锁等待（回答未知数 2）

只在 `UncontendedMutex::lock()` 的**慢路径**（§2.5 已经说明为什么只打
这条路径）记录：
- 锁的标签（`LayerLock`/`PioLock`/`CacheLock`/`ConsumerLock`，§2.5 表格）
- 进入慢路径前的时刻、拿到锁的时刻，二者之差就是这次调用的阻塞时长

### 3.3 打点类型三（可选，强烈建议一起做）：每域每 quantum 处理的
事件数

单靠类型一只能看到"域 X 最后到达、耗时比别人长"，看不出这是因为
"域 X 这个 quantum 真的分到了更多事件"还是"域 X 卡在类型二的某把锁上
动弹不得"。两者对应的出路 3 方案完全不同（前者是重新切分拓扑/负载
均衡，后者是先去解决锁竞争、跟"塞更多工作"关系不大）。加一个
`thread_local` 计数器，在 `EventQueue` 每次真正执行一个事件的地方
自增（候选挂载点：`EventQueue::serviceOne()`，需要在设计定稿、准备
实现时读一遍这个函数当前实现确认具体自增位置），每次类型一的"到达
屏障"打点时把计数器的值一起记下、然后清零。

### 3.4 关联键：quantum 边界 tick（修订——初稿误用了 `simQuantumStart`）

三类打点各自发生在不同域的线程上，要拼成"同一个 quantum 里各域分别
发生了什么"，需要一个各域读到的值确实对应同一个 quantum 的键。初稿
用的 `simQuantumStart` 是运行期常量（§2.6），做不了这件事。修订后的
键不引入任何新的跨线程读取：

- **类型一（屏障）记录**：打点时记录本线程自己队列的
  `curEventQueue()->getCurTick()`。每个域的 `BarrierEvent` 被
  `BaseGlobalEvent::schedule()/reschedule()` 调度在同一个 `when` 上
  （`src/sim/global_event.cc:100-113`，所有 `barrierEvent[i]` 同一个
  tick），所以同一个 quantum 边界上各域读到的这个值相同——这就是跨域
  join 键，而且读的是线程自己的队列，纯线程本地。同一个边界的第一道/
  第二道屏障 tick 相同，靠 §3.1 已有的顺序位区分。
- **类型二（锁等待）记录**：同样记录本线程自己的
  `curEventQueue()->getCurTick()`（`curEventQueue()` 是线程本地的，
  S-011 已把它用作域身份 idiom）。离线聚合时按同一个域自己的类型一
  记录时间线，把锁等待分桶到相邻两次屏障之间——纯域内排序操作，对
  `simulate()` 被 Python 侧多次进入、`simQuantumStart` 每次重新锚定
  （§2.6）的情况天然稳健，不需要做"减锚点除以 Q"这类折算。

## 4. 具体改动点

### 4.1 `UncontendedMutex` 加标签 + 慢路径计时

在 `src/base/uncontended_mutex.hh` 给 `UncontendedMutex` 加一个默认为
"不追踪"的标签，行为在标签为默认值时和现在完全一样（多一次标签比较，
会被编译器和现有分支合并，可忽略）：

```cpp
enum class CritPathLockTag : uint8_t
{
    None = 0, LayerLock, PioLock, CacheLock, ConsumerLock
};

class UncontendedMutex
{
    ...
    const CritPathLockTag tag;
  public:
    explicit UncontendedMutex(CritPathLockTag t = CritPathLockTag::None)
        : flag(0), tag(t) {}

    void lock() {
        if (testAndSet(0, 1))
            return;                      // 快路径：跟原来完全一样
        const bool tracing =
            tag != CritPathLockTag::None && critPathTracing();
        const auto t0 = tracing ? critPathNow()
                                 : CritPathClock::time_point{};
        do {
            std::unique_lock<std::mutex> ul(m);
            if (flag++ == 0)
                break;
            cv.wait(ul);
        } while (!testAndSet(0, 1));
        if (tracing)
            critPathRecordLockWait(tag, critPathNow() - t0);
    }
    ...
};
```

`tag != None` 这个条件不是可有可无的优化：`EventQueue::service_mutex`
（不打标签，§6 明确排除）的慢路径在每次跨线程 async 调度时都会走到，
不加这个条件的话，开着追踪跑就会把这条被排除的路径也计时+记录——既
往缓冲区里灌无用记录，又给一条不在测量范围内的热路径加了时间戳开销。

（`unlock()` 不需要改——关键路径要测的是"等锁等了多久"，不是"持锁
多久"；持锁时长不是这个实验要回答的问题，加了反而多一处热路径开销。）

`do { } while` 结构跟原来的 `while (!testAndSet())` 在控制流上等价
（原来第一次 `testAndSet` 已经在 `if` 里消费掉，`do-while` 接着走第二
次开始的同一段逻辑），是纯粹的行为保持重构，不是新逻辑——这一点在
实现时要专门写一条"改动前后单线程 `stats.txt` 逐字节相同"的验证
（沿用 S-009/S-010/S-011 一直在用的方法论），确认重构本身没有引入
任何行为变化。

四个已知跨域锁改成显式打标签的默认构造（其余所有其它
`UncontendedMutex` 实例，包括 `EventQueue::service_mutex`/
`async_queue_mutex`、RISC-V/GPU-compute 用到的 `AddrRangeMap` 实例，
维持默认 `None`，完全不受影响）：

```cpp
// src/mem/xbar.hh
mutable UncontendedMutex layerLock{CritPathLockTag::LayerLock};

// src/dev/io_device.hh
mutable UncontendedMutex pioLock{CritPathLockTag::PioLock};

// src/mem/ruby/common/Consumer.hh
UncontendedMutex m_wakeup_mutex{CritPathLockTag::ConsumerLock};
```

`AddrRangeMap::cacheLock` 需要多一步：这个类是模板、被域私有和跨域
两种场景共用（S-010 §7），不能整体默认打标签。给 `AddrRangeMap` 的
构造函数加一个默认 `CritPathLockTag::None` 的参数，透传给
`cacheLock`；只有 `PhysicalMemory::addrMap`（`src/mem/physical.hh`，
需要在实现阶段确认具体成员声明行）和 `BaseXBar::portMap`
（`src/mem/xbar.hh`）这两处构造时显式传 `CacheLock`。

### 4.2 `Barrier::wait()` 计时

`src/base/barrier.hh` 的 `wait()`/`waitCv()`/`waitSpin()` 各自的入口/
出口打点。因为 `wait()` 已经区分 cv/spin/hybrid 三种机制，打点要放在
`wait()` 这一层的公共包装上（进入时记 `tEnter`，拿到 `waitCv()`/
`waitSpin()` 的返回值后记 `tExit`+`isLast`），不要分别改三个私有方法
——否则三条路径的计时口径可能不小心不一致。

`Barrier` 目前不知道调用者是哪个域、这是第几道屏障（§3.1 的"第一道/
第二道"），这两个身份信息需要从调用方（`globalBarrier()`，
`src/sim/global_event.hh:93`）传进来，而不是让 `Barrier` 自己猜——
`Barrier` 类本身是通用同步原语（`SimulatorThreads` 也用了一个独立的
`Barrier` 实例，那个不是本设计要测的对象，见 §6 排除范围），不应该
知道"域"或"quantum"这些上层概念。设计成 `wait()` 接受一个可选的
打点上下文参数（默认空，不追踪时零开销），由 `globalBarrier()`
构造并传入。注意 `globalBarrier()` 自己也拿不到"第几道"这个信息
（它只是被同一个 `process()` 调了两次的同一个函数）——它需要加一个
参数，由 `GlobalSyncEvent::BarrierEvent::process()` 的两个调用点
分别传第一道/第二道；域号则不必传参，直接读 §4.3 的线程本地
`critPathDomainId` 即可。

### 4.3 域号线程本地变量

`src/sim/simulate.cc`：
- `thread_main(EventQueue *queue)` 改成 `thread_main(EventQueue *queue,
  uint32_t domainId)`，入口设 `critPathDomainId = domainId;`
  （`thread_local uint32_t`，新变量，建议放在 `src/base/critpath_trace.hh`
  统一管理）。
- 生成子线程的 lambda（约 111-114 行）已经在循环里持有 `i`，顺手多传
  一个参数即可：`threads.emplace_back([this](EventQueue *eq, uint32_t
  domainId) { thread_main(eq, domainId); }, mainEventQueue[i], i);`
- 主线程（域 0）：在 `simulate()` 调 `doSimLoop(mainEventQueue[0])`
  之前（约 305 行）设一次 `critPathDomainId = 0;`。

### 4.4 每域缓冲区与落盘

- 每个域线程私有一个 `std::vector<CritPathRecord>`（域内单线程写、
  无需加锁——这正是这份插桩要避免"为了测量而引入新竞争"的关键设计
  选择）。
- 预留容量而不是让它自然增长：`std::vector::push_back` 偶尔触发的
  重新分配+拷贝本身会在被测的时间线里制造一次不该有的抖动。用
  `reserve()`，容量按实验预算的 quantum 数估算（例如 `MAX_TICKS /
  SIM_QUANTUM_TICKS`），通过 Root 参数或环境变量传入，允许留一点余量、
  超出后退化为正常 vector 增长（接受偶发抖动，好过实验完全跑不完）。
- 落盘时机：域 0 在 `simulate()` 返回前，其余域在 `thread_main` 的
  `while (!terminate)` 循环结束、线程退出前——把 `vector` 一次性
  `fwrite`/写 CSV 到 `<outdir>/critpath-domain<N>.csv`，这一步在计时
  区间之外，不影响被测数据。注意 `simulate()` 可能被 Python 侧多次
  进入（子线程只在 `terminateThreads()` 时退出一次，但域 0 的
  `simulate()` 每次退出事件都会返回一次）——域 0 的落盘要么用追加写
  且不清缓冲区以外的状态，要么干脆挪到和子线程对称的进程级退出点
  （如 `atexit`）做一次；实现时二选一并写明，不要隐式截断重写。

### 4.5 全局开关（沿用 §2.7 的既有参数管线）

`src/sim/Root.py` 加 `critpath_trace = Param.Bool(False, "...")`；
`src/sim/root.cc` 的 `Root` 构造函数里 `g_critPathTraceEnabled =
p.critpath_trace;`；驱动脚本仿照 `EVENTQ_BARRIER_MODE` 的写法从环境变量
`EVENTQ_CRITPATH_TRACE` 读值赋给 `root.critpath_trace`。

## 5. 为什么不会污染已经验证过的结果

- 关闭时（`critpath_trace=False`，默认）：`UncontendedMutex` 快路径
  完全不变；慢路径多一次 `bool` 判断，编译器可预测；`Barrier::wait()`
  同理。`thread_local` 域号变量的写入只发生在线程入口一次，不在热
  循环里。**不改变任何已有的 stats.txt 结果**，但这句话本身在实现
  阶段需要专门验证（§4.1 已经提到那条"重构本身"的验证），不能只凭
  这里的论证就当作已确认。
- 打开时：这是一次专门的诊断实验，不用于任何"官方"性能数字——跟
  S-009 §27.5 把 TSan 验证和性能 A/B 明确分成两件独立的事是同一个
  原则。开着 `critpath_trace` 跑出来的 `hostSeconds` 不能拿来跟
  S-009 §27.4 的 0.91x 比较。

## 6. 明确排除的范围

- `SimulatorThreads::barrier`（`src/sim/simulate.cc`）：这是"整个仿真
  循环开始/结束"的外层屏障，每次 `runUntilLocalExit()` 调用触发一次，
  不是每 quantum 触发一次，跟本设计要测的 quantum 屏颈完全是两个不同
  粒度的对象，不在本设计范围内。
- `EventQueue::service_mutex`/`async_queue_mutex`：这两个是"域自己
  服务事件时的自锁"，绝大多数调用是本域线程自己无竞争地锁/解锁，语义
  上和"跨域阻塞"不是一回事（虽然底层同样是 `UncontendedMutex`，但默认
  不打标签，§4.1），排除在外。
- `GlobalEvent::BarrierEvent::process()`（只让域 0 跑 `process()` 那条
  路径，`src/sim/global_event.cc:129`）：FS 定期 quantum 同步用的是
  `GlobalSyncEvent`（S-009/S-007 的所有 A/B 都是这条路径），`GlobalEvent`
  是另一个更少用的机制，本设计不覆盖，需要的话是独立的后续工作。
- 不设计"实时可视化"或"运行中动态开关"——本实验是离线跑一次、事后
  聚合分析，不需要这些复杂度。

## 7. 建议的实验协议（供实现后使用，本节仍是设计不是结果）

- 复用 S-009 §27 已经验证过的操作点：`build/X86_MESI_Three_Level`
  （加上本设计的插桩，需要单独一次编译）、检查点
  `x86-threads3-roi-classic`、`SIM_QUANTUM_TICKS=6660`、
  `EVENTQ_BARRIER_MODE=spin`、隔离核 92-99（S-009 §27.3 的确切配置）。
  用同一个操作点是为了让关键路径分析直接解释"这个已经测过 0.91x 的
  运行，时间到底花在哪"，而不是另起一个没有性能基准可对照的新配置。
- 窗口先用 S-009 §27.3 的 `MAX_TICKS=2e8`（Q=6660 那一档，约几秒
  `hostSeconds`）小规模验证插桩本身工作正常、`stats.txt` 逐字节不变，
  再考虑要不要扩到更大窗口拿更稳的统计量。
- 离线聚合（新脚本，Python，不在本设计范围内写出，只列产出）：
  - 按 quantum 边界 tick（§3.4）分组，每个 quantum 输出"哪个域最后
    到达 + 到达时间的跨域极差"，整个运行汇总成"域 X 是最后到达者的
    quantum 占比"直方图 → 直接回答未知数 1。
  - 按域、按锁标签汇总类型二记录的总等待时长/次数，归一化到该域的
    `hostSeconds` → 回答未知数 2。
  - 如果做了类型三：按域汇总每 quantum 处理事件数的分布，和类型一的
    "最后到达"标记做交叉 → 回答未知数 3，并把"最后到达是因为真的
    工作多"和"最后到达是因为卡锁"两种情况分开。

## 8. 已知局限（如实记录，不在本轮设计范围内解决）

- 类型三的具体挂载点（`EventQueue::serviceOne()`还是别的函数）需要
  在实现阶段重新读一遍当前代码确认，本设计只给出候选，没有钉死行号
  ——避免在没有编译验证的情况下给出可能过时的精确代码位置。
- `PhysicalMemory::addrMap` 的具体声明行号本设计同样没有钉死，理由
  相同。
- 本设计没有覆盖"域内"（同一线程自己排队/自己处理事件）的耗时构成
  ——只测跨域相关的三类打点。如果关键路径分析显示某个域整体耗时长、
  但既不卡锁也不是屏障等待的受害者（即它自己确实在埋头处理大量域内
  事件），要进一步细分"这些事件具体是什么类型、能不能减少"，是本设计
  之外的下一层问题。
- 没有设计"多次运行取统计量/置信区间"的协议——关键路径数据本质上是
  一次运行的时间线，由于 S-011 §10.5 记录的 serial/spin 在非 TSan
  大窗口下的既有分歧（`docs/specs/OPEN-ISSUES.md` B2）这类已知的
  非确定性，同一配置多次跑之间的关键路径细节可能不完全一致
  ——这份数据应该当作"这一次运行大致是什么样子"来读，不是当作确定性
  的架构保证；如果后续要用这份数据做定量决策，需要另外设计多次运行的
  统计口径。

## 9. 对现有验证方法论的影响

新增的代码路径（`UncontendedMutex` 的构造函数签名变化、`Barrier::wait()`
签名变化）会触达 S-009/S-010/S-011 已经用 TSan+正确性 A/B 验证过的
文件（`xbar.hh`、`io_device.hh`、`addr_range_map.hh`、`Consumer.hh`）
——哪怕关闭状态下行为不变（§5），落地实现后应该重新跑一遍这几处已有
的 TSan 扩时长 A/B（沿用 S-009 §24.5/S-010 §11/S-011 §10 的方法），
确认插桩本身（关闭状态）没有引入新的竞争或者改变既有报告数量，而不是
假设"设计上不改变行为"就等于"验证过不改变行为"。

## 10. 下一步：需要 checkpoint

本文档到这里是完整设计，**没有写一行实现代码**。CLAUDE.md 明确要求
"即使已经有整体授权，开始一个质变更冒险的新子阶段前也要停下来跟用户
过一遍"——这份插桩是本项目第一次做白盒、按线程打点的测量，性质上和
之前的黑盒 stats.txt A/B 不同，实现过程大概率需要现场调试（时间戳
粒度是否够用、缓冲区容量估算是否现实、落盘格式是否好解析等，都要见
了实际数据才知道），符合"应该先 checkpoint"的标准。如果用户同意按
本设计实现，建议的顺序是：先落地 §4.1-4.5 的改动、跑一次§9 的 TSan+
正确性回归确认插桩关闭时行为不变，再打开插桩按 §7 协议跑一次小窗口
验证插桩本身产出合理的数据，最后才是扩大窗口做正式的关键路径分析。

## 11. 实现记录：Step 1（脚手架，本节由实现会话补充）

用户过了一遍本设计后，确认按 5 步实现（不是一次性全做）：

1. **Step 1**（本节记录的就是这一步）——脚手架，默认关闭：新增
   `critpath_trace.{hh,cc}`（枚举、时钟别名、记录结构体、线程本地域号
   +缓冲区、落盘函数骨架）、`Root.py`/`root.cc` 的 `critpath_trace`
   开关、驱动脚本 `EVENTQ_CRITPATH_TRACE`、`simulate.cc` 的域号线程本地
   变量接线（§4.3）。
2. Step 2——类型一+类型三：`Barrier::wait()` 计时、`globalBarrier()`
   第一道/第二道参数、`serviceOne()` 事件计数器。
3. Step 3——第一次打开插桩跑小窗口（真正的 checkpoint 所在）：验证
   缓冲区容量估算、时间戳粒度、CSV 格式是否可用，产出"谁最后到达
   屏障"的第一份直方图。
4. Step 4——类型二：`UncontendedMutex` 打标签+慢路径计时、
   `AddrRangeMap` 构造函数传参。
5. Step 5——§9 的 TSan+正确性回归（覆盖全部改动）+ 正式关键路径分析
   跑，外加 2-3 次重复跑评估 §8 提到的跑间非确定性对直方图稳定性的
   影响。

用户明确要求**本次会话只做 Step 1**，Step 2-5 留给后续会话（各自
独立 checkpoint）。

### 11.1 Step 1 改动内容

跟 §4 设计的对应关系：

- `src/base/critpath_trace.hh`/`.cc`（新文件，`src/base/SConscript`
  加一行 `Source('critpath_trace.cc')`）：`CritPathLockTag`/
  `CritPathRecordKind` 枚举、`CritPathClock`（`std::chrono::steady_clock`
  别名）、`CritPathRecord` 结构体、`g_critPathTraceEnabled`（全局开关，
  §4.5）、`critPathDomainId`/`critPathBuffer`（线程本地，§4.3/§4.4）、
  `critPathFlush()`（把当前线程的 `critPathBuffer` 写成
  `<outdir>/critpath-domain<N>.csv`，用 `simout.create()`）。
  **`critPathFlush()` 在这一步还没有被任何生产代码调用**——Step 1
  的范围只到"骨架存在"，真正产生记录、需要在线程退出点调用它，是
  Step 2/3 的事（届时缓冲区才会非空）。
- `src/sim/Root.py`：新增 `critpath_trace = Param.Bool(False, ...)`。
- `src/sim/root.cc`：`Root::Root()` 里加
  `g_critPathTraceEnabled = p.critpath_trace;`。
- `docs/refs/scripts/x86_fs_mesi3_parallel_eventq.py`：新增
  `EVENTQ_CRITPATH_TRACE`（环境变量读法照抄 `EVENTQ_BARRIER_MODE` 的
  先例，§2.7），赋给 `root.critpath_trace`，仅在 `PARALLEL_EVENTQ`
  分支内设置（跟 `eventq_barrier_mode` 等其它旋钮同一个位置）。
- `src/sim/simulate.cc`：`thread_main` 签名加 `uint32_t domainId` 参数，
  入口设 `critPathDomainId = domainId;`；子线程 lambda 多传一个 `i`；
  主线程在 `doSimLoop(mainEventQueue[0])` 前设
  `critPathDomainId = 0;`。

### 11.2 验证结果

按 §7 协议的操作点（`build/X86_MESI_Three_Level`、检查点
`x86-threads3-roi-classic`、`MAX_TICKS=2e8`）：build 成功（无警告，
除环境已知的 tcmalloc/png/hdf5/capstone 提示）。

关闭状态（`critpath_trace` 默认 `False`，两个 binary 都没有设
`EVENTQ_CRITPATH_TRACE`）下，改动前（stash 到 `8d96a95f9e`）与改动后
的两个 binary 分别跑：

- 串行臂：`taskset -c 54`，无 `PARALLEL_EVENTQ`。
- 并行臂：`taskset -c 92`，`PARALLEL_EVENTQ=1
  HOST_PIN_CPUS=92,93,94,95,96,97,98,99 EVENTQ_BARRIER_MODE=spin
  SIM_QUANTUM_TICKS=6660`（S-009 §27.3 的确切配置）。

结果：两组（串行/并行）`stats.txt`（排除 `host*` 字段）改动前后
**逐字节相同**；`simInsts`=74062，两组一致；两组日志里都没有
`assert`/`panic`/`abort`/段错误关键字。这确认了 §5 的论证（关闭时不
改变任何已验证结果）在这一步的具体改动上成立，不是只凭代码走查就
采信。

**已知局限（如实记录）**：这次验证只覆盖了 Step 1 本身引入的改动
（脚手架 + 线程入口的一次性域号写入），还没有做 §9 要求的 TSan
回归——因为 Step 1 还没有触达 §9 列的四个文件（`xbar.hh`/
`io_device.hh`/`addr_range_map.hh`/`Consumer.hh`），那次回归应该等
Step 4（真正给这些文件打标签）之后再做一次，覆盖全部改动，跟 §9
的意图一致（"落地实现后应该重新跑一遍"，指的是全部落地之后）。

## 12. 实现记录：Step 2（类型一+类型三，本节由实现会话补充）

按 §11 的 5 步计划，本节记录 Step 2——barrier 计时 + 事件计数器，
仍然默认关闭。

### 12.1 Step 2 改动内容

跟 §3.1/§3.3/§4.2 设计的对应关系：

- `src/base/critpath_trace.hh`/`.cc`：给 `CritPathRecord` 加
  `eventCount` 字段（§3.3 要求"把计数器的值一起记下"，Step 1 定的
  结构体里漏了这个字段，Step 2 补上）；新增 `CritPathBarrierCtx`
  结构体（`tick`+`pass`，§3.4/§4.2 的关联键+顺序位）；新增线程本地
  `critPathEventCount` 计数器 和 `critPathCountEvent()`（关闭时只有
  一次可预测的 `bool` 判断）；新增 `critPathRecordBarrierPass()`
  （写一条 `BarrierPass` 记录，读并清零 `critPathEventCount`，即
  §3.3 说的"记下、然后清零"）。CSV 表头加一列 `eventCount`。
- `src/base/barrier.hh`：`wait()` 签名改成
  `wait(const CritPathBarrierCtx *ctx = nullptr)`——`ctx` 为空或
  追踪关闭时走原来完全不变的路径；非空且追踪打开时记 `tEnter`、调用
  原有的 `waitCv()`/`waitSpin()`、根据返回值算 `dur`（`isLast` 为
  `true` 时是 0，按设计 §3.1）、调 `critPathRecordBarrierPass()`。
  `SimulatorThreads::barrier`（`src/sim/simulate.cc`，§6 明确排除）
  的四处 `wait()` 调用没有改，全部走 `ctx=nullptr` 的旧路径。
- `src/sim/global_event.hh`：`globalBarrier()` 加
  `uint8_t pass = 0` 参数——`pass == 0`（默认）或追踪关闭时走原来的
  `barrier.wait()`；否则用本域自己的
  `curEventQueue()->getCurTick()`（§3.4，线程本地读取，没有引入新的
  跨线程读）构造 `CritPathBarrierCtx` 传给 `barrier.wait(&ctx)`。
- `src/sim/global_event.cc`：只改了
  `GlobalSyncEvent::BarrierEvent::process()` 的两处调用点——第一道
  传 `globalBarrier(1)`，第二道传 `globalBarrier(2)`。
  `GlobalEvent::BarrierEvent::process()` 的两处调用点**没有改**（保持
  `globalBarrier()` 默认参数 `0` = 不追踪），对应 §6 明确排除
  `GlobalEvent` 这条路径的设计决定——不是遗漏。
- `src/sim/eventq.cc`：`EventQueue::serviceOne()` 里，`event->process()`
  调用之后、`isExitEvent()` 判断之前加一行 `critPathCountEvent();`，
  挂在"事件确实没被 squash、真的执行了 `process()`"这个分支里（§3.3
  "每次真正执行一个事件"对应的就是这个分支，squash 分支不计数）。

### 12.2 验证结果

沿用 Step 1 一样的方法（§11.2），操作点不变：
`build/X86_MESI_Three_Level`、检查点 `x86-threads3-roi-classic`、
`MAX_TICKS=2e8`。

Build：干净编译通过（无警告，除环境已知的 tcmalloc/png/hdf5/capstone
提示）。

关闭状态（`critpath_trace` 默认 `False`，两个 binary 都没有设
`EVENTQ_CRITPATH_TRACE`）下，改动前（Step 1 完成点 `eef74976dd`）与
改动后的两个 binary 分别跑：

- 串行臂：`taskset -c 54`，无 `PARALLEL_EVENTQ`。
- 并行臂：`taskset -c 92`，`PARALLEL_EVENTQ=1
  HOST_PIN_CPUS=92,93,94,95,96,97,98,99 EVENTQ_BARRIER_MODE=spin
  SIM_QUANTUM_TICKS=6660`（S-009 §27.3 的确切配置）。

结果：两组（串行/并行）`stats.txt`（排除 `host*` 字段）改动前后
**逐字节相同**；`simInsts`=74062，四次跑（串行×2、并行×2）全部一致；
四份日志里都没有 `assert`/`panic`/`abort`/段错误关键字。

额外做了一次不在 §11 验证协议要求范围内、但成本很低的冒烟检查：
打开 `EVENTQ_CRITPATH_TRACE=1` 跑并行臂一次小窗口（`MAX_TICKS=2e7`），
确认追踪打开时程序照常跑完、无崩溃。**没有产出 `critpath-domain*.csv`
文件**——这是预期行为，不是 bug：`critPathFlush()` 在 Step 1 就没有
被任何生产代码调用（§11.1 已注明），本 Step 2 的范围只到"往
`critPathBuffer` 里追加记录"，接线程退出点调用 `critPathFlush()` 落盘
是 Step 3 的事（§11 步骤划分）——Step 3 本来就是"第一次打开插桩跑小窗口
"、"真正的 checkpoint 所在"，所以本 Step 2 会话不提前做这一步。

**已知局限（如实记录）**：跟 Step 1 一样，这次验证没有覆盖 §9 要求的
TSan 回归——Step 2 改的 `barrier.hh`/`global_event.hh`/
`global_event.cc`/`eventq.cc` 都不在 §9 列出的四个跨域锁文件
（`xbar.hh`/`io_device.hh`/`addr_range_map.hh`/`Consumer.hh`）里，那次
回归仍然留到 Step 4 之后一次性做（§11.2 定的计划不变）。另外，因为
`critPathFlush()` 还没接线程退出点，本 Step 2 没有办法验证
`critPathBuffer` 里实际记录内容是否正确（时间戳、`eventCount`、
`isLast` 语义等）——这些正确性问题要等 Step 3 能读到 CSV 之后才能真正
验证，本节的"验证"只覆盖"关闭时行为不变"+"打开时不崩溃"两件事。

## 13. 实现记录：Step 3（第一次打开插桩跑小窗口，真正的 checkpoint）

按 §11 的 5 步计划，本节记录 Step 3——把 `critPathFlush()` 接到线程
退出点、跑第一次真正打开插桩的小窗口、写聚合脚本 v1、产出第一份
"谁最后到达屏障"直方图。

### 13.1 落盘接线

`src/sim/simulate.cc`：

- 子线程（`thread_main`）：`while (!terminate)` 循环结束、函数返回前调
  `critPathFlush()`（§4.4 设计的两个落盘时机之一，缓冲区空则是
  no-op）。
- 域 0：没有用"追加写"，选了 §4.4 提到的另一条路——`atexit`。
  `simulate()` 第一次创建 `simulatorThreads` 时注册一次
  `std::atexit(critPathFlush)`，跟子线程"线程退出时落盘一次"对称。选
  这条不选追加写的理由：追加写需要域 0 自己判断"这是不是最后一次
  `simulate()` 调用"，`atexit` 不需要这个判断，天然只跑一次，实现更
  简单也更不容易在多次 `simulate()` 调用之间出错。

### 13.2 验证：关闭状态 stats.txt 逐字节相同

沿用 Step 1/2 一样的方法（§11.2/§12.2），操作点不变：
`build/X86_MESI_Three_Level`、检查点 `x86-threads3-roi-classic`、
`MAX_TICKS=2e8`、`SIM_QUANTUM_TICKS=6660`、`EVENTQ_BARRIER_MODE=spin`、
隔离核 92-99（并行臂 `taskset -c 92`，串行臂 `taskset -c 54`）。

对照组是 Step 2 完成点（`149b830e8f`，本 Step 3 改动之前）编译的
binary。两个 binary 都没有设 `EVENTQ_CRITPATH_TRACE`：

- 串行臂、并行臂的 `stats.txt`（排除 `host*` 字段）改动前后**逐字节
  相同**；`simInsts`=74062，四次跑（串行×2、并行×2）全部一致。
- 两组日志里都没有 `assert`/`panic`/`abort`/段错误关键字。

这确认了新增的 `critPathFlush()` 调用点（子线程循环尾部、域 0 的
`atexit`）在关闭状态下不改变任何已验证结果，跟 §5 的论证一致。

### 13.3 第一次打开插桩：缓冲区/时钟粒度/CSV 格式，三个未知数全部确认

协议：`EVENTQ_CRITPATH_TRACE=1`，其余参数不变（Q=6660、spin、
`MAX_TICKS=2e8`、隔离核 92-99、`x86-threads3-roi-classic`）。跑完
无崩溃，退出码 0，8 个域各自产出一份
`<outdir>/critpath-domain<N>.csv`。

**缓冲区估算**：§7 预估约 3 万 quanta、48 万条记录。实测每域
30030 个 quantum 边界（`tick` 唯一值数量）× 2 道屏障 = 60060 条
`BarrierPass` 记录/域，8 域合计 480480 条——跟预估几乎完全吻合。
**但**：走查代码确认 §4.4 设计里说的"用 `reserve()` 预留容量、避免
`push_back` 触发的重新分配污染被测时间线"这一步，Step 1/2 都没有做
（`critpath_trace.cc`/`.hh` 里没有任何 `reserve` 调用，`Root.py`/
`root.cc` 也没有传缓冲区容量预算的参数）——这是本轮之前遗留的一个
设计-实现落差，如实记录：这次 3 万 quanta 的窗口没有出现任何异常
（无崩溃、无长尾极端值），说明默认 vector 增长策略在这个规模下代价
可以接受，但没有专门验证"重新分配本身有没有在时间线里制造一次不该有
的抖动"（§4.4 原文承认的"偶发抖动，好过实验完全跑不完"）。**留给
后续（Step 5 或更早）补上 `reserve()`，尤其是要扩大窗口做正式分析
之前。**

**时钟粒度**：`dur_ns` 列（`steady_clock` 差值）实测最小非零值在
334ns 量级（跨 8 个域取的最小值），相对于典型屏障阻塞时长（中位数
量级是微秒级，见下）粒度足够细，没有出现"抖动小于时钟精度、量不出来"
的问题。`isLast=1` 的行 `dur_ns` 恒为 0（设计 §3.1 的定义,不是缺
数据）。

**CSV 可解析性**：表头
`kind,tick,domainId,barrierPass,isLast,eventCount,dur_ns,lockTag`
跟 `critpath_trace.cc` 里 `critPathFlush()` 实际写的完全一致，标准
CSV、没有转义/引号问题，`csv.DictReader` 直接读没有任何特殊处理。
`lockTag` 列本次全部是 `0`（`None`）——符合预期，Step 4 才会产生
非零值。

**不变量核实**：60060 个 `(tick, barrierPass)` 分组里，**每一组
`isLast=1` 的行数恰好是 1**（聚合脚本对此做了断言式检查，零违反）
——直接验证了设计 §2.2 论证的"`Barrier::wait()` 对恰好一个调用者
返回 `true`"这个契约在真实 8 线程 spin-barrier 运行下确实成立，不是
只在代码走查层面成立。

### 13.4 聚合脚本 v1 与第一份"谁最后到达"直方图

脚本：`docs/refs/scripts/critpath_aggregate.py`（新文件，按 §7 说
"离线聚合，不在设计范围内写出，只列产出"——这份脚本就是那个产出，
现在针对真实 CSV 格式写的，不是设计阶段想象的格式）。用法：

```sh
python3 docs/refs/scripts/critpath_aggregate.py <outdir>
```

按 `(tick, barrierPass)` 分组（§3.4 的关联键），每组取 `isLast=1`
的那一行的 `domainId` 计数，汇总成"域 X 是最后到达者的 quantum 占比"
直方图；同时把每组里非最后到达域的 `dur_ns` 取最大值，作为"这个
quantum 边界上最早到达者和最后到达者之间的到达时间差"的代理值（设计
§3.1 原本想比较各域 `tEnter` 的极差，但 Step 2 实际记录的是"阻塞
时长"而非绝对 `tEnter`——推导：某个非最后到达域的 `dur_ns` 约等于
"最后到达者的到达时刻 − 它自己的到达时刻"，取这些值里的最大值，就
约等于最早到达域到最后到达域之间的时间差，信息等价，只是从"阻塞
时长"反推而非直接读 `tEnter`）。

跑在 `x86-threads3-roi-classic`/Q=6660/spin/MAX_TICKS=2e8 这个窗口
上的实际输出：

```
loaded 60060 (tick, pass) groups across 30030 distinct quantum-boundary ticks

=== barrier pass 1 (quantum-local events done) -- 30030 quanta ===
  domain   last-arriver count    share   avg eventCount when last
       1                15128    50.4%                       27.2
       5                 4439    14.8%                        6.6
       6                 3368    11.2%                        4.3
       7                 3241    10.8%                        4.2
       4                 1341     4.5%                        1.0
       3                 1222     4.1%                        1.0
       2                  804     2.7%                        1.0
       0                  487     1.6%                        1.0
  cross-domain arrival spread (ns): min=1209 p50=11998 p95=29841 p99=36548 max=250077

=== barrier pass 2 (global event body (domain 0 only)) -- 30030 quanta ===
  domain   last-arriver count    share   avg eventCount when last
       1                14268    47.5%                        0.0
       5                 4399    14.6%                        0.0
       6                 3473    11.6%                        0.0
       7                 3324    11.1%                        0.0
       3                 1489     5.0%                        0.0
       4                 1447     4.8%                        0.0
       2                  883     2.9%                        0.0
       0                  747     2.5%                        0.0
  cross-domain arrival spread (ns): min=1297 p50=2375 p95=3231 p99=4398 max=285758
```

（Pass 2 的 `avg eventCount when last` 恒为 0——符合预期：`eventCount`
只在 pass 1 之前的域内事件循环里累积，pass 2 之前只跑
`_globalEvent->process()` 本体，见 §3.1/§4.2 对两道屏障语义的
区分，不是数据缺失。）

### 13.5 这份直方图说了什么：域 1（核 0 私有 L1+L2）是关键路径，不是
L3/目录

这是本设计 §1 想拿的数据，也是"未知数 1"的直接答案，结果**推翻了
§2.3 记录的直觉**（"域 1..4 大概率工作量偏轻、域 5 共享 L3 和域
6-7 目录+DRAM 大概率工作量偏重"）：

- **域 1（核 0 的私有 L1+L2+本地 APIC）是压倒性的最后到达者**：
  pass 1 里 50.4%、pass 2 里 47.5% 的 quantum 边界上，域 1 是最后
  到达的域——远超域 5（L3，14.8%/14.6%）和域 6-7（目录+DRAM，合计
  22%/22.7%）。
- **核间严重不均衡，跟核私有 vs 共享的拓扑分类无关**：域 2/3/4
  （核 1/2/3 的私有栈）的最后到达占比只有 2.7%-4.5%，域 1 是它们的
  10-20 倍。四个"同类"域（都是核私有 L1+L2）之间的差距，比"核私有"
  和"共享 LLC/目录"这两个不同类别之间的差距还大——这意味着 §2.3
  "核私有域大概率工作量偏轻"这个直觉的分类粒度本身就不对：不是
  "核私有 vs 共享"这条线在决定谁是关键路径，是某个特定的域（这次
  是域 1／核 0）在决定。
- **域 1 最后到达时，`eventCount` 显著更高（pass 1 平均 27.2）**，
  而域 2/3/4 最后到达时平均只有 1.0——域 1 大多数时候是最后到达者，
  意味着它绝大多数 quantum 都在真的处理更多事件，不是偶尔卡一下；
  域 2/3/4 极少数几次当上最后到达者时，`eventCount` 恰好是 1.0（几乎
  没多干活），暗示它们那几次"最后到达"更像是随机的时序噪声，而不是
  真的工作多。**这初步把"域 1 是关键路径"归因到"真的工作多"而不是
  "卡在某把跨域锁上"**——但这只是初步归因：本 Step 3 完全没有类型二
  （锁等待）数据，Step 4 才能验证"域 1 有没有同时也卡锁"，不能排除
  两个因素叠加。
- **到达时间差（spread）本身量级不小**：pass 1 的中位数约 12µs、
  p99 约 36.5µs、单次最大 250µs——对比 Q=6660 ticks 在 3GHz 板时钟
  下对应的真实时间量级（`SIM_QUANTUM_TICKS`/板时钟频率），这个 spread
  不是噪声量级,是实打实的"其余域等一个域"的空闲时间,方向上支持"出路 3
  应该往域 1 头上加东西/减负,而不是均匀地往每个核私有域加"这个更
  具体的结论。

**这条数据本身已经部分回答了 OPEN-ISSUES §A1 未知数 1 提出的
"固定一个域还是轮换"问题**：不是均匀轮换，域 1 压倒性固定占优——但
"为什么恰好是核 0"（工作负载调度到核 0 上的东西本来就多？ROI 窗口里
某个单线程阶段绑在核 0 上跑？）本 Step 3 没有回答，需要看域 1 内部
具体在跑什么事件类型，是 §8 提到的"域内耗时构成细分"这个更深一层
问题,不在本设计范围内。

### 13.6 已知局限（如实记录）

- **`reserve()` 未实现**（§13.3）：§4.4 设计要求的缓冲区预留容量
  这一步，Step 1/2 都没有做，本 Step 3 也没有补——这次窗口（3 万
  quanta/域）没有因此出问题，但没有专门测过"vector 重新分配本身
  有没有在被测时间线里留下痕迹"，扩大窗口前应该补上。
- **归因只到"初步"**：§13.5 说的"域 1 是因为真的工作多，不是卡锁"
  是基于 `eventCount` 的间接证据，不是直接的类型二（锁等待）数据——
  Step 4 做完、能把域 1 的锁等待时长也摆到同一张表上之后，才能把这个
  归因坐实。
- **单次运行,非统计量**：跟 §8 记录的一致,这是一次运行的结果,同一
  配置多次跑之间细节可能不完全一致(S-011 §10.5/OPEN-ISSUES B2 的
  非确定性)——本节的具体百分比数字(50.4%等)应该当作"这一次运行大致
  是这样"读,重复次数/置信区间留给 Step 5。
- **"为什么是域 1／核 0"没有解释**：见 §13.5 结尾——需要域内事件
  类型细分,是本设计范围之外的下一层问题。
- **聚合脚本的 spread 代理值有一个小假设**：用非最后到达域的
  `dur_ns` 最大值代表"最早到达域到最后到达域的时间差",隐含假设是
  "域被释放的时刻 ≈ 最后到达者到达的时刻"(spin 模式下这个假设很紧,
  cv 模式下会多算一点 futex 唤醒延迟)——这次跑的是 spin 模式,代理值
  可信;如果以后拿 cv 模式的数据做同样分析,这个代理值会系统性偏大
  一点,不能直接跨模式比较。

## 14. 实现记录：Step 4（类型二锁等待打点 + `reserve()` 落地）

按 §11 的 5 步计划，本节记录 Step 4——§4.1 的 `UncontendedMutex` 标签
+ 慢路径计时，以及 §13.6 记录的 `reserve()` 遗留项。

### 14.1 落地内容

跟 §4.1/§4.4 设计的对应关系：

- `src/base/critpath_trace.hh`/`.cc`：新增
  `critPathRecordLockWait(CritPathLockTag tag, CritPathClock::duration
  dur)`——跟 `critPathRecordBarrierPass()` 不同，这个函数自己读
  `curTick()`（`sim/cur_tick.hh`）而不是靠调用方传 tick：
  `UncontendedMutex` 在 `base/`，不能像 `globalBarrier()`
  （`sim/global_event.hh`）那样直接调 `curEventQueue()->getCurTick()`
  ——`sim/eventq.hh` 本身就 `#include "base/uncontended_mutex.hh"`，
  反向包含会循环。`curTick()` 是同一个线程本地值的另一条读取路径
  （`base/trace.hh`、`base/stats/storage.hh` 已经在用这条路径，不是
  新开的先例）。同时新增 `g_critPathTraceReserve`/`critPathReserve()`
  （§14.4）。
- `src/base/uncontended_mutex.hh`：按 §4.1 给出的代码原样落地——
  加 `const CritPathLockTag tag` 成员、`explicit
  UncontendedMutex(CritPathLockTag t = CritPathLockTag::None)`
  构造函数，`lock()` 从 `while(!testAndSet())` 改写成
  `if(testAndSet()) return;` + `do{...}while(!testAndSet())`（纯行为
  保持重构，§4.1 已经论证过等价性，§14.2 验证）。
- 四个已知跨域锁改成显式打标签：`src/mem/xbar.hh` 的
  `layerLock{CritPathLockTag::LayerLock}`，`src/dev/io_device.hh` 的
  `pioLock{CritPathLockTag::PioLock}`，
  `src/mem/ruby/common/Consumer.hh` 的
  `m_wakeup_mutex{CritPathLockTag::ConsumerLock}`。
- `src/base/addr_range_map.hh`：给 `AddrRangeMap` 加
  `explicit AddrRangeMap(CritPathLockTag tag = CritPathLockTag::None)`
  构造函数，透传给 `cacheLock`；`src/mem/physical.hh` 的 `addrMap` 和
  `src/mem/xbar.hh` 的 `portMap` 两个跨域实例显式传
  `CritPathLockTag::CacheLock`，其余实例（RISC-V/GPU-compute 等）维持
  默认 `None`，不受影响。

### 14.2 验证：关闭状态 stats.txt 逐字节相同（§4.1 强制要求的
重构中立性检查）

方法沿用 §11.2/§12.2/§13.2：`build/X86_MESI_Three_Level`、检查点
`x86-threads3-roi-classic`、`MAX_TICKS=2e8`、`SIM_QUANTUM_TICKS=6660`、
`EVENTQ_BARRIER_MODE=spin`、隔离核 92-99（并行臂 `taskset -c 92`，
串行臂 `taskset -c 54`）。改动前二进制用 `git stash` 还原到 Step 3
完成点（`cb91748e71`）在同一个 `build/X86_MESI_Three_Level` 目录里
重新编译取得，改动后二进制是 `git stash pop` 换回来再编译一次；两次
编译都只有环境已知的第三方库缺失警告。

**一个需要如实记录的操作细节**：驱动脚本
`docs/refs/scripts/x86_fs_mesi3_parallel_eventq.py` 本身也在这次改动
里（新增 `EVENTQ_CRITPATH_RESERVE`/`root.critpath_trace_reserve`，
§14.4），改动前的二进制没有 `critpath_trace_reserve` 这个 `Root`
参数，用改动后的脚本跑会在 `_pre_instantiate()` 里
`AttributeError: Invalid assignment for Class Root with parameter
critpath_trace_reserve` 直接失败——这不是这次重构引入的行为回归，是
"脚本版本要跟二进制版本对齐"这个一直存在但之前没有被踩到的前提（之前
几步脚本没有随二进制一起变过）。改动前的二进制改用
`git show cb91748e71:docs/refs/scripts/x86_fs_mesi3_parallel_eventq.py`
取出的旧版脚本重新跑，问题消失。

两组（串行/并行）`stats.txt`（排除 `host*` 字段）改动前后**逐字节
相同**；`simInsts=74062`，四次跑（串行×2、并行×2）全部一致；四份
日志里都没有 `assert`/`panic`/`abort`/段错误关键字；关闭状态下四次
跑都没有产出任何 `critpath-domain*.csv`（`critPathFlush()` 在空
缓冲区上正确地保持 no-op）。这确认了 §4.1 论证的"`do-while` 重构+
标签比较在关闭时行为不变"在这次具体改动上成立。

**另一个如实记录的操作细节（与本次代码改动无关，纯环境问题）**：
第一轮验证跑撞上一个陈旧的 `/home/wxl/.cache/gem5/x86-ubuntu-24.04-
img-4.0.0.lock.lock`——`src/python/gem5/utils/filelock.py` 用
`O_CREAT|O_EXCL` 手工锁文件实现互斥，不是 `flock()`，进程被
`SIGKILL`（本会话早前一次手动中断的 5 分钟超时跑）不会自动清理这个
锁文件，导致之后所有需要这个资源的跑全部在 900 秒超时后失败。资源
本身（`.gz`，约 1GB）已经完整，删掉这个空锁文件、重跑即可恢复——
如果之后的会话在这个沙箱里撞到同样的 `FileLockException`，这是已知
根因，不代表代码有问题。**这个沙箱当前被至少另一个并发的 Claude Code
会话共用**（观察到一个跑
`x86_fs_classic_save_ckpt_balanced.py`/重新编译
`build/X86_MESI_Three_Level` 的独立进程树，`cwd` 打印指向不同的
`scratchpad` 会话 ID）——这次锁文件残留大概率是本会话自己造成的，但
以后如果 `build/X86_MESI_Three_Level` 被别的会话同时 `scons`，也要
考虑一起构建撞车的可能性，本会话这次没有踩到后者，如实记录风险存在
而非确认发生过。

### 14.3 §9 要求的 TSan 回归：本次会话明确推迟，不是遗漏

Step 4 是这四个文件（`xbar.hh`/`io_device.hh`/`addr_range_map.hh`/
`Consumer.hh`）第一次被真正改动（构造函数签名变化），§9 论证过这时候
应该重新跑一遍 S-009 §24.5/S-010 §11.2/S-011 §10.4 的 TSan A/B。这次
实现会话就是否要在本次一并做这件事跟用户核对过——**用户选择推迟，
记在这里作为明确的后续待办**，不是本会话疏漏：下一次涉及这几个文件
或者准备用这份锁等待数据做定量结论之前，应该先补上
`scons --with-tsan build/X86_MESI_Three_Level_TSAN/gem5.opt` +
大窗口（参考 S-011 用的 `MAX_TICKS=1.3e9`）serial+spin 各至少一轮，
确认新增的标签比较/`do-while` 重构没有在这四个文件上引入/去除任何
TSan 报告。

### 14.4 `reserve()` 落地（§13.6 记录的 Step 3 遗留项）

按 §4.4"通过 Root 参数或环境变量传入"的要求：

- `src/sim/Root.py`：新增 `critpath_trace_reserve =
  Param.Unsigned(0, ...)`——0（默认）= 不预留，行为跟 Step 1-3 完全
  一样。
- `src/sim/root.cc`：`g_critPathTraceReserve = p.critpath_trace_reserve;`。
- `src/base/critpath_trace.{hh,cc}`：`critPathReserve()`，
  `g_critPathTraceReserve > 0` 时对当前线程的 `critPathBuffer`
  调用一次 `reserve()`，否则 no-op。
- `src/sim/simulate.cc`：在 `thread_main()` 设置
  `critPathDomainId` 之后（线程入口，一次性）、以及 `simulate()`
  里域 0 设置 `critPathDomainId = 0` 之后（每次 `simulate()`
  重入都调一次，但 `reserve()` 在容量已经足够时是 no-op，重复调用
  代价可忽略）各加一次 `critPathReserve()` 调用。
- `docs/refs/scripts/x86_fs_mesi3_parallel_eventq.py`：新增
  `EVENTQ_CRITPATH_RESERVE`（默认 `"0"`）→
  `root.critpath_trace_reserve`。

**如实记录一个限制**：本 Step 4 的 §14.5 打开插桩验证跑用的是默认
`EVENTQ_CRITPATH_RESERVE=0`（没有专门设置非零值测试）——`reserve()`
本身的代码路径跟着 §14.2 的关闭状态回归和 §14.5 的打开状态跑一起
执行过（`critPathReserve()` 在 `g_critPathTraceReserve==0` 时直接
返回，是已经覆盖到的 no-op 分支），但"设置了非零预算、真的跳过了
vector 重新分配"这条路径本身**没有专门跑一次验证**——§13.3 记录的
"没有专门测过重新分配本身有没有在时间线里留下痕迹"这个问题，落了
`reserve()` 的地基，但还没有真的用非零预算跑一次去回答它,严格说
仍然是留给以后(扩大窗口前)的待办,只是现在有了做这件事需要的参数
管线。

### 14.5 打开插桩：跟 §13.3 同一操作点，加锁等待记录

协议不变（`EVENTQ_CRITPATH_TRACE=1`、Q=6660、spin、`MAX_TICKS=2e8`、
隔离核 92-99、`x86-threads3-roi-classic`，`EVENTQ_CRITPATH_RESERVE`
维持默认 0）。跑完无崩溃，退出码 0，`simInsts=74062`（跟关闭状态
完全一致——打开插桩不改变模拟的指令级行为，只是这次没有像 §13.2
那样做逐字节 `stats.txt` 比对，因为 §5 已经论证过"打开插桩时
`hostSeconds` 不能拿来跟关闭状态比较"，但功能性的 `simInsts` 一致
仍然是一个有意义的健全性检查）。

8 个域各自产出的 `critpath-domain<N>.csv` 这次明显更大（约 2.2MB/域，
对比 Step 3 的量级），因为除了 `barrier` 行还有本 Step 新增的
`lockwait` 行：`grep -c "^lockwait"` 统计,8 个域里只有 3 个域出现过
`lockwait` 行——域 1: 54 次,域 5: 2 次,域 6: 1 次,其余 5 个域
(0/2/3/4/7)一次都没有。`(tick, barrierPass)` 分组的 `isLast=1`
恰好一行这条不变量（跟 §13.3 一样)本次同样零违反。

### 14.6 聚合脚本 v2 输出与未知数 2 的初步回答

`docs/refs/scripts/critpath_aggregate.py` 这次扩展了
`load_lockwait_records()`/`aggregate_lockwait()`/
`print_lockwait_summary()`（按 §7 第二条"按域、按锁标签汇总类型二
记录的总等待时长/次数，归一化到该域的 `hostSeconds`"实现——
`hostSeconds` 从同目录 `stats.txt` 读取，找不到就跳过归一化而不是
报错）。跑在同一个窗口上：

```
=== lock-wait summary (unknown 2) ===
  domain        lockTag      count       total_ns     avg_ns
       1   ConsumerLock         54         362392     6711.0
       5   ConsumerLock          2           6839     3419.5
       6   ConsumerLock          1           7245     7245.0

  domain   total lock-wait ns  % of hostSeconds
       1               362392             0.024%
       5                 6839             0.000%
       6                 7245             0.000%
```

（同一次跑的屏障直方图跟 §13.4 几乎完全一致——域 1 pass 1/2 分别是
50.6%/47.3%,域 5 是 14.4%/14.3%,不重复贴表；两次独立跑之间的具体
百分比小幅波动符合 §13.6/§8 记录的"单次运行,非统计量"预期。）

**这份数据是未知数 2 的第一份直接证据，结论清晰**：

- **`LayerLock`/`PioLock`/`CacheLock` 这三类锁在这个窗口里一次慢
  路径都没有走过**——8 个域、30030 个 quantum 边界的整个窗口内，
  三者的 `lockwait` 行数都是 0。这个窗口是纯 Ruby 协议流量为主的
  FS ROI（classic 边只在 IOXBar/PIO 场景才会被踩到，§2.5 表格），
  这个结果符合"这段 ROI 窗口里 classic 跨域路径本来就很少被踩到"的
  预期，但目前只有这一次运行的数据，不能推广成"这三类锁在所有
  workload 下都不构成关键路径"。
  - **`ConsumerLock`（Ruby wakeup 跨域路径,S-011 已经修过 owner
  竞争的那把锁)是唯一走过慢路径的锁,但绝对量极小**：域 1 慢路径
  命中 54 次、累计 362392 ns（约 0.36 毫秒）,相对这次跑的
  `hostSeconds=1.49` 秒是 0.024%——三个数量级以下的占比。
- **这直接支持、而不是否定 §13.5 的初步归因**："域 1 是关键路径是
  因为真的工作多，不是卡在跨域锁上"——现在有了类型二的直接数据：
  域 1 慢路径累计阻塞时间（0.36ms）比它在屏障上贡献的到达时间差
  （§13.4 记录的 pass 1 spread p50=11.9µs × 30030 个 quantum，量级
  在几百毫秒）小了两个数量级还不止。§13.6 记录的"归因只到初步"这条
  已知局限，到这里可以去掉了——**S-012 §1 提出的未知数 2（"是卡在
  某把跨域锁上,还是纯粹计算量大"）在这次窗口里有了明确答案：不是
  锁，是计算量**。
- **域 5（L3）、域 6（一个目录+DRAM）也各有极少量 `ConsumerLock`
  命中（2 次、1 次），域 0/2/3/4/7 一次都没有**——量级太小，不足以
  支撑任何关于"哪个域更容易卡锁"的结论，如实记录观测到的原始计数
  而不过度解读。

### 14.7 已知局限（如实记录）

- **TSan 回归推迟**（§14.3）——已跟用户确认是明确决定,不是遗漏,
  但在"当前状态是否完全验证过"这个问题上,答案仍然是"还没有",这份
  待办应该在下一次触达这几个文件或者要拿这份数据做定量结论之前
  完成。
- **`reserve()` 代码落地但未用非零值实跑验证**（§14.4）——`§13.3`
  提出的"vector 重新分配本身有没有制造抖动"这个问题,`reserve()`
  的管线已经打通,但还没有真的用它去回答这个问题。
- **锁等待样本量很小**（§14.6）——57 次命中（54+2+1）不足以对
  `ConsumerLock` 本身的等待时长分布做任何统计意义上的论证,只能
  支持"绝对量远小于屏障 spread"这个数量级上的粗略比较。
- **单次运行,非统计量**——跟 §13.6/§8 一致,本节数字应该当作"这一次
  运行大致是这样"读。
- **`hostSeconds` 归一化用的是开着 `critpath_trace` 跑出来的
  `hostSeconds`**（§14.6 表格）——§5 已经论证过这个值不能跨"开/关
  插桩"比较,但拿它作为同一次跑内部"锁等待时间占这次跑总时间的
  比例"的分母是自洽的,不涉及跨运行比较,这里的用法没有违反 §5 的
  警告。

## 15. 新发现的问题（根因已改判，见 §16）：长窗口下 `critPathFlush()` 段错误——`OutputDirectory` 非线程安全

> **本节的根因假设已经被 §16 用调试器证据推翻。** 按本节假设加的
> `OutputDirectory` 互斥锁（`src/base/output.hh`/`.cc`）已经实现、编译、
> 用同样的长窗口重跑验证——**段错误原样复现，一字不差**（同一个函数、
> 同一个源码行）。这说明本节"8 个域线程并发插入同一个 `std::map`"的
> 假设是错的，至少不是这次崩溃的真正原因。锁本身不是坏改动（§16 会
> 说明它仍然值得保留，理由不一样了），但不能靠它宣布这个 bug 已修——
> 保留本节作为"第一次读代码给出的假设，以及为什么这个假设看似合理"的
> 记录，真正的根因和证据见 §16。

S-013 分支的一次会话（2026-07-18）想验证 S-014/S-015/S-016 修复合并进
`main` 后，能否重新跑通 S-013 §9 因为 `occupyLayer` 崩溃而中断的长窗口
关键路径校验——把 `MAX_TICKS` 从之前唯一验证过的 `2e8` 提到 `2e9`（同一
工作点：`SIM_QUANTUM_TICKS=6660`、`EVENTQ_BARRIER_MODE=spin`、
`EVENTQ_CRITPATH_TRACE=1`、检查点 `x86-threads-balanced3-roi-classic`，
宿主 CPU 100-107，避开了同时段另一个在跑的 ~8.7h 三臂对照任务占用的
54-55/92-99）。这次校验不是本文档的话题，但撞上的问题出在本文档设计/
实现的插桩基础设施本身，记在这里而不是 S-013。

**仿真本身跑到头了**：`STAT_DUMP_PERIOD=1e8` 周期性打点 20 次，每个
周期约 5.0-5.8 万条指令，量级持续、没有"还卡在 guest 启动阶段"的迹象
——跟 S-013 §9 那次只有 74588 条指令、明显还在启动阶段的结果不同，这
次窗口大概率已经覆盖到了 benchmark 真正的并行计算阶段，`occupyLayer`
崩溃（S-014）这次全程没有再出现。

**但退出阶段段错误了**：崩溃点在 `critPathFlush()`
（`src/base/critpath_trace.cc:59`），backtrace 顶部经过
`OutputDirectory::create`/`open`（`src/base/output.cc`）内部的分配
路径，底部是 `_start → __libc_start_main → …exit handler… →
critPathFlush`——即域 0 通过 `std::atexit(critPathFlush)`
（`src/sim/simulate.cc:274`）注册的那次调用，运行在主线程、在
`simulate()` 已经正常返回、7 个子线程都已经 `join()` 完之后。

**根因（读代码确认，未做进一步复现/调试）**：`OutputDirectory`（全局
单例 `simout`）的文件表是一个普通
`std::map<std::string, OutputStream*> files`
（`src/base/output.hh:141/147`），整个类没有任何锁（`mutex`/`lock`
零命中）。本文档 §4 的插桩设计是"每个域线程退出时自己调
`critPathFlush()`"（`thread_main` 里 `terminate` 变 true 之后，
`src/sim/simulate.cc:221`）——因为所有域共享同一个 quantum 屏障，7 个
子线程大概率在几乎同一个 wall-clock 时刻一起看到 `terminate==true`
并几乎同时各自调用 `simout.create()`，对同一个 `files` map 做并发
`operator[]` 插入/红黑树重新平衡——这是未定义行为，能造成堆损坏，
表现为之后（不一定是触发那次插入本身）某次分配崩溃，跟这次崩溃恰好
出现在"最后一个、也是覆盖时间最长的"域 0 atexit 调用里的现象吻合。

**一个还没解释清楚的细节**：输出目录里只有一个空的
`critpath-domain0.csv`（0 字节），域 1-7 一个文件都没有产生——如果
纯粹是堆损坏在域 0 这次调用才触发崩溃，域 1-7 的 flush 应该已经在
此之前成功完成、留下非空文件才对。目前没有确认这是"域 1-7 的
`critPathBuffer` 在这次跑里实际上是空的"（插桩没生效，另一个功能性
bug）、还是"域 1-7 也崩了但某种原因没有终止整个进程"（不太像，一个
线程段错误通常会终止整个进程）、还是别的原因——需要进一步读代码/
复现才能确定，这次会话没有做。

**范围**：这个 bug 独立于 S-013 本身的问题（四核不均衡）和
S-014/S-015/S-016——是本文档（S-012）自己的插桩基础设施里一个此前
从未触发过的线程安全缺口，因为之前所有开着 `EVENTQ_CRITPATH_TRACE`
的跑（§13、S-013 §7）都只验证过 `MAX_TICKS=2e8` 这个短窗口，从没有
真正让 8 个域线程在同一时刻各自触发过退出路径。S-013 §9 遗留的"更长
窗口下四核是否依然不均衡"这个问题因此继续被挡住——现在挡路的是这个
新问题，不再是 S-014 的 `occupyLayer` 崩溃（那个已经在这次跑里被绕过，
2e9 ticks 全程跑完都没有再触发）。

**本次会话未做、留给后续**：
1. 确认域 1-7 CSV 缺失的原因。
2. 在 `OutputDirectory::create`/`open`/`find`/`createSubdirectory` 等
   公共接口上补一把互斥锁（最直接的修法，范围小，风险主要在于
   `OutputDirectory` 是否有别的地方假设它不会被并发调用，需要审计）。
3. 修复后用同样的长窗口（`MAX_TICKS=2e9`，同检查点）重跑一次确认不再
   崩溃，且 8 份 CSV 都产生、内容合理。
4. 确认之后再回到 S-013 自己的问题——用这份长窗口的关键路径数据判断
   域 3 的不均衡在完整 benchmark 尺度下是否依然存在。

这份记录只是"如实报告一次意外的崩溃并定位到读代码能确认的根因"，不是
一次完整调试/修复的记录——按 CLAUDE.md 的 checkpoint 约定，动手修
`src/base/output.cc` 属于新的、更有风险的子阶段（触碰的是 gem5 通用
基础设施而不是本项目自己的代码，且目前的根因分析还没有实际复现/调试
验证过），需要先跟用户确认范围再开始。

## 16. §15 假设被推翻；用 gdb 实测到的真正根因——`critPathBuffer` 的
线程本地析构在 `atexit(critPathFlush)` 之前跑掉，是 use-after-free；
子线程在正常退出路径上从未被 join 过

用户明确要求"直接修 bug，这样合并回 main 后 S-012 能继续、不用再带着
这个 bug"。按 §15 的假设加了互斥锁（`std::recursive_mutex mtx`，
`OutputDirectory::create`/`open`/`find`/`close`/`isFile`/
`createSubdirectory`/`remove`/`setDirectory`/`directory`/`resolve`/
析构函数全部加锁，recursive 是因为这些方法互相调用，`create()` 内部
调 `open()`）。`X86_MESI_Three_Level` 干净重编译，用 §15 完全相同的
长窗口协议重跑——**段错误一字不差地复现**：同一个函数
（`critPathFlush`）、同一个符号偏移（`+0xe0`）、同样的空
`critpath-domain0.csv`、域 1-7 同样一个文件都没有。这排除了"8 个域
线程并发插入同一个 map"这个假设——锁已经把所有并发路径都串行化了，
崩溃还在，说明根本不是并发问题。

### 16.1 装 gdb，实测到真正的崩溃现场

沙盒里没有 gdb，`sudo apt-get install -y gdb`（本项目此前的会话记录
里没有用过 sudo，这次确认可用）装上后，把同一个跑法包进
`gdb -batch -ex run -ex bt -ex "thread apply all bt" -ex quit --args
./build/.../gem5.opt -d ... docs/refs/scripts/x86_fs_mesi3_parallel_
eventq.py`（env 变量在 gdb 前面 export，不走 `--args` 之外的机制），
崩溃点复现只花了大约 80 秒（不是几小时）。带符号的 backtrace：

```
Thread 1 "gem5.opt" received signal SIGSEGV, Segmentation fault.
#0  gem5::critPathFlush () at src/base/critpath_trace.cc:72
#1  __run_exit_handlers (...) at ./stdlib/exit.c:108
#2  __GI_exit (...) at ./stdlib/exit.c:138
#3  __libc_start_call_main (...) 
#4  __libc_start_main_impl (...)
#5  _start ()

Thread 2..8 (全部 7 个子线程，均):
#0  __futex_abstimed_wait_common64 (...)
...
#5  gem5::Barrier::waitCv (this=0x...) at src/base/barrier.hh:171
#6  gem5::Barrier::wait (ctx=0x0, this=0x...) at src/base/barrier.hh:146
#7  gem5::SimulatorThreads::thread_main (...) at src/sim/simulate.cc:214
```

两个事实一次性都验证了：

1. **7 个子线程全部还卡在 `barrier.wait()`（`simulate.cc:214`，
   `while (!terminate) { doSimLoop(queue); barrier.wait(); }` 里退出
   一轮之后的那次 wait），一次都没有跑到自己的
   `critPathFlush()`**——它们的 `critPathBuffer` 从未被 flush，这就是
   §15 记录的"域 1-7 一个文件都没有"的**完整**解释，跟并发/竞态无关：
   它们根本没有机会执行到那行代码。
2. **主线程（域 0）不在任何屏障上，已经在
   `__run_exit_handlers`（glibc 正常 `exit()` 流程）里**，说明主线程
   越过了"等子线程"这一步，直接朝进程退出走，途中触发
   `std::atexit(critPathFlush)` 注册的回调，在
   `critpath_trace.cc:72`（`for` 循环体内第一条 `*s << ...` 语句）
   崩溃。

### 16.2 为什么子线程从未被 join——`terminateEventQueueThreads()`
只接在 `m5.simulate.fork()` 上，不接在正常退出路径上

读 `src/sim/simulate.cc`：`simulate()` 函数本身只调用一次
`simulatorThreads->runUntilLocalExit()`（释放子线程开始跑，§4.2/
§11.2 记录过的设计）加一次 `doSimLoop(mainEventQueue[0])`
（主线程自己的份），然后直接 `return global_exit_event;`
（§16 引用的行号：simulate.cc:324/327/343）——**`simulate()` 自己从来
不调用 `simulatorThreads->terminateThreads()`**。会调用它的只有两处：

- `~SimulatorThreads()`（对象析构时兜底）。
- `terminateEventQueueThreads()`（`simulate.cc:369-373`），这是一个
  单独导出给 Python 的函数（`pybind11/event.cc:112`），全仓库唯一的
  调用点是 `src/python/m5/simulate.py:551` 的 `fork()`
  ——"Terminate helper threads that service parallel event queues"，
  只在**进程 fork**前才需要（避免子进程继承一堆卡在 barrier 上的
  线程）。本项目用的驱动脚本
  （`docs/refs/scripts/x86_fs_mesi3_parallel_eventq.py`）从不调用
  `m5.simulate.fork()`，所以在**普通**（非 fork）的一次跑完退出路径
  上，`terminateEventQueueThreads()` 从来没有被调用过——子线程只能
  靠 `simulatorThreads`（`static std::unique_ptr<SimulatorThreads>`，
  `simulate.cc:230`）自己的析构函数兜底，而这个析构函数的运行时机，
  见下一节，恰好排在 `critPathFlush` 的 atexit 回调**之后**。

这不是本次改动引入的新问题——`terminateEventQueueThreads()` 这个
"只接在 fork 上"的接线方式在 S-012 插桩之前就是这样，S-012 的
`critPathFlush()` 设计（§4.4 的注释："subordinate threads... normally
be waiting on the barrier"）其实已经**默认承认**了子线程在正常退出时
不会被主动终止，只是没有意识到这对 domain 0 自己的 atexit flush
意味着什么。

### 16.3 真正的根因——`atexit` 与主线程 `thread_local` 析构的 LIFO
顺序竞争，是确定性的 use-after-free，不是并发竞态

`std::atexit()` 注册的回调和"静态存储期对象析构 + 主线程的
`thread_local` 对象析构"共享**同一个** LIFO（后注册先执行）退出序列
（Itanium C++ ABI 里都是通过 `__cxa_atexit`/`__cxa_thread_atexit`
挂到同一张表）。关键的两个注册时刻：

- `std::atexit(critPathFlush)`：在 `simulate()` 函数**最开头**、
  `if (!simulatorThreads) {...}` 分支里、**在**
  `doSimLoop(mainEventQueue[0])` 第一次被调用**之前**注册
  （`simulate.cc:274`，先于 §16.2 提到的 324/327 行）。
- `critPathBuffer`（`thread_local std::vector<CritPathRecord>`，
  `critpath_trace.cc:19`）的析构函数注册：`thread_local` 变量的
  析构注册是**惰性**的——只有在这个变量第一次被真正访问（读/写）时，
  编译器生成的 TLS wrapper 才会顺带调用 `__cxa_thread_atexit`
  登记析构函数。主线程第一次真正碰
  `critPathBuffer` 是在 `critPathRecordBarrierPass()`
  （`critpath_trace.cc:34`，`critPathBuffer.push_back(r)`），这只会在
  `doSimLoop` 跑起来、真正经过一次带 `ctx` 的
  `Barrier::wait()`（quantum 屏障）之后才第一次执行——**晚于**
  `atexit(critPathFlush)` 的注册时刻。

后注册的先执行——`critPathBuffer` 的析构（晚注册）比
`critPathFlush`（早注册）**先**跑。于是真正的退出序列是：

1. `critPathBuffer.~vector()` 先执行，释放它持有的堆内存，vector 对象
   自身进入"已析构"状态（libstdc++ 的 `~vector()` 不保证事后把
   `_M_start`/`_M_finish` 清零）。
2. `critPathFlush()`（`atexit` 回调）后执行，`if
   (critPathBuffer.empty())` 读的是一个**已经析构的** vector 对象——
   这个检查本身已经是未定义行为，多半不会按预期返回 true，于是继续走
   到 `for (const auto &r : critPathBuffer)`，用 `_M_start`/`_M_finish`
   这两个悬空指针遍历一段已经释放的堆内存，循环体第一条语句
   （`critpath_trace.cc:72`，`*s << (r.kind == ...)`）解引用垃圾内存
   ——段错误。

这完整解释了**所有**观测到的现象，且不需要任何并发假设：

- **100% 确定性、逐字节相同的崩溃位置**（§16 开头提到的复现）——这是
  纯单线程的 UAF，不是竞态，同样的堆布局每次都读到同样的坏内存，不是
  "有时候崩有时候不崩"的模式，跟真正的数据竞争的典型症状不一样。
- **`critpath-domain0.csv` 存在但是空的**——`simout.create()`（UAF
  发生**之前**，在 for 循环之前）确实成功创建了文件、表头那行文字确实
  写进了 `ofstream` 的用户态缓冲区，但崩溃发生在 `simout.close(os)`
  （唯一会真正 flush/close 底层文件的调用）之前，缓冲区里的内容从来
  没有落盘。
- **域 1-7 一个文件都没有**——§16.1 已经证实：不是它们的
  `critPathFlush()` 跑了但输出丢了，是它们的 `critPathFlush()`
  从未被调用过（§16.2）。

一个仍然没有完全查清、不影响上面结论但值得记录的疑点：**为什么 S-012
§13、S-013 §7 那些短窗口（`MAX_TICKS=2e8`）的跑清清楚楚报告过"8 份
CSV 都产生、内容合理"**？§16.2 的"子线程从不 join"和 §16.3 的
"atexit 顺序"这两条推理对窗口长度并不敏感，理论上短窗口也应该踩中
同一个 bug。留给下一步复现确认，候选解释包括：那几次运行的驱动方式
（是否走了 `m5.simulate.fork()` 或某种当时没记录下来的额外清理调用）
和这次不同；或者短窗口下 `critPathBuffer` 很小，释放后的那块堆内存
恰好还没被覆盖/取消映射，`_M_start==_M_finish`
恰好凑巧仍然成立（`empty()` 恰好读到"看起来是空"的垃圾值，提前
`return`，绕过了后面真正解引用悬空指针的代码）——纯属侥幸而非设计
上安全，只是"读了未初始化/已释放的内存但没有踩到近期被覆盖或已经
`munmap` 的页"，跟长窗口下更大的 buffer 更容易被 `munmap` 整块归还
系统、访问必然触发缺页从而必然段错误的情形不同。这条待确认，不影响
"必须修" 这个结论——即使短窗口"恰好没崩"，代码本身仍然是 UAF，是
定时炸弹。

### 16.4 §15 加的 `OutputDirectory` 锁怎么处理

这把锁**不是**这次崩溃的病因，但也不是无意义的改动——`OutputDirectory
::files`/`dirs` 本来就没有任何线程安全保证，而这份插桩设计的另一条
合法路径（子线程各自在自己的 `thread_main` 里调用
`critPathFlush()`，§4.4 设计、§14 实现）**确实**会让多个域线程并发
调用 `simout.create()`——只是这次崩溃复现时子线程从未真正跑到那一步
（§16.2），所以这把锁在这次复现里"没用上"，不代表它保护的场景不存在。
决定：**保留这把锁**，作为独立于本次崩溃修复的加固；下一步真正修好
"子线程正常退出时会被 join、各自 flush"之后，子线程并发 flush 的
场景就会被真实触发，到时候这把锁就是必需的，不是防御性冗余。

### 16.5 真正的修复方案（已实现，见 §16.6）

用户明确的方向确认："不是应该每个核自己记录自己的信息，输出独立的
文件，就行了吗？有没有说必须要生成一个统一的文件"——纠正了本节
最初的框架（"找一个统一的清理钩子"听起来像是要重新设计），确认了
本来的每域独立文件设计不用变，缺口只是"让每个域已经写好的自己那份
flush 代码真正被执行到"。§16.3 指出了两个需要修的独立缺口：

1. **子线程在正常（非 fork）退出路径上从未被终止/join**——
   `terminateEventQueueThreads()` 需要在正常退出路径上也被调用一次
   （不只是 `fork()` 前），这样每个子线程能跑到自己
   `thread_main()` 里的显式 `critPathFlush()` 调用（§4.4/§14 设计的
   本意），而不是永远卡在 barrier 上直到进程整个退出。这个改动的
   落点在 Python 层（`m5/simulate.py`）还是 C++ 层（`core.cc`
   `doExitCleanup` 之类的既有钩子，如果存在的话）需要先找到 gem5
   现有的"一次跑真正结束"的钩子点，不能假设。
2. **domain 0 自己的 flush 不能再靠 `std::atexit()` 卡这个
   LIFO 时序赌局**——即使 §16.5-1 修好了子线程的 join，domain 0
   这次用 `atexit` 注册的隐患还在（这次复现只暴露了它，没有证明它是
   唯一受害者）。更稳妥的做法是把 domain 0 的 flush 也挪到跟
   §16.5-1 同一个"确定性早于任何析构"的显式清理点上调用，不再依赖
   `atexit`/`thread_local` 析构的注册顺序这种实现细节。

这两处改动都会碰到 `src/sim/simulate.cc`（本项目已经改过的文件）和
`src/python/m5/simulate.py`（影响所有 gem5 使用者的通用 Python 驱动
代码，不只是本项目的脚本）——比 §15 原来设想的"给一个类加锁"的范围
明显更大，属于 CLAUDE.md 说的"新的、更有风险的子阶段"，报告完根因后
先等用户确认了方向（§16.5 开头）才动手，没有自行展开。

### 16.6 实现 + 验证

**`src/sim/simulate.cc`**：删掉 `simulate()` 里的
`std::atexit(critPathFlush);`。`terminateEventQueueThreads()`
（原来只有 `simulatorThreads->terminateThreads();` 一行）改成两步：
先给 `simulatorThreads` 补一个判空（`m5.simulate()`
从未被调用过的脚本也能安全调用这个函数，不会解引用空指针）再调用
`terminateThreads()`，然后显式调用一次 `critPathFlush()`——domain 0
的 flush 从此和子线程的 flush 走同一个调用点，不再单独依赖
`atexit`。

**`src/python/m5/simulate.py`**：在 `simulate()` 函数
`if need_startup:` 分支里，紧跟着已有的
`atexit.register(_m5_core.doExitCleanup)` 之后，新增
`atexit.register(_m5_event.terminateEventQueueThreads)`。选择"跟在
`doExitCleanup` 后面注册"是为了让它比 `doExitCleanup`/`stats.dump`
都先执行（Python exit handler 是后注册先执行，这份文件自己
`stats.dump` 那行上面的注释已经点出这条规则）——线程 join
和两类 flush 应该在其它退出期清理动作之前就确定性地完成，不依赖
`doExitCleanup` 到底做了什么。这是 Python 层的
`atexit.register()`，在 `Py_Finalize()` 阶段运行，保证先于 §16.3
说的 C++ 层 `atexit()`/主线程 `thread_local` 析构顺序，从根上绕开
了那场时序赌局，不是"调整时序赢下这场赌局"。

**编译**：`X86_MESI_Three_Level` 干净重编译（这次改动会牵连
`python/m5/defines.py.cc` 之类的内嵌 Python 产物，触发的重编译范围
比只改 `output.cc` 大）。

**崩溃修复验证**：用 §16.1 完全相同的长窗口协议（`MAX_TICKS=2e9`，
其余参数不变）重跑——**不再崩溃，8 份 `critpath-domain{0..7}.csv`
全部生成，每份 60-69 万行、22-25MB，内容格式正确**（`kind,tick,
domainId,barrierPass,isLast,eventCount,dur_ns,lockTag` 表头 +
逐行有效数据，抽查过 `critpath-domain3.csv` 开头几行）。日志里
grep 不到任何 `segmentation`/`SIGSEGV`/`panic`/`fatal`/`dumped
core` 字样。

**正确性回归**（这处改动影响*每一次* gem5 运行，不只是开
critpath tracing 的场合，所以不能只验证崩溃修好了）：同一检查点，
`SIM_QUANTUM_TICKS=6660`、`MAX_TICKS=2e8`、**不开**
`EVENTQ_CRITPATH_TRACE`，serial 臂（`taskset -c 100`）+ parallel/spin
臂（host-pin 100-107）各跑一次，均 `exit=0`，两份 `stats.txt`
（排除 `host*` 行）**逐字节相同**，日志里没有任何 error/exception/
traceback。确认这个改动对已经验证过的行为没有引入回归——`
terminateEventQueueThreads()` 现在会在**每一次** gem5 运行结束时
被调用（以前只有 `fork()` 前才调用），但 `simulatorThreads`
判空 + `terminateThreads()` 自己的 `if (threads.empty()) return;`
+ `critPathFlush()` 自己的 `if (critPathBuffer.empty()) return;`
三层保护让它在"没开并行 EventQueue"/"没开 critpath tracing"的
普通场景下都是安全的空操作。

**TSan 验证**：`build_opts/X86_MESI_Three_Level_TSAN` + `scons
--with-tsan` 编译（沙盒里第一次装 TSan build，之前的会话都是复用
已经编译好的，这次确认 `--with-tsan` 这条路径本身也是通的）。同一
长窗口协议缩到 `MAX_TICKS=2e8`（保持开着 `EVENTQ_CRITPATH_TRACE`，
跑到真正触发 join+双重 flush 这条新代码路径即可，不需要重跑 2e9
去省时间）——**不崩溃，8 份 CSV 正常产生**，但 TSan 报了 3 条新
警告：

1. 一条在 `EventQueue::getCurTick()`（`eventq.hh:875`）——T3 通过
   `Consumer::commitTick → EventQueue::schedule → getCurTick()`
   读某个 EventQueue 的 `curTick`，和 T5 自己 `serviceOne()` 里的
   `setCurTick()` 写并发。
2. 两条在 `Flags<unsigned short>::set()`/`isSet()`
   （`flags.hh:116`/`83`）——同一个 `Event`（挂在一个 `Switch`
   路由器对象上）的标志位，T3 通过同一条
   `Consumer::commitTick → EventQueue::schedule` 调用链写
   （给事件打 "Scheduled" 一类的标志），T5 在自己的
   `serviceOne() → Event::isExitEvent()` 里并发读。

这三条都出在 `Consumer::scheduleEventAbsolute`/`commitTick`
（`src/mem/ruby/common/Consumer.cc`）跨域调度 `Throttle`/`Switch`
的 wakeup 事件这条路径上——**跟这次改动完全无关**（没碰
`Consumer.cc`/`eventq.cc`/`eventq.hh`/`flags.hh`，这次只改了
`output.cc`/`simulate.cc`/`simulate.py`）。`Consumer.cc:94` 自己的
注释说"Always called under lock()...so all the scheduling state
here is race-free"，指的是 `Consumer` 自己的记账状态
（`m_inflight_ticks` 等）在并发调用间受 `m_wakeup_mutex`
（CLAUDE.md 列的四把已知跨域锁之一）保护；但 TSan 抓到的读写发生
在 `em->schedule(...)` **内部**、直接触达**目标域自己的**
`EventQueue`/`Event` 状态，而目标域自己的 `serviceOne()`
循环并不知道、也不需要去拿这把 `Consumer` 锁——`Consumer.cc:131-134`
的注释("schedule() is legal from any thread -- ... routed through
asyncInsert() ... quantum snap guarantees is still ahead")暗示这条
路径本来设计上应该走异步安全的插入通道，但 TSan 抓到的这两个具体
操作（`getCurTick()` 读、`Flags::set()` 写）看起来发生在真正路由
到 `asyncInsert()` 之前——是不是这个设计缝隙、还是本来就该被前面
某层挡住，需要专门去读 `EventQueue::schedule()` 内部实现才能确认，
本次没有做这一步。

**这三条警告不是这次改动引入的新竞态**——`Consumer::
scheduleEventAbsolute` 跨域调度贯穿整个仿真过程，不只是退出阶段，
S-002/S-003 就已经在处理这一类问题。但这大概率是**这三条竞态第一次
在这个项目的 TSan 记录里被观测到**：在这次修复之前，任何一次
critpath-traced 或者非-traced 的正常（非 fork）退出，子线程都从未
真正被 join 过（§16.2）——包括 S-009 §24/§25、S-016 §8
等历史上所有跑过 TSan 扩时长 A/B 的会话，因为它们同样从未调用过
`terminateEventQueueThreads()`。这次是这个项目历史上第一次有 TSan
观测覆盖到"多个域几乎同时收敛到退出条件、同时结束各自 doSimLoop"
这个特定时间窗口——不是这次改动本身制造了竞态，而是这次改动第一次
让 TSan 有机会看到这个一直存在、但从未被真正跑到退出路径的窗口。
是否需要单独立项（新 S-NNN）追查 `EventQueue::schedule()`
跨域路径这个具体缝隙，留给用户决定，本节到此为止不展开。

## 17. 实现记录：Step 5（正式关键路径分析 + 全量 TSan/正确性回归）

新会话（2026-07-18），专属 worktree/branch
（`s012-eventq-critical-path-instrumentation-design`，per ADR 0001——
S-012 之前的所有工作都是直接在 `main`/在
[S-013](./S-013-balanced-four-core-workload-checkpoint.md) 的分支上做的，
这是它第一次有自己的分支）。§11 的 5 步计划里剩的最后一步——"§9 的
TSan+正确性回归（覆盖全部改动）+ 正式关键路径分析跑，外加 2-3 次
重复跑评估非确定性"——本节完成其中两项，第三项（真实长窗口下的重复跑）
如实记录为未做。

**数据来源说明**：本节不是本会话新跑的数据，而是把
[S-013](./S-013-balanced-four-core-workload-checkpoint.md) §10-11
那个会话已经跑出来的产物正式收编为 S-012 自己的 Step 5 交付物——那次
会话是在解决 S-012 自己 §16 的 `critPathFlush()` UAF 挡住 S-013 验证
这条链路时，顺手第一次把 Step 1-4 的完整插桩跑到了真实的长窗口
（`MAX_TICKS=2e9`，而不是 §13/§14 一直用的、后来被 S-013 §9 证明大概率
还在客户机启动阶段的 `MAX_TICKS=2e8`），数据留在
`/tmp/s013-recheck/`（用户提到"用 S-013 的 checkpoint"指的正是这批
产物，检查点本身是 S-013 §4 建的
`/workspace/gem5-ckpt/x86-threads-balanced3-roi-classic`）。本节重跑了
一遍 `critpath_aggregate.py` 核实数字，未发起任何新的 gem5 进程。

### 17.1 正确性回归（不开插桩）

`/tmp/s013-recheck/regress-serial.log`/`regress-parallel.log`：同一
均衡检查点，`SIM_QUANTUM_TICKS=6660`、`MAX_TICKS=2e8`、**不开**
`EVENTQ_CRITPATH_TRACE`，serial/parallel 各一次，`build/
X86_MESI_Three_Level`（非 TSan）。两份 `stats.txt`（排除 `host*`）
**逐字节相同**，`diff` 退出码 0。这是这批 Step 1-4+§16 UAF 修复
组合改动第一次在均衡检查点（而不是一直用的
`x86-threads3-roi-classic`）上做正确性回归，结果和 §14.2/§16.6
在原检查点上的结论一致：关闭插桩时行为不变。

### 17.2 TSan 回归（覆盖 Step 1-4 全部改动，不只是 §16 的 UAF 修复）

`/tmp/s013-recheck/tsan-check.log`：`build/
X86_MESI_Three_Level_TSAN`，同一均衡检查点，`MAX_TICKS=2e8`，**开**
`EVENTQ_CRITPATH_TRACE=1`（日志确认 `critpath_trace=1`，即 Step 4 的
四处打标签构造函数——`layerLock`/`pioLock`/`m_wakeup_mutex`/
`AddrRangeMap` 的 `cacheLock`——在这次跑里是真正编译进、启用的，不是
像 S-013 §6 那样为了对照 Step 3 基线而 `git stash` 掉的 clean-Step-3
状态）。构建于本次会话之前（源码状态是 §16 修复合入之后的
`main`），因此这次 TSan 跑覆盖的是 Step 1-4 + §16 UAF 修复的**组合**
改动，不是分开验证的。

结果：不崩溃，8 份 CSV 正常产生，**3 条警告，和 §16.6 报的完全同一
组签名**（`EventQueue::getCurTick()`/`Flags::isSet()`/`Flags::set()`，
都在 `Consumer::commitTick`→`Throttle::wakeup()` 这条跨域 wakeup 路径
上，T3/T5 两个域线程之间）。跟 §16.6 的结论一致：这三条不是这次
改动引入的新竞态，是 `Consumer.cc` 这条一直存在、S-016 之前的会话
从未真正 join 过子线程所以从未被 TSan 观测到的既有缝隙——在两个不同
检查点（原始 `x86-threads3-roi-classic` 和这次的均衡检查点）上都
复现同一组签名，是又一次交叉验证，不是新发现。

这也一并回答了 §14.3 记录的待办——"Step 4 涉及的四个文件（`xbar.hh`/
`io_device.hh`/`addr_range_map.hh`/`Consumer.hh`）第一次被真正改动，
需要专门补一轮 TSan"：这轮 TSan 跑的二进制里 Step 4 的改动是启用状态
（上一段已确认），锁标签构造函数本身、`UncontendedMutex` 的
`do-while` 重构都没有制造任何新警告——§14.3 的待办到这里算完成，
只是完成方式是"顺带被 S-013 那次跑覆盖到"，不是专门为它起的一轮。

**局限，如实记录**：这轮 TSan 跑的窗口仍是 `MAX_TICKS=2e8`（跟
§16.6 一样），不是 §17.3 用的真实 `MAX_TICKS=2e9` 长窗口——TSan
构建跑得慢，同样的 tick 数覆盖的真实指令数远少于满速 build（B6/C1
条目在 `OPEN-ISSUES.md` 里对这一点有过讨论），所以不能把这轮
"不崩溃"当作"长窗口下 TSan 也干净"的证据，只能说"这个短窗口下没有
新增警告"。

### 17.3 正式关键路径分析（真实长窗口，300300 个 quantum）

`/tmp/s013-recheck/spin-2e9-fixed2/`：均衡检查点，
`SIM_QUANTUM_TICKS=6660`、`EVENTQ_BARRIER_MODE=spin`、
`MAX_TICKS=2e9`（不是 §13/§14 一直用的 `2e8`）、
`EVENTQ_CRITPATH_TRACE=1`，host-pin 100-107。20 次周期性 stat dump
每次约 5-6 万条指令，全程稳定，S-014 的 `occupyLayer` 崩溃和 §16
修复前的 `critPathFlush()` 崩溃都没有再出现。8 份
`critpath-domain{0..7}.csv`，每份 60-69 万行。这是这份插桩第一次
跑在真正覆盖了工作负载并行计算阶段的窗口上——`critpath_aggregate.py`
重跑核实：

```
=== barrier pass 1 -- 300300 quanta ===
  domain   last-arriver count    share   avg eventCount when last
       4                94622    31.5%                       28.0
       3                74905    24.9%                       27.3
       1                63531    21.2%                       27.3
       5                25009     8.3%                        7.4
       6                15219     5.1%                        4.8
       7                13436     4.5%                        5.1
       2                11852     3.9%                       18.6
       0                 1726     0.6%                        1.0

=== lock-wait summary ===
  domain        lockTag      count       total_ns     avg_ns
       1      CacheLock      87234      126915991     1454.9
       1   ConsumerLock        485        2624334     5411.0
       4      CacheLock      89509      129616239     1448.1
       4   ConsumerLock        428        2101590     4910.3
       (domain 2/3: CacheLock+ConsumerLock 各一小部分；5/6/7 只有
        ConsumerLock，量级见下表)

  domain   total lock-wait ns  % of hostSeconds
       1            129540325            10.795%
       4            131717829            10.976%
       2             33280133             2.773%
       3             13153144             1.096%
       5/6/7                 ~百万ns量级         <0.2%
```

（完整表格见 S-013 §11，这里不重复贴全；`critpath_aggregate.py` 这次
额外输出了按 `lockTag` 拆开的明细，S-013 自己的写作只贴了汇总表，
补在这里。）

**这份数据对 S-012 §1 三个未知数的正式回答**（对比 §13/§14 用短窗口
给出的初步回答）：

- **未知数 1（谁在关键路径上，固定还是轮换）**：不是单个域压倒性
  主导（§13.5 短窗口的结论），也不是简单"从一个域换到另一个域"
  （S-013 §7 短窗口的结论）——真实长窗口下是**域 1/3/4 三个核私有
  域共同分担**（21-31% 各），域 2 是唯一的低占比异常
  （3.9%，甚至低于共享的 L3/内存域）。§13.5/S-013 §7 那两次"单域
  主导"的结果都是短窗口（大概率仍在客户机启动阶段，S-013 §9 已经
  论证）的产物，不能代表真实工作负载下的关键路径形状。
- **未知数 2（卡锁还是纯算量）**：**这一步的答案和 §14.6
  在短窗口/原始检查点上给出的"不是锁，是算量"不一样，需要更正**。
  §14.6 当时测到的是 `ConsumerLock`，且命中次数极小（57 次，
  0.024% hostSeconds），据此给出"三类跨域锁在这段 ROI 里几乎不走
  慢路径"的结论。这次真实长窗口在均衡检查点上测到了大量
  `CacheLock` 命中（域 1/4 各 8.7-9 万次），域 1/4 的锁等待总时长
  占各自 `hostSeconds` 的 ~11%——不是压倒性主导（域 1/4 仍有
  ~89% 的时间花在别处，多半是真实计算），但也不再是 §14.6 那种
  "两个数量级以下、可以忽略"的量级。域 1/4 恰好也是三个最后到达域
  里锁等待最重的两个（域 3 只有 1.1%），提示**这两个域的"最后到达"
  份额里，锁竞争是一个真实但次要的成分，不是纯粹算量的翻版**——
  跟 S-013 §11 自己的措辞一致（"值得进一步看,但这次没有深入"）。
  §14.6 的结论没有错（在它测的那个窗口/检查点上是对的），但不能
  外推成"这三类锁在所有 workload/窗口下都不构成关键路径"——这正是
  §14.6 §14.7 自己列的已知局限，这里是那条局限第一次被真实数据
  证实"确实换个窗口/检查点就不一样"。
- **未知数 3（各域每 quantum 工作量是否不均衡）**：`avg eventCount
  when last` domains 1/3/4 是 27-28，domains 0/2 是 1.0（域 2 虽然
  份额低但一旦真的最后到达，`eventCount` 反而不低，18.6——样本量
  小，§17.4 会记），域 5-7 是 4.8-7.4。域 1/3/4 最后到达时处理的
  事件数是域 0/2 的 27-28 倍，是共享域（5-7）的 4-6 倍——**不均衡
  确认存在，且量级和 §13.5 短窗口的~27 倍量级基本一致**，只是现在
  分布在三个域而不是一个域上。

### 17.4 已知局限（如实记录）

- **Step 5 计划要求的"2-3 次重复跑评估直方图跑间稳定性"，在真实
  `MAX_TICKS=2e9` 长窗口上没有做**——§17.3 的数字来自单次跑。
  S-013 §7 确实做过确定性检验（同一实验重复两次，域 3
  51.2%/52.6%），但那是短窗口（`2e8`，S-013 §9 已证明大概率仍在
  客户机启动阶段），不能替代长窗口本身的稳定性检验。这是本节相对
  §11 那份 5 步计划**唯一没有完成的一项**，留给下一次要拿这份数据
  做定量结论（尤其是出路 3 具体设计）之前补上。
- **本节数据来自 S-013 那次会话的跑，不是本会话新起的进程**——数据
  提供方是均衡检查点+完整 Step 1-4 插桩，跟 S-012 §13/§14 用的原始
  检查点（`x86-threads3-roi-classic`）不是同一个，域 1/3/4 三分这个
  结果本身混杂了 S-013 §2 记录的"均衡基准修复不完全"这个变量——
  §17.3 的结论应该读成"这条插桩在一个真实长窗口下测出的关键路径
  形状"，不应该脱离 S-013 的上下文去论证"均衡检查点比原始检查点更
  能代表关键路径"。
- **域 2 异常偏低（3.9%）和域 1/3/4 为什么恰好是这三个域，仍未解释**
  ——这是 S-013 自己留白的问题（S-013 §11 结尾），不是本节要解决的
  范围，出路 3"往哪塞工作"的具体设计决策在这个问题解决前仍然缺一份
  干净基线，跟 `OPEN-ISSUES.md` A1 条目的现状一致。
- **锁等待数据的因果关系没有验证**——§17.3 只指出域 1/4 锁等待占比
  和它们的高关键路径份额相关，没有做任何控制实验（比如人为加大/
  去掉 `CacheLock` 竞争看份额是否变化）去证明因果，如实记录为相关性
  观察。

## 18. 小时级别真实并行负载测试（§17.4 第一条遗留项的部分回应）

用户要求：先把 §17 的结果记录下来（已完成），再"进行小时级别的测试，看看
真实的并行负载下的情况是怎样的"。这一节记录那次跑的结果——**部分成功**：
拿到了跨 70 分钟真实 wall-clock 的稳定性数据，但没能拿到 hour-scale 的
关键路径 CSV（域级 last-arriver / 锁等待），原因是一个新发现的、跟 §16
修复的 UAF 不同的操作性缺口。

### 18.1 跑法

沿用 S-013 §10 均衡检查点 `x86-threads-balanced3-roi-classic`，不设
`MAX_TICKS`（即 §16.6 验证过的"跑到真实 `m5 exit`"模式），`STAT_DUMP_PERIOD=
2000000000`（每 2e9 tick 落一次 stats.txt），`EVENTQ_CRITPATH_TRACE=1`，
8 核心 `100-107`（核 92-99 当时被另一个并发会话的三臂对比任务占用，
54-55/92-99 已用，只有 100-111 空闲——按 CLAUDE.md 的按核互斥策略选了
100-107）。外层套了 `timeout 4200`（70 分钟）作为安全网，不是预期的
终止方式：

```
CHECKPOINT_DIR=/workspace/gem5-ckpt/x86-threads-balanced3-roi-classic \
PARALLEL_EVENTQ=1 SIM_QUANTUM_TICKS=6660 EVENTQ_BARRIER_MODE=spin \
HOST_PIN_CPUS=100,101,102,103,104,105,106,107 \
EVENTQ_CRITPATH_TRACE=1 STAT_DUMP_PERIOD=2000000000 \
timeout 4200 taskset --cpu-list 100-107 ./build/X86_MESI_Three_Level/gem5.opt \
  -d /tmp/s012-step5-hourscale docs/refs/scripts/x86_fs_mesi3_parallel_eventq.py
```

进程在 4200 秒（70 分钟）安全网到点后被 `timeout` 用默认信号
（`SIGTERM`）杀掉，退出码 124——这是预期内的"没跑到真实 `m5 exit`"，
不是崩溃。

### 18.2 拿到了什么：1039 个周期的 wall-clock 吞吐数据

`stats.txt` 长到 746 MB，包含 1039 个完整的周期性 dump（每个 2,000,006,660
tick，累计约 2.078×10¹² tick 的模拟窗口，压在 70 分钟真实时间里）。逐个
dump 的 `hostSeconds`（该 2e9-tick 周期消耗的真实墙钟秒数）：

- 稳态（跳过前 20 个周期的暖机）均值 3.01s，但中位数/众数落在
  2.63–2.72s 附近——吞吐大约 (2×10⁹ tick)/2.7s ≈ 7.4×10⁸ tick/s，
  跟 S-013 §10 短窗口测的速率量级一致，**长跑 70 分钟没有看到平均
  吞吐随时间系统性下降**（这是这次测试原本想确认的问题之一）。
- 但有 **17 个周期的 `hostSeconds` 超过 5 秒**，最大 121.44s（第 893
  个周期）和 60.29s（第 446 个周期），这 17 个异常周期加起来占了
  386.82 秒，即整个 70 分钟真实时间的 **12.4%**。异常周期在跑的前半
  段（1039 个周期的前 519 个）出现 12 次，后半段出现 5 次——不是简单
  的单调恶化，但两个最大的异常值（60.29s、121.44s）都出现在后半段。
  没有做根因排查（跟这次跑共存的还有另一个会话占用 92-99 核心的三臂
  任务，可能是共享 LLC/内存带宽争用；也可能是 746MB `stats.txt`
  增长带来的 I/O 开销；也可能是宿主机上无关的周期性任务）——如实记录
  为**未解释的长尾停顿**，不归因。

### 18.3 拿不到的东西：关键路径 CSV 全部丢失，一个新的操作性缺口

`EVENTQ_CRITPATH_TRACE=1` 本该在退出时把每个域的 critpath buffer 落盘成
`critpath-domain{0..7}.csv`（§13.4 起用于 `critpath_aggregate.py`），但
`/tmp/s012-step5-hourscale/` 下一个 CSV 都没有。

根因：`src/sim/simulate.cc` 里 `critPathFlush()` 是通过 Python 层
`atexit.register()` 挂的（§16.3/§16.5 的修复——特意选 atexit 而不是
signal handler，是为了避开 §16 那次 UAF）。`timeout` 默认发 `SIGTERM`，
而 Python 进程对 `SIGTERM` **没有默认处理器**，OS 直接终止进程，不会
触发解释器正常退出路径，因此不会跑 `atexit` 注册的回调——`critPathFlush()`
根本没被调用。相比之下，gem5 原生 `Stats` 框架的周期性 dump
（`STAT_DUMP_PERIOD`）是同步写入的，每次 dump 立即落盘，不依赖进程
干净退出，所以 §18.2 的 1039 个周期数据完整保留了下来。

这是一个跟 §15/§16 的 `critPathFlush()` UAF **不同的**缺口——§16 修的是
"正常退出路径下线程没 join 导致用后释放"，这次暴露的是"非正常退出路径
（外部信号杀掉）压根不会走到这条 atexit 路径"。两者都指向同一个更通用
的问题：`critPathFlush()` 目前只在真实 `m5 exit`（`simulate.run()` 正常
返回）时可靠，不管是崩溃、外部 kill，还是——理论上——`MAX_TICKS` 之外
的任何非正常终止路径，关键路径 CSV 都会丢失，而 `stats.txt` 不会。

### 18.4 这次测试对 §17.4 遗留项的净贡献

- §17.4 第一条"2-3 次重复跑评估直方图跑间稳定性"**仍未解决**——这次
  70 分钟跑没能提供任何新的关键路径直方图数据（§18.3），跟 §17.3 的
  单次 `2e9`-tick 样本没有可比对象。
- 新拿到的是一个 §17.3 没有覆盖的问题的答案："真实并行负载跑到小时
  尺度，wall-clock 吞吐是否稳定"——答案是稳态吞吐确实稳定（§18.2 第一
  条），但叠加了一个此前未知的、量级不小的长尾停顿现象（§18.2 第二
  条），值得作为后续投资"出路 3"（往哪塞工作）之前的一个新警示信号，
  而不是可以忽略的噪声——12.4% 的真实时间花在 17 个异常周期上，如果
  这些停顿被证明系统性存在（而非这次跑偶然撞上邻居任务），会直接影响
  用 wall-clock 时间做速度对比的可信度。
- 若要真正拿到 hour-scale 的关键路径 CSV，需要改用有界 `MAX_TICKS`
  （让 `simulator.run(max_ticks=...)` 正常返回，走干净退出路径），而
  不是用 `timeout` 外部杀进程；本节没有做这次重跑，留给下一步。

## 19. 有界 `MAX_TICKS` 重跑：拿到关键路径 CSV，同时定位到插桩自己的
一个问题

用户要求"跑一次有界 `MAX_TICKS` 重跑，找回关键路径 CSV"。这一节记录
那次重跑——**CSV 成功拿到了，但数据量和分析方法都撞上了新问题，而且
过程中意外定位到 §18 那 17 个"未解释长尾停顿"的真正根因，不是主机噪声，
是插桩自己的一个已知但从未测过的缺口**。

### 19.1 定规模：为什么选 `MAX_TICKS=1.34e12`，为什么不用 `/tmp`

沿用同一份均衡检查点。用户明确要求"保存 ~124GB 数据"，对应 §18 估算过
的"真正小时尺度"（约 45 分钟模拟时间）那个量级，不是当时 `AskUserQuestion`
里推荐的"~100x 温和挡"（约 18.5GB）——这是用户在澄清问题里明确覆盖掉的
选择。按 §18 记录的吞吐率（≈4.95×10⁸ tick/s）反推，`MAX_TICKS=
1,340,000,000,000`（1.34×10¹²）对应约 45 分钟模拟时间、约 124GB CSV。
`/tmp` 只有 16GB tmpfs（当时约 9-10GB 空闲），装不下，改用
`/workspace/shm/gem5/s012-eventq-critical-path-instrumentation-design/
critpath-bounded/`——这是一个独立于 `/tmp` 的、更大的 tmpfs（378GB
总容量，命令行核对过跑之前有 350GB 空闲），跟 CLAUDE.md 里
`build/` 落 `/workspace/shm/gem5/<branch>/build/` 那条约定用的是同一
个挂载点。

```
CHECKPOINT_DIR=/workspace/gem5-ckpt/x86-threads-balanced3-roi-classic \
PARALLEL_EVENTQ=1 SIM_QUANTUM_TICKS=6660 EVENTQ_BARRIER_MODE=spin \
HOST_PIN_CPUS=100,101,102,103,104,105,106,107 \
EVENTQ_CRITPATH_TRACE=1 STAT_DUMP_PERIOD=2000000000 \
MAX_TICKS=1340000000000 \
timeout 5400 taskset --cpu-list 100-107 ./build/X86_MESI_Three_Level/gem5.opt \
  -d /workspace/shm/gem5/s012-eventq-critical-path-instrumentation-design/critpath-bounded \
  docs/refs/scripts/x86_fs_mesi3_parallel_eventq.py
```

`timeout 5400`（90 分钟）这次只是安全网，不是预期的终止方式——`MAX_TICKS`
到点后 `simulator.run(max_ticks=...)` 应该正常返回，走干净退出路径，
触发 `atexit` 挂的 `critPathFlush()`。跑了约 46 分钟（14:15:47 启动，
stats.txt 最后写入约 15:02），**退出码 0，正常结束**，8 份 CSV 全部
落盘，单份约 14GB，共约 111GB（比 124GB 估算略低，量级吻合）。

### 19.2 聚合脚本 v1/v2（§13.4/§14.6）在这个数据量下不能直接用

`critpath_aggregate.py` 的 `aggregate()` 会把每一行都塞进
`groups = defaultdict(list)` 再统一处理——在 S-013 的 2×10⁹-tick
数据（约 500 万行）上没问题，但这次单个域文件就有 4.024×10⁸ 行
（`wc -l critpath-domain0.csv` = 402,402,524），8 个域合计约 32 亿行
barrier 记录。粗算这个 groups 结构会在 Python 对象开销下占到几百 GB
量级（每个 dict 键+8 个 4 元组的 Python 对象开销远超原始 CSV 字节数），
大概率把这台机器 653GB 空闲内存吃穿，而且 `csv.DictReader` 逐行跑
32 亿次在纯 Python 里也慢。**没有直接跑这个脚本**，改用 `awk`/`paste`/
`sort` 管道重新实现同样的聚合逻辑：

- 未知数 1（谁最后到达）和锁等待汇总（未知数 2）**其实不需要跨域
  join**——每个域自己文件里 `isLast==1` 的行数、`eventCount` 之和，
  或者每个 `lockTag` 的计数/耗时之和，都是纯粹域内的聚合，单个 `awk`
  单遍扫描就够（8 个域并行跑，几分钟内完成）。
- 只有"到达时间差"（`spread_ns`，跨域最大等待时间）真正需要跨域对齐。
  验证发现一个关键的简化：**8 个域文件里 `kind=="barrier"` 的行数
  完全相同**（`402,402,402`，`grep -c '^barrier,'` 逐域核对过），且
  由于这是全局屏障、8 个域每个 quantum 都参与两次 pass，域内部
  按 tick 严格递增——把 8 个域文件各自过滤出 barrier 行后按行号
  `paste` 在一起，天然对齐到同一个 `(tick, pass)` 分组，不需要真正
  的按键 join。跑完后用 `badcount=0` 核对了全部 4.024×10⁸ 组的
  tick/pass 在 8 个域之间逐行一致，这个简化是站得住的。
- 这套 awk/paste/sort 管道本身也踩了一次操作性教训：第一次直接在
  前台跑，撞上 Bash 工具默认 120 秒超时被杀掉（进程组被终止，产出的
  空文件说明什么都没算出来）；改成 `run_in_background: true` 之后
  没有再撞到这个问题。

这套 v1/v2 脚本在真正 hour-scale 数据量下不可用，是一个新发现的、
之前从未测过的规模上限——所有之前的关键路径分析（§13/§14/§17）都在
2×10⁸~2×10⁹-tick 窗口做的，从未在千倍窗口上跑过聚合脚本本身。

### 19.3 未知数 1：真实 hour-scale 下最后到达者分布，再次大幅改判

201,201,201 个 quantum（`MAX_TICKS/SIM_QUANTUM_TICKS` 取整）：

| 域 | pass1 份额 | pass2 份额 | pass1 最后到达时平均 eventCount |
|---|---|---|---|
| 2 | 16.6% | 16.3% | 2.0 |
| 6 | 13.7% | 13.7% | 1.0 |
| 3 | 13.3% | 13.3% | 1.8 |
| 7 | 13.0% | 12.8% | 1.0 |
| 5 | 12.8% | 12.8% | 1.0 |
| 4 | 12.8% | 12.8% | 1.8 |
| 1 | 10.9% | 11.1% | 2.8 |
| 0 | 6.9% | 7.2% | 1.0 |

（`badcount=0` 核对过：两个 pass 的份额总和都精确等于 201,201,201。）

跟 §17.3（真实 2×10⁹-tick 窗口，300300 quanta）的画像**再次发生实质
变化，而且是相反方向的变化**：§17.3 是"域 1/3/4 三分天下（21-31%
各），域 2 异常偏低（3.9%）";这次 670 倍长的窗口下，**域 2 反而是
份额最高的（16.6%），域 1 掉到了倒数第二（10.9%），八个域的份额
摊得相当平（6.9%-16.6%，不再有任何域压倒性主导或压倒性掉队）**。
这不是同一个结论的量级修正，是方向性的反转——§17.3 末尾"为什么恰好
是域 1/3/4""域 2 为什么持续偏低"这两个留白问题，在这份数据下已经
不成立了（域 2 不再偏低，域 1 也不再主导），意味着**关键路径的域
分布本身可能不是这个工作负载的稳定属性，而是随窗口长度系统性漂移
的**——S-013 §9 已经提醒过 2×10⁸ 窗口太短、大概率还在客户机启动
阶段，这次的结果说明就连 2×10⁹ 窗口（S-013/S-012 §17.3 一直当作
"真实长窗口"用的那个）相对于真正的小时尺度可能仍然不够长、仍然带着
某种未收敛的瞬态。

### 19.4 未知数 2：锁等待占比暴跌到可忽略——§17.3 的"~11%"结论不成立

汇总（`hostSeconds` 用排除了被 CSV flush 拖长的最后一个周期性 dump 后
的累计值，1,732.84 秒——`stats.txt` 里最后一条 `hostSeconds=400.28`
是退出时落盘 111GB CSV 的 I/O 时间混进了这个周期，不代表模拟吞吐，
排除它更准确）：

| 域 | CacheLock 次数 | CacheLock 总耗时 | ConsumerLock 次数 | ConsumerLock 总耗时 | 域内总锁等待占 hostSeconds |
|---|---|---|---|---|---|
| 3 | 1,569,052 | 1,695,288,438 ns | 2,634 | 12,493,570 ns | **0.099%** |
| 4 | 1,635,159 | 1,753,155,162 ns | 2,488 | 11,283,683 ns | **0.102%** |
| 2 | 174,989 | 336,856,161 ns | 2,368 | 12,227,440 ns | 0.020% |
| 1 | 117,045 | 166,466,197 ns | 1,300 | 7,896,369 ns | 0.010% |
| 0/5/6/7 | 0/35/0/0 | — | 632-1598 | ~4-6M ns | <0.001% |

§17.3 的结论是"`CacheLock` 在域 1/4 占 ~11% `hostSeconds`，是真实但
次要的成分"；这次 670 倍长窗口下，同样是域 3/4 的 `CacheLock`（域号
对不上是因为 §17.3 用的是原始检查点、这次是均衡检查点，域布局不完全
一样），占比却只有 **0.1% 左右，掉了两个数量级**。用 tick 十等分
检查过锁等待是否集中在开局瞬态（怕重蹈 S-013 §9 的覆辙）——**不是**：
域 3/4 十个十分位区间里锁等待耗时相当均匀（每段 1.3-1.4 亿 ns 左右），
只有第 5 个十分位（40%-50% 处）明显偏高（域 3 4.30 亿 ns、域 4 4.53
亿 ns，约 3 倍于其余区间）——这个偏高区间没能跟 §19.5 那个已定位的
停顿根因对上（见下），留白，未解释。**结论：§17.3"锁竞争是真实但
次要成分"这个结论本身在真正小时尺度下也不成立，锁等待在真实工作
负载里几乎可以忽略（<0.15%）**——这是这次会话第二次看到"短窗口
（无论是 §13.5 短窗口的域 1 主导,还是 §17.3 的 2e9-tick 锁等待占比）
在真正长窗口下被推翻"，进一步佐证 §19.3 末尾"关键路径画像可能系统性
随窗口漂移"这个猜测。

### 19.5 意外发现：§18 那 17 个"未解释长尾停顿"，根因就是插桩自己——
`critPathBuffer` 没设 `reserve()`

跨域到达时间差（`spread_ns`，8 个域按行 `paste` 对齐后取每组
最大等待）：

| pass | min | p50 | p95 | p99 | max |
|---|---|---|---|---|---|
| 1 | 773 ns | 2274 ns | 3009 ns | 26905 ns | **10,011,050,735 ns**（10.01s） |
| 2 | 767 ns | 1749 ns | 2555 ns | 3718 ns | **10,148,007,785 ns**（10.15s） |

p99 到 max 差了 5-6 个数量级——pass1 里 dur_ns > 1ms 的组只有 402 个
（out of 2.012 亿），> 100ms 的只有 19 个；pass2 是 366 个和 37 个。
这是一条极端厚尾，值得深挖而不是当噪声略过。

把 dur_ns > 100ms 的原始行单独拉出来看（393 行），发现一个非常干净
的模式：**每次出现，都是同一个 tick 上，7 个域同时报出几乎相同的
巨大 `dur_ns`（域间差距在几百纳秒内），缺席的第 8 个域就是那个
quantum 真正的"最后到达者"**——不是随机的某个域偶尔卡顿，是"某一个
域自己拖慢了，另外 7 个域全在等它"。把这些事件按 `(tick-起始tick)/
2e9` 换算成"第几个周期性 dump 区间"分组，结果是：

| 周期区间 | 该区间内最大 dur_ns |
|---|---|
| 6 | 109,998,610 ns |
| 13 | 219,802,998 ns |
| 27 | 433,554,073 ns |
| 55 | 1,106,840,387 ns |
| 111 | 2,535,498,922 ns |
| 222/223 | 5,061,569,857 ns |
| 444/445/446 | 10,148,007,785 ns |

**周期区间号和停顿时长同步、几乎精确地每次翻倍**（6→13→27→55→111→
222→444，公比≈2.0；110ms→220ms→434ms→1.1s→2.5s→5.06s→10.15s，
同样公比≈2）。换算成"这个域当时已经往自己的 `critPathBuffer` 里
推了多少条记录"（barrier 记录约 2 条/quantum，一个周期区间约
300300 个 quantum，约 600600 条/区间）：区间 6/13/27/55/111/222/444
分别对应约 2²²/2²³/2²⁴/2²⁵/2²⁶/2²⁷/2²⁸ 条记录——**跟 2 的幂次精确对上
（算出来是 6.98/13.97/27.94/55.87/111.75/223.5/447，跟实测的
6/13/27/55/111/222-223/444-446 几乎逐一吻合）**。

**根因confirmed**：`critPathBuffer`（`src/base/critpath_trace.hh`
的 `thread_local std::vector<CritPathRecord>`）这次跑用的是默认
`EVENTQ_CRITPATH_RESERVE=0`（§14.4 设计的"预留容量"从落地到现在
第一次真正在非零规模下被间接验证——不是直接测 nonzero 值，是测出了
"不设它会怎样"），`std::vector` 在容量不够时按几何增长（libstdc++
默认扩容因子 2）重新分配+搬迁全部已有元素——**每次扩容的拷贝耗时
正比于当时的元素数量，而扩容点本身又落在 2 的幂次上，于是拷贝耗时
和触发时机同步翻倍**，正是这张表的形状。8 个域各自独立增长、各自
独立触发扩容，同一时间窗口里通常有好几个域前后脚触发（例如区间
444/445/446 三个区间合计 8 次触发事件，刚好对应 8 个域）——这也
解释了为什么"缺席的那个域"在不同事件里不总是同一个域（域 4、域 1、
域 3 都出现过）：不是某个域本身有问题，是 8 个域各自的
`critPathBuffer` 各自独立触发同一种扩容开销，只是触发时机因为各域
推入速率的细微差异（有没有锁等待记录、events 计数等）而互相错开
几个 quantum。

**这直接解释了 §18 那 17 个"未解释长尾停顿"里的大多数**：§18 记录的
异常周期索引是 `{0,1,27,55,111,222,223,294,444,445,446,778,779,780,
890,893}`（那次是 `MAX_TICKS=0` 无界跑，同一份检查点、同样的
`STAT_DUMP_PERIOD=2e9`）。本节这次有界重跑独立测到的停顿周期索引是
`{6,13,27,55,111,222,223,444,445,446}`——**27/55/111/222/223/444/
445/446 完全重合**（§18 的 0/1 是暖机期，294 和 778-780 不吻合，见
下）。而且按同一套"2 的幂次"外推：2²⁹≈536,870,912 条记录，换算周期
索引≈894——**跟 §18 的 893 几乎精确对上**，进一步支持这个机制。
**§18 原文"未解释的长尾停顿"这个判断需要改判**：这不是主机噪声，
不是邻居任务争用，**是这次调查自己加的插桩基础设施（`critPathBuffer`
未设 `reserve()`）的副作用**——`STAT_DUMP_PERIOD` 周期性 dump 的
`hostSeconds` 无辜地把插桩自己的开销也计进了"模拟吞吐"里。

**没有解释的部分，如实记录**：§18 的周期 294 和 778-780 三个索引，
本节的机制推算不出对应的 2 的幂次（294 不在任何 2^k/600600 附近；
778-780 落在 2²⁹≈894 之前，中间没有下一个二次方阈值）——这两组
仍然是未解释的，可能是真正的主机噪声/邻居任务争用，也可能是
另一个尚未定位的机制，不应该被本节的解释一并带过。

**对 §18 吞吐稳定性结论的影响**：§18"长跑 70 分钟没有看到平均吞吐
随时间系统性下降"这个结论本身不受影响（那是排除异常周期后的稳态
均值），但"12.4% 的真实时间花在未解释的长尾停顿上"这句话需要改判为
"这些停顿的大部分（很可能是全部关键路径插桩本身在真正 hour-scale
才会触发的一次性代价，不是并行 EventQueue 机制或工作负载本身的
性质"——**如果关掉 `EVENTQ_CRITPATH_TRACE`（生产 wall-clock 测量
不需要它），这些停顿大概率不会出现**；换句话说，§18 那次跑测到的
"12.4%"高估了真实并行仿真在无插桩场景下的长尾停顿比例。

### 19.6 已知局限（如实记录）

- **§19.2 的 awk/paste/sort 管道没有跟 `critpath_aggregate.py` v1/v2
  在同一份数据上做过交叉验证**——两者用的是不同规模的数据集（v1/v2
  只验证过 S-013 的 2×10⁹-tick 数据），这次的新管道本身的正确性
  依赖 §19.2 记录的"8 个域 barrier 行数逐行对齐"这个断言（`badcount
  =0` 核实过），但没有反过来用小数据集跑一遍新旧两条管道对比数字
  是否一致——建议下次先在 S-013 那份小数据上跑通新管道、跟已发表的
  §17.3 数字核对一致后再信任大数据的结果。
- **§19.5 定位的根因没有做修复验证**——没有设置 `EVENTQ_CRITPATH_
  RESERVE` 重跑来确认停顿消失，这次的证据链是"周期索引和公比精确
  匹配 2 的幂次增长模型"这个强相关性，不是直接的对照实验。
- **294 和 778-780 两组停顿仍未解释**（§19.5 结尾），不应被这次
  发现的机制掩盖。
- **§19.3/§19.4 两个"短窗口结论被长窗口推翻"的发现，进一步削弱了
  §17.3 数据本身的可信度，但没有反过来证明"1.34×10¹² tick 就足够
  长"**——§19.3 末尾已经指出"关键路径画像可能系统性随窗口漂移"这个
  猜测，本节的数据本身也可能仍然没有收敛，只是没有更长的窗口去验证。
- **§17.4 第一条遗留项（2-3 次重复跑评估直方图稳定性）依然没有做**——
  这次是单次跑，不是重复跑；而且鉴于 §19.3/§19.4 显示结论本身还在
  随窗口长度漂移，"重复同一窗口两次"能回答的"是否可复现"和"是否已
  经收敛到稳定值"是两个不同的问题，本条目目前只能回答不了后者。
- **中间产出文件占用了可观的磁盘/tmpfs 空间**——原始 CSV 约 111GB
  （`/workspace/shm/gem5/s012-eventq-critical-path-instrumentation-
  design/critpath-bounded/`），§19.2 的中间聚合文件（`reduced-
  domain*.csv`、`spread-pass*-sorted.txt` 等）另有约 20GB
  （`.../agg/`），都留在 tmpfs 上未清理，会在下次机器重启时随 tmpfs
  清空一起消失，但如果需要保留应该主动搬到非 tmpfs 磁盘。

---

**上一篇**：[S-011：Consumer 锁 owner 字段竞争审计](./S-011-consumer-lock-owner-race-audit.md)
**返回**：[INDEX.md](./INDEX.md)
