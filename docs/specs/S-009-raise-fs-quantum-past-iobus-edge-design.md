# S-009 — 设计：把 FS quantum 上限从 classic iobus 边抬到 Ruby 合法值（未实现）

**状态：设计稿，未实现**。这是 S-008 §15.4 缺口算术投影之后，用户明确要求的下一步
设计文档；本文档只做设计，不动代码。按项目工作方式，这是新架构territory（一个
之前只在一个点上验证过的修复模式，现在要判断是否要推广/搬到第二个点），实现前
再找用户对一遍这份设计。

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

| 边 | 机制 | 延迟来源 | 现状 |
|---|---|---|---|
| Ruby int_link（核内 L1↔L2、核间到 L3/目录） | `MessageBuffer`/`Consumer` + per-consumer 锁（S-002/S-003 已加固） | `il.latency = LINK_LATENCY`（脚本里显式设成 20 周期 = 6660 tick，两臂对称） | **已经支持 Q 到 ~6660**，S-008 用的就是这个上限 |
| 中断投递唤醒（IOAPIC/PIC → 目标核的 local APIC → `activateContext`） | 经 `Consumer::wakeup()`/`recvMessage` 同步跨线程调用，最终落到 `cpu/simple/timing.cc` 里一次 `schedule()` | S-006 §11.5 已经把这次 `schedule()` 的 `when` 改成**锚定 barrier 网格**的 snap 公式，不再依赖任何具体延迟数值 | **已经对任意 Q 安全**——这条边不是 S-009 要修的对象，11.5 已经解决 |
| **PIO/MMIO 响应**（Sequencer 的 `PioRequestPort`/`MemRequestPort` 收到来自 iobus/设备的响应，转发回自己域） | **直接 `curTick() + owner.m_ruby_system->clockPeriod()`**（`src/mem/ruby/system/RubyPort.cc:174-183` 和 `:185-207`，两处几乎一样） | **硬编码"1 个 ruby 周期之后"**，ruby 时钟 = 板钟 3GHz，1 周期 = 333 tick——**这正是脚本注释里"iobus forward_latency = 1 board cycle = 333 ticks"说的那条边**，只是延迟值不是来自 IOXBar 的 `Cycles` 参数，而是这两处硬编码的 `clockPeriod()` | **卡住 Q 的就是这条边**：Q=300 < 333，卡在边缘内侧，Q 抬过 333 这两处的 `schedule()` 就会撞上 `assert(when >= getCurTick())` |

**为什么之前以为是 IOXBar 的 `forward_latency`/`response_latency` 参数**：这两个
Cycles 参数（默认 2/1/2 周期）确实存在、也确实控制 IOXBar 内部转发排队的时序，
但它们只影响 `pkt->headerDelay`/`payloadDelay` 的标注，**从不出现在任何跨域
`schedule()` 调用里**——`NoncoherentXBar::recvTimingReq/recvTimingResp`
（`src/mem/noncoherent_xbar.cc:101-177/179-`）只是直接同步调用下一跳端口，IOXBar
自己不跨 eventq 调度任何东西。真正跨域调度、真正撞上 8.1 断言的，是上面这两处
`RubyPort.cc` 里的 `curTick() + clockPeriod()`。所以**改 IOXBar 的参数不解决
问题**——这条路线在最初的设计构想里是错的，读代码之后已经排除。

### 17.3 为什么这是"第三个同类 bug"，不是新问题

这两处代码在写的时候显然假设"单 EventQueue、`curTick()` 全局一致"，"1 个周期
之后"在那个世界里永远安全。这和 S-003 §8.1（`sim_quantum` 方向搞反）、S-006
§11.5（`activateContext` 的 snap 没锚在 barrier 网格上）是**同一类根因**——
为单线程世界写的、局部算出来的 `when`，不知道自己可能被跨域调用、也不知道
两个域之间存在有界但非零的漂移。11.5 已经证明了修法：把 `when` 换成**锚定
barrier 网格**的公式，而不是依赖某个具体延迟数值 >= Q。S-009 要做的就是把
这个**已经验证过的修法**，原样搬到 `RubyPort.cc` 这两处。

## 18. 一个独立的、目前完全没排查过的问题：这条边有没有做跨域线程安全

`RubyPort::PioRequestPort::recvTimingResp` 之所以能被调用，前提是先有一次
`PioResponsePort::recvTimingReq`（`RubyPort.cc:209-227`）——它在 iobus 那一侧
被同步调用（如果响应来自跨域的 iobus/设备，调用发生在**域 0 的线程**上，直接
操作 `owner`（Sequencer，域 i+1）的状态），然后 `owner.request_ports[i]->
sendTimingReq(pkt)` 继续同步往下传。**整条 PIO 请求路径（域 i+1 的 Sequencer
发起 → 直接同步调用到域 0 的 IOXBar → 再同步调用到域 0 的南桥/设备）都是裸
虚函数调用，全程没有一处 `Consumer`/`MessageBuffer` 式的锁**——不像 Ruby 自己
的 int_link 消息（S-002/S-003 专门为跨域访问加固过 `Consumer::lock()`），
`RubyPort.cc`/`noncoherent_xbar.cc` 这条经典 Port 路径从没被这样审计过。

`grep` 确认 `RubyPort.cc` 全文件没有任何 `lock()`/`mutex` 调用。

**这不是 Q 大小的问题，是任意 Q（包括现在的 300）下都可能存在的问题**：
一个域的线程直接读写另一个域 SimObject 的内部状态（IOXBar 的
`reqLayers`/`respLayers`/`routeTo`，或者 Sequencer 自己的
`request_ports`/`m_outstanding_count` 之类的簿记状态），没有锁保护。S-008
的定窗测试碰巧没有暴露它（可能是这个窗口里 PIO 流量本身不够密、真正并发触达的
概率低），不代表它不存在——这和 S-003 §8.4-8.6"崩溃是竞态、不是每次都触发"
的教训完全一致。

**建议**：S-009 实现阶段，在设计 17 节的 snap 修法的**同时**，需要专门审计这条
路径上被跨域触达的每一处共享状态，参照 S-002/S-003 的方法论（先读代码定位所有
读写点，判断是否需要 `Consumer` 式的锁，还是可以论证"实际不可能并发"）。这是
**和抬高 Q 正交但同等重要**的一块工作，如实记录在这里，不因为 17 节的 snap 修法
能让断言不炸就误以为路径本身安全了。

## 19. 设计：把 11.5 的 grid-anchored snap 搬到 `RubyPort.cc` 这两处

### 19.1 具体改动

```cpp
// RubyPort.cc, 两处等价替换（PioRequestPort::recvTimingResp /
// MemRequestPort::recvTimingResp），复用 S-006 §11.5 已经引入的
// simQuantumStart/simQuantum 全局锚点（sim/eventq.{hh,cc}）：
Tick when = curTick() + owner.m_ruby_system->clockPeriod();
if (inParallelMode) {
    when = std::max(when,
        simQuantumStart +
        divCeil(curTick() + 1 - simQuantumStart, simQuantum) * simQuantum);
}
owner.pioResponsePort.schedTimingResp(pkt, when);   // 或 port->schedTimingResp(pkt, when)
```

**为什么加 `std::max` 而不是像 11.5 那样无条件替换**：11.5 那个点
（`activateContext`）天然就是"跨域才会走到"的路径，无条件 snap 没有额外代价。
这两处 `RubyPort.cc` 的调用**在串行模式（`inParallelMode==false`）下也会执行**
（PIO 响应处理不区分串行/并行），如果无条件套用 barrier-网格公式，会把串行模式
下的 PIO 响应延迟也悄悄改掉——这会破坏 S-002 到 S-008 一直保持的"串行基线
`simTicks` 逐字节不变"这条底线。`std::max` + `inParallelMode` 判断保证串行模式
**完全不变**（继续用原来的 `curTick()+clockPeriod()`），只有并行模式下、且原始
`when` 不够安全时才会被抬高到网格边界——**并行模式下也不是无条件抬高**：如果
`clockPeriod() >= 剩余到下一个网格边界的距离`，`std::max` 会保留原始值，只在
真正需要时才多花时间等到网格边界，把不必要的额外延迟降到最小（这点比 11.5 的
无条件版本更保守，值得在实现时一并考虑要不要把这个 `std::max` 优化回填到
`timing.cc` 的原实现里，或者认为 11.5 那个点足够稀疏、不值得优化——留给实现
阶段判断，不在这份设计里预先决定）。

### 19.2 为什么不是"抬高 IOXBar 参数"或"在 EventQueue::schedule 里做通用 snap"

- **抬高 IOXBar 参数**：17.2 已经排除——IOXBar 的 `Cycles` 参数根本不出现在
  跨域 `schedule()` 调用里，改了也不解决断言问题，纯属精力浪费。
- **在 `EventQueue::schedule()` 的跨域分支里做通用 snap**（曾经考虑过的备选
  方案）：架构上更"一次性解决所有未来同类 bug"，但代价是**任何**跨域
  `schedule()` 调用都会被无条件抬到网格边界，包括 Ruby int_link 那些**已经
  用真实延迟值（ll=20）保证安全、不需要也不应该被再延迟**的消息——会在
  已经验证过时序中性的路径上引入新的、无由头的松弛，且没有办法用
  `std::max` 精确到"只在真正需要时才生效"（`EventQueue::schedule()` 这一层
  拿不到"这次调用原本该有多少物理延迟"这个信息，没法判断 `when` 是"本来就
  该是网格边界"还是"发起方本来就精确算出了一个安全值，只是不巧比网格边界
  早"）。19.1 的按点修复虽然要多找几个点，但每个点都能拿到足够的上下文做
  `std::max` 式的最小干预，不会牵连已经验证正确的路径。**结论：按点修，不
  做全局通用 snap**——除非 20 节的实验证明还有第三、第四个同类点，多到
  按点修不经济，才重新考虑通用方案。

### 19.3 对既有 timing-neutral 不变式的影响

19.1 的 `std::max` 设计保证：
1. 串行模式：完全不变（`inParallelMode==false` 分支永远不触发 snap）。
2. 并行模式、Q 仍然 `<= clockPeriod()`（即今天的 Q=300 场景）：`std::max`
   总是保留原始值（因为原始值已经安全），**行为不变**，S-008 已经验证过的
   "并行 stats.txt 与串行逐字节相同"这条结论不受影响。
3. 并行模式、Q 被抬过 `clockPeriod()`（S-009 的目标场景）：PIO 响应会被
   snap 到网格边界，比"真实"的 1-周期延迟晚最多接近一个 Q——**这是一次新的、
   之前从未测过的真实时序松弛，不再是"机制替换、结果不变"，而是"真的会让
   PIO 响应变慢"**。S-008 的缺口算术投影（0.33x → ~0.92x）是假设 simInsts
   不变算出来的；这个假设在跨过这条边之后不再自动成立，必须重新测，不能
   直接当结论用。

## 20. 实验协议（供实现阶段参考，本次不执行）

1. 先做 18 节的线程安全审计（读代码 + 有必要的话在 debug 构建里加断言/
   TSan 跑一遍 S-008 的定窗场景，确认这条路径在当前 Q=300 下没有静默竞态）。
2. 实现 19.1 的两处 snap。用 S-003 §8.7 验证计划的方法论：先跑几次确认
   `assert(when >= getCurTick())` 不再在 Q > 333 时触发。
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

- **18 节的线程安全问题是本设计最大的不确定性**：如果审计发现真实竞态，
  S-009 的范围会从"改两处 snap"扩大到"给经典 Port 路径也做一遍 S-002/S-003
  那种加锁工作"，工作量和风险都会显著上升，需要重新过一遍范围和排期。
- 19.1 的 `std::max` 判断依赖 `inParallelMode`/`simQuantumStart`/
  `simQuantum` 三个全局量在 `RubyPort.cc` 里是否已经可见（`eventq.hh` 是
  否已经被这个翻译单元包含）——实现时需要确认，不确认可能要多加一个头文件。
- 是否还有第三个同类"硬编码 curTick()+常量"的跨域调度点，17.2 的表格是
  基于目前读过的代码（`RubyPort.cc`、`noncoherent_xbar.cc`、
  `cpu/simple/timing.cc`）整理的，不是穷举式 grep 全代码库的结果——实现前
  应该对 `curTick() *(\+|-)` 附近跟着 `schedule\(` 的模式做一次全仓库
  grep，确认没有漏掉的第四个点。

## 检查点

这份文档到此为止。**下一步（18 节审计 + 19 节实现）按项目工作方式需要单独
请示**——审计可能发现范围比预期大，实现阶段大概率需要现场调试（S-003/S-006
的历史经验：新拓扑/新参数几乎每次都撞上没预见到的墙），不属于"照既定设计
执行"，请求用户确认是否现在开始，还是先做 18 节审计、审计结果出来后再决定
19-20 节要不要做、怎么做。
