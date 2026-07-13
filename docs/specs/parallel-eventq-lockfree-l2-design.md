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

**需要改的地方，不只是"加 Throttle"**：现有 `Throttle` 是被
`Switch::addOutPort()` 创建、存进 `std::list<Throttle> throttles`
（`Switch.hh:121`），一个 `Switch` 对象名下所有 throttle 共用**同一个
`_em`**（即那个 `Switch` 自己，`Switch.cc:105-111` 传入
`this`）——而 `--topology=Crossbar`/`SimplePt2Pt` 这类拓扑，通常是
**一个共享的 crossbar `Switch` 对象**路由所有 5 个节点的流量。如果
不改，`Throttle_L2→L1_i` 和 `Throttle_L1_i→L2` 会因为"挂在同一个
`Switch`"而共用同一个 `_em`/`EventQueue`，6.2 节"寄生在发送方线程"
这个前提就不成立了，死锁风险原样保留。

**这是本设计里唯一必须动的架构点**：4 核 1 L2 这种规模，链路是纯
点对点（每个 L1 只跟 L2 一条双向链路），不需要 `Switch`/
`PerfectSwitch` 那一整套多端口交换机路由机制。建议不复用
`Switch`，而是新增一个更轻量的、专为点对点跨 domain 链路设计的
持有者对象（结构上是"`Throttle` + 一个只属于一侧 controller 的
`_em`"），由拓扑配置脚本（`configs/ruby/Network.py`/
`SimpleNetwork.py` 这一层）在创建 L1_i↔L2 链路时，分别把
`Throttle_L2→L1_i` 的 `_em` 设成 L2 controller 一侧的对象、
`Throttle_L1_i→L2` 的 `_em` 设成 L1_i controller 一侧的对象。

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
