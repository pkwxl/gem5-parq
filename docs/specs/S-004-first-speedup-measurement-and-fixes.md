# S-004: 第一次真实加速比测量——125 倍减速到追平串行

> **状态**：精确模式（Q=25000）首次追平串行（9.5）；CPU 入域后 SE 模式一路
> 修到底层内存安全问题全部清除（futex UAF、跨域 TLB 竞争、跨域 deschedule，
> 9.7-9.8 均已提交），但最终停在客户机线程退出的 glibc arena 竞争墙（9.9）
> ——这是项目决定转向 FS 模式（[S-006](./S-006-fs-mode-migration.md)）的直接
> 原因。对应原单体设计文档第 9 节，内容原样保留。

---

## 9. 第一次真实加速比测量：约 125 倍减速，同步开销完全吞噬一切

崩溃修完之后（8.5-8.8），第一次可以真正测"5+1 个 EventQueue、6 个
OS 线程"配置相对单队列基线的速度了。结果非常糟糕，如实记录。

### 9.1 测量设置与原始数据

两种配置跑**同一段模拟区间**（`--abs-max-tick=2000000000`，即 2e9
tick，保证模拟工作量逐 tick 对齐；完整跑完工作负载对并行配置来说
需要数小时，不现实），除 `--parallel-l2-eventq --sim-quantum=0.1ns`
外其余参数完全一致，各跑 3 次取 `hostSeconds`。宿主机在测量期间
负载很低（开始时 load average 0.41，结束时 3.4——后者就是测量
本身），不是资源竞争造成的噪声。二进制为 `968d48d377` 树的构建。

| 配置 | hostSeconds（3 次） | 中位数 |
|---|---|---|
| 基线（1 线程，单 EventQueue） | 4.17 / 4.20 / 4.13 | 4.17 |
| 并行（6 线程，quantum=100 tick） | 512.36 / 540.33 / 571.86 | 540.33 |

**减速比 ≈ 130 倍**（中位数比；最好对最好也有 124 倍）。目标是
>3 倍加速，实测是两个数量级的减速。

### 9.2 开销在哪里：逐 quantum 的算术分解

- 2e9 tick / quantum 100 tick = **2×10⁷ 次全局同步**。
- 并行运行 512.36 s ÷ 2×10⁷ quantum ≈ **25.6 µs / quantum**。
- 基线吞吐 2e9 tick / 4.17 s ≈ 4.8×10⁸ tick/s，即 100 tick 的
  quantum 里全部有用模拟工作只值 **≈0.21 µs**。
- 也就是说每个 quantum 里，同步机制的成本是有用工作的 **~120 倍**
  ——这个运行本质上是一台纯粹的同步机器，模拟只是副业。
- 更荒诞的一层：quantum（100 tick）只有 ruby 时钟周期（500 tick）
  的 1/5——**每 5 个 quantum 里有 4 个连一条时钟沿都不包含**，
  绝大多数全局同步醒来后发现无事可做，再睡回去。

同步路径的具体构成（代码已核实）：每个 quantum、每个队列线程都要
执行一次 `GlobalSyncEvent::BarrierEvent::process()`
（`src/sim/global_event.cc`）——**两次** `Barrier::wait()`（
`src/base/barrier.hh`，`std::mutex` + `std::condition_variable`、
`notify_all`，即每次都是 futex 睡眠/唤醒）加一次
`handleAsyncInsertions()`。6 线程 × 2 次 cv 屏障 × 2×10⁷ quantum，
每次唤醒都要走 OS 调度器——25.6 µs/quantum 完全对得上量级。之前
观察到的"6 个线程只用了 ~300% CPU"也吻合：线程约一半时间睡在
futex 里等屏障。

（脚本里对并行 run 1 的中途线程采样打错了对象——`ps -T -p` 采的是
`timeout` 包装进程而不是 gem5 本体，只显示 0%；~300% 的数字来自
此前对未加界限运行的手工采样，这里如实说明数据来源。）

### 9.3 这对 >3x 目标意味着什么：定量的差距和可能的出路

设串行工作量 W=4.17 s（2e9 tick），理想 5 路切分后每线程 W/5，
同步成本 = (2e9/Q)×c，c≈25.6 µs 为实测每 quantum 成本。要达到
3 倍加速需要 (2e9/Q)×c ≤ W(1/3−1/5) ≈ 0.56 s，解出
**Q ≥ ~9.2×10⁴ tick ≈ 184 个 ruby 周期**。而 S-003 §8.1确立的正确性
约束是 Q 必须小于最小跨域链路延迟——当前 Crossbar 拓扑里这个值
只有 **1 个周期（500 tick）**。差距约 **200 倍**，靠微调是填不上的。

可能的组合出路（都还没做，按可信度排序）：

1. **把跨域链路延迟改成物理上现实的值**。目前拓扑里 L1↔L2 的链路
   延迟是缺省的 1 周期——真实共享 L2 的访问延迟是几十个周期。
   parti-gem5 的"quantum 略小于共享缓存命中延迟"规则本来就预设了
   这个前提（他们的 quantum 是几十 ns，不是 0.05 ns）。把链路延迟
   建模成例如 20-40 周期（这是更真实的模型，不是为了并行而造假），
   quantum 上限就抬到 1-2 万 tick。
2. **换掉 cv 屏障**。对 6 个线程、亚微秒级的等待间隔，自旋屏障比
   futex 睡眠/唤醒便宜一个数量级以上是常识范围。若 c 降到 ~2 µs，
   3x 所需的 Q 降到 ~7×10³ tick ≈ 14 周期——和出路 1 的 20-40 周期
   链路延迟正好接得上。
3. **加大每 quantum 的有用工作**：更多核/更大的域（每域多个 L1）、
   更重的负载——分摊固定同步成本。

也就是说：**这次测量否定的不是方向，而是当前这组参数**——1 周期
跨域延迟 + cv 屏障 + 100 tick quantum 的组合在数学上就不可能赢。
出路 1+2 组合后的量级刚好对上（真实延迟 20-40 周期 vs 所需
14-184 周期），这是下一阶段该做的实验，而不是继续在当前参数下
优化锁的细节（S-002 §7.3那 ~15% 的单线程锁开销在 130 倍的减速面前
完全不值得现在去追）。

### 9.4 按 9.3 出路做的第一轮实验：三个新发现（两个是真 bug），
9.3 的"提高链路延迟"前提本身被修正

按 9.3 节的方向跑了一轮参数扫描（`--link-latency` 已是现成选项，
Crossbar.py 的所有链路都用它，无需改代码）。矩阵：性能用固定
2e9 tick 窗口（大负载，各 2 次），精度用完整小负载
（`--options="20000 1"`，比较跑完的 simTicks）。结果按重要性排列：

**修正 9.3 的前提：提高网络链路延迟并不能提高"精确模式"的 quantum
上限。** 实验设计阶段重读 se.py 的域分配代码时发现：4 个 CPU 从来
没有被分配 `eventq_index`，全部继承了默认队列 0；而 sequencer 是
`l1_cntrl` 的后代、跟着 L1 走（域 1-3）。于是 CPU→sequencer 这条
mandatory queue 入队边（延迟 ~1 个 ruby 周期，经
`MessageBuffer::enqueue`，有锁、有 snap）对 CPU 1-3 来说是**跨域**
的——它、而不是网络链路，才是"最小跨域延迟"的约束边。只要 CPU 还
留在队列 0，精确模式（snap 不产生扰动）的 quantum 上限就被钉死在
1 个周期（500 tick），跟链路延迟无关。另外 `--link-latency=20` 本身
改变了被模拟的系统（小负载 simTicks 从 6389611000 变成 9297647000，
+45.5%）——每个延迟配置都需要重建自己的基线，不同延迟下的数字不能
直接互比。

**性能数据（2e9 tick 窗口，hostSeconds）**：

| 配置 | hostSeconds | 相对串行基线 |
|---|---|---|
| 串行 ll=1（9.1 节） | 4.17 | 1x |
| 并行 Q=100（9.1 节） | 512-572 | ~0.008x |
| 并行 Q=500（本轮） | 99.61 / 84.48 | **~0.05x** |
| 串行 ll=20 | 2.78 / 2.78 | （另一个系统，不可互比） |

Q=500（精确模式上限）比 Q=100 快了约 6 倍，和屏障模型完全一致
（4×10⁶ 次同步 × ~21µs ≈ 84s，几乎全部是屏障）。**精确模式的
地板就是 ~20 倍减速**，只要 CPU 还在队列 0（Q 被钉在 500）且屏障
还是 cv 实现（~21-26µs/次）。

**新 bug 一（8.7/8.8 自己的缺陷，大 quantum 下立刻暴露）**：所有
Q≥10000 的运行在启动后 2-3 秒内断言失败——
`ensureScheduled()` 的 `*m_inflight_ticks.begin() >= when`
（Q=25000，tick 5450500）和 `processCurrentEvent()` 的
`erased == 1`（Q=10000+ll20，tick 26040500）。机制：**跨域线程调用
`em->clockEdge()` 时，它是用调用者自己的线程本地 `curTick()`（TLS
的 curEventQueue）计算的，不是 em 所属队列的时间**——发送方可以
领先/落后 em 至多一个 quantum，于是 `ensureScheduled()` 里
`lower_bound(em->clockEdge())` 的"未来视界"是歪的。Q≤500 时歪斜
不超过一个时钟周期、被取整吸收；Q=10000+ 时歪斜几十个周期，
bookkeeping 的比较立刻崩。更糟的一层：`Clocked::clockEdge()` 不是
纯读——它会**更新对象缓存的 tick/cycle 字段**，跨线程调用本身就是
对 em 时钟缓存的数据竞争。修复方向（未实现）：跨域插入路径不该
评估 `em->clockEdge()`——插入者只需要"自己刚插入的（已 snap 的）
tick"就能判断要不要提交（未覆盖 ⇔ 自己的 tick < inflight 最小值，
S-003 §8.8已证明"未覆盖的最早 tick 只可能是刚插入的那个"）；完整的
重新锚定（需要真时钟）只发生在触发路径上，而那永远在 em 自己的
线程里执行。这样跨线程的 `clockEdge()` 调用从 Consumer 路径上
彻底消失。

**新 bug 二（更根本，且宣告此前所有"干净验证"都不完整）**：
Q=500 的完整小负载运行在 tick **5083884501**（注意：超过了此前
所有验证窗口的 2e9 上限）崩在
`TimingSimpleCPU::IcachePort::recvTimingResp:
assert(!tickEvent.scheduled())`——**端口层的跨线程直调**。
sequencer 的 hitCallback（跑在 L1 域的线程上）经由
`sendTimingResp` **直接调用**队列 0 上 CPU 对象的
`recvTimingResp`，同一时刻 CPU 自己的线程也在动 `tickEvent`——
这条 CPU↔Ruby 端口边完全绕过了 Consumer 锁体系，此前 7 次
"2e9 tick 干净通过"只是没跑到暴露窗口而已（负载不均衡让队列 0
恒落后，掩盖了大部分交错）。这条边在任何 quantum 下都不安全。

**候选出路（都未实现，需要决策）**：

1. **修 bug 一**（无论如何都要做）：按上面的方向重构
   `ensureScheduled()` 的跨线程调用约定——改动局部、风险可控。
2. **修 bug 二的正道 = parti-gem5 的原始形态**：把每个 CPU（及其
   descendants）分进它自己 L1 的域。一石三鸟：CPU↔sequencer 边
   变成同域（bug 二的这条边消失）；精确模式 quantum 上限从"1 周期"
   抬到"最小跨域网络链路延迟"（9.3 的 link-latency 手段重新生效）；
   每域的工作量也更均衡（现在队列 0 背着 4 个 CPU）。代价：SE 模式
   的系统调用仿真状态（futex 表、页分配、mmap——threads 负载重度
   使用 futex）变成跨线程共享，是一片全新的、没设计过的竞争面——
   这正是 parti-gem5 选择 FS 模式/更粗粒度域的原因之一。
3. **换自旋屏障**：与 1、2 正交，21-26µs/quantum 的 cv 屏障对
   亚微秒工作窗口是数量级浪费；但在 1、2 落地前单独做它救不了
   20 倍减速的地板。

诚实的现状总结：崩溃修复的三层（8.5/8.7/8.8）在它们被验证过的
参数域（Q≤500、≤2e9 tick）内依然成立，但这轮实验证明了（a）验证
窗口不够长，端口层还有一整类未同步的边；（b）8.7/8.8 的 bookkeeping
在大 quantum 下有自己的时钟歪斜缺陷。>3x 的目标现在明确依赖出路
2 的成败——CPU 入域是绕不开的下一座山。

### 9.5 修掉 bug 一之后：Q=25000 首次跑通，并行配置首次追平串行

按 9.4 节给出的方向修了 bug 一（提交 `5aa1353f0a`）：
`ensureScheduled()` 改名为 `commitTick(when)`，插入路径不再读
`em->clockEdge()`——调用者把自己刚插入的 tick 直接传进来（同线程
tick 基于自己的 clockEdge、跨线程 tick 已经 snap 过，都保证在 em
的未来），"要不要提交一次触发"只由 inflight 集合决定（S-003 §8.8已
证明"未覆盖的最早 tick 只可能是刚插入的那个"）；完整的重新锚定只
发生在触发路径（永远在 em 自己的线程上，那里的 `clockEdge()` 读取
是正当的，且消费后剩余的 begin() 必然是未来 tick，连 lower_bound
都不需要了）。`scheduleEvent(Cycles)` 加了 own-thread 断言（树内
所有调用者都是对象在自己的 wakeup 里调度自己，已核实）。

**验证结果**：基线 `simTicks` 不变（31555788000）；Q=100/Q=500 的
2e9 窗口依然干净（420s/100s，与此前一致）；**Q=25000——修复前
2 秒内必崩——现在干净跑完 2e9 窗口，两次分别 3.99s / 4.16s，
与串行基线 4.17s 打平**。这是本项目第一次出现"并行配置不比串行
慢"的数据点。屏障算术也对上：8×10⁴ 次同步 × ~21µs ≈ 1.7s 同步
开销，加上并行化的工作量，合计 ~4s。

两个如实的限定：（1）Q=25000 仍是"松弛模式"（snap 在起作用，
跨域到达时刻被取整到 quantum 边界，时序有失真，本轮没有测失真
幅度——9.6 节的方案会直接去掉 snap，此处不单独测了）；（2）2e9
窗口内没撞上 bug 二不等于 bug 二消失了——9.4 节 Q=500 的崩溃发生
在 tick 5.08e9，本轮窗口根本没跑到那个暴露区间。端口层的修复
（CPU 入域）是下一节的主体。

### 9.6 CPU 入域（parti-gem5 原始形态）：精确模式首次跑通完整负载，
以及撞上 SE 模式最后的大山——functional 访问

实现（提交 `fd31250bf8`）：`--parallel-l2-eventq` 下每个 CPU 的
subtree 分进它自己 L1 的域；配套四件事：①去掉 Consumer 的跨域
quantum snap（原始到达 tick 在 Q ≤ 最小跨域延迟时本来就安全，S-001 §2.5
论证；该前提改为 se.py 配置期 `fatal()` 强制）；②全局递归锁
`Process::seEmulLock` 串行化系统调用仿真和缺页修复；③
`EmulationPageTable` 加 shared_mutex（TLB miss 查表 vs 系统调用
改表）；④ `TimingSimpleCPU::activateContext` 跨域激活（clone/futex
唤醒）时把 fetch tick snap 到下一个 quantum 边界——修复实测撞到的
`eventq.hh` schedule 断言（clone3 → `Process::initState` → 对另一个
域的 CPU 直接 `activateContext`，用调用方的歪斜时钟算激活时刻，
tick 7.0e9 / 22.6e9 两次实锤，backtrace 完整）。

**结果**：
- 串行基线逐字节不变（31555788000，锁无竞争时零行为差异）。
- **ll=1、Q=500（精确模式上限）完整小负载首次通过**：负载自校验
  Success，simTicks=6389611500，vs 串行 6389611000——差恰好一个
  ruby 周期（0.5ns / 6.4e9 tick ≈ 8×10⁻⁸ 相对偏差），且**两次运行
  逐 tick 可复现**。接近但不完全精确；一个周期的差异来自某处跨域
  同 tick 事件的执行顺序与串行不同（具体在哪一层未查，如实记录）。
- ll=20、Q=10000 的 2e9 窗口通过：7.06-7.84s，vs 串行 ll=20 的
  2.78s → **0.37x**（此前 Q=100 是 0.008x）。
- **未解决**：ll=20 的长运行（完整小负载 27s 处、完整大负载 87s 处）
  段错误在 `Sequencer::functionalWrite` ← `RubySystem::
  functionalWrite` ← 缺页修复的 `VMA::fillMemPages`（文件后备页的
  惰性装载）。

**新发现的真正大山：SE 模式的 functional 访问在多域下完全未同步**。
机制：SE 模式所有对客户机内存的 functional 访问——缺页时的文件页
填充、`read()`/`write()` 等系统调用对客户缓冲区的拷贝、futex 对
futex 字的读取——都走 `RubySystem::functionalRead/Write`，它会
**遍历所有域的所有 controller 的所有 MessageBuffer、所有 sequencer
的在途请求表、所有交换机的缓冲**（为了找到/更新一条 cache line 的
最新副本），而这些结构正被各域自己的线程并发修改。`seEmulLock`
只把系统调用彼此串行化，管不到"系统调用线程 vs 各域模拟线程"这个
维度。ll=1/Q=500 能通过只是交错窗口更窄、更走运，不是安全。

**候选修复方向（未实现，需要决策）**：
1. **细粒度锁住 functional 遍历**：遍历到每个 controller 时取它的
   `Consumer::lock()`（controller 的状态转换本来就在自己的
   Consumer 锁内执行，天然互斥）；Sequencer 的在途请求表加一把
   小 mutex（makeRequest/回调/functionalWrite 三方共用，热路径
   但无竞争时开销 ~几十 ns）；网络交换机的缓冲同理用其 Consumer
   锁。遍历者一次只持一把对象锁（持有间释放），锁序
   seEmulLock → 单个 Consumer 锁，无环。估计 4-6 个文件、
   60-100 行，机械但面广。
2. **停世界（stop-the-world）**：functional 操作只在 quantum 边界
   执行（所有域线程停在屏障时）。语义最干净、覆盖所有未知角落，
   但 gem5 没有现成的"从任意点挂起到下一个屏障再执行回调"机制，
   要新造，改动更深。
3. （补充性）fillMemPages 这一特定实例还有个窄修法——新分配的物理
   页不可能在任何 cache 里，直接写 backing store 语义等价——但它
   救不了 read()/write()/futex 这些访问"真的可能被缓存的行"的
   场景，只能算规避不算修复。

这是 parti-gem5 选择 FS 模式的原因在我们自己的数据里的重演。方向 1
是当前倾向（Consumer 锁体系已经存在，顺势延伸），但它是又一个
"预期会撞到未知"的子阶段，动手前值得停一下。

### 9.7 实现方向 1：Ruby functional 访问加锁——段错误消除，但撞上
第二座大山（SE 的 mmap 仿真）

按 9.6 的方向 1 实现（5 个文件，约 130 行）：

- **改动 A（`MessageBuffer::functionalAccess`，统一收口点）**：函数体
  拆成 `functionalAccessUnlocked()`，`functionalAccess()` 在调用它之前
  取该 buffer 的 `m_consumer->lock()`（判空）。这一处就覆盖了 walk 会
  碰到的**所有** MessageBuffer——controller 的缓冲、网络交换机的
  port buffer、内部链路 buffer——因为每个 buffer 的增删
  (`enqueue`/`dequeue`) 本来就在同一把 consumer 锁下执行。
  `Consumer::lock()` 是同线程可重入的，所以当 walk 已经持有该 controller
  的锁时（见改动 B），这里的再次加锁安全无死锁。
- **改动 C（Sequencer 在途请求表 mutex，`m_reqTableMutex`，
  `UncontendedMutex`）**：`functionalWrite()` 只读遍历整表时全程持有；
  每一处结构性修改（`insertRequest` 的插入、
  `writeCallback`/`readCallback`/`atomicCallback` 里的
  `pop_front`/`erase`）各自短暂持有。**严格叶子锁**——绝不跨
  `issueRequest`/`hitCallback`/`enqueue` 持有，因此永不与任何 Consumer
  锁嵌套，也就不会与回调路径天然的 `ctrl→seq` 顺序成环。
- **改动 B（`RubySystem` walk 里 controller 的 cache-state 操作）**：
  `simpleFunctionalRead`/`partialFunctionalRead`/`functionalWrite` 三个
  walk 里，`getAccessPermission` 和读写 cache 数据的
  `functionalRead/Write(addr,…)`（这些碰的是 SLICC 状态而非 buffer，
  改动 A 覆盖不到）各用 `cntrl->lock()/unlock()` 包住，一次一个
  controller，碰它的 sequencer 之前先释放。

端到端锁序：`seEmulLock → 任一时刻至多一把 Consumer 锁`，
`m_reqTableMutex` 永远最内层——与 9.6 声明的不变量一致，无环。

**结果**：
- 串行基线逐字节不变（`simTicks=31555788000`，`--options="200000 1"`；
  锁在无竞争时零行为差异）。
- **`Sequencer::functionalWrite` 段错误消除**。此前 ll=20 完整小负载
  在约 27s 处 SIGSEGV（139）于 `Sequencer::functionalWrite ←
  RubySystem::functionalWrite ← VMA::fillMemPages`；改动后同一配置
  （`--parallel-l2-eventq --sim-quantum=10ns --link-latency=20
  --options="20000 1"`）跑到 **tick ~8.47e9**（远超此前所有 2e9
  验证窗口）才失败，且失败点已不在 Ruby。
- **撞上第二座、性质不同的大山：SE 的 mmap 仿真**。新失败是
  SIGABRT（134），断言 `MemState::mapRegion` 的
  `isUnmapped(start_addr, length)`（`mem_state.cc:182`），调用链
  `TimingSimpleCPU → SESyscallFault::invoke → doSyscall →
  mmapFunc<X86Linux64> → mapRegion`，前面还有一条
  `Process::allocateMem: addr 0x7ffff7e34000 already mapped` 警告——
  即一次 **非 MAP_FIXED 的 mmap 把已映射的区域又映射了一遍**。
- **判定：是"时序发散"而非 gem5 自身结构的撕裂竞争**。依据：
  (1) 系统调用全程在 `seEmulLock` 下串行（`se_workload.cc:75`
  `SEWorkload::syscall` 整体加锁），mmap 仿真读改的
  `getMmapEnd/setMmapEnd/MemState` 不可能被并发撕裂；
  (2) 两次重跑崩在**同一个固定客户机地址** 0x7ffff7e34000（mmap
  向下增长区，即 pthread 线程栈所在段）但**不同 tick**
  （8466338000 vs 8466898500，差 ~56 万 tick）。同址+异 tick =
  放宽的跨域时序改变了多线程客户机 pthread 创建/退出 + mmap 的
  交错，使某个线程栈区被映射两次。这正是 9.6 预言的"SE 模式共享
  进程/OS 状态不是为并行时序设计的"在 Ruby 之上再上一层的重演。

**当前状态**：9.6 方向 1 达成其既定目标（消除 Ruby functional 访问
段错误）。新暴露的 mmap/MemState 墙是一个**性质不同、独立的**子问题
（不在 Ruby，不是 seEmulLock 能覆盖的撕裂，而是客户机行为随时序发散）。
按既定协作节奏，在这个"又一处预期外未知"的边界停下，未继续动手修
MemState——候选方向（尚未决策）至少有：把线程栈/mmap 生命周期也纳入
某种时序规整（quantum snap 线程创建/退出，类似 9.6 对 activateContext
的处理），或接受 SE 模式在此类多线程负载下的不确定性并转向 parti-gem5
所选的 FS 模式。

**回归复测（确认没打破已跑通的配置、也没引入死锁）**：ll=1、Q=500
完整小负载（`--parallel-l2-eventq --sim-quantum=0.5ns
--options="20000 1"`）跑到底，`Validating...Success!`，
`simTicks=6389611500`——与 9.6 加锁前的可过结果逐 tick 一致，
用时 374s（落在此前记录的 56–620s 沙盒负载波动区间内），说明
functional 加锁在真实并行运行下既未改变时序、也未在跨域 walk 上
造成死锁（walk 全程只持叶子/短锁，屏障处不持 controller 锁，
持锁者要么在跑要么在屏障且不持锁，恒能推进）。

### 9.8 用 sanitizer 定位 mmap 墙：不是时序发散，是两个真实的跨域
数据竞争/UAF——修掉后 mmap 墙消除，露出下一层线程退出竞争

9.7 结尾把 mmap 墙猜成"客户机行为随时序发散"。这个诊断**错了**，
用 sanitizer 一测就翻案（如实更正记录）。

**先确认不是"发散"而是内存破坏**：给 `MemState::mapRegion` 的
`assert(isUnmapped)` 换成打印重叠 VMA 的 panic + 追踪 mmap/munmap，
重跑发现崩溃点其实先炸在页表 `Trie: Inconsistent parent/kid`
（`base/trie.hh:331`）并伴随 glibc `malloc(): unaligned tcache chunk`
——**堆已经被破坏**。mapRegion 断言和 Trie panic 只是同一处堆破坏
的两个先后触发的探针，两次重跑还崩在同一 tick（8466898500）。

**TSan 在本沙盒不可用**：ThreadSanitizer 需要固定 shadow 布局，
撞上 PIE+ASLR（"unexpected memory mapping"）。本沙盒关不掉 ASLR
（`randomize_va_space` 只读、`setarch -R`/`personality` 被 seccomp
挡住，连 sudo 也不行），非 PIE 重建也救不了共享库的高位随机映射。
改用 **AddressSanitizer**（ASLR 兼容，且症状本来就是堆破坏，正对
ASan 口径）。为此给构建系统加了 `--with-tsan`（附带成果，TSan 在别的
环境能用）和 ASan 的常规路径；ASan 的 X86_ASAN 用单独 build_opts。

**ASan 直接锤出两个真 bug**：
1. **`FutexMap::wakeup` 的 use-after-free**（`futex_map.cc:75`，
   **串行模式也复现**，是个和并行无关的既存 gem5 bug）：
   `auto& tc = waiterList.front().tc;` 绑的是 front 结点内 `.tc`
   字段的引用，`pop_front()` 释放该结点后 `waitingTcs.erase(tc)`
   仍读它——UAF。串行下释放的结点还没被复用所以侥幸不炸；并行下
   另一线程的分配可能在 free 和 erase 之间回收这 32 字节结点，读到
   垃圾指针。修法：pop 前把指针**按值拷贝**（`auto *tc = …`）。
2. **X86 TLB 的跨域数据竞争**（mmap 墙的真正根因）：
   `MemState::unmapRegion`/`remapRegion`（`mem_state.cc:268/364`）里
   ```
   for (auto *tc: _ownerProcess->system->threads)
       tc->getMMUPtr()->flushAll();
   ```
   一个域线程上的 munmap/mremap 会伸手 flush **每一个** CPU 的 TLB，
   而 `X86ISA::TLB::flushAll()` 改的是该 TLB 的 `trie`/`freeList`/
   `tlb[]`——与此同时那些 CPU 各自的域线程正在自己的 TLB 上
   lookup/insert。`seEmulLock` 只把 munmap 彼此串行，管不到"munmap
   线程 vs 各 CPU 自己的翻译线程"。ASan 报的正是 flushAll 里
   `freeList` 的 **double-free**（同一 `&tlb[i]` 被 push 两次）＋
   Trie 撕裂，这才是 Trie/tcache/mapRegion 一连串堆破坏的源头。
   修法（按用户选定的"锁住该结构再复验"）：给 X86 TLB 加一把
   `UncontendedMutex tlbLock`，在 `lookup`/`insert`/`flushAll`/
   `flushNonGlobal`/`demapPage` 里持有（`evictLRU` 在 `insert` 锁内、
   不再单独加锁）。叶子锁，锁序 `seEmulLock → tlbLock`（munmap 侧）
   与 owner 侧单独持 `tlbLock`，无环。

**复验结果**：两处都修掉后，并行 ll=20 完整小负载在 ASan 下**不再有
任何堆破坏**（无 Trie、无 double-free、无 tcache、无 mapRegion 断言），
**mmap 墙消除**，且比之前多跑到 tick 8502397000。

**露出的下一层墙（性质更干净，属线程退出的跨域事件调度）**：ASan 干净
之后撞上 `eventq.hh:794` 的 `deschedule(): !inParallelMode ||
this == curEventQueue()` 断言。backtrace：`exit_group`/`tgkill` →
`BaseSimpleCPU::haltContext` → `TimingSimpleCPU::suspendContext`
（`timing.cc:272-273` `deschedule(fetchEvent)`）。即一个线程执行
`exit_group` 时要停掉**所有**线程的上下文，伸手去 deschedule 别的域
CPU 的 fetchEvent——正是 9.6 对 `activateContext`（跨域 **schedule**）
和 8.8 对跨域 **reschedule** 处理过的同一族问题的第三个实例（这次是
跨域 **deschedule**，发生在线程退出路径）。这也说明用户最初"规整线程
生命周期"的直觉方向没错，只是先被上面两个堆破坏 bug 挡着看不到。修法
同族。

**跨域 deschedule 的修法（已实现，提交 `0f941af218`）**：不需要路由或
kick——`fetch()` 开头本来就有 `if (_status == Idle) return;`（注释写着
"刚被 suspend"），所以 `suspendContext` 里那个 `deschedule(fetchEvent)`
只是"取消一次已无用的 fetch"的优化，不是正确性必需。跨域时
（`inParallelMode && curEventQueue() != eventQueue()`）直接**跳过**
deschedule，让 fetchEvent 之后在 owner 线程上正常触发、被 Idle 判据
无害吞掉即可。与 9.6 对 activateContext 的跨域 schedule 处理同构。

**回归与复验（全部通过）**：
- 串行基线逐字节不变：非 ASan `build/X86/gem5.opt`，`--options=
  "200000 1"` → `simTicks=31555788000`（futex + TLB 锁 + suspendContext
  三处修改都时序中性）。
- 串行 ASan `--options="20000 1"`：`Validating...Success!`，
  `simTicks=6389611000`，无任何 ASan 报错。
- **并行 ll=20 完整小负载在 ASan 下不再有任何 gem5 内部错误**（无堆破坏、
  无 Trie、无 double-free、无 deschedule 断言），一路跑到 tick
  8502407000、所有客户机线程正常退出（"last active thread context"）。

TLB 的 `lookup` 在热路径，加锁的单线程开销尚未测量——按项目一贯做法先
保正确性、开销如实另测；更省开销的替代是把跨域 flush 路由到 owner 域
（免热路径锁），列为后续可选优化。

### 9.9 清掉所有 gem5 内部 bug 之后：并行专属的客户机线程退出墙

并行 ll=20 在 ASan 下跑完整个执行、gem5 侧全绿，但**没有打印
`Validating...Success!`**，且在客户机线程退出（pthread teardown、
`madvise`/`mprotect` 清理）阶段冒出一条
`Fatal glibc error: arena.c:907 (__malloc_arena_thread_freeres):
assertion failed: a->attached_threads > 0`。

**判定为并行专属**（不是 ASan 假象）：同一 ASan 二进制、同一负载
（`--options="20000 1"`）串行跑是干净的——`Validating...Success!`、
simTicks 6389611000、无 arena 报错。只有并行配置才触发。

这已不是"gem5 自身结构被并发破坏"（那一类到 9.8 为止全部堵住、ASan
全绿），而是更上一层：并行放宽时序改变了客户机多线程程序自己的
pthread 创建/退出交错，使**客户机 glibc 的 per-thread arena 记账
（`attached_threads`）在退出清理时不自洽**，客户机在自校验前就终止。
这正是 parti-gem5 选 FS 模式所规避的那类"SE 模式对多线程客户机的
OS/线程语义仿真不足"的问题，在把所有底层内存安全 bug 修干净之后，
以最上层的形态浮现。修它需要审视 gem5 SE 模式对线程 arena/TLS/
`set_robust_list`/`rseq`（目前都被 ignore）及退出顺序的仿真，
是又一个独立、更深的子问题——用户最初"规整线程生命周期"的直觉指向的
正是这一层，只是先被下面三个真 bug（futex UAF、TLB 竞争、跨域
deschedule）挡着。停在此处等决策。


---

**上一篇**：[S-003：gem5 首次多线程 Ruby 实测](./S-003-first-multithreaded-ruby-crashes.md)
**下一篇**：[S-005：宿主机线程绑核（CPU affinity）](./S-005-host-thread-cpu-affinity.md)
**返回**：[INDEX.md](./INDEX.md)
