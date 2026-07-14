# 设计问题记录：4 私有核队列 + 1 共享 L2 队列的并行加速方案

> 这是一份**问题记录/设计前置材料**，不是结论报告——目的是让后续新开的会话
> 能够直接带着完整背景进入设计讨论，不用重新梳理。截至本文写完，双方尚未
> 就具体架构方案做出决定。

## 0. 背景：为什么会走到这个问题

前三份实验报告（[`gem5-parallel-event-dispatch-analysis.md`](./../refs/gem5-parallel-event-dispatch-analysis.md)、
[`gem5-parallel-eventq-speedup-experiment.md`](./../refs/gem5-parallel-eventq-speedup-experiment.md)）
依次验证了：

1. gem5 现有的 quantum-barrier 多 `EventQueue` 机制，本地事件几乎零开销，
   跨队列事件走 `async_queue` + 全局 barrier 合并；
2. 完全独立、零通信的负载能拿到 87%~99% 的并行效率（8 路以内）；
3. 但只要跨队列的两侧共享一个**需要 timing 精度**的 cache/内存，经典
   （非 Ruby）gem5 内存系统就没有可用路径——直连会在
   `EventQueue::schedule()`（`eventq.hh:759`）断言崩溃；gem5 官方提供的
   跨队列桥接对象 `mem.ThreadBridge` 明确只支持 atomic/functional，见到
   timing 请求直接 `panic`（`thread_bridge.cc:57`）。

结论定格在："只要还要保 timing 精度，经典内存系统 + 多 EventQueue 无解"。

这次的提案，是换一个更接近"真正的并行离散事件仿真"的思路来正面解决这个
问题，而不是继续在经典内存系统的现有接口里找空子。

## 1. 提案复述

- 拓扑：4 个"核 + 私有 L1"各自一个 `EventQueue`（4 个私有队列/线程），
  这 4 个核共享的 L2 单独占第 5 个 `EventQueue`/线程，作为一致性仲裁点。
- 核→L2 是多生产者场景（MPSC），L2→某个核的响应是单生产者场景（SPSC）。
- 目标：这 5 个线程（对应 5 个宿主核）跑起来，加速比要 **> 3**（相对基线
  待定，见第 4 节）。
- 手段：用**无锁队列**技术做核和 L2 之间的通信，避免用锁做同步。
- 明确愿意为此**牺牲一部分仿真精度**，类比"parti-gem5"这类工作——即
  接受重构 gem5 的事件处理机制本身，而不只是在现有接口上打补丁。

## 2. 关键外部参考：parti-gem5

用户提供的文献：[`Scalable_and_Accurate_Parallel_Timing_Simulations_.pdf`](../refs/Scalable_and_Accurate_Parallel_Timing_Simulations_.pdf)

> Cubero-Cascante, Zurstraßen, Nöller, Leupers, Joseph. *Scalable and
> Accurate Parallel Timing Simulations with parti-gem5*. International
> Journal of Parallel Programming, 2026, 54:16.
> （前身：SAMOS 2023 会议论文 *parti-gem5: gem5's timing mode
> parallelised*；再前一代工作是 DATE 2023 的 *par-gem5*，只支持 atomic
> 模式）

这篇论文和我们这次的设想**高度重合**，读下来有几个关键信息，会直接改变
或修正上一轮讨论里悬而未决的几个问题（见第 3 节）：

### 2.1 系统分区：和这次提案的拓扑完全一致

论文用的分区规则和这次提案几乎一模一样：每个 CPU 核 + 其私有资源
（私有 cache、TLB、局部互连）独占一个 time domain（EventQueue）；所有
共享资源（共享 cache、主存、外设）放在**额外的一个** domain 里。
**N 个核 → N+1 个线程**——这次提案里"4 核 + 1 个共享 L2 队列 = 5 队列"
正是这个公式在 N=4 时的实例，不是我们凭空想的，是有实证支持的既有方案。

### 2.2 前提条件：必须用 Ruby，不能用经典内存系统

论文明确对比了 par-gem5（前作，只支持 atomic 模式）和 parti-gem5（这篇，
支持 timing 模式）的区别，关键就在于**parti-gem5 是建立在 Ruby 一致性
子系统上的，不是经典 `BaseCache`**。原因论文里说得很直白：Ruby 的
消息传递模型（`MessageBuffer` + `Consumer`，见下）天然就是"发送方把消息
放进缓冲区，接收方异步被唤醒来处理"的生产者-消费者模式，不像经典
`BaseCache`/`CoherentXBar` 那样通过同步的 C++ 函数调用链直接扎进对方
对象——这正好对应我们自己在实验一、三里踩到的坑（`sendTimingReq` 同步
调用链跨线程直接触发断言/`panic`）。**这件事基本一锤定音：这次提案如果
要实现，必须构建在 Ruby 之上，经典内存系统这条路已经被我们自己的实验和
这篇论文的设计选择双重验证是走不通的。**

### 2.3 线程安全机制：不是无锁，是"精心限定范围的锁"

这一点**直接挑战了提案里"用无锁队列、避免锁"的前提**，需要在设计会上
重新讨论。论文原文（4.2 节）明确说：给每个 `MessageBuffer` 独立加锁是
**不够的**（"using an independent mutex to protect each buffer is
insufficient"）。原因：一个 Consumer（比如 L2 的一致性控制器）可能同时
从多个 Sender（比如 4 个核）收消息，Consumer 在 `wakeup()` 事件里要
**一次性检查它所有的输入缓冲区**；如果每个缓冲区各自独立加锁，就没法
保证"Consumer 检查所有输入 buffer 的这个过程"和"某个 Sender 正在往其中
一个 buffer 里塞新消息"之间的正确交错。

论文的解法：**给每个 Consumer 分配一个共享的 "wakeup mutex"，这个
Consumer 名下所有的输入 `MessageBuffer` 都共用同一把锁**。Consumer 处理
`wakeup()` 时锁住它，所有 Sender 往它任何一个输入 buffer 塞消息前也要
拿这把锁——本质上是把"1 个 Consumer 对多个 Sender"这个 N:1 关系，收敛成
一把锁保护的临界区，而不是多把细粒度锁。论文里全文没有出现
"lock-free"/"CAS"/"atomic<>"这类无锁编程的字眼。

**这次提案里"用无锁队列避免锁"这个手段本身，在已发表的、跑出 3.37×~
18.18× 实测加速比的方案里并不是必要条件**——真正必要的是"锁的粒度和
范围要设计对"，而不是"完全不用锁"。这不代表无锁一定没有意义（无锁在
高竞争场景下确实可能进一步降低开销），但意味着：

- **第一版能不能先按论文的"per-consumer 共享 mutex"方案去做，作为已知
  可行、有实测数据支撑的基线，无锁作为后续的可选优化，而不是第一版的
  硬性前提？** 这是设计会上应该重新拍板的问题。

### 2.4 双向通信的死锁风险：环形等待

论文额外指出一个我们之前没考虑到的风险：如果两个跨 domain 的节点**互为
发送方和接收方**（比如两个 Router 双向通信），简单地"各自一个消息缓冲区"
会造成环形等待死锁——如果 R0 和 R1 的 `wakeup()` 同时发生，两者都会锁住
自己的输入缓冲区，然后永远等待对方释放输出缓冲区的空间。论文的解法是
在每个方向上插入一个额外的中间对象（`Throttle`），把一条双向链路拆成两条
独立的单向链路，从而打破环形等待。

**这一点需要我们对着自己的拓扑仔细检查**：如果核和 L2 之间只是单纯的
"核发请求、L2 发响应"（请求方永远是核、数据提供方永远是 L2），风险应该
比论文里"两个对等 Router 互相通信"要小；但如果 L2 除了响应请求外，还要
**主动向核发起 snoop/invalidate**（一致性协议几乎必然需要这个），那么
L2 对某个核而言既是响应者又是发起者，核对 L2 而言既是请求者又是
snoop 的接收者——这就具备了论文描述的那种双向关系，需要评估是否也要引入
类似 `Throttle` 的机制。

**验证结论（已确认，不再是待评估项）**：对照 `MESI_Two_Level` 协议源码
（`src/mem/ruby/protocol/MESI_Two_Level-L2cache.sm`、
`MESI_Two_Level-L1cache.sm`）走查了具体锁链，**环形等待风险确认存在**，
不是理论上的可能性：

- L2 的 `wakeup()`（持有 `M_L2`）处理 L2 replacement 或转发请求时，调用
  `f_sendInvToSharers`/`fw_sendFwdInvToSharers`
  （`MESI_Two_Level-L2cache.sm:609,620`），把 `INV` 塞进
  `L1RequestFromL2Cache`——这要求在**仍持有 `M_L2`** 的情况下获取目标核的
  `M_L1_i`。
- 与此同时，L1ᵢ 的 `wakeup()`（持有 `M_L1_i`）处理本地 miss 发起
  `GETS`/`GETX`，或者正在执行 `fi_sendInvAck`
  （`MESI_Two_Level-L1cache.sm:810`）回 ACK 给 L2，都要把消息塞进
  `L1RequestToL2Cache`/`responseFromL1Cache`——这要求在**仍持有
  `M_L1_i`** 的情况下获取 `M_L2`。
- 两条线程：L2 线程持有 `M_L2` 等 `M_L1_i`；L1ᵢ 线程持有 `M_L1_i` 等
  `M_L2`——经典 AB-BA 循环等待。

**一个容易误判的地方**：现有的 vnet/MessageBuffer 方向性拆分
（`L1RequestFromL2Cache` vnet2 / `L1RequestToL2Cache` vnet0 等）**不能**
规避这个问题——论文方案里锁的粒度是"每个 Consumer 一把锁，覆盖它名下所有
inbound buffer"，跟消息走哪个 vnet/buffer 无关，只跟"谁在等谁的
Consumer 级别锁"有关，方向拆分解决的是另一类问题（缓冲区容量/顺序），
不解决这个锁序问题。

**结论**：如果第一版按论文的"per-consumer 共享 wakeup mutex"方案实现，
针对"4 核 + 1 L2"这个拓扑，`Throttle`（或等价的、能打破"持有己方锁时
反过来去请求对方锁"这个模式的机制）**不是可选项，是必需的设计元素**。

### 2.5 精度损失具体是怎么放宽的

跨 domain 调度事件时，**消息的到达时间和唤醒事件的时间会被统一挪到"下一个
quantum 边界"**（不是"只要满足某个下限就行"，是直接对齐到边界），这是
论文里精度损失的主要来源——效果是系统性地**高估**了跨 domain 链路的延迟。
论文没有放宽一致性协议本身的逻辑正确性（协议状态机的处理顺序在每个
domain 内部依然是精确的、由本地事件队列保证的因果序），放宽的只是
"跨 domain 消息到底哪一刻抵达"这个时间粒度——对应我们上一轮讨论里
"精度放宽"的选项 A（只放宽事件时间戳精度，不放宽协议逻辑正确性），
不是选项 B（协议本身也做近似）。论文原话也印证了这一点："we did not
encounter any causality errors that affected the correctness of the
simulated workloads"——但同时坦诚："conducting more formal verification
... would be an important contribution"，即：**没有做过正式的正确性
证明，只是大量实测没发现问题**，这是我们如果复用这套思路也需要承担的
同等风险，不能想当然认为"协议正确性"是有严格保证的。

quantum 大小的选取方式：设成**略小于共享 cache（非私有的那一级）的命中
延迟**。论文的实测系统里（32 核目标，L1 命中 1ns、L2 命中 4ns、L3 命中
6ns、NoC 单跳延迟 0.5ns、L1→L2→L3 往返约 10 跳共 5ns），算出来的理论
上限约 16ns，但实际扫描了 4/8/12/16ns 四档后发现 **4ns 时 16 个 benchmark
的仿真时间误差全部低于 5%**，作为推荐设置。这个量级和我们之前在讨论里
自己估算的"L1↔L2 往返 15~25ns"基本吻合，说明我们之前的推算方法（哪怕
因为架构限制没能真正跑出数据）方向是对的。

### 2.6 实测数据（32 核目标，供参考基线）

| 指标 | 数值 |
|---|---|
| 分区方式 | N 核 → N+1 线程 |
| 加速比范围（16 个 benchmark，quantum 4~16ns 综合） | 3.37×（PARSEC.DEDUP）～ 18.18×（BM.SORT），均值 10.52× |
| quantum=4ns 时的均值加速比 | 9.70× |
| quantum=4ns 时的仿真时间误差 | 全部 16 个 benchmark < 5%，均值 1.95% |
| 关键活动指标（CPU/cache/DRAM 的功耗状态、操作计数）综合 MAPE | 2.62%（另一处评估口径给出 5.13%，两者统计范围不同） |
| 最大规模实测 | 120 核目标 / 64 核宿主机，速比 42.7×（计算密集、几乎不共享数据的 BM.SORT） |
| 宿主机 | AMD Ryzen 3990x，64 核 128 线程 x86-64，128GiB DDR4 |
| 目标平台 | ARM O3CPU + Ruby，ARM AMBA CHI 一致性协议 |
| 基于的 gem5 版本 | 21.1.0.2（**我们仓库当前是 25.1.0.1，跨了约 4 年/多个大版本，Ruby 内部接口不能假设完全一致，需要重新核对**） |

一个直接相关的参考点：论文里加速比最低的场景（PARSEC.DEDUP，3.37×）
发生在 **32 核（33 线程）**的规模下；这次提案是 **4 核（5 线程）**，
线程数、共享 L2 上的竞争强度都远低于 32 核场景。按照论文里"核数越少、
相对同步开销占比通常越低"的趋势，**"5 线程做到 > 3 倍加速"这个目标，
对照已发表的数据看是一个合理、大概率可达的目标**，但具体能到多少还是
高度依赖负载的数据共享强度（BM.SORT 这类几乎不共享数据的负载在论文里
即使 32 核也能到 17~18×；PARSEC.DEDUP 这类共享/同步密集的负载 32 核也
只有 3.37×）——这意味着**给这次的验证工作选一个合适的目标 workload
（共享强度适中，不是两个极端）非常关键**，我们之前的两个自制 benchmark
（`work.c` 零共享、`threads.cpp` 共享但在经典内存系统上跑不通）都不满足
这个要求，需要重新准备。

## 3. parti-gem5 如何修正上一轮讨论里悬而未决的问题

对照上一轮 `AskUserQuestion` 的四个问题和当时的回答：

1. **加速比基线**（当时回答："留到设计会上再定"）——现在有了参考锚点：
   论文用"同一目标系统单线程 gem5 的 host 耗时"作为基线，"加速比 = 单线程
   host 时间 / 并行 host 时间"。建议沿用同样口径，但仍需要在设计会上
   明确一件事：我们的基线跑，是要跑通"4 核 + 1 L2 共享、单 EventQueue"
   这个配置本身（目前还没验证过这个中间态配置在单队列模式下能不能正常
   跑——如果要上 Ruby，这一步理论上应该没问题，因为单队列时不存在
   跨队列问题，但还是要实测确认）。
2. **精度放宽到哪一层**（当时回答："不确定，需要先看 parti-gem5 具体
   做法再定"）——现在有答案：**只放宽跨 domain 消息的到达时间（对齐到
   quantum 边界），不放宽一致性协议本身的逻辑正确性**，见 2.5 节。但
   "协议正确性"目前只有实测支撑、没有形式化证明，这个风险需要显式记录
   并在设计会上决定我们是否需要（或者说到什么程度需要）自己做额外验证。
3. **原型策略**（当时回答："两者都要，先独立原型再合入"）——这个策略
   需要重新评估：因为 parti-gem5 已经是一个经过同行评审、有完整方法论
   和实测数据的参考实现，"先写一个 gem5 之外的简化原型验证思路"的
   必要性降低了（思路本身已经被验证过），更现实的第一步可能是：
   **先确认 parti-gem5 的代码是否公开可以直接参考/复用**（论文正文没有
   给出代码仓库链接，只在前作 par-gem5 的一个脚注里提到一个作者个人仓库
   `github.com/jose-cubero/gem5.bare-metal`，这是 benchmark 仓库，不是
   parti-gem5 本体的实现仓库），如果找不到公开代码，"独立原型"这一步
   仍然有价值，但可以直接照着论文 4.1~4.3 节描述的机制去写，而不是从零
   摸索设计。
4. **"parti-gem5" 具体指什么**——已解决，见第 2 节。

## 4. 需要在设计会上继续确认/决定的问题（更新版）

延续上一轮记录、并入这次读论文之后新出现的问题：

**A. 加速比基线**——**已确认，配置能跑通，基线数字已实测拿到**。

用 `configs/deprecated/example/se.py --ruby --l2cache --topology=Crossbar
--num-cpus=4 --cpu-type=X86TimingSimpleCPU`（`--ruby` 走
`configs/ruby/Ruby.py`，协议由 build 目标决定，见 C 节确认用的是
`MESI_Two_Level`）+ `tests/test-progs/threads/bin/x86/linux/threads`
（200000, chunk_size=1，即原始交错分配）作为单队列基线跑了两次：

| 次数 | simSeconds（确定性，两次完全一致） | hostSeconds |
|---|---|---|
| 第 1 次 | 0.027161 | 70.42 |
| 第 2 次 | 0.027161 | 78.85 |

- `simSeconds`/`simTicks` 两次完全一致（27160630500 tick），确认仿真本身
  是确定性的，不受 host 调度抖动影响。
- `hostSeconds` 两次相差约 12%（70.42 vs 78.85），是宿主机侧的噪声
  （共享/虚拟化环境下正常），**结论：基线数字要多跑几次取均值，不能用
  单次结果**，论文的"单线程 host 时间"分母也应该做同样处理。
- 配置本身（4 核 + 1 L2 共享、单 `EventQueue`、Ruby `MESI_Two_Level`）
  跑通、验证通过（`Validating...Success!`），没有出现第 0 节里经典内存
  系统那种 `EventQueue::schedule()` 断言问题——符合预期，因为单队列时
  本来就不存在跨队列问题，这里主要是把"Ruby 版本本身能正常工作"坐实。

**验收口径决定见 G 节**：这个基线只保留吞吐量相关的两个数字
（`simSeconds`、`hostSeconds`），不采集功耗/活动类 stats。

**B. 锁 vs 无锁**——**已决定：第一版采用论文的"per-consumer 共享 mutex"
方案，无锁化列为后续可选优化项，不作为第一版的硬性前提**。

依据：仓库里已经有一个为这个场景量身定制、成熟稳定的原语——
`src/base/uncontended_mutex.hh` 的 `UncontendedMutex`（2020 年引入，
commit "base,sim: implement a faster mutex for single thread case"）：

- 快路径是一次原子 CAS（无竞争时几乎零开销），只有真正发生竞争时才退化
  到 `std::mutex` + 条件变量。
- 头文件注释明确说明其设计目的就是"for most cases without multi-thread
  event queues... avoid the usage of [a full] mutex and speed up the
  simulation"——跟我们"4 核 + 1 L2"这种低线程数场景高度吻合。
- **它已经在用**：`eventq.hh` 里 `EventQueue::service_mutex` 和
  `async_queue_mutex`（现有 quantum-barrier 机制的核心同步原语）都基于
  它实现，也就是我们第 0 节里提到的、在零通信负载下测出 87%~99% 并行
  效率的那套机制背后的锁。
- 全仓库只有两处使用（`eventq.hh` 和一个不相关的 ARM KVM 文件），
  是一个成熟、单一用途、专为"低线程数 event queue"场景设计的原语，
  不是边缘工具。

**结论**：论文里"per-consumer 共享 wakeup mutex"的设计可以直接用
`UncontendedMutex` 实现（复用而非重新发明），无锁化推迟到后续、且要
等 v1 跑起来后**实测**发现真实竞争瓶颈再考虑，而不是预先假设需要。
主要权衡：如果核↔L2 之间的实际消息频率远超预期，这把锁的慢路径
（真正的 mutex + 条件变量）仍可能成为瓶颈——但这是 v1 跑通后才能验证
的经验性问题，不构成现在就要上无锁结构的理由。

**C. 是否必须基于 Ruby**——结论已经比较确定：是。**build 目标已确认
现成可用，不需要新增**：`build/X86/gem5.opt` 就是用
`build_opts/X86_MESI_Two_Level`（`RUBY=y`、`PROTOCOL="MESI_Two_Level"`）
配置编译的，`strings build/X86/gem5.opt | grep MESI_Two_Level` 能看到
`gem5::ruby::MESI_Two_Level::*` 符号，A 节的基线实测就是直接用这个现成
二进制跑通的，没有额外编译准备工作。

**D. 选哪个 Ruby 一致性协议做原型**——**已确定：`MESI_Two_Level`**。
仓库现成 build（见 C）已经是这个协议，A 节的基线实测也是用它跑的——
复杂度低、协议源码在第 2.4 节死锁分析里已经走查过一遍，作为"4 核 1 L2"
原型的起点不需要再额外调研或换协议。

**E. L2→核方向的 snoop/invalidate 是否构成论文描述的"双向通信死锁风险"**
——**已确认：是**。见 2.4 节验证结论：走查 `MESI_Two_Level` 的
`L2cache.sm`/`L1cache.sm` 源码后确认，L2 发 INV、L1 回 ACK/发
GETS/GETX 这一对双向路径，在"per-consumer 共享 wakeup mutex"方案下会
形成 AB-BA 锁序死锁，且现有的 vnet 方向拆分无法规避。**如果坚持论文的
锁方案，`Throttle` 或等价机制是必需项，不是可选优化**——这个结论应该
直接影响后续架构设计，不需要再单独排一次会话去分析。

**F. gem5 版本差异**——**已核对，风险低**。直接用仓库里的 `v21.1.0.2`
tag（论文使用的确切版本）对 `git diff` 到当前 `HEAD`（25.1.0.1）：

- `Consumer`：基本未变，`wakeup() = 0` 接口完全一致，只多了一个可选的
  `Event::Priority` 构造参数。
- `MessageBuffer`：只新增了功能性改动（消息随机化、按周期限速出队、
  routing priority、strict-FIFO bypass），enqueue/dequeue 的基本契约
  没有变化，没有涉及任何线程/锁相关的改动。
- `Sequencer`：diff 最大（513 行），但都是原子内存操作（AMO）支持，跟
  并发/锁无关。
- `eventq.hh`：现有跨 EventQueue 同步机制本身
  （`async_queue_mutex`/`service_mutex`/`UncontendedMutex`，也就是
  quantum-barrier 依赖的那套）在两个版本之间结构完全一致；其余 diff 是
  `EventWrapper`→`MemberEventWrapper` 的现代化重命名等表面改动。

**结论**：版本差异不构成阻碍，论文里"per-consumer 共享 wakeup mutex"
建立在 `MessageBuffer`/`Consumer` 之上的设计思路，基本可以照搬到当前
代码，不需要因为版本差异重新设计机制。

**G. 验收/测量方法**——**已决定：只测吞吐量相关指标，不照搬论文的功耗/
活动 MAPE**。用户明确这次验证工作只关心跟">3 倍加速"目标直接相关的
数字，具体为：

- 精度损失：`simSeconds` 误差 %（并行 vs 单队列基线的仿真时间之差）。
- 加速比：`hostSeconds`（单队列基线，多次取均值，见 A 节）/
  `hostSeconds`（5 队列并行）。
- 不采集功耗状态、操作计数一类的活动指标 MAPE——这类指标论文里用来验证
  "架构级活动特征没有因为精度放宽而失真"，但不是这次"验证 >3 倍加速比
  是否可达、且协议正确性不受影响"这个目标的必要产出，省下额外的 stats
  提取/对比脚本工作量。

**H. workload 选择**——**已决定：给 `tests/test-progs/threads/src/
threads.cpp` 加一个共享强度旋钮，不引入外部 benchmark**。

背景：实测发现 `threads.cpp` 现在跑在 Ruby `MESI_Two_Level` 下完全没
问题（A 节的基线就是用它跑通的）——此前"跑不通"的判定只针对经典内存
系统的 `ThreadBridge` 限制（第 0、2.2 节），换到 Ruby 后这个限制已解除。
但它原来的访问模式是逐元素交错分配（`i += threads`），几乎每条 cache
line 都在全部 4 个核之间来回失效，是接近论文里 PARSEC.DEDUP 那种
「重度共享」的极端，不满足"适中共享"的要求。

调研过外部方案：`configs/splash2/run.py`、
`configs/example/gem5_library/x86-npb-benchmarks.py` 这类脚本仓库里
已经现成，但 SPLASH2 需要外部源码/`rootdir`（本地没有），NPB 走的是
`obtain_resource` 下载 + KVM 全系统启动，环境依赖和工作量都明显大于
"验证 4 核规模的加速比"这个目标所需。

改动：给 `array_add` 加一个 `chunk_size` 参数（block-cyclic 分配），
`argv[2]` 可选，缺省 `chunk_size=1` 与原行为完全一致（向后兼容旧调用
方式）：

- `chunk_size=1`：原始逐元素交错，重度共享/最坏情形。
- `chunk_size >= num_values/threads`：每个线程一段连续、基本不共享
  的分块，对应"零共享"那一端（类似 `work.c`）。
- 中间取值：连续可调的共享强度旋钮，用同一个二进制就能扫出一条
  "共享强度 vs 加速比"曲线，不需要额外准备/移植 benchmark。

已实测验证旋钮确实生效（4 核 + 1 L2 共享、单队列、`MESI_Two_Level`，
`--options="200000 <chunk_size>"`）：

| chunk_size | simSeconds | hostSeconds |
|---|---|---|
| 1（交错，重度共享） | 0.031547 | 107.94 |
| 50000（分块，近似零共享） | 0.026163 | 66.88 |

交错模式比分块模式多出约 20.5% 的仿真时间（0.031547 vs 0.026163），
方向符合预期（更多 cache line 级别的跨核失效 → 更多一致性流量 →
更长仿真时间），确认这个旋钮对共享强度确实敏感，可以用来在"4 核 1 L2"
规模上定位一个"适中共享"的 `chunk_size` 取值（比如让每个分块大小接近
1~2 条 cache line 的元素数，即 `chunk_size` 取 4~8 这个量级，而不是
两个极端）。

## 5. 建议的设计会话切入顺序

1. ~~先确认 B、C、D、F（技术选型和环境准备类问题）~~——**已全部确认**，
   见对应小节：B（`UncontendedMutex` 复用）、C（`build/X86` 已是
   Ruby-enabled，无需新建 build）、D（`MESI_Two_Level`）、F（版本
   差异风险低）。
2. ~~花一次会话专门核对 E（死锁风险）~~——**已核对，确认存在，
   `Throttle` 或等价机制是必需项**，见 2.4 节。
3. ~~再定 A、G、H（怎么测、拿什么当基线、用什么 workload）~~——
   **已全部确认**：A（基线配置跑通，`hostSeconds` 需多次取均值）、
   G（只测吞吐量相关指标，不采集功耗/活动 MAPE）、H（给
   `threads.cpp` 加 `chunk_size` 共享强度旋钮，替代外部 benchmark）。
   ">3 倍加速比"目标现在有了一个可以被清晰验收的实验设计：固定基线
   跑法（A）、固定验收指标（G）、固定 workload 及其可调参数（H），
   下一步是选定 `chunk_size` 的具体扫描点（建议 1、4、8、50000 四档，
   覆盖两端和中段）。
4. ~~E 节要求的 `Throttle`（或等价机制）具体怎么设计~~——**已设计，
   见第 6 节**。无锁化仍然按第 3 节的建议，放在 v1 跑通、测出真实
   加速比之后再决定是否值得投入。

## 6. Throttle 死锁规避机制设计

针对 E 节确认的 L2↔L1 AB-BA 死锁（`M_L2` 与 `M_L1_i` 互相嵌套等待），
本节给出具体设计。**核心结论：只是"在 L2 和 L1 之间塞一个 Throttle
对象"这个动作本身不解决问题——必须配合一条放置规则，否则只是把一个
2 节点死锁环变成一个未必更安全的 4 节点死锁环。**

### 6.1 复用现有 `Throttle`，而不是新造一个类

`src/mem/ruby/network/simple/Throttle.hh` 里已经有一个现成的
`Throttle : public Consumer`，今天的用途是单线程环境下的链路带宽/
延迟建模：上游把消息放进 `m_in`，`Throttle::wakeup()`（由
`Consumer::scheduleEvent()` 按延迟调度，`Throttle.cc`）算完带宽占用后
把消息挪进 `m_out`。它已经具备我们需要的形状——**一个独立的 Consumer
身份、自己的调度节奏、天然地把"发送方决定要发"和"接收方真正收到"这两
件事用一个调度出来的事件分开**——不需要新造类，需要新增的是（a）给它
加锁，（b）改变它的"归属"方式（见 6.3）。

### 6.2 真正破环的机制：单线程任一时刻最多持有一把 wakeup mutex

先看错误的直觉方案：给 L2→L1_i 和 L1_i→L2 各插一个 Throttle，
`Throttle::wakeup()` 内部仍然是"从 `m_in` 取出消息、嵌套加锁写入
`m_out`"一次性完成，且每个 Throttle 各自占一个独立 OS 线程。这样
嵌套锁图变成：

```
M_L2 --nest--> M_T(L2→L1_i) --nest--> M_L1_i --nest--> M_T(L1_i→L2) --nest--> M_L2
```

这是一个 4 节点环，不是把 2 节点 AB-BA 环变没了，只是变大了——如果
四个线程恰好同时卡在环上四个不同的等待点，一样死锁，只是概率更低、
更难复现，是把 bug 变隐蔽而不是修掉。

**真正必要的设计规则**：每个方向的 Throttle **不作为独立 OS 线程/
独立 `EventQueue` 存在，而是"寄生"在发送方所在的 `EventQueue` 里**
（`Throttle`/`Consumer` 调度用的是构造时传入的 `ClockedObject *_em`
所属的 `EventQueue`，见 `Consumer::Consumer(ClockedObject *_em, ...)`，
`Consumer.cc:49`）——具体来说：

- `Throttle_L2→L1_i` 的 `_em` 指向 L2 所在 `EventQueue` 上的某个对象
  （`eventq_index` 与 L2 controller 一致）。
- `Throttle_L1_i→L2` 的 `_em` 指向 L1_i 所在 `EventQueue` 上的某个对象
  （`eventq_index` 与 L1_i controller 一致）。

这样一来：

1. L2 的线程执行 `L2.wakeup()`（持有 `M_L2`）要发 INV 时，只需要
   `lock(M_T_L2→L1_i)`、把消息塞进这个 throttle 自己的 `m_in`、
   `unlock(M_T_L2→L1_i)`——**全程不碰 `M_L1_i`**。`L2.wakeup()` 返回、
   `M_L2` 自然释放。
2. `Throttle_L2→L1_i.wakeup()` 是**同一个 OS 线程（L2 的线程）在稍后
   的一个独立事件**，不是嵌套调用——执行到这里时 `M_L2` 早已释放
   （因为同一线程一次只能跑一个 `wakeup()`）。它这时候才去
   `lock(M_L1_i)`，此时它手上没有同时握着 `M_L2` 或 `M_T_L2→L1_i`。
3. 对称地，`Throttle_L1_i→L2` 寄生在 L1_i 的线程里，`L1_i.wakeup()`
   持有 `M_L1_i` 时只碰它自己的 throttle 锁，真正去碰 `M_L2` 的动作
   发生在 L1_i 线程稍后的独立事件里，那时 `M_L1_i` 也已经释放。

**不变量（这是要 enforce 的设计规则，不只是观察）**：任意 OS 线程在
任意时刻，最多同时持有 `{M_L2, M_L1_0..3, M_T_L2→L1_0..3,
M_T_L1_0..3→L2}` 里的一把锁。只要这条规则成立，"线程 A 拿着锁 1 等
锁 2，线程 B 拿着锁 2 等锁 1"这个死锁的必要条件就不可能出现——因为
根本不存在"同时拿着两把锁"的线程。这比"分析嵌套图里有没有环"更强、
更容易验证，也更抗未来协议改动（哪怕以后 SLICC 加新 action，只要
新代码依然遵守"发送方只碰自己/自己的 throttle 的锁，绝不直接碰对端
consumer 的锁"，这条不变量自动继续成立，不需要每加一个 action 就重新
画一遍嵌套图）。

**建议用 debug-only 断言 enforce 这条不变量**：给 `UncontendedMutex`
的 `lock()`/`unlock()`（或者在 `Consumer`/`Throttle` 这一层包一层）
加一个 `thread_local` 的"当前线程是否已持有某把这一类锁"标记，`lock()`
时 `assert` 该标记为空，`unlock()` 时清空。这样一旦未来有代码不小心
违反"最多一把锁"规则，debug build 立刻能抓到，而不是依赖代码审查
永远不出错。

### 6.3 拓扑落地：需要 8 个 Throttle 实例，且不能挂在共享 Switch 下

**先确认过 L1↔L1 之间没有直连消息**：查了
`MESI_Two_Level-L1cache.sm`/`MESI_Two_Level-L2cache.sm` 的
`MessageBuffer` 声明——L1 的出口只有 `requestFromL1Cache`/
`responseFromL1Cache`/`unblockFromL1Cache`，入口只有
`requestToL1Cache`/`responseToL1Cache`；L2 的出口
`L1RequestFromL2Cache`/`responseFromL2Cache`，入口
`L1RequestToL2Cache`/`responseToL2Cache`/`unblockToL2Cache`——vnet
名字两两对应，都是"L1↔L2"，**没有 L1 直接发给另一个 L1 的
`MessageBuffer`**。也就是说本协议里跨核数据转发都经 L2 中转（数据
owner L1 把 response 发回 L2，L2 再转发给 requester L1），不是
cache-to-cache 直连。这把 6.4 节需要覆盖的链路限定在 L2↔L1 这 4 对，
不需要额外处理 L1×L1（本来 4 核会有 12 条有向边）。

拓扑需要的 Throttle 实例：

- `Throttle_L2→L1_i`（i=0..3）：寄生在 L2 的 `EventQueue`，覆盖
  `L1RequestFromL2Cache`/`responseFromL2Cache` 流向 L1_i 的部分
  （一个 Throttle 内部本来就能同时处理多个 vnet，见
  `Throttle::m_vnets`/`operateVnet`，不需要按 vnet 拆成多个对象）。
- `Throttle_L1_i→L2`（i=0..3）：寄生在 L1_i 的 `EventQueue`，覆盖
  `requestFromL1Cache`/`responseFromL1Cache`/`unblockFromL1Cache`
  流向 L2 的部分。

共 8 个实例，4 个挂在 L2 侧、4 个分别挂在各自 L1_i 侧。

**修正（原先这里判断错了，记录下来避免下次重新踩一遍）**：最初以为
现有 `Throttle`/`Switch` 机制需要新增 C++ 类才能做到"按方向绑定
`_em`"，实测查证后发现**不需要新 C++ 代码，现有机制已经支持**：

- `eventq_index` 是 `SimObject` 基类自带的通用 Param
  （`src/python/m5/SimObject.py:637`：
  `eventq_index = Param.UInt32(Parent.eventq_index, ...)`）——每个
  `Switch`/`Router` SimObject 实例本来就能在 Python 配置里独立指定
  自己的 `eventq_index`，`Throttle` 的 `_em` 就是创建它的那个
  `Switch`（`Switch.cc` 里 `throttles.emplace_back(...", this, ...)`），
  `Consumer::scheduleEvent()` 用 `em->schedule()`
  （`Consumer.cc:56-60`）天然就落在 `em` 自己的 `eventq_index` 对应
  的 `EventQueue` 上——**不需要新写一个"轻量 holder 对象"，只要在
  拓扑创建时把正确的 `eventq_index` 赋给正确的 `Switch` 实例即可**。
- 真正的障碍不是"Throttle 绑定不了单一 `_em`"，而是**拓扑本身的
  结构**：查了 `configs/topologies/Crossbar.py`（也就是 A/H 节基线
  实测用的 `--topology=Crossbar`）——它不是"一个共享 Switch 路由
  全部 5 个节点"，而是**给每个 controller 建一个专属的 per-node
  `Switch`，另外再加一个共享的中心 `xbar` `Switch`**
  （`len(self.nodes) + 1` 个 router）。一条 L2→L1_i 的消息实际上过
  `L2 endpoint → L2 自己的 Switch →`（`IntLink`，经
  `SimpleNetwork::makeInternalLink`/`Switch::addOutPort` 加了
  throttle）`→ 共享的 xbar Switch →`（`IntLink`，同样加了
  throttle）`→ L1_i 自己的 Switch →`（`ExtLink`，经
  `SimpleNetwork::makeExtOutLink`/`addOutPort` 加了 throttle）
  `→ L1_i endpoint`。L2 自己的 Switch、L1_i 自己的 Switch 都是单一
  归属，`eventq_index` 分别设成对应 domain 即可；**唯一有问题的是
  中间那个所有节点共享的 `xbar` Switch**——它的 throttle 无法只
  归属于某一侧。
- 结论：**不用改 `Switch`/`Throttle` 的 C++ 代码**，需要做的是
  拓扑层面的选择，见下面两个方案（用户要求两个都做成可配置的，
  便于对比）：
  1. **方案一（新拓扑，去掉共享 xbar 节点）**：写一个精简的
     "L1↔L2 星形"拓扑，每个 L1_i 的 `Switch` 直接通过一条 `IntLink`
     连到 L2 自己的 `Switch`，不再经过额外的共享中心节点——这样
     每条 L2↔L1_i 路径只有两跳（各自的 Switch），两跳分别单一
     归属，`eventq_index` 各自设置即可，完全对应 6.2 节"寄生在
     发送方线程"的设计。
  2. **方案二（复用现有 Crossbar 拓扑，把共享 xbar 当成独立的第 6
     个 domain）**：不改拓扑代码，直接给 Crossbar.py 生成的那个
     共享 `xbar` router 也单独分配一个 `eventq_index`（比如 5）。
     这样链路变成 3 跳（L2 域 → xbar 域 → L1_i 域），每一跳仍然
     只涉及一把锁（6.2 节的不变量对链长不敏感，多一跳只是多一次
     "先释放、再交给下一跳"的顺序移交，不引入新的嵌套持锁），
     但多了一次真正的跨线程移交开销，预期比方案一慢、但改动量
     几乎为零，适合先用来验证机制本身是否work，再决定要不要换
     方案一。

### 6.4 控制流（伪代码）

发送方（以 L2 发 INV 给 L1_i 为例，对应 `MESI_Two_Level-L2cache.sm:609`
的 `f_sendInvToSharers`）：

```
// 运行在 L2.wakeup() 内部，此时持有 M_L2
lock(M_T_L2_to_L1[i]);
throttle_L2_to_L1[i].m_in.enqueue(inv_msg, curTick(), link_latency);
if (!throttle_L2_to_L1[i].hasPendingWakeup())
    throttle_L2_to_L1[i].scheduleEvent(link_latency);   // 挂在 L2 自己的 EventQueue
unlock(M_T_L2_to_L1[i]);
// L2.wakeup() 继续处理本地状态机剩余逻辑，最终返回、释放 M_L2
```

Throttle 侧（L2 线程稍后的一个独立事件，与上面不是同一次调用栈）：

```
void Throttle_L2_to_L1_i::wakeup() {
    lock(M_T_L2_to_L1[i]);
    msg = m_in.dequeue(curTick());   // 现有的带宽/延迟核算逻辑不变
    unlock(M_T_L2_to_L1[i]);         // <<< 关键：先释放，再去碰对端锁

    lock(M_L1_i);                    // 这个调用栈里唯一持有的锁
    L1_i.inboundBuffer.enqueue(msg, deliver_tick);
    scheduleCrossEventQWakeup(L1_i, deliver_tick);  // 复用现成的跨
                                                      // EventQueue 调度
                                                      // 原语（第 0 节
                                                      // 提到的
                                                      // async_queue 机制）
    unlock(M_L1_i);
}
```

`Throttle_L1_i→L2`（对应 `fi_sendInvAck`，
`MESI_Two_Level-L1cache.sm:810`）结构完全对称，寄生在 L1_i 的
`EventQueue`，最终去碰的对端锁是 `M_L2`。

### 6.5 还没解决、需要显式标记的残留风险

- **背压/流控**：如果 `Throttle_L2_to_L1_i.wakeup()` 尝试
  `enqueue` 时 L1_i 的真实入口 buffer 已满
  （`MessageBuffer::areNSlotsAvailable` 返回 false)，现有 Ruby 的
  stall/重试机制要不要改、`unlock(M_T)` 之后到 `lock(M_L1_i)` 之间
  这段"消息已经从 throttle 出队但还没真正交给 L1_i"的状态怎么表示，
  这里没有细化，需要在实现阶段单独设计（大概率是"重试整个
  `Throttle::wakeup()`"，但要确认不会把已经 dequeue 的消息弄丢）。
- **顺序保证**：引入 store-and-forward 这一跳，要确认没有破坏现有
  `m_strict_fifo`/vnet 顺序语义——理论上每个 throttle 是单一 FIFO，
  顺序应该保得住，但需要在实现后专门写一个乱序检测的回归测试确认，
  不能只凭推理。
- **`in_port` rank 交互**：SLICC 里 `in_port` 的 `rank=` 决定
  一次 `wakeup()` 内多个入口 buffer 的服务顺序（如
  `MESI_Two_Level-L1cache.sm:334,347,422,470,496`），这个设计不改变
  每个 vnet 各自的 buffer，理论上不影响 rank 语义，但同样需要实现后
  验证。
- **这是一条通用规则，不是一次性补丁**：这里只覆盖了 2.4 节确认的
  L2↔L1 这一个死锁环。6.2 节的不变量（"发送方绝不直接碰对端
  consumer 的锁，只碰自己/自己的 throttle 的锁"）是通用设计规则——
  以后如果换协议、或者协议本身改出新的双向消息模式，都要用同一条
  规则去检查是否需要新增 throttle，而不是假设"L2↔L1 修好了，别的
  地方就没事"。

## 7. v1 实现进度：per-consumer 锁

第一步（6.2 节的核心机制，独立于 6.3/6.4 的 Throttle/拓扑改动）已经
实现并验证，记录如下。

### 7.1 改动内容

- `src/mem/ruby/common/Consumer.hh`/`.cc`：给 `Consumer` 加了
  `lock()`/`unlock()`，基于 `UncontendedMutex`（`src/base/
  uncontended_mutex.hh`）、**同线程可重入、跨线程互斥**——用
  `std::thread::id` 记录持有者 + 一个深度计数器，同一线程重复
  `lock()` 只是深度+1，不重新走一遍 `UncontendedMutex`；不同线程
  必须真正互斥等待。做成可重入是必需的，不是可选的健壮性加固：
  SLICC 生成的 `wakeup()` 里，`MessageBuffer::recycle()`/
  `reanalyzeMessages()`/`enqueueDeferredMessages()` 这几个自己给
  自己入队的路径，会在已经持有本 consumer 锁的情况下再次触发
  enqueue，如果锁不可重入会 100% 自死锁（不是概率性 bug，每次触发
  都会卡死）。
- `Consumer::processCurrentEvent()`（`Consumer.cc`，所有
  Consumer 子类——各协议 controller、`Sequencer`、`Throttle`——
  唯一的 `wakeup()` 派发点）现在把 `wakeup()` 调用整段包在
  `lock()`/`unlock()` 之间，对应 2.3/6.2 节"consumer 处理 wakeup()
  期间锁住自己"的要求。
- `MessageBuffer::enqueue()`（`mem/ruby/network/MessageBuffer.cc`）
  现在在修改 `m_prio_heap`/调度 consumer wakeup 之前，先拿
  `m_consumer` 的锁（`std::unique_lock<Consumer>`，因为下面这条
  快速路径需要能条件性跳过加锁）。
- **同 EventQueue 快速路径**：`enqueue()` 里先比较
  `curEventQueue()`（`sim/eventq.hh` 里 `__thread`-local，"当前线程
  正在服务哪个 EventQueue"）和 `m_consumer->getObject()->
  eventQueue()`——两者相等就直接跳过加锁。依据：gem5 的多
  EventQueue 模型里一个 EventQueue 全程只由一个固定 OS 线程服务，
  所以"发送方当前正在服务的 EventQueue == consumer 所属的
  EventQueue"能推出"发送方就是 consumer 自己所在的那个线程"——
  跟它并发的可能性为零，加锁是可以证明的多余开销。这一优化直接
  对应真实拓扑里的大多数消息（同一个 domain 内部的本地消息，比如
  controller 跟自己的 `Sequencer`/directory 交互），只有真正跨
  domain 的发送才会付加锁的成本。

### 7.2 正确性验证

用 A 节的基线配置（`configs/deprecated/example/se.py --ruby
--l2cache --topology=Crossbar --num-cpus=4
--cpu-type=X86TimingSimpleCPU`，`threads` workload，
`--options="200000 1"`，单 `EventQueue`）做了改动前/改动后对照——
**同一天、同一套刚装好的工具链（这次环境里 scons/python3-dev/m4
都需要重新装，见 7.3）分别编译两版二进制**，排除了工具链版本差异
造成的干扰后：

| 版本 | simTicks | 是否一致 |
|---|---|---|
| 改动前（未打补丁，当天重新编译） | 31555788000 | 基准 |
| 改动后（打了 7.1 的补丁） | 31555788000 | **完全一致** |

`simTicks`/`simSeconds` 逐 tick 相同，`Validating...Success!`
两边都通过——单 `EventQueue` 场景下这次改动没有引入任何行为差异，
符合预期（改动本身不该在只有一个线程时改变任何调度结果）。

### 7.3 意外发现：单线程场景下仍有约 15% 的可测量开销

**没有隐藏这个数字**：即使是"理论上几乎免费"的 `UncontendedMutex`
快速路径（无竞争时只是一次 CAS），在单 `EventQueue`（不存在真实
竞争）场景下，`hostSeconds` 仍然从改动前的 107.03s 涨到改动后
（含 7.1 的同队列快速路径优化）的 122.60s，**约 15% 的开销**，
且加了"同 EventQueue 跳过加锁"这个优化后几乎没有改善
（123.90s → 122.60s）。

这说明开销大头**不在 `enqueue()` 侧**（那里的锁在单队列场景下
本来就该被快速路径完全跳过），**而在 `processCurrentEvent()` 侧**
——那里为了保证 2.3 节的正确性要求，无条件对每次 `wakeup()`
调用加锁/解锁，而 `wakeup()` 的调用频率在一个真实的 MESI_Two_Level
协议里相当高（远高于"整个仿真周期"的粒度）。CAS + 一次
`std::this_thread::get_id()` + 比较，单次看起来"几乎免费"，
乘以千万级调用次数后就不是免费的了。

**这是设计上需要正视、而不是掩盖的权衡**：v1 的目标是"先跑通、测出
真实加速比"（第 3/5 节），15% 的单线程开销会直接侵蚀最终加速比的
空间——如果 5 线程并行只测出比如 3.2 倍，其中可能有相当一部分是被
这 15% 的锁开销吃掉的，需要在解读加速比结果时把这一项算进去，不能
假设"锁的快速路径没有成本"。

**下一步可选的优化方向（未实现，留待需要时再做）**：给
`processCurrentEvent()` 加一个类似的"是否值得加锁"快速判断——
比如一个全局的"这次仿真是不是单 `EventQueue`/没有其它线程可能碰
这个 consumer"标记，如果确定不需要，跳过锁而不是依赖
`UncontendedMutex` 内部的 CAS 快速路径。这个优化本节先不做，等
5-`EventQueue` 并行版本真正跑起来、能测出"锁开销在真实加速比里占
多大比例"之后再决定值不值得投入——跟第 3 节"无锁化先不做,
等测出真实瓶颈再说"是同一个原则。

### 7.4 尚未完成的部分

6.3/6.4 节的 Throttle 拓扑改动（拆分 `Switch` 共享的 throttle
归属、给 8 个方向性 Throttle 分别绑定正确的 `_em`）还没有实现——
这是 v1 剩下的、范围更大也更有风险的一块（要动
`Switch.hh`/`.cc`、`configs/ruby/Network.py`/`SimpleNetwork.py`
这一层的拓扑创建代码），本节先记录 per-consumer 锁这一半的完成
状态，Throttle 改动留到下一次会话专门做。

## 8. gem5 史上第一次多线程 Ruby 实测（方案二）：两次真实崩溃

按 6.3 节"方案二"（复用 `Crossbar.py` 不改代码，只给共享 xbar
router 单独分配一个 `eventq_index`）在
`configs/deprecated/example/se.py` 里加了一段实验性代码（新增
`--parallel-l2-eventq`/`--sim-quantum` 两个选项，gated，默认关闭，
不影响任何现有行为——关闭状态下跑 A 节基线命令，`simTicks` 仍然是
`31555788000`，和 7.2 节的验证结果一致）。域划分用的是 6.3 节的方案：
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

**根因，回头看其实 2.5 节已经写明白了，只是没有对上号**：gem5 的
`GlobalSyncEvent`/`sim_quantum` 屏障机制只保证"任意两个域之间的时钟
漂移不超过一个 quantum"，不保证"漂移为零"——两次全局屏障之间，各个
域按各自的 wall-clock 速度独立跑，跑得快的域可能几乎跑满整个
quantum 窗口后才等到跑得慢的域追上来。要让跨域调度的 `when`
（通常是"本地当前 tick + link_latency"这个量级）不落在目标域已经
跑过去的时间点之前，**唯一的保证方式是 `sim_quantum <= 最小跨域
消息延迟（这里是 Throttle 的 `link_latency`，默认 1 个 ruby 时钟
周期 = 2GHz 下 500 ticks）`**——这正是 2.5 节"quantum 取得比共享
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
- 而 `MessageBuffer::enqueue()`（7.1 节新增的逻辑）里
  `m_consumer->scheduleEventAbsolute(arrival_time)`（往
  `m_wakeup_ticks` 里插入新的到达时间）**是在 `consumer_lock` 的
  保护范围内**调用的（`MessageBuffer.cc:239-241,320`，跨域时才真正
  加锁）。
- 问题：`processCurrentEvent()` 读/删 `m_wakeup_ticks` 这一步*没有*
  拿同一把锁。当它在本地域线程上因为一个更早调度好的 wakeup 事件被
  触发而执行到这里时，另一个域的线程完全可能*在同一时刻*正通过
  `enqueue()`（持锁）往这同一个 `m_wakeup_ticks` 里插入一个新的、
  时间上更早的到达时间——因为跨域消息的时序本来就是放宽过的
  （2.5 节），"稍后插入的条目时间上反而更早"这件事在这个设计里是
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
这个时序倒挂本身。真正的修复大概率需要 2.5 节已经点出但我们还没
实现的机制：**跨域调度的到达时间要被动地对齐（不是自然计算出来的
`current_time + delta`，而是主动向上取整）到"下一个 quantum 边界"**
——如果所有跨域消息的目标 tick 都精确落在 quantum 边界上，而
`GlobalSyncEvent` 的屏障机制保证边界时刻两侧的域都已经真正同步
（`handleAsyncInsertions()` 就是在越过屏障那一刻做的，
`global_event.cc:154`），"插入一个比当前已处理进度更早的 tick"这个
情况就不会再发生——但这个"跨域到达时间取整到 quantum 边界"的逻辑
目前完全没有写，只存在于论文描述和 2.5 节的转述里。

### 8.3 当前状态和下一步

- 崩溃一已解决（配置层面：`sim_quantum` 要设得比 `link_latency`
  小，不是大）。
- 崩溃二**未解决**，且不是配置问题，是 7.1 节的 per-consumer 锁
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
可重入的（6.2 节的死锁规避机制要求），这意味着 `wakeup()` 内部理论
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
`enqueue()` 函数体本身，7.1 节引入）：这段注释论证"如果调用线程已经
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

**这意味着 7.1 节引入、7.3 节为了避免约 15% 单线程开销而保留的"同域
快速路径"优化是不安全的，需要修**。候选方向：

(a) **直接去掉快速路径**，`enqueue()` 无论同域跨域一律加锁——最简单
    、明确正确，但会让 7.3 节测过的约 15% 开销失去唯一的缓解手段，
    需要重新测量真实代价（当时测的是"锁的存在"本身的开销，不是"是否
    跳过锁"这个优化的收益幅度，两者不是一回事，需要单独测）。
(b) **把"是否需要加锁"从按消息判断改成按 `Consumer` 判断**：一个
    `Consumer` 只要它挂在拓扑里的任何一条入边跨过了域边界（换句话说
    ，只要它可能收到*任何*跨域消息），就必须对它的*所有*入队——包括
    同域的——都走锁定路径；只有真正"只收同域消息、永远不会被跨域线程
    碰到"的 `Consumer`（比如纯粹寄生在发送方 `EventQueue` 上、输出
    端才跨域的 `Throttle`，见 6.2 节）才能真正安全地跳过锁。这个属性
    在配置阶段（拓扑/`eventq_index` 分配完成后）就能静态算出来，可以
    做成 `Consumer` 上的一个一次性计算的布尔标志，而不是 7.1 节现在
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
`MessageBuffer::enqueue()` 早在 7.1 节就把整个 enqueue 包在锁里；后者
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
7.3 节的单线程开销数字因此明显变差，可以后续对 size==1 的常见情形
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
**Q ≥ ~9.2×10⁴ tick ≈ 184 个 ruby 周期**。而 8.1 节确立的正确性
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
优化锁的细节（7.3 节那 ~15% 的单线程锁开销在 130 倍的减速面前
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
8.8 节已证明"未覆盖的最早 tick 只可能是刚插入的那个"）；完整的
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
的未来），"要不要提交一次触发"只由 inflight 集合决定（8.8 节已
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
quantum snap（原始到达 tick 在 Q ≤ 最小跨域延迟时本来就安全，2.5 节
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
