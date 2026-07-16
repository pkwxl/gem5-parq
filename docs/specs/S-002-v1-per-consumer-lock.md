# S-002: v1 实现进度：per-consumer 锁

> **状态**：已实现并在单 `EventQueue` 下验证正确（`simTicks` 逐 tick 不变）。
> 发现的 ~15% 单线程锁开销（7.3）和"同域快速路径绕过锁"的不安全性，后续在
> [S-003](./S-003-first-multithreaded-ruby-crashes.md) 的多线程实测中被证实
> 是真实 bug 并修正。承接 [S-001](./S-001-design-background-and-proposal.md)
> 的 §6.2 设计。对应原单体设计文档第 7 节，内容原样保留。

---

## 7. v1 实现进度：per-consumer 锁

第一步（S-001 §6.2 的核心机制，独立于 S-001 §6.3/§6.4 的 Throttle/拓扑改动）已经
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
  `lock()`/`unlock()` 之间，对应 S-001 §2.3/§6.2"consumer 处理 wakeup()
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
都需要重新装）分别编译两版二进制**，排除了工具链版本差异造成的
干扰后：

（沙箱环境笔记，原来只存在于 memory `gem5-sandbox-missing-build-tools`：
即使 `build/X86/gem5.opt` 已经在磁盘上存在（大概率是别的环境实例编译的），
第一次在这个沙箱里重新编译仍然会依次报 `scons`、`python3-config`
（缺 `python3-dev`）、`m4` 三个 "Can't find ..." 错误。沙箱自带免密码
`sudo`，直接 `sudo apt-get update && sudo apt-get install -y scons
python3-dev m4` 三个一起装上即可，装完 `scons build/X86/gem5.opt
-j$(nproc)` 从头到尾编译顺利——这是环境缺口，不是仓库配置问题，下次遇到
同样报错不用重新排查，直接装。）

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
——那里为了保证 S-001 §2.3 的正确性要求，无条件对每次 `wakeup()`
调用加锁/解锁，而 `wakeup()` 的调用频率在一个真实的 MESI_Two_Level
协议里相当高（远高于"整个仿真周期"的粒度）。CAS + 一次
`std::this_thread::get_id()` + 比较，单次看起来"几乎免费"，
乘以千万级调用次数后就不是免费的了。

**这是设计上需要正视、而不是掩盖的权衡**：v1 的目标是"先跑通、测出
真实加速比"（S-001 §3/§5），15% 的单线程开销会直接侵蚀最终加速比的
空间——如果 5 线程并行只测出比如 3.2 倍，其中可能有相当一部分是被
这 15% 的锁开销吃掉的，需要在解读加速比结果时把这一项算进去，不能
假设"锁的快速路径没有成本"。

**下一步可选的优化方向（未实现，留待需要时再做）**：给
`processCurrentEvent()` 加一个类似的"是否值得加锁"快速判断——
比如一个全局的"这次仿真是不是单 `EventQueue`/没有其它线程可能碰
这个 consumer"标记，如果确定不需要，跳过锁而不是依赖
`UncontendedMutex` 内部的 CAS 快速路径。这个优化本节先不做，等
5-`EventQueue` 并行版本真正跑起来、能测出"锁开销在真实加速比里占
多大比例"之后再决定值不值得投入——跟 S-001 §3"无锁化先不做,
等测出真实瓶颈再说"是同一个原则。

### 7.4 尚未完成的部分

S-001 §6.3/§6.4 的 Throttle 拓扑改动（拆分 `Switch` 共享的 throttle
归属、给 8 个方向性 Throttle 分别绑定正确的 `_em`）还没有实现——
这是 v1 剩下的、范围更大也更有风险的一块（要动
`Switch.hh`/`.cc`、`configs/ruby/Network.py`/`SimpleNetwork.py`
这一层的拓扑创建代码），本节先记录 per-consumer 锁这一半的完成
状态，Throttle 改动留到下一次会话专门做。


---

**上一篇**：[S-001：设计背景与提案](./S-001-design-background-and-proposal.md)
**下一篇**：[S-003：gem5 首次多线程 Ruby 实测——两次真实崩溃与修复](./S-003-first-multithreaded-ruby-crashes.md)
**返回**：[INDEX.md](./INDEX.md)
