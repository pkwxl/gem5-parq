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

---

**上一篇**：[S-011：Consumer 锁 owner 字段竞争审计](./S-011-consumer-lock-owner-race-audit.md)
**返回**：[INDEX.md](./INDEX.md)
