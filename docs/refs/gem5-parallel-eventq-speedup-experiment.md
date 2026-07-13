# gem5 多核并行仿真加速实验：从"设计分析"到实测数据

> 承接 [`gem5-parallel-event-dispatch-analysis.md`](./gem5-parallel-event-dispatch-analysis.md)
> 的源码分析，本报告用两个真实跑通的 gem5 SE 模式实验，检验那份报告里的判断：
> quantum 同步的 lookahead 约束是不是真的会咬人？"局部事件留在本地"这条原则
> 落实到位之后，实测加速比能到多少？复现脚本见
> [`scripts/`](./scripts)。

## 0. 实验环境

- gem5 25.1.0.1，`build/X86/gem5.opt`（已有构建，未改动仿真核心代码）
- 宿主机 112 逻辑核，SE（用户态系统调用仿真）模式，classic（非 Ruby）
  内存系统，`X86TimingSimpleCPU`
- 两个工作负载：
  1. **`tests/test-progs/threads`**（仓库自带）——`std::thread` 实现的并行
     数组加法，多个线程共享同一块内存，用返回值自检正确性；
  2. **`work.c`**（自己写的，见 `scripts/work.c`）——单线程、CPU/内存双重
     压力的确定性内核（重复对一个 `int` 数组做读-改-写），用于独立多进程
     实验里给每个"核"提供可控的工作量

## 1. 实验一：把一个真·共享内存多核应用硬拆到多个 EventQueue 上

配置见 `scripts/parallel_bench_coherent.py`：`N` 个
`X86TimingSimpleCPU`，各自私有 L1I/L1D，但共享一个 `L2Cache` + `SystemXBar`
+ `MemCtrl`（也就是真实的、有 cache 一致性流量的多核系统）。`--parallel`
模式下每个 CPU（连同它私有的 L1）各自拿到一个 `eventq_index`，共享的
L2/总线/内存控制器留在 `eventq_index=0`——这正是最初设想里"跨队列事件由
共享部件兜底"的直接实现。

```bash
gem5.opt parallel_bench_coherent.py --num-cpus 2 --num-values 1000 \
    --parallel --sim-quantum 1us
```

**结果：稳定崩溃**，不管 `--sim-quantum` 取 `100ns`、`1us`、`10us` 还是
`100us`，都在**同一个 tick（1000）**报同一个断言：

```
gem5.opt: src/sim/eventq.hh:759: void gem5::EventQueue::schedule(gem5::Event*, gem5::Tick, bool):
Assertion `when >= getCurTick()' failed.
Program aborted at tick 1000
  ... BaseCache::allocateMissBuffer
  ... BaseCache::recvTimingReq
  ... CoherentXBar::recvTimingReq
  ... EventQueue::serviceOne
  ... doSimLoop
```

### 为什么调大 quantum 也救不了

上一份报告从代码注释里读出的推论——"跨队列事件必须领先目标队列
≥ `simQuantum` 个 tick"——在这里成立，但**真正的问题出在更前面一步**：
`BaseCache`/`CoherentXBar` 这些经典（非 Ruby、非 KVM）内存系统对象，在计算
一次 cache miss 的重试/响应延迟时，用的是真实的、纳秒级的时序参数
（`tag_latency`、`data_latency` 等），**完全不知道自己的对端端口是不是在
另一个 EventQueue 上**。哪怕把 `simQuantum` 调到 100 微秒这种远大于任何
真实内存延迟的值，L1 miss 产生的重试事件依旧是按纳秒级延迟去调度的——
它从来没有被"拉长"到满足 lookahead 约束过。于是问题不是"quantum 选小了"，
而是**经典内存系统的端口时序模型里根本没有"我在跨 EventQueue 通信"这个
概念**，四组不同的 quantum 取值给出一模一样的崩溃位置，直接证实了这一点。

这把上一份报告的 4.5 条建议（"按拓扑关系设定 lookahead，而不是全局一刀切"）
的必要性从"理论推测"变成了"不这么做，压根跑不起来"。也说明为什么现有
gem5 代码库里 `eventq_index` 的实际用例（`fs_bigLITTLE.py`、
`realview64-kvm-dual.py`）从不在**同一个 cache 一致性域内部**跨队列
分——它们要么用 KVM（CPU 大部分时间直接在宿主机上跑，不产生细粒度内存
时序事件），要么把 queue 边界画在"大核集群 vs 小核集群"这种本来就很少
互相访问对方缓存的粒度上。

## 2. 实验二：把"局部事件留在本地"贯彻到底

配置见 `scripts/parallel_bench_independent.py`：不再共享任何东西。`N` 个
完全独立的 `System`（各自的 CPU、私有 L1、私有 `SystemXBar`、私有
`MemCtrl`、私有地址空间、独立的 SE 进程），只是恰好挂在同一个 `Root` 下面。
`--parallel` 模式下每个 `System` 拿一个独立的 `eventq_index`（子对象通过
`Param.UInt32(Parent.eventq_index, ...)` 自动继承，设一次即可）。这是
"局部事件限制在本地"这条原则的极限情况：**根本没有相互影响的事件需要
分发**。

```bash
gem5.opt parallel_bench_independent.py --num-sys 8 --n 20000 --reps 100 \
    --parallel --sim-quantum 1us
```

对每个 `num_sys ∈ {1,2,4,8}` 都跑了 serial（不设 `eventq_index`，退化成
gem5 平时的单队列模式）和 parallel 两个版本，工作量固定为
`n=20000, reps=100`（单系统约 2.3×10^5 条指令量级的 memory-bound 循环）。
全部 8×N 份运行的自检输出（`done: n=20000 reps=100 sum=-51629232462024`）
**完全一致**，说明并行拆分没有破坏任何一次运行的功能正确性。

### 实测数据

| N（独立系统数） | serial 总耗时 (s) | parallel 耗时 (s) | 加速比 | 理想加速比 | 并行效率 |
|---|---|---|---|---|---|
| 1 | 23.07 | — | — | — | — |
| 2 | 47.45 | 24.04 | **1.97×** | 2× | 98.7% |
| 4 | 97.86 | 25.67 | **3.81×** | 4× | 95.3% |
| 8 | 195.06 | 28.03 | **6.96×** | 8× | 87.0% |

（`wall_seconds`，即 shell `time` 测得的端到端时间，含 Python/SWIG 启动、
`m5.instantiate()` 构图等固定开销；原始数据见
[`scripts/results.csv`](./scripts/results.csv) 之前跑出的
`m5out_*/stats.txt`。）

gem5 自己在 `stats.txt` 里报告的 `hostSeconds`（只统计 `m5.simulate()`
内部、纯事件循环的耗时，不含 Python 侧的构图开销）给出的效率更高：

| N | serial hostSeconds | parallel hostSeconds | 加速比 | 并行效率 |
|---|---|---|---|---|
| 2 | 46.40 | 22.99 | 2.02× | ~100% |
| 4 | 96.21 | 24.03 | 4.00× | ~100% |
| 8 | 192.24 | 25.22 | 7.62× | 95.3% |

### 怎么解读这两组数字

**结论是正面的**：只要真的做到"局部事件不产生任何跨队列流量"，
gem5 现有的 quantum 并行机制在 8 路以内给出的是接近线性的加速
（`hostSeconds` 口径下 8 路效率 95%，`wall_seconds` 口径下 87%）。这也
反过来印证了 1.3 节里的判断——`UncontendedMutex` 保护的本地事件循环确实
是近零开销的路径，`async_queue`/quantum barrier 因为在这个实验里完全没有
真实跨队列流量要合并，只是"空转"，代价很小。

**两个口径的差距本身也是一个发现**：`wall_seconds` 效率（87%）明显低于
`hostSeconds` 效率（95%），且差距随 N 增大而增大。`hostSeconds` 只覆盖
`m5.simulate()` 内部的事件循环，而 `wall_seconds` 还包含**在
`SimulatorThreads` 启动之前、单个 Python 主线程按顺序把 N 个 `System`
的 SimObject 图构造出来、再交给 `m5.instantiate()` 转成 C++ 对象**的
这段准备时间——这段时间不受益于 EventQueue 层面的任何并行机制，且随
`N` 线性增长，是一段经典的"Amdahl 串行部分"，只是它不在
`sim/eventq.hh` 里，而在 Python 配置脚本 + SimObject 构造这一层。也就是
说，**如果目标是压榨端到端 wall-clock 加速比，配置/实例化阶段的开销
和事件循环本身的并行度同样值得关注**——这是纯粹读代码不容易看出来、
需要跑一次真实实验才会暴露的点。

## 3. 两个实验合在一起说明了什么

| | 实验一（共享一致性域跨队列） | 实验二（完全独立子系统跨队列） |
|---|---|---|
| 跨队列事件量 | 高（每次 L1 miss） | 零 |
| 正确性 | **崩溃**，与 quantum 大小无关 | 8/8 次运行结果一致 |
| 加速比 | 无法测量（跑不完） | 87%~99% 并行效率（8 路以内） |

这不是"gem5 的并行机制有 bug"，而是它对**分区方式**极度敏感：机制本身
（本地零开销 + 跨队列走 async_queue + quantum barrier 合并）在零流量场景
下工作得很好，但没有任何保护措施阻止你把队列边界画在一条高频、低延迟的
硬件通路中间——画错了直接断言失败，而不是性能下降。这比"渐进式变差"
更危险，也更值得在文档/工具层面提前拦住（对应上一份报告的建议 4.4）。

## 4. 对"如何优化并行度"的建议：结合实测数据重新排序

在上一份报告 6 条建议的基础上，这次的实测结果建议调整优先级：

1. **（新增，最高优先级）先把"分区安全性检查"做出来，而不是先追加速比**。
   实验一说明一个语法上完全合法、乍看合理的 `eventq_index` 划分可以直接
   让仿真崩掉，且没有任何运行前的警告。对应上一份报告的建议 4.4——基于
   `Port` 连接图做静态检查——现在有了具体的验收标准：**检测"是否有一条
   跨 `eventq_index` 的 `Port` 连接，其两端在经典内存系统里的往返延迟
   明显小于 `sim_quantum`"**，命中就该在 `m5.instantiate()` 之前直接报错
   /警告，而不是让用户在 `Program aborted` 里自己 addr2line。
2. **分区本身找对"天然低流量的缝"，比调 quantum、比优化 barrier 都重要**。
   实验二证明只要缝找对了（这里是"完全独立"这种极限情况），不需要任何
   额外的代码改动就能拿到 87%~99% 的并行效率。现实中更实际的目标是
   NUMA 节点边界、大小核集群边界这类**天然弱耦合**的切分点（对应上一份
   报告的建议 4.6），不必是完全独立——只要跨边界流量足够稀疏/延迟要求
   足够宽松，就有希望复现这次实验二的效果。
3. **quantum barrier 本身的开销，在这次实测里确实很小，不必急于优化**。
   8 路、`sim_quantum=1us`、约 3.1×10^4 次 barrier 同步的条件下，
   `hostSeconds` 口径效率仍有 95%，说明上一份报告 4.2 条（自旋 barrier）
   在这个规模下收益有限；只有当 quantum 被迫调得更小、或线程数远超 8
   （这次机器有 112 个核，还有很大空间没测）时才值得重新评估。
4. **构图/实例化阶段的串行开销值得单独关注**，这是这次实验才暴露出来的
   新发现，不在上一份报告的建议列表里。如果未来要跑更大规模（比如
   `N=32`、`N=64`）的多队列实验，`wall_seconds` 和 `hostSeconds` 之间的
   差距很可能进一步拉大，值得单独测一版"只测 Python 构图 + instantiate
   耗时 vs N"的关系，判断是否需要并行化 SimObject 构造本身。
5. **建议 4.1（quantum 合并时批量排序归并）和 4.3（无锁跨队列投递）
   仍然维持原判断——留到真的出现跨队列流量、且能验证收益之后再做**，
   这次两个实验分别是"零流量"和"流量大到直接跑不起来"两个极端，都还
   没有一个"流量适中、可以正常运行"的场景来验证 4.1/4.3 的收益，是一个
   明显的后续实验方向（见下一节）。

## 5. 下一步可以做的实验

- **在实验一崩溃的路径上，人为把跨队列端口的延迟拉到 ≥ `sim_quantum`**
  （例如把共享 L2 换成一个模拟"集群间总线"的、延迟被显式设成大于
  quantum 的连接），验证"只要满足 lookahead 约束，共享一致性域也能跨
  队列"这个假设，并测出这种配置下（为了正确性被迫引入的额外延迟）对
  仿真时序精度和并行加速比分别有多大代价——这是把 4.5 条建议从纸面
  设想变成可测量结论的关键实验。
- **把 `num_sys` 推到远超 8**（机器有 112 核），观察实验二的并行效率
  曲线什么时候开始明显下降，以及 `wall_seconds` 与 `hostSeconds` 的
  差距如何随 N 增长，验证第 4 节里"构图阶段是新瓶颈"的猜测。
- **在实验二的独立子系统之间人为引入少量、可控频率的跨队列消息**
  （比如每隔固定条指令发一次），做出一条"跨队列流量 vs 并行效率"的
  曲线，替代目前"零流量"和"崩溃"两个极端之间的空白。
