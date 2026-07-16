# S-009 — 设计：把 FS quantum 上限从 classic iobus 边抬到 Ruby 合法值（未实现）

**状态：设计+审计已完成，未实现，无代码改动**。这是 S-008 §15.4 缺口算术投影之后，
用户先要求做设计（17/19 节），再明确要求做跨域线程安全审计（18 节）；本文档只做
设计和只读的代码审计，不动代码。审计把最初设计（"两个孤立调用点"）纠正成了一个
更大范围的问题（共享基础设施 + 真实数据竞争，不是理论风险），17.2/17.3/18/19 节
都已按审计结果原地更正，不是并列保留新旧两版结论。实现前再找用户对一遍这份设计。

## 17. 精确定位：Q=300 到底被哪条边摁住的

### 17.1 S-003 §8.1 的规则，重新写一遍公式

quantum 屏障只保证任意两个域之间的时钟漂移 `< Q`，不保证为零。任何一次跨域
`EventQueue::schedule(event, when)` 调用（`src/sim/eventq.hh:781-807`），
`when` 是发起方用**自己域的 curTick()** 算出来的，但断言检查的是`when >=
getCurTick()`——这个 `getCurTick()` 读的是**目标域**的时钟（`eventq.hh:784`）。
两个域之间最坏情况下能漂移 `Q-1` 个 tick，所以要保证断言恒成立，`when` 相对
发起方自己 `curTick()` 的偏移量必须 `>= Q-1`，也就是**这次调度用的"延迟"必须
不小于 Q**——这就是 S-003 §8.1"quantum 要比链路延迟小"这条规则的另一种说法。

### 17.2 目前"合法"的边和"卡住"的边，分类整理

**这一节是审计后的修正版——比最初设计稿里写的范围大得多，17.2/17.3 原文对
"只有 RubyPort.cc 两处"的判断是错的，按工作方式原地更正，不留旧结论。**

| 边 | 机制 | 延迟来源 | 现状 |
|---|---|---|---|
| Ruby int_link（核内 L1↔L2、核间到 L3/目录） | `MessageBuffer`/`Consumer` + per-consumer 锁（S-002/S-003 已加固） | `il.latency = LINK_LATENCY`（脚本里显式设成 20 周期 = 6660 tick，两臂对称） | **已经支持 Q 到 ~6660**，S-008 用的就是这个上限 |
| 中断投递唤醒（IOAPIC/PIC → 目标核的 local APIC → `activateContext`） | 经 `Consumer::wakeup()`/`recvMessage` 同步跨线程调用，最终落到 `cpu/simple/timing.cc` 里一次 `schedule()` | S-006 §11.5 已经把这次 `schedule()` 的 `when` 改成**锚定 barrier 网格**的 snap 公式，不再依赖任何具体延迟数值 | **已经对任意 Q 安全**——这条边不是 S-009 要修的对象，11.5 已经解决 |
| **经典 Port 的每一次跨域 timing response/request 调度**（`RubyPort.cc` 的 `PioRequestPort`/`MemRequestPort`，**以及 iobus 下游每一个 `SimpleTimingPort` 设备**：UART、RTC、PIT、PIC、IOAPIC、`IsaFake` 等南桥全家桶） | **都收敛到同一个共享函数**：`PacketQueue::schedSendTiming`/`schedSendEvent`（`src/mem/packet_queue.cc:106-176`），`QueuedRequestPort::schedTimingReq`/`QueuedResponsePort::schedTimingResp`（`mem/qport.hh:94-95/150-151`）只是转发到这里；`SimpleTimingPort::recvTimingReq`（`mem/tport.cc:63-80`，几乎所有简单 PIO 设备的基类）自己也调用同一条链路 | **每个调用点各自的 `curTick() + X`**：`RubyPort.cc` 两处硬编码 `owner.m_ruby_system->clockPeriod()`（1 ruby 周期 ≈ 333 tick）；每个 `SimpleTimingPort` 设备是 `curTick() + recvAtomic() 返回的 latency`（**逐设备不同**，没有统一值） | **卡住 Q=300 的表面上是 `clockPeriod()`=333 那两处，但抬过 333 之后，下一个卡住的大概率是某个具体设备自己的 `recvAtomic()` 延迟**——17.2 的旧版本只看到了第一层，没看到这是一整类问题 |

**为什么之前以为是 IOXBar 的 `forward_latency`/`response_latency` 参数**：这两个
Cycles 参数（默认 2/1/2 周期）确实存在、也确实控制 IOXBar 内部转发排队的时序，
但它们只影响 `pkt->headerDelay`/`payloadDelay` 的标注，**从不出现在任何跨域
`schedule()` 调用里**——`NoncoherentXBar::recvTimingReq/recvTimingResp`
（`src/mem/noncoherent_xbar.cc:101-177/179-`）只是直接同步调用下一跳端口，IOXBar
自己不跨 eventq 调度任何东西。这个判断没错，但**下一步"所以只有 RubyPort.cc
两处"是错的**——见下面 17.3。

### 17.3 更正：这不是两个点的 bug，是一整条经典 Port 链路共享的一个 bug

顺着 `RubyPort::PioResponsePort::recvTimingReq` 往下追（一次跨域 PIO 请求从
Sequencer 域 i+1 发起，同域 `schedule()` 到 `memRequestPort`，`sendTimingReq`
直接同步调用穿过 `NoncoherentXBar::recvTimingReq`（域 0，但仍在域 i+1 的线程
上执行）、南桥、一直到具体设备（`UART`/`RTC`/`PIT`/`PIC`/`IOAPIC`/`IsaFake`
等，几乎全部继承自 `PioDevice`，用 `SimpleTimingPort`）——**这些设备的
`recvTimingReq` 全部走 `SimpleTimingPort::recvTimingReq`
（`mem/tport.cc:63-80`）**：

```cpp
bool SimpleTimingPort::recvTimingReq(PacketPtr pkt) {
    ...
    Tick latency = recvAtomic(pkt);   // 设备自己算的处理延迟
    schedTimingResp(pkt, curTick() + latency);
    ...
}
```

`curTick()` 这里读的是**调用线程**的 `curEventQueue()`，而调用线程是发起
这次 PIO 请求的**域 i+1 的线程**（因为整条链路从 Sequencer 到设备都是裸同步
虚函数调用，从未切换过线程）——**不是设备自己域（域 0）的 curTick()**。
`schedTimingResp`/`schedTimingReq` 最终都落到 `PacketQueue::schedSendEvent`
（`packet_queue.cc:154-176`）：

```cpp
when = std::max(when, curTick() + 1);   // 同样读的是调用线程的 curTick()
...
em.schedule(&sendEvent, when);          // em 永远正确地指向设备自己的域
                                         // （构造时绑定，不受调用线程影响）
```

`em`（目标域）永远是对的，**错的是 `when` 用了错误线程的 `curTick()` 去算**。
`em.schedule()` 内部的 `EventQueue::schedule()` 断言检查的是 `em` 自己域的
`getCurTick()`（`eventq.hh:784`）——如果域 0 恰好领先域 i+1 超过这次调用算出
的延迟量，断言就会炸。**`RubyPort.cc` 的两处只是这一整条链路里"离 CPU 最近
的那一跳"，恰好先被 S-008 的 Q=300→333 边界撞到；一旦 Q 抬过 333，下一个
挡路的大概率是某个具体设备（南桥的 UART/RTC/PIT/PIC/IOAPIC/IsaFake 之类）
自己 `recvAtomic()` 算出来的延迟，逐设备不同，没法用一张表穷举**。

这仍然是"第三个同类 bug"（S-003 §8.1、S-006 §11.5 是第一、第二个）——为
单 EventQueue 世界写的代码，局部 `curTick()+常量`，不知道自己可能被跨域
调用——只是这次的病灶不是两个孤立调用点，而是**一整条链路共享的一个基础设施
函数**（`PacketQueue::schedSendEvent`），这个定位直接决定了 19 节的设计要
改在哪一层。

## 18. 跨域线程安全审计（已完成排查——结论：确认存在真实的、结构性的数据竞争）

这一节原来只是"标记待查"，现在审计做完了，结论比预期更明确：**这不是需要
靠运气才会触发的潜伏 bug，是这个拓扑下架构性地必然可达的数据竞争**。

### 18.1 排查方法

`grep` 遍历这条链路涉及的每一个文件（`RubyPort.cc/.hh`、`noncoherent_xbar.cc`、
`packet_queue.cc/.hh`、`qport.hh`、`tport.cc/.hh`，以及 `src/dev/x86/`、
`src/dev/` 下 PIO 设备的 `.cc`），找 `mutex`/`lock`/`std::atomic` 等同步原语：
**一个都没有**。然后顺着 17.3 追出来的完整调用链，逐段确认"这一段实际在哪个
线程上执行、touch 了谁的状态"。

### 18.2 竞争点在哪、为什么"结构性必然可达"而不是运气问题

关键结构事实：**iobus（`self.iobus`）、南桥、南桥下所有设备，都是域 0 里
唯一的一份实例——所有核（域 2..N+1）想做 PIO 都要经过同一组对象**。而
17.3 已经确认：一次跨域 PIO 请求的整条链路（`Sequencer.memRequestPort/
pioRequestPort → IOXBar → 南桥 → 具体设备 → 原路返回`）全程在**发起请求的
那个核自己的域线程**上执行，从未真正切到域 0 的线程。

推论：**如果核 2（域 3）和核 3（域 4）在同一个 quantum 窗口内都发起了一次
PIO 请求**（现实场景：两个核都在处理各自的中断、都要读/写 IOAPIC 或 PIC 或
RTC），它们的线程会**同时**、**各自独立地**执行到同一个 IOXBar 实例的
`recvTimingReq`/`recvTimingResp`（`noncoherent_xbar.cc`），同时读写
`reqLayers[]`/`respLayers[]`（每个目的端口一个 `Layer` 对象，记录"这个端口
当前是否被占用/什么时候空闲"的忙闲状态机）和 `routeTo`（`std::unordered_map
<RequestPtr, PortID>`，记录每个在途请求该往哪个端口送响应）——**这两个都是
普通容器，没有锁，被两个不同线程同时读写**。`routeTo` 更明确：`recvTimingReq`
里 `routeTo[pkt->req] = cpu_side_port_id`（插入），`recvTimingResp` 里
`routeTo.erase(route_lookup)`（删除）——如果核 2、核 3 的请求前后脚落在同一个
`unordered_map` 上，插入/删除/查找并发执行是**教科书式**的 STL 容器数据竞争
（不是"低概率角落场景"，只要两个核都有 PIO 流量、时间上有重叠就会发生，
只是不是每次都会崩——UB 的典型特征）。同样的论证对南桥下每个具体设备的内部
状态（比如 PIC 的中断屏蔽寄存器、PIT 的计数器）也成立：**只要两个核都可能
访问同一个设备，就有竞争**，跟 Q 的大小完全无关——即使 17.3 的 snap 修好了、
断言不再炸，这个竞争依然在，只是从"崩溃"变成"静默数据损坏"，更难发现。

**这和 S-002/S-003 为 Ruby 自己的 `Consumer`/`MessageBuffer` 做的事情性质
完全一样，只是这次是对经典 Port 世界，而且经典 Port 世界目前完全没做**。
S-008 的定窗测试没有暴露它，最合理的解释是：这个特定窗口/负载里多个核的
PIO 流量在时间上恰好没有重叠到能让 `unordered_map` 的内部状态真正被破坏
（竞争"发生"和"造成可观测后果"是两回事，尤其是像 `unordered_map` 这种在
比较小的负载下不太容易触发内部重新哈希/结构调整的场景）——不是"审计后
确认没问题"，是"目前的测试窗口没有强到足以稳定复现"。

### 18.3 这对 S-009 范围意味着什么

**19 节的 snap 修法只解决"断言会不会炸"（正确性的必要条件），完全不解决
18.2 这个数据竞争（正确性的另一个必要条件）**。两者是正交的，缺一不可：
只做 19 节，Q 抬高后要么断言炸（如果 snap 没覆盖到某个设备），要么断言不炸
但静默产生错误结果（数据竞争造成的状态损坏，S-008 那种"逐字节 diff"的验证
方法**不保证能测出来**——竞争是否触发取决于两个线程的相对时序，同一个配置
跑多次结果可能不一样，这正是 S-003 §8.4-8.6 反复强调的教训）。

**修复方向**（具体实现留给下一轮，这里只定方向）：给共享的经典 Port 状态
加锁，参照 S-002 的方法——最小的加锁范围大概率是 IOXBar 实例本身一把锁
（保护 `reqLayers`/`respLayers`/`routeTo`），加上每个可能被跨域触达的设备
一把锁（或者证明某个具体设备实际不可能被多核并发访问，比如如果每个设备的
地址范围本来就被设计成只有一个核会访问，需要读代码/读 x86 platform 的地址
分配逐个论证，不能假设）。工作量和 S-002/S-003 当年给 Ruby 做的事情是同一
量级，**不是"顺手在 19 节的 snap 旁边捎带做了"能打发的**。

## 19. 设计更正：把 grid-anchored snap 放在 `PacketQueue::schedSendEvent` 这一个
共享点，不是 `RubyPort.cc` 的两处

**这一节也是审计后的修正版**：17.3 确认卡住 Q 的不是两个孤立调用点，是
`RubyPort.cc` 和每一个 `SimpleTimingPort` 设备共享的同一条基础设施
（`PacketQueue::schedSendEvent`）。原 19.1-19.2"按点修、不做通用方案"的
结论建立在"只有两个点"这个已经被推翻的前提上，19.2 反对的"通用 snap"说的
是在 `EventQueue::schedule()` 这一层做——那个反对理由（会连累 Ruby int_link
已经验证安全的消息）依然成立，但 `PacketQueue::schedSendEvent` 是**另一个、
更合适的公共点**，19.2 的反对理由不适用于这一层，见下面。

### 19.1 为什么 `PacketQueue::schedSendEvent` 是对的落点

这个函数（`packet_queue.cc:154-176`）是 `QueuedRequestPort`/
`QueuedResponsePort`（因此也是 `SimpleTimingPort`，因此也是几乎所有经典
PIO 设备）发送任何 timing 包的唯一入口，函数签名自带一个 `EventManager
&em`——**永远正确地绑定着这个 port 所属对象自己的域**（构造时绑定，不受
调用线程影响，17.3 已确认）。这意味着在这一个函数里，能同时拿到"目标域是
哪个"（`em.eventQueue()`）和"当前实际在哪个线程上执行"
（`curEventQueue()`，`sim/eventq.hh`），两者一比较就知道这次调用是不是
跨域——**这正是 `EventQueue::schedule()` 那一层做不到的事**（19.2 原文的
反对理由：那一层拿不到"这次调度本该有多少物理延迟"，没法只在真正跨域时才
生效；`PacketQueue::schedSendEvent` 不受这个限制，因为它天然只服务经典
Port 世界，永远不会被 Ruby 的 int_link 消息调用到——Ruby 的跨域消息走的是
完全不同的 `MessageBuffer`/`Consumer` 路径，从不经过 `PacketQueue`）。

### 19.2 具体改动

```cpp
// packet_queue.cc, PacketQueue::schedSendEvent，复用 S-006 §11.5 引入的
// simQuantumStart/simQuantum 全局锚点（sim/eventq.{hh,cc}）：
void
PacketQueue::schedSendEvent(Tick when)
{
    if (waitingOnRetry) { ... }          // 不变

    if (when != MaxTick) {
        when = std::max(when, curTick() + 1);   // 原有逻辑，不变

        // 新增：只在真正跨域时才生效，本地调用（curEventQueue() ==
        // em.eventQueue()，包括串行模式——此时全局只有一个队列，这个判断
        // 恒假）完全不受影响。
        if (inParallelMode && curEventQueue() != em.eventQueue()) {
            when = std::max(when,
                simQuantumStart +
                divCeil(when - simQuantumStart, simQuantum) * simQuantum);
        }

        if (!sendEvent.scheduled()) {
            em.schedule(&sendEvent, when);
        } else if (when < sendEvent.when()) {
            em.reschedule(&sendEvent, when);
        }
    } else { ... }                        // 不变
}
```

**一次改动，覆盖 17.2 表格里"经典 Port 跨域调度"那一整行**——`RubyPort.cc`
的两处、每一个 `SimpleTimingPort` 设备，全部自动获得保护，不需要逐个找、
逐个改。`RubyPort.cc` 原来的 `curTick() + owner.m_ruby_system->
clockPeriod()` 这行代码**不需要动**：它算出来的 `when` 照样传给
`schedTimingResp`→`schedSendTiming`→`schedSendEvent`，只是在 `schedSendEvent`
这一层，如果这个 `when` 不够安全，会被兜底抬高到网格边界——原调用点完全不用
知道自己是不是跨域，这是这个设计比 19.1 旧版本"每个调用点自己判断"更省事、
也更不容易漏掉新设备/新调用点的地方。

### 19.3 为什么不是"抬高 IOXBar 参数"或者"在 `EventQueue::schedule` 里做通用 snap"

- **抬高 IOXBar 参数**：17.2 已经排除——IOXBar 的 `Cycles` 参数根本不出现在
  跨域 `schedule()` 调用里，改了也不解决断言问题。
- **在 `EventQueue::schedule()` 的跨域分支里做通用 snap**：19.1 已经说明为
  什么不选这一层——会连累 Ruby int_link 那些已经用真实延迟值（ll=20）保证
  安全、不需要也不应该被再延迟的消息，且这一层拿不到"是否真的需要 snap"
  所需的上下文。`PacketQueue::schedSendEvent` 没有这个问题，是更合适的
  公共点。

### 19.4 对既有 timing-neutral 不变式的影响

19.2 的 `std::max` 设计保证：
1. 串行模式：完全不变（`inParallelMode==false` 分支永远不触发 snap；就算
   忘了这个判断，串行模式下 `curEventQueue() == em.eventQueue()` 恒成立，
   条件也不会满足——两层保险）。
2. 并行模式、这次调用原本的 `when` 已经足够安全（比如 Q 仍然小于这次调用的
   物理延迟）：`std::max` 总是保留原始值，**行为不变**，S-008 已经验证过的
   "并行 stats.txt 与串行逐字节相同"这条结论不受影响。
3. 并行模式、Q 被抬过某次具体调用的物理延迟（S-009 的目标场景，17.3 已经
   说明这条边界因设备而异，不是单一数值）：这次响应会被 snap 到网格边界，
   比"真实"延迟晚最多接近一个 Q——**这是一次新的、之前从未测过的真实时序
   松弛，不再是"机制替换、结果不变"，而是"真的会让某些经典 Port 响应变
   慢"**。S-008 的缺口算术投影（0.33x → ~0.92x）是假设 simInsts 不变算出
   来的；这个假设在跨过这条边之后不再自动成立，必须重新测，不能直接当
   结论用。

## 20. 实验协议（供实现阶段参考，本次不执行）

1. 18 节的线程安全审计已经完成、确认了真实竞争；实现阶段要先给共享的经典
   Port 状态（IOXBar 的 `reqLayers`/`respLayers`/`routeTo`，以及可能被
   多核并发访问的具体设备）加锁（18.3 的修复方向），这一步和下面的 snap
   工作量相当，不是小任务，需要单独规划、可能需要 TSan 验证加锁是否完备。
2. 实现 19.2 的 `PacketQueue::schedSendEvent` 改动。用 S-003 §8.7 验证计划
   的方法论：先跑几次确认 `assert(when >= getCurTick())` 不再在 Q 抬高后
   触发。
3. **不要一步跳到 Q=6660**：先小幅抬（比如 Q=1000）确认没有新的、之前没
   见过的墙（S-006 §11.4-11.5 的教训——每次拓扑/参数变化都可能踩到新墙，
   宁可多花一轮先在小 Q 增量上排除掉，再往上抬）。
4. 确认 Q=1000 稳定后，抬到 S-008 §15.4 算出的目标 Q≈6660（20 ruby 周期，
   和 Ruby int_link 的 `LINK_LATENCY=20` 保持同一个物理假设，两条路径统一）。
5. 每一步都用 S-008 §15 的方法做 A/B（serial vs spin，2 轮，simInsts +
   `diff stats.txt` 对照，`taskset`/`HOST_PIN_CPUS` 复用现有的
   `isolcpus=54-55,92-111` 隔离核配置）。

## 21. 验证计划

- 正确性：每个 Q 台阶都跑 serial + parallel-spin 各 2 轮，比较
  `simInsts`/完整 `stats.txt`（排除宿主计时字段）。**预期从某个 Q 开始
  两者会出现真实差异**（19.3 point 3）——如果出现，如实记录差多少、体现在
  哪些统计量上，不能当 bug 处理、也不能忽略不提。
- 性能：每个 Q 台阶记录 `hostSeconds` 中位数，与 S-008 §15.2 的 0.33x
  基线放在同一张表里，验证 S-008 §15.4 的 ~0.92x 投影是否成立。
- 结果无论好坏都要写回一份新 spec（`S-010` 或按到时候的下一个空号），不
  在 S-009 里预写"结论"——这份文档到此为止只是设计。

## 22. 风险与未决问题

- **18 节确认的数据竞争是目前最大的工作量来源**：加锁范围（IOXBar 一把锁
  是否够、哪些设备需要各自的锁、哪些设备能论证不需要）需要在实现阶段逐一
  过一遍南桥下的设备清单，量级和 S-002/S-003 当年给 Ruby 做的事情相当，
  是本设计目前最大的不确定性和最大的工作量，不是"顺手做了"。
- 19.2 的判断依赖 `inParallelMode`/`simQuantumStart`/`simQuantum` 三个
  全局量在 `packet_queue.cc` 里是否已经可见（`eventq.hh` 是否已经被这个
  翻译单元包含，以及 `curEventQueue()`/`em.eventQueue()` 的比较在这个
  上下文里是否线程安全——`curEventQueue()` 读的是调用线程自己的 TLS，
  `em.eventQueue()` 读的是构造时绑定、之后不再变的指针，两者都不涉及跨
  线程的可变共享状态，预期安全，但实现时要确认）——实现时需要确认。
- 17.2/17.3 的调用链分析基于目前读过的代码（`RubyPort.cc`、
  `noncoherent_xbar.cc`、`packet_queue.cc`、`qport.hh`、`tport.cc`、
  `cpu/simple/timing.cc`），不是穷举式 grep 全代码库的结果——19.2 的设计
  由于落点在共享基础设施而不是逐个调用点，理论上不再需要穷举每个调用点，
  但仍然应该在实现前对 `schedTimingReq\(|schedTimingResp\(` 全仓库
  grep 一次，确认没有绕过 `PacketQueue`/`QueuedPort` 机制、自己直接调
  `EventQueue::schedule()` 的例外情况（比如某些设备可能没有用
  `SimpleTimingPort`/`QueuedResponsePort`，而是自己手写了 `recvTimingReq`
  的调度逻辑）。

## 检查点

这份文档到此为止。**下一步（18 节的加锁工作 + 19 节的 `PacketQueue`
改动）按项目工作方式需要单独请示**——18 节的加锁范围目前还没有逐个设备
过一遍，工作量可能比预期大；19 节的改动虽然定位到了一个干净的公共点，
实现阶段大概率仍会现场撞上没预见到的墙（S-003/S-006 的历史经验：新拓扑/
新参数几乎每次都有意外）。请求用户确认：是先做 18 节的加锁范围排查（把
"南桥下有哪些设备、哪些会被多核并发访问"这张清单列出来），还是直接开始
19 节的 `PacketQueue` 改动（先解决断言，加锁工作单独排期）。
