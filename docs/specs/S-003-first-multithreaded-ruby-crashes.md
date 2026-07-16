# S-003: gem5 首次多线程 Ruby 实测——两次真实崩溃与修复

> **状态**：已修复并验证（提交 `2755972435`/`f960ae745c`/`968d48d377`/
> `9af48716b3`）。7 次独立复现运行干净通过 2e9-tick 窗口；kick 路径（8.8）
> 构造上正确，但 [S-004](./S-004-first-speedup-measurement-and-fixes.md) 的
> 实测确认在当前拓扑/负载下未被实际触发。对应原单体设计文档第 8 节，内容
> 原样保留。

---

## 8. gem5 史上第一次多线程 Ruby 实测（方案二）：两次真实崩溃

按 S-001 §6.3"方案二"（复用 `Crossbar.py` 不改代码，只给共享 xbar
router 单独分配一个 `eventq_index`）在
`configs/deprecated/example/se.py` 里加了一段实验性代码（新增
`--parallel-l2-eventq`/`--sim-quantum` 两个选项，gated，默认关闭，
不影响任何现有行为——关闭状态下跑 A 节基线命令，`simTicks` 仍然是
`31555788000`，和 S-002 §7.2的验证结果一致）。域划分用的是 S-001 §6.3的方案：
`l1_cntrl0..3` 各一个域（0..3）、`l2_cntrl0`+`dir_cntrl0`+
`system.mem_ctrls` 共享一个域（4）、`network.ext_links` 覆盖不到的
router（也就是共享 xbar）单独给一个域（5）。分配方式对每个域的
controller 用 `descendants()` 显式设置每个后代对象的 `eventq_index`
（不依赖 `Parent` proxy 沿 SimObject 树自动传播——参考
`configs/example/arm/fs_bigLITTLE.py` 里 KVM 多 `eventq_index` 场景
同样是对 `cpu.descendants()` 显式赋值，不依赖隐式传播，这次跟随
同一个已验证过的模式）。

打开 `--parallel-l2-eventq` 实际跑起来，**在真正跑通之前连续遇到了
两次崩溃**，都如实记录在这里（不是"順利跑通"）：

### 8.1 崩溃一：`sim_quantum` 设置方向想反了

第一次尝试用 `--sim-quantum=10us`（模仿 `fs_bigLITTLE.py` 里 KVM
异构核场景的默认值 1ms 量级），几乎立刻在 `tick 2000` 就触发
`src/sim/eventq.hh:759` 的 `assert(when >= getCurTick())`——某个域
把跨域消息往另一个域调度时，目标域的本地 `curTick_` 已经跑到了
比这条消息的到达时间还晚的地方。

**根因，回头看其实 S-001 §2.5已经写明白了，只是没有对上号**：gem5 的
`GlobalSyncEvent`/`sim_quantum` 屏障机制只保证"任意两个域之间的时钟
漂移不超过一个 quantum"，不保证"漂移为零"——两次全局屏障之间，各个
域按各自的 wall-clock 速度独立跑，跑得快的域可能几乎跑满整个
quantum 窗口后才等到跑得慢的域追上来。要让跨域调度的 `when`
（通常是"本地当前 tick + link_latency"这个量级）不落在目标域已经
跑过去的时间点之前，**唯一的保证方式是 `sim_quantum <= 最小跨域
消息延迟（这里是 Throttle 的 `link_latency`，默认 1 个 ruby 时钟
周期 = 2GHz 下 500 ticks）`**——这正是 S-001 §2.5"quantum 取得比共享
cache 命中延迟略小"这条经验规则的数学来源，只是当时读的时候没有
反应过来这是个方向性约束（quantum 要比延迟小，不是比延迟大），
第一次选 10us（1e7 ticks）完全选反了量级。改成 `--sim-quantum=0.1ns`
（100 ticks，比 500-tick 的 link_latency 小）后这个特定崩溃消失，
仿真真正往前推进到了 `tick 107508500`。

### 8.2 崩溃二：`Consumer::processCurrentEvent()` 的一个未被注意到的
隐藏假设，在跨域场景下不成立

修完 8.1 后新崩溃：`src/mem/ruby/common/Consumer.cc:89`
`assert(em->clockEdge() == *curr)` 失败。查了代码，这是一个**真实的、
之前没写代码时就该想到但没想到的数据竞争/时序假设错误**，不是环境
问题：

- `Consumer::processCurrentEvent()`（`Consumer.cc:86-98`）开头直接
  `m_wakeup_ticks.begin()`/`m_wakeup_ticks.erase(curr)`，**完全没有
  加锁**——只在中间调用 `wakeup()` 本体那一段才 `lock()`/`unlock()`。
- 而 `MessageBuffer::enqueue()`（S-002 §7.1新增的逻辑）里
  `m_consumer->scheduleEventAbsolute(arrival_time)`（往
  `m_wakeup_ticks` 里插入新的到达时间）**是在 `consumer_lock` 的
  保护范围内**调用的（`MessageBuffer.cc:239-241,320`，跨域时才真正
  加锁）。
- 问题：`processCurrentEvent()` 读/删 `m_wakeup_ticks` 这一步*没有*
  拿同一把锁。当它在本地域线程上因为一个更早调度好的 wakeup 事件被
  触发而执行到这里时，另一个域的线程完全可能*在同一时刻*正通过
  `enqueue()`（持锁）往这同一个 `m_wakeup_ticks` 里插入一个新的、
  时间上更早的到达时间——因为跨域消息的时序本来就是放宽过的
  （S-001 §2.5），"稍后插入的条目时间上反而更早"这件事在这个设计里是
  被允许发生的常规情况，不是异常。`processCurrentEvent()` 里
  `assert(em->clockEdge() == *curr)` 这行代码隐含的假设是"当前要
  处理的这个 wakeup，一定对应 `m_wakeup_ticks` 里最早的那个 tick，
  而且没有别的线程会在我处理的同时改这个 set"——这个假设在单
  `EventQueue`（没有其它线程能碰这个 Consumer）场景下自动成立，
  但在真正的跨域场景下不成立，而且完全没有代码保护它。

**这不只是"加个锁"就能修的表面问题**：即使给
`processCurrentEvent()` 开头的读/删也套上 `lock()`/`unlock()`，
逻辑上还是可能出现"本地线程正准备处理 tick=107508500 的 wakeup，
外部线程恰好在这一刻插入了一个 tick=107508000（更早）的到达时间"——
加锁只能防止数据结构本身被同时读写破坏（内存安全），不能防止
"更早的 tick 在逻辑上已经来不及排进当前正在处理的这一次 wakeup"
这个时序倒挂本身。真正的修复大概率需要 S-001 §2.5已经点出但我们还没
实现的机制：**跨域调度的到达时间要被动地对齐（不是自然计算出来的
`current_time + delta`，而是主动向上取整）到"下一个 quantum 边界"**
——如果所有跨域消息的目标 tick 都精确落在 quantum 边界上，而
`GlobalSyncEvent` 的屏障机制保证边界时刻两侧的域都已经真正同步
（`handleAsyncInsertions()` 就是在越过屏障那一刻做的，
`global_event.cc:154`），"插入一个比当前已处理进度更早的 tick"这个
情况就不会再发生——但这个"跨域到达时间取整到 quantum 边界"的逻辑
目前完全没有写，只存在于论文描述和 S-001 §2.5的转述里。

### 8.3 当前状态和下一步

- 崩溃一已解决（配置层面：`sim_quantum` 要设得比 `link_latency`
  小，不是大）。
- 崩溃二**未解决**，且不是配置问题，是 S-002 §7.1的 per-consumer 锁
  实现里一个真实的遗漏（`processCurrentEvent()` 对 `m_wakeup_ticks`
  的访问没上锁，且即使上锁也不足以解决时序倒挂）。这是 gem5 第一次
  真正跑多线程 Ruby 触发出来的、设计阶段没有预见到的具体问题，
  如实记录，不打算掩盖或简化描述。
- 下一步可选方向（都还没做，留到下次会话决定）：(a) 实现"跨域到达
  时间对齐到 quantum 边界"这个 8.2 节分析指向的根本修复；(b) 或者
  先给 `processCurrentEvent()` 也加锁，把断言从"必须是最早"放宽成
  "重新取当前最早的 tick 再处理，不假设是最初触发这次 wakeup 时的
  那个 tick"，作为更小范围的修补，但需要重新论证这样改是否还能保证
  `wakeup()` 语义正确（尤其是 SLICC 生成代码里对"这次 wakeup 对应
  哪个 tick"的隐含假设有没有依赖）。
- 实验用的 se.py 改动（`--parallel-l2-eventq`/`--sim-quantum`）保留
  在树上，默认关闭、不影响现有行为，方便下次会话直接复现这两次崩溃
  而不用重新写配置代码。

### 8.4 后续会话：锁修好了内存安全，但暴露出一个和最初理论不同的
新崩溃形态

按 8.3 节方向 (b) 先落地了两处改动：

1. `Consumer::processCurrentEvent()` 现在把 `lock()`/`unlock()` 扩大
   到整个方法体（`m_wakeup_ticks.begin()`/`erase()`、`wakeup()`、
   `scheduleNextWakeup()` 全部在锁内），不再只包住 `wakeup()` 本体。
2. `Consumer::scheduleEventAbsolute()` 加了 8.3 节方向 (a) 的"跨域到
   达时间对齐到 quantum 边界"：`inParallelMode && curEventQueue() !=
   em->eventQueue()` 时，把已经按 `em->clockPeriod()` 取整过的到达
   时间再按 `simQuantum` 向上取整一次。

**实测结果，两部分都如实记录**：

- 关掉 `--parallel-l2-eventq` 跑 A 节基线命令，`simTicks` 仍是
  `31555788000`，和 7.2/8 节此前的验证结果一致——两处改动都没有影响
  单线程行为。
- 打开 `--parallel-l2-eventq --sim-quantum=0.1ns` 重跑复现命令，**内存
  安全问题（改动 1 前的 std::set 红黑树段错误）确认修复**——之前会在
  `Consumer::scheduleEventAbsolute` 内部因为 rb-tree 结构被并发破坏而
  直接 segfault（`std::_Rb_tree_decrement` 崩溃，`exit 139`），现在同
  样的复现命令稳定地退化成一个干净、确定性的 `assert(em->clockEdge()
  == *curr)` 失败（`Consumer.cc:112`，`exit 134`，不再是段错误）——
  从"未定义行为的内存破坏"变成"可复现、可调试的断言"，是真实的
  改进，但断言本身还在。
- 改动 2（quantum 边界对齐）在这个具体测试配置下**实测是个空操作**：
  `em->clockPeriod()` 在 2GHz ruby 时钟下是 500 ticks，`sim_quantum
  =0.1ns` 是 100 ticks，500 恰好是 100 的整数倍——所以任何已经按
  `clockPeriod` 取整过的到达时间*天然*就已经落在 quantum 边界上，
  `divCeil(when, simQuantum) * simQuantum` 这一步不会改变任何值。这
  条改动本身没有错，但没有在当前测试参数下真正被验证过是否解决了
  它想解决的问题——这是一个记录下来但当时没意识到的参数耦合。

**加了临时诊断（已移除，结论保留在这里）后发现的新数据点**：在
`processCurrentEvent()` 里、`lock()` 之后、`assert` 之前打印
`em->clockEdge()`、`*curr`、`m_wakeup_ticks.size()`、
`m_wakeup_event.scheduled()`，重跑一次复现命令，唯一一次断言失败时
打印的是：

```
clockEdge=108267500 earliest=108268000 set_size=1 ev_sched=0
```

也就是说：断言失败那一刻，`m_wakeup_ticks` 里**只有一个元素**，而且
这个元素比触发这次 wakeup 的 `clockEdge` **还要晚整整一个 ruby 时钟
周期（500 ticks）**——不是 8.2 节原来设想的"更早的 tick 后来居上"，
而是"这次 wakeup 本该对应的那个 tick（108267500）已经从集合里彻底
消失了，只剩下下一个周期的 108268000"。同一次诊断构建里还在
`scheduleNextWakeup()` 的跨线程 `em->reschedule()` 分支加了计数打印
（"当已经调度的 wakeup 需要被跨线程改到更早的时间"这个 8.3 节曾经
怀疑过的场景）——**这个分支在整次复现运行里触发次数是 0**，说明
"跨线程调用 `em->reschedule()`（`eventq.hh:819` 要求只能本线程调用）"
这个此前怀疑的机制，至少在这次复现里不是真正原因，可以排除（不是
"没有发生这类崩溃"，而是"这条代码路径压根没被走到"）。

**结论**：8.2 节的原始理论（更早的跨域 tick 在处理中途插入）描述的是
一种可能的时序倒挂，但这次实测抓到的具体失败实例不是那种形状——是
"应该存在的那个 tick 不见了"，更像是同一个 `m_wakeup_ticks`/
`m_wakeup_event` 在某处被重复消费或者被另一次（可能是同线程可重入
锁允许的嵌套）调用提前清空。`Consumer::lock()` 是刻意设计成同线程
可重入的（S-001 §6.2的死锁规避机制要求），这意味着 `wakeup()` 内部理论
上可以递归地再次触碰同一个 `m_wakeup_ticks`（比如通过某个会调用回
`scheduleEvent`/`scheduleEventAbsolute` 的路径）——这条"可重入锁本身
成为 bug 来源"的可能性，8.2 节完全没有分析过，是目前最值得优先排查
的新方向，但还没有做（需要更细粒度的、按具体 tick 过滤而不是全量
打印的诊断，因为崩溃发生的具体 tick 在不同次运行里不是固定的
107508500/106529500/108267500，本身就是竞态，无法用固定 tick 窗口
过滤日志）。

**当前状态**：改动 1（锁扩大到整个 `processCurrentEvent()`）和改动 2
（quantum 边界对齐，虽然在当前测试参数下是空操作但逻辑本身无害且
以后调整 `sim_quantum`/`clockPeriod` 比例时可能开始起作用）都保留在
树上，两者都不改变任何现有行为（基线 `simTicks` 验证过没变）。真正
的崩溃二仍未解决，下一步大概率要从"可重入锁 + 嵌套 wakeup()"这个新
方向继续挖，而不是继续在"跨域时序"这个原假设上打转。诊断代码（
`std::cerr` 打印）已经移除，不留在树上。

### 8.5 真正的根因找到了：不是可重入锁，是"同域快速路径"绕过锁
之后和跨域线程之间没有 happens-before 关系

按 8.4 节末尾指向的方向继续挖，加了一版更细的临时诊断（同样是
lock-free 环形缓冲区，单个 `std::atomic<unsigned>::fetch_add` 记录
序号，不加任何 mutex，目的是尽量不干扰要观察的这个竞态本身；只在
`processCurrentEvent()` 的断言真要失败那一刻才把缓冲区里属于*这个*
`Consumer` 实例的记录按序号打印出来，从而绕开"崩溃发生的具体 tick
每次都不一样、没法用固定 tick 过滤日志"这个 8.4 节末尾提到的障碍）。
重跑复现命令，在真正断言失败的那一次抓到了完整的因果链（`A`
`B` 代表两个不同的 OS 线程，`A` 是发消息的跨域线程，`B` 是这个
`Consumer` 归属的域自己的线程；已裁剪到关键片段，完整 82 行原始
日志不放进文档）：

```
536940 B  I  tick=106379500          depth=0   // B 给自己的 consumer 发消息，
                                                // 同域快速路径：完全没加锁
536941 B  S  tick=106379500          depth=0   // 同一条路径上把 m_wakeup_event
                                                // 调度到 106379500（仍然没加锁）
536959 A  l  (加锁成功)              depth=1   // A（跨域）正确地加了锁
536960 A  X  tick=106380000          depth=1   // A 插入 106380000（跨域、有锁，正确）
536961 A  S  tick=106379500          depth=1   // A 的 scheduleNextWakeup 读到
                                                // m_wakeup_event.scheduled()==false
                                                // ——这是过期值，B 两步之前刚设成 true
                                                // ——于是 A 对同一个 Event 对象又调用了
                                                // 一次 em->schedule()
536962 B  l  (加锁成功)              depth=1
536963 B  B  tick=106379500                    // B 的 wakeup 正常触发，处理
536964 B  E  tick=106379500                    // 106379500 并 erase 掉
536967 B  N  tick=106380000 aux=106379500      // scheduleNextWakeup 发现
                                                // m_wakeup_event 仍然"已调度"，
                                                // when 还是过期的 106379500
                                                // （A 那次污染性调用的残留状态）
   ...   （quantum 边界的 async 队列合并发生在这里，日志里没有单独记录）
536973 B  l  (加锁成功)              depth=1
536974 B  B  tick=106379500                    // m_wakeup_event 对同一个已经
                                                // 处理过的 tick 又触发了一次
                                                // （被重复插入：一次是 B 自己
                                                // 直接 insert()，一次是 A 的
                                                // asyncInsert()）
536975 B  !  clockEdge=106379500 vs earliest=106380000   // 断言失败
```

**整个日志里没有出现任何一条 `depth>=2` 的记录**（`grep -c
"depth=[2-9]"` 结果是 0）——8.4 节的"可重入锁本身是 bug 来源"这个
假设，在这次抓到的具体失败实例里被数据**证伪**：从头到尾没有发生
过一次真正的同线程嵌套加锁，谈不上"嵌套导致集合被提前清空"。

**真正的根因，是 `MessageBuffer::enqueue()` 的"同域快速路径"这个
优化本身不成立**（该优化的原始注释见 `MessageBuffer.cc:230-237`，
`enqueue()` 函数体本身，S-002 §7.1引入）：这段注释论证"如果调用线程已经
在服务 `m_consumer` 自己的 `EventQueue`，就不会有*其它*线程同时碰
`m_consumer`，所以锁在这种情况下必然是冗余的，可以跳过"——这个论证
本身只考虑了"会不会有*另一个同域*线程同时来碰"（不会，因为一个
`EventQueue` 一辈子只由一个线程服务），却没有考虑"会不会有一个
*跨域*线程同时来碰"——而这正是这个拓扑里几乎总会发生的情况（同一个
`Consumer`——比如 L2/目录控制器——既会收到同域消息（走快速路径、不
加锁），也会收到跨域消息（走锁定路径、正确加锁））。锁只在"两边都去
获取同一把锁"的前提下才提供 happens-before 顺序保证；**当本域线程
根本不获取锁、直接读写 `m_wakeup_event`/`m_wakeup_ticks` 时，跨域
线程即使自己正确地拿着锁，也没有任何机制保证它能看到本域线程刚刚
（无锁）写入的最新状态**——这就是一次教科书式的数据竞争：跨域线程
读到 `m_wakeup_event.scheduled()` 的过期值（`false`），进而对同一个
`Event` 对象重复调用 `em->schedule()`；由于跨域线程走的是
`inParallelMode` 下的 `asyncInsert()` 分支（`eventq.hh:772-773`），
这个已经存在于主队列里的 `Event`（本域线程直接 `insert()` 进去的）
又被塞进了同一个 `EventQueue` 的 `async_queue`，等下一次
`handleAsyncInsertions()`（quantum 边界触发）把 `async_queue` 合并
回主队列时，同一个 `Event` 对象被第二次插入——这正好解释了两种
观察到的症状：改动 1 之前是内存破坏（段错误，`std::set` 的红黑树被
并发破坏），改动 1 之后是这个 `Event` 对象本身的调度状态被破坏、对
一个已经处理并从 `m_wakeup_ticks` 里删除的 tick 重复触发一次
`processCurrentEvent()`。

**这意味着 S-002 §7.1引入、S-002 §7.3为了避免约 15% 单线程开销而保留的"同域
快速路径"优化是不安全的，需要修**。候选方向：

(a) **直接去掉快速路径**，`enqueue()` 无论同域跨域一律加锁——最简单
    、明确正确，但会让 S-002 §7.3测过的约 15% 开销失去唯一的缓解手段，
    需要重新测量真实代价（当时测的是"锁的存在"本身的开销，不是"是否
    跳过锁"这个优化的收益幅度，两者不是一回事，需要单独测）。
(b) **把"是否需要加锁"从按消息判断改成按 `Consumer` 判断**：一个
    `Consumer` 只要它挂在拓扑里的任何一条入边跨过了域边界（换句话说
    ，只要它可能收到*任何*跨域消息），就必须对它的*所有*入队——包括
    同域的——都走锁定路径；只有真正"只收同域消息、永远不会被跨域线程
    碰到"的 `Consumer`（比如纯粹寄生在发送方 `EventQueue` 上、输出
    端才跨域的 `Throttle`，见 S-001 §6.2）才能真正安全地跳过锁。这个属性
    在配置阶段（拓扑/`eventq_index` 分配完成后）就能静态算出来，可以
    做成 `Consumer` 上的一个一次性计算的布尔标志，而不是 S-002 §7.1现在
    这种"看这一条消息的发送方和接收方是不是同一个 `EventQueue`"的
    逐消息判断。比 (a) 复杂，但保留了大部分场景下的优化收益。

两个方向都还没有实现，需要先决定选哪个方向再动手（(b) 需要先搞清楚
这次测试拓扑里到底有多少 `Consumer` 满足"真正不会被跨域碰到"这个
条件，如果绝大多数控制器（L1/L2/目录）都会被跨域消息碰到，那 (b)
相对 (a) 的收益可能很有限，直接选 (a) 更划算——这个判断本身也需要
先在这次测试拓扑上数一遍，不能拍脑袋）。诊断代码（环形缓冲区）已经
移除，不留在树上；上面的日志片段是从一次实际运行里摘录、手工裁剪的
证据。

### 8.6 选了方向 (a) 之后：崩溃仍然存在，而且是一个更深层的、
`Consumer::lock()` 本身治不了的问题

选了 8.5 节的方向 (a)：`MessageBuffer::enqueue()` 去掉同域快速路径，
无论同域跨域一律 `Consumer::lock()`。改完重新编译，A 节基线命令
`simTicks` 依然是 `31555788000`（没有影响现有行为）。重新跑
`--parallel-l2-eventq --sim-quantum=0.1ns` 复现命令——**还是崩在同一个
`assert(em->clockEdge() == *curr)`**，只是撑得比改动前更久（改动前
最早在 tick ~1.06 亿量级崩，改动后有一次撑到了 tick 163405000）。
这说明方向 (a) 确实修掉了一部分真实的竞态（不是没用），但没有修完。

用和 8.5 节同款的 lock-free 环形缓冲区诊断（这次额外加了两样东西：
①在 `scheduleNextWakeup()` 里无条件记录一条 `C`（check）事件，把
`m_wakeup_event.scheduled()` 的原始读数直接打出来，不再从"走了哪个
分支"里反推；②在 `em->schedule()` 调用**之后**立刻补一条 `s`（同一个
临界区内的复查），确认那次调用是否真的把 `Scheduled` 位置上了；
③给 `Consumer::unlock()` 也加了一条 `u` 记录，这样能看清楚每次加锁/
解锁的精确边界）抓到了下面这段完整的因果链（`D` 是跨域发送方线程，
`B` 是这个 `Consumer` 归属域自己的线程；序号是全局单调的，可以当成
真实的时间顺序）：

```
1649410 D  l                                     // D 第一次加锁（新申请，非重入）
1649411 D  X  tick=168452000                     // D 跨域插入 168452000
1649412 D  C  tick=168452000            sched=0  // 检查：未调度
1649413 D  S  tick=168452000            sched=0  // 调用 em->schedule()
1649414 D  s  tick=168452000 aux=168452000 sched=1  // 调用后立刻复查：确认已经是"已调度"
1649415 D  u                                     // D 解锁，第一次 enqueue() 结束
1649423 D  l                                     // D 第二次加锁（新申请，同一个线程 D）
1649424 D  X  tick=168452500                     // D 跨域插入另一条消息 168452500
1649425 D  C  tick=168452000            sched=0  // 检查：又读到"未调度"！！
1649426 D  S  tick=168452000            sched=0  // 于是又调用了一次 em->schedule()
1649427 D  s  tick=168452000 aux=168452000 sched=1
1649428 D  u
1649430 B  l
1649431 B  B  tick=168452000                     // B 第一次正常触发、处理 168452000
1649432 B  E  tick=168452000
1649439 B  C  tick=168452500 aux=168452000 sched=1  // scheduleNextWakeup：发现还"已调度"
1649440 B  N  tick=168452500 aux=168452000 sched=1  // （这是 D 第二次 schedule() 的残留）
1649441 B  u
1649442 B  l
1649443 B  B  tick=168452000                     // m_wakeup_event 对同一个 tick 又触发一次！
1649444 B  !  clockEdge=168452000 vs earliest=168452500   // 断言失败
```

关键点：`1649414` 和 `1649425` 之间，**从头到尾没有任何其它线程碰过
这个 consumer**（过滤后的日志里两条记录直接相邻）——`D` 自己在
`1649414` 用一次立即复查确认了 `m_wakeup_event.scheduled()==true`，
紧接着（同一个线程，程序顺序上必然在后面执行）在 `1649425` 却读到
`false`。对同一个线程而言，这不可能是"读到过期缓存值"（单线程程序
顺序内自己写的东西自己必然能看见，这是 C++ 内存模型的基本保证），
所以这次是**这个标志位真的、确实地被清除了**——而清除它的，只能是
`src/sim/eventq.cc:229` 的 `event->flags.clear(Event::Scheduled)`，
这行代码在 `EventQueue::serviceOne()` 里，**发生在真正调用
`event->process()`（也就是我们的 `processCurrentEvent()` 回调，从而
才会走到 `Consumer::lock()`）之前**。也就是说：

**`B` 的 `serviceOne()` 已经把 `m_wakeup_event` 从队列里取出、把
`Scheduled` 位清掉了，但 `B` 还没来得及真正调用
`processCurrentEvent()` 去拿 `Consumer::lock()`（可能正好被 `D` 当时
还没释放的锁挡住，也可能只是两次操作之间线程调度的间隙）——这个
"标志已清、但对应的 `m_wakeup_ticks`/锁保护的状态还没来得及更新"
的窗口，`Consumer::lock()` 完全保护不到，因为这次清除标志位的代码
根本不属于 `Consumer` 自己，是 gem5 通用事件队列机制
（`EventQueue::serviceOne()`）里的一行、在拿到我们的锁*之前*就执行
了。** `D` 在这个窗口里拿着（对它自己而言完全合法持有的）
`Consumer::lock()`，看到的 `m_wakeup_event.scheduled()==false` 是
*真实、当下正确*的读数，不是竞态造成的脏读——但这个"真实"本身具有
误导性：`B` 马上就要真正处理这次 wakeup 了，`D` 却因为看到"未调度"
而对同一个 Event 对象又调用了一次 `em->schedule()`，把它第二次塞进
`em` 的 `async_queue`（跨域调用走 `asyncInsert()`，`eventq.hh:772-773`
）。等 `handleAsyncInsertions()` 把 `async_queue` 里的两份都 drain
进主队列时，同一个 `Event` 对象被插入了两次，于是它对同一个（已经
被 `B` 正常处理过一次的）tick 又触发了一次——这正是
`1649442-1649444` 观察到的现象。

**结论：8.5 节的"去掉同域快速路径"（方向 a）确实修掉了一种真实的
竞态（不同域发送方和本域"自己发给自己"之间缺 happens-before），但
`Consumer::lock()` 这一层的设计本身有一个更根本的问题**——它试图用
`m_wakeup_event.scheduled()`（gem5 核心 `Event` 对象自己的标志位）
来判断"要不要发起新的调度"，但这个标志位的*清除*时机（`serviceOne()`
在真正调用回调之前）完全不受 `Consumer::lock()` 管辖，是 gem5 通用
事件机制自己的时序，不是 `Consumer`/`MessageBuffer` 这一层能通过
"更早、更全地加锁"来控制的——不管把 `Consumer::lock()` 的覆盖范围
扩到多大，只要判断依据仍然是 `m_wakeup_event.scheduled()`，就永远
可能在"标志已清、`m_wakeup_ticks` 还没被这次触发对应的
`processCurrentEvent()` 调用更新"这个窗口里被跨域线程看到一个
"诚实但过时"的读数。

**候选修复方向**（都还没实现，需要先讨论/决定）：不要用
`m_wakeup_event.scheduled()` 判断"是否需要发起新调度"，改成
`Consumer` 自己维护一个只在 `Consumer::lock()` 保护下读写的状态
（比如一个 `bool`，标记"当前是否已经有一次对 `em` 的调度请求在途"）
——`processCurrentEvent()` 拿到锁之后才把它清掉（而不是依赖
`serviceOne()` 在拿锁*之前*就清掉的 `Event::Scheduled`），这样这个
状态的每一次变化都严格发生在同一把锁的保护范围内，不再依赖 gem5
核心事件机制自己的、不受这把锁管辖的标志位时序。这比 8.5 节的修复
改动更深——要动 `Consumer` 的核心调度状态机，不只是
`MessageBuffer::enqueue()` 的加锁范围，值得先讨论清楚再动手，尤其是
要重新论证这样改会不会影响 `reschedule()`（"发现有更早的 tick，要把
已经排队的 wakeup 提前"）这条路径的正确性。诊断代码（环形缓冲区，
这次带 `s`/`u`/无条件 `C` 记录的第三版）已经移除，不留在树上；
`Consumer.cc` 当前树上状态与上一次提交（`cc36b250a2`）完全一致，
`MessageBuffer.cc` 的同域快速路径移除（8.5 节方向 a）还没有提交。

（补记：8.5 节方向 (a) 实际已经在 `d14e8c3f08` 提交，早于本节写作
时间——上一段末尾那句话是撰写时的笔误，当时忘了核对提交历史，这里
一并更正。）

### 8.7 修复设计：把"是否已有一次调度在途"的判断从 `Event::Scheduled`
搬进 `Consumer` 自己、由同一把锁保护的状态里

8.6 节的结论是：问题不在于锁覆盖的范围不够大，而在于判断依据本身
（`m_wakeup_event.scheduled()`）的清除时机不受 `Consumer::lock()`
管辖——`EventQueue::serviceOne()`（`src/sim/eventq.cc:229`）在真正
调用回调（从而进入 `processCurrentEvent()`、拿到锁）**之前**就清掉
了这个标志位。这一节把候选修复方向落成一个具体的设计，供实现前过一遍。

**新增状态**（`Consumer` 私有成员，替换掉对 `m_wakeup_event.
scheduled()`/`m_wakeup_event.when()` 的读取）：

```cpp
bool m_wakeup_scheduled = false;  // 是否已经有一次对 em 的调度请求在途
Tick m_wakeup_scheduled_when = 0; // 在途请求对应的 tick（仅当上面为 true 时有意义）
```

**不变式**：这两个字段只在持有 `m_wakeup_mutex`（即调用者已经进过
`Consumer::lock()`）的代码路径里被读或写——`scheduleNextWakeup()`
和 `processCurrentEvent()`，两者现在都只能在锁内被调用（前者已经是
这样，`scheduleEvent`/`scheduleEventAbsolute` 的调用方
`MessageBuffer::enqueue()` 早在 S-002 §7.1就把整个 enqueue 包在锁里；后者
本来就在 8.2 节的修复里锁住了）。因此这两个字段的每一次读写之间都有
锁提供的 happens-before 关系，不会重演 8.6 节那种"标志真实但过时"
的问题——因为这次清除它的代码本身就在锁的保护范围内，不像
`Event::Scheduled` 那样在 gem5 核心的 `serviceOne()` 里被锁管不到的
地方清除。

**`scheduleNextWakeup()` 改法**（伪代码，替换现有实现）：

```cpp
void
Consumer::scheduleNextWakeup()
{
    auto it = m_wakeup_ticks.lower_bound(em->clockEdge());
    if (it == m_wakeup_ticks.end())
        return;

    Tick when = *it;
    assert(when >= em->clockEdge());

    if (m_wakeup_scheduled) {
        if (when < m_wakeup_scheduled_when) {
            // 只有 em 的宿主线程能调用 reschedule()（eventq.hh:819 的
            // assert 要求如此）；跨域线程理论上也可能走到这个分支
            // （见下面"未解决的遗留问题"），但目前复现里没有观察到，
            // 保留 eventq.hh 自带的 assert 作为兜底——命中就是一次
            // 干净的、确定性的崩溃，而不是静默的错误行为。
            em->reschedule(m_wakeup_event, when, true);
            m_wakeup_scheduled_when = when;
        }
        return;  // 已经有一次调度在途，不需要（也不能）再发起一次
    }

    em->schedule(m_wakeup_event, when);
    m_wakeup_scheduled = true;
    m_wakeup_scheduled_when = when;
}
```

**`processCurrentEvent()` 改法**：在拿到锁之后、动 `m_wakeup_ticks`
之前，先把 `m_wakeup_scheduled` 清掉——这一步就是"消费掉这次在途
请求"的唯一时刻，而且严格发生在锁的保护范围内：

```cpp
void
Consumer::processCurrentEvent()
{
    lock();
    m_wakeup_scheduled = false;   // 这次在途请求已经真正触发，清掉
                                   // 标志；必须在锁内、在读 m_wakeup_ticks
                                   // 之前做，让并发的 scheduleNextWakeup()
                                   // 看到的状态始终和"是否真的还有一次
                                   // 调度在途"一致
    auto curr = m_wakeup_ticks.begin();
    assert(em->clockEdge() == *curr);
    m_wakeup_ticks.erase(curr);
    wakeup();
    scheduleNextWakeup();
    unlock();
}
```

**为什么这修得掉 8.6 节的具体崩溃**：8.6 节的因果链里，`D`
（跨域线程）在 `1649425` 读到 `m_wakeup_event.scheduled()==false`——
这是 `serviceOne()` 提前清除、但 `B` 还没跑到 `processCurrentEvent()`
去真正处理这次 wakeup 时的"诚实但过时"读数。换成 `m_wakeup_scheduled`
之后，这个字段在同样的时间窗口里仍然是 `true`（因为它只会在 `B`
真正拿到锁、进入 `processCurrentEvent()` 之后才被清掉，而不是被
`serviceOne()` 提前清掉）——所以 `D` 在 `1649425` 这一步会读到
`m_wakeup_scheduled==true`，直接走 `return` 分支，不会对同一个
`m_wakeup_event` 发起第二次 `em->schedule()`，也就不会把它重复插入
`async_queue`，8.6 节观察到的"同一个 tick 触发两次"就不会发生。

**为什么这不会重新引入 8.5 节修掉的问题**：8.5 节的根因是"同域快速
路径跳过锁，导致本域线程的无锁写入和跨域线程的加锁读取之间没有
happens-before 关系"。这个修复完全不涉及要不要加锁——它假设锁已经
覆盖了 `scheduleNextWakeup()`/`processCurrentEvent()` 的全部访问
（8.5 节的修复已经做到这一点），只是把"判断依据"从一个锁管不到的
外部标志位换成锁管得到的内部标志位。两个修复是正交的、都需要，
不是互相替代。

**已知未解决的遗留问题**（不在这次修复范围内，如实记录）：
`scheduleNextWakeup()` 里"发现更早的 tick，需要把已经在途的调度提前"
这个分支调用了 `em->reschedule()`，而 `reschedule()` 按
`eventq.hh:819` 的 assert 只能由 `em` 的宿主线程调用。如果这个分支
被一个跨域线程走到（比如两个不同的跨域发送方，各自把 tick snap 到
不同的 quantum 边界后，后到的那个 snap 结果反而更早），会直接触发
`eventq.hh` 自己的 assert——这本身是"崩得干净"而不是"静默出错"，
但说明这个分支目前只对"同域线程发现一个比跨域已排队的请求更早的
同域 tick"这一种情况是安全的，对"两个跨域线程互相抢跑"这种情况没有
真正设计过。8.4 节的诊断曾经测过这个分支在当前测试拓扑里触发次数是
零，所以现阶段不是这次崩溃的成因，但如果以后拓扑变化导致这个分支
真的被跨域线程走到，会是一个新的、需要单独设计的问题（大概率需要
让跨域线程也只能通过类似 `schedule()`/`asyncInsert()` 的路径提交
"请求提前"这件事，而不是直接调用 `reschedule()`）。

**验证计划**：实现后（1）用 A 节基线命令确认 `simTicks` 不变；
（2）重跑 `--parallel-l2-eventq --sim-quantum=0.1ns` 复现命令，确认
`assert(em->clockEdge() == *curr)` 不再触发；由于崩溃发生的具体 tick
在不同次运行里本身是竞态、不是确定的，跑一次通过不能说明问题已经
彻底修好，需要多跑几次（至少 5-10 次)看是否稳定不崩，而不是只看
一次干净退出就下结论。

### 8.8 补齐 8.7 留下的缺口："把在途 wakeup 提前"这条路径的
跨线程正确性设计（顺带发现同一分支里第二个潜伏缺陷）

8.7 节实现后明确留下了一个"如实记录、暂不修复"的缺口：
`scheduleNextWakeup()` 里"发现更早的 tick、需要把已在途的调度提前"
这个分支调用 `em->reschedule()`，而 `eventq.hh` 的
`assert(!inParallelMode || this == curEventQueue())` 要求它只能由
`em` 的宿主线程调用。本节把这个缺口的修复设计写清楚。设计过程中还
发现了**同一个分支里的第二个、更隐蔽的潜伏缺陷**，一并修。

**缺陷一（P1，8.7 已知）**：跨域线程在锁内发现新插入的 tick 比在途
的 `m_wakeup_scheduled_when` 更早时，走 `em->reschedule()` 会直接
命中 `eventq.hh` 的宿主线程断言。可达场景：跨域链路延迟较长（比如
snap 后落在较远的 quantum 边界 W），而另一个跨域发送方随后插入了
一个更早的 snap 边界 E < W。8.4 节的诊断测得这个分支在当前拓扑/
负载里触发次数为零，所以是潜伏而非现行——但它是"设计上没考虑"，
不是"设计上不可能"。

**缺陷二（P2，本节新发现）**：**宿主线程自己**走这个 reschedule
分支也可能出错。链条是：跨域线程 D 在 `m_wakeup_event` 未在途时
调用 `em->schedule()`——跨线程走 `asyncInsert()`，事件进入 `em` 的
`async_queue`，要等下一个 quantum 边界的 `handleAsyncInsertions()`
才被合并进主队列；在合并发生**之前**，宿主线程 B 插入了一个更早的
本地 tick E（完全可能：D 的 snap 边界 W 可以在多个周期之外，而 B
的本地事件只需 `clockEdge()+1` 个周期），`scheduleNextWakeup()` 看
`m_wakeup_scheduled==true` 且 `E < m_wakeup_scheduled_when`，于是
调用 `em->reschedule()`——线程归属合法，但 `reschedule()` 内部的
`remove()` 只在**主队列**里找这个事件，而它此刻还躺在
`async_queue` 里，于是直接 `panic("event not found!")`
（`eventq.cc` 的 `EventQueue::remove()`，两处 panic 已核实）。
这个缺陷同样还没有实测触发过（触发了也是干净的 panic 而不是
静默错误），但结构上可达，而且和 P1 是同一个分支的两张面孔：
**`m_wakeup_event` 一旦经由异步路径调度过，在它真正触发之前，
对它做 `reschedule()` 从任何线程都不安全**。

**修复设计的核心规则**：`em->reschedule(m_wakeup_event, ...)` 只在
同时满足两个条件时允许——①当前线程是 `em` 的宿主线程；②
`m_wakeup_event` 上一次是被**本地**（宿主线程直接 `insert()`）调度
的，而不是经由跨域 `asyncInsert()`。其余所有"需要一次更早的触发"
的情形，改用一个**一次性的、堆分配的 AutoDelete "kick" 事件**通过
`em->schedule()` 提交——`schedule()` 本来就是任意线程可调用的
（跨线程自动走 `asyncInsert()`），这正是 gem5 自己给"让某个队列的
线程在 tick T 做一件事"提供的惯用机制。kick 用
`EventFunctionWrapper(callback, name, /*del=*/true, prio)` 构造
（`del=true` 置 `AutoDelete`/`Managed` 标志，`serviceOne()` 在
dispatch 后自动 `release()` 释放，已核实 `eventq.hh`/`eventq.cc`
两端的机制），优先级用和 `m_wakeup_event` 相同的 `ev_prio`（需要在
`Consumer` 里存一份副本）。

**状态变更**（全部仍然只在 `Consumer::lock()` 保护下读写，延续
8.7 的不变式）：

```cpp
// 替换 8.7 的 m_wakeup_scheduled_when，语义细化：
bool m_wakeup_scheduled = false;   // m_wakeup_event 是否在途（含 async_queue 里）
Tick m_wakeup_when = 0;            // m_wakeup_event 在途时的目标 tick
bool m_wakeup_async_pending = false; // 在途的这次调度是否经由跨域 asyncInsert
                                     // （true 则在它触发前禁止 reschedule）
std::set<Tick> m_inflight_ticks;   // 所有"已承诺会在该 tick 精确触发一次"
                                   // 的 tick 集合（m_wakeup_event 的 + 各 kick 的）
Event::Priority m_ev_prio;         // 构造 kick 时复用
```

`m_inflight_ticks` 必须是集合而不是单个标量：一旦存在多个在途事件
（主事件 + 若干 kick），某一次触发之后"剩余在途事件里最早的是哪个"
这个信息就无法从单个标量恢复——8.7 的单标量 `_when` 在引入 kick 后
不够用了，这是本节把它替换掉的原因。

**核心不变式**：在每次对 `m_wakeup_ticks` 的插入或消费之后（同一个
锁临界区内立即执行修复动作），未来最早的待处理 tick E 必然满足
`m_inflight_ticks` 中存在一个**恰好等于 E** 的承诺。比 E 晚的 tick
不需要提前承诺——每次触发后重新锚定（链式覆盖），这和现在（以及
改动前单线程版本）的行为一致。由此可推出：每个 tick 都会被恰好一次
、在恰好它自己的时刻的触发消费掉，`processCurrentEvent()` 的
`assert(em->clockEdge() == *curr)` 恒成立；同一 tick 不会有两个
在途事件（承诺前先查 `m_inflight_ticks`，已有即跳过）。

还有一个不显然但重要的推论：**"发现最早 tick 没有被承诺"的线程，
必然就是刚刚插入这个 tick 的线程自己**（因为更早的每次插入都在
各自的临界区里当场完成了承诺）。所以跨域线程创建 kick 时用的 tick
一定是它自己刚 snap 过的 quantum 边界——8.7 节 snap 论证的"合并
发生在目标 tick 之前"的保证对 kick 自动成立，不需要额外论证。

**改写后的调度逻辑**（伪代码；`ensureScheduled()` 即现在的
`scheduleNextWakeup()`，调用者必须已持锁）：

```cpp
void Consumer::ensureScheduled()   // 调用者持有 Consumer::lock()
{
    auto it = m_wakeup_ticks.lower_bound(em->clockEdge());
    if (it == m_wakeup_ticks.end()) return;
    Tick when = *it;

    if (!m_inflight_ticks.empty()) {
        assert(*m_inflight_ticks.begin() >= when);  // 不变式自检
        if (*m_inflight_ticks.begin() == when)
            return;                 // 最早 tick 已有承诺
    }

    bool owning = !inParallelMode
               || curEventQueue() == em->eventQueue();

    if (m_wakeup_scheduled && owning && !m_wakeup_async_pending) {
        // 唯一允许 reschedule 的情形：宿主线程 + 本地调度的在途事件
        em->reschedule(&m_wakeup_event, when, true);
        m_inflight_ticks.erase(m_wakeup_when);
        m_inflight_ticks.insert(when);
        m_wakeup_when = when;       // 原来的 tick 失去承诺，之后链式补上
    } else if (!m_wakeup_scheduled) {
        em->schedule(&m_wakeup_event, when);   // 任意线程合法（跨域走 async）
        m_wakeup_scheduled = true;
        m_wakeup_when = when;
        m_wakeup_async_pending = !owning;
        m_inflight_ticks.insert(when);
    } else {
        // 主事件在途但不可动（跨域线程，或 async-pending）：一次性 kick
        auto *kick = new EventFunctionWrapper(
            [this]{ processKick(); }, "Consumer kick",
            /*del=*/true, m_ev_prio);
        em->schedule(kick, when);
        m_inflight_ticks.insert(when);
    }
}

void Consumer::processCurrentEvent()   // m_wakeup_event 触发
{
    lock();
    m_wakeup_scheduled = false;
    m_wakeup_async_pending = false;
    m_inflight_ticks.erase(em->clockEdge());
    // （以下与 8.7 相同）
    auto curr = m_wakeup_ticks.begin();
    assert(em->clockEdge() == *curr);
    m_wakeup_ticks.erase(curr);
    wakeup();
    ensureScheduled();
    unlock();
}

void Consumer::processKick()           // 某个一次性 kick 触发
{
    lock();
    m_inflight_ticks.erase(em->clockEdge());
    auto curr = m_wakeup_ticks.begin();
    assert(em->clockEdge() == *curr);
    m_wakeup_ticks.erase(curr);
    wakeup();
    ensureScheduled();
    unlock();
}
```

（`processCurrentEvent()`/`processKick()` 的共同体可以抽成一个私有
helper，伪代码为清晰起见写开。）

**开销分析**：串行/单线程模式下 `inParallelMode==false`，`owning`
恒真、`m_wakeup_async_pending` 恒假——kick 分支**永远不会走到**，
不产生任何堆分配，调度行为和现在完全一致（基线 `simTicks` 必须
逐字节不变，这仍然是硬性验证条件）。新增的常态开销只有
`m_inflight_ticks` 的一次 insert + 一次 erase（常态大小为 1）；如果
S-002 §7.3的单线程开销数字因此明显变差，可以后续对 size==1 的常见情形
做特化，但先不做。

**验证计划**：（1）基线 `simTicks` 逐字节不变；（2）复现命令多次
稳定通过（同 8.7 的标准）；（3）加一个临时计数器统计 kick 的创建
次数——如果在当前拓扑/负载里是零（8.4 的测量结果暗示很可能是零），
要如实记录"该路径是构造上正确、但未被实测行使过"，不能把"没触发"
当成"验证通过"；届时可以考虑构造一个跨域延迟差异更大的压力配置来
真正行使它，作为后续工作。

**实现与验证结果**（实现提交：`968d48d377`）：

- **基线**：`simTicks` 不变，仍为 `31555788000`（最终二进制上跑了
  两次，逐 tick 一致）。
- **复现命令**：最终二进制上 5 次独立运行（`--parallel-l2-eventq
  --sim-quantum=0.1ns --abs-max-tick=2000000000`）全部干净跑到
  2e9 tick 上限退出，无断言、无段错误；加上带计数器的中间构建的
  2 次，共 7 次全过。
- **kick 计数（验证计划第 3 条，如实记录）**：临时计数器（已按惯例
  移除，不留在树上）在基线（串行）运行里为 **0**——符合"串行模式下
  kick 分支不可达"的设计承诺；在两次并行复现运行里也都是 **0**——
  和 8.4 节"跨线程 reschedule 分支触发次数为零"的测量一致，说明
  **kick 路径在当前拓扑/负载下没有被实测行使过**。P1/P2 的修复
  在这个测试里是"构造上正确"而非"实测验证过"；真正行使这条路径
  需要构造跨域延迟差异更大的压力配置（比如两个跨域发送方、其
  snap 边界相互交错的拓扑），列为后续工作。
- **验证过程中发现的一个与本改动无关、但值得记录的坑**：SE 模式下
  `-c` 传入的程序路径会原样成为被模拟程序的 `argv[0]`，改变初始
  栈镜像的内容和长度，从而**确定性地**改变模拟时序——用绝对路径
  `-c /workspace/gem5/tests/.../threads` 跑基线得到
  `simTicks=31552008500`，用仓库根目录下的相对路径
  `-c tests/.../threads` 得到 `31555788000`，两种写法各自完全可
  复现（各跑两次逐 tick 一致）。第一次从脚本里用绝对路径跑基线时
  曾被误判为"基线被改坏了"。**结论：所有以 tick 数逐位对比为目的
  的基线运行，必须固定在 `/workspace/gem5` 下用相对路径调用**（
  历史基准值 `31555788000` 绑定的是这种调用方式）。


---

**上一篇**：[S-002：v1 实现：per-consumer 锁](./S-002-v1-per-consumer-lock.md)
**下一篇**：[S-004：第一次真实加速比测量](./S-004-first-speedup-measurement-and-fixes.md)
**返回**：[INDEX.md](./INDEX.md)
