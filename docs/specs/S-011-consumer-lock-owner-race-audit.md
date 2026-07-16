# S-011 — `Consumer` 自己的 per-consumer 锁 owner 字段无同步读写
（发现 + 精确复现窗口分析 + 可达性确认 + 修法设计 + 实现 + TSan/正确性验证）

**状态：已实现，TSan A/B 干净，行为保持不变（§10）**。这份 spec 依次做完了
"把问题精确定位到一个具体的、有窗口边界的时序竞争"（§3）、"确认这个窗口在
实际调用模式下可达，不是纯理论"（§5，回答 §4 开放问题 1）、"设计一份专门的
压力测试来直接观测这个窗口是否真的被撞上"（§6，回答 §4 开放问题 2 的设计
部分——**用户看过设计后决定不实现/不跑这份压力测试**，见 §7）、"设计一个
具体的修法方向"（§8，回答 §4 开放问题 3）、**"实现+编译+验证这份修法"**
（§10，用户明确要求"go ahead and implement §8"之后本会话做的）五步。**没有**
做 S-009 §23/S-010 §7 那种"全部调用点排查、确认修法范围"级别的完整审计（这
里只有三个调用点，§3.3 已列全，不需要那种规模的排查），没有实际跑过任何
测试拿到 §3.2 误判窗口的触发概率数字（§7 记录的用户决定）。按用户的要求
单独立文（不并进 S-009），因为这是完全不同的子系统——S-009 管的是经典
Port/南桥，这里是 Ruby 自己的 `Consumer`/`MessageBuffer` 同步机制（S-002
引入，S-003 修过一次）。§10 验证过程中意外发现一个**跟这次修法无关、本来
就存在**的 serial/spin 分歧（§10.4），已如实记录，未解决，是新的独立待办。

## 1. 来源

[S-010 §11.2](./S-010-addr-range-map-cache-race.md) 验证 `AddrRangeMap` 修法时
跑的 TSan 扩时长 A/B（`MAX_TICKS=1.3e9`，spin×2），两份日志里 `Consumer.cc:217/
222/231` 一共报了 66 次（round1 32 次 `:217`，round2 类似量级），是这次警告数量
第二多的类别，仅次于（已经修掉的）`AddrRangeMap` 那一类。跟同一轮报告里的
`EventQueue::_curTick`（`eventq.hh:862/875`）、`Event::Flags`（`flags.hh:83/116`）
放在一起被 S-009 §24.4/24.5 归为"既有背景噪声"，但那个归类是在验证 S-009/S-010
自己的锁改动"没有引入新问题"这个前提下做的粗筛（"看调用栈里有没有出现这次改的
文件"），**不代表这三类彼此性质相同、都可以同等地当噪声忽略**——`_curTick`/
`Flags` 是 CLAUDE.md"Primary research goal"一节明确写的项目自己的设计取舍
（"relaxed cross-domain timing, not lock-free structures"，S-009 §24.4 已经论证
过这属于有意为之、被 quantum 上界容忍的陈旧读），但 `Consumer::lock()`/
`unlock()` 是这套设计取舍**用来实现自己的机制本身**——如果这把锁自己的记账
字段不可靠，S-002/S-003 建立的"per-consumer 锁保证 wakeup() 扫描所有 inbound
buffer 这件事对并发的跨线程 enqueue 是原子的"这条不变式就没有保证了。这两类
问题不能用同一句"这是背景噪声"打发，需要分开判断。

## 2. 机制精读

`Consumer::lock()`/`unlock()`（`src/mem/ruby/common/Consumer.cc:213-233`，声明
见 `Consumer.hh:92-109/143-145`）是给一个非递归的 `UncontendedMutex`
（`m_wakeup_mutex`）手工叠加"同线程可重入、跨线程互斥"语义的包装：

```cpp
// src/mem/ruby/common/Consumer.cc:213-224
void
Consumer::lock()
{
    std::thread::id self = std::this_thread::get_id();
    if (m_wakeup_mutex_owner == self) {      // :217 -- 无锁读
        ++m_wakeup_mutex_depth;
        return;
    }
    m_wakeup_mutex.lock();                   // 真正的互斥点
    m_wakeup_mutex_owner = self;             // :222 -- 拿到真锁之后才写
    m_wakeup_mutex_depth = 1;
}

// src/mem/ruby/common/Consumer.cc:226-233
void
Consumer::unlock()
{
    assert(m_wakeup_mutex_owner == std::this_thread::get_id());
    if (--m_wakeup_mutex_depth == 0) {
        m_wakeup_mutex_owner = std::thread::id();  // :231 -- 释放真锁之前写
        m_wakeup_mutex.unlock();
    }
}
```

`m_wakeup_mutex_owner`（`std::thread::id`）和 `m_wakeup_mutex_depth`
（`unsigned`）都是普通字段，**不是** `std::atomic`，`Consumer.hh:143-145` 声明
处没有任何注释说明这两个字段的并发语义（跟 `m_wakeup_mutex` 本身、以及
`m_wakeup_scheduled`/`m_wakeup_when` 等"设计上只在持锁时touch"的字段不同——
`m_wakeup_mutex_owner`/`m_wakeup_mutex_depth` 恰恰是**用来决定要不要去持锁**
的，天然不可能全程都在锁保护下访问，这是整个设计的症结所在，不是疏忽）。

设计意图（`Consumer.hh:92-106` 注释）：SLICC 生成的 `wakeup()` 代码可能在已经
持有这把锁的同一线程上递归调用 `MessageBuffer::enqueue()`（给自己的 buffer 塞
消息），如果每次都无条件走 `m_wakeup_mutex.lock()`，非递归锁会自锁死锁——
`:217` 这次无锁读就是用来"抢在真正加锁之前，先问一句我是不是已经拿着了"的
快速路径。这本身是合理的设计目标，问题在于**实现这个快速路径的检查用的是
一次完全不同步的读**，而这次读的目的恰恰是"回答一个需要精确、即时答案的
问题"（我现在持锁吗），跟 `_curTick` 那种"回答一个允许滞后、只要求有界误差的
问题"（另一个域大概什么时候）在需求性质上正好相反。

## 3. 实测证据 + 精确复现窗口分析

### 3.1 TSan 抓到的一次完整报告（S-010 §11.2 spin round1 日志摘录）

```
WARNING: ThreadSanitizer: data race (pid=105974)
  Read of size 8 at 0x7b6400057f68 by thread T5:
    #0 gem5::ruby::Consumer::lock() Consumer.cc:217
    #1 std::unique_lock<Consumer>::lock()
    #2 std::unique_lock<Consumer>::unique_lock(Consumer&)
    #3 MessageBuffer::enqueue(...) MessageBuffer.cc:246
    #4 L2Cache_Controller::a_issueFetchToMemory(...)   <- L2 域自己的 SLICC 动作
    #5 L2Cache_Controller::doTransitionWorker(...)
    ...
    #7 L2Cache_Controller::wakeup()
    #8 Consumer::consumeCurrentTick()
    #9 Consumer::processCurrentEvent()
    ...（L2 域自己的 EventQueue 线程 T5）

  Previous write of size 8 at 0x7b6400057f68 by thread T3:
    #0 gem5::ruby::Consumer::unlock() Consumer.cc:231
    #1 std::unique_lock<Consumer>::unlock()
    #2 std::unique_lock<Consumer>::~unique_lock()
    #3 MessageBuffer::enqueue(...) MessageBuffer.cc:327
    #4 Throttle::operateVnet(...)
    #5 Throttle::wakeup()
    #6 Consumer::consumeCurrentTick()
    #7 Consumer::processCurrentEvent()
    ...（这个 Switch 自己的域线程 T3）

  Location is heap block ... allocated by main thread:
    #1 create ... param_Switch.cc  <- 竞争双方共享的是一个 gem5::ruby::Switch
                                       对象自己的 Consumer 基类子对象
```

（完整调用栈见 `/tmp/s10-tsan-spin-1.3e9-r1/tsan.log`，这次验证运行留下的日志；
`/tmp` 是临时目录，机器重启会丢，如果要长期保留证据需要另存。）

也就是说：**竞争双方是两个不同域的线程**——T5 是 L2 域自己的线程，在
`a_issueFetchToMemory` 这个 SLICC 动作里往一个 `Switch`（网络交换机，属于另一个
域）的 inbound buffer 塞消息，这是一次真正的跨域 `MessageBuffer::enqueue()`
调用，会走到这个 `Switch` 对象自己的 `Consumer::lock()`；T3 是这个 `Switch`
自己域的线程，在处理它自己的 `wakeup()`（`Throttle::operateVnet` 往下游转发）
时，对同一个 `Consumer` 做 `unlock()`。**这正是 `Consumer.hh` 注释里"同线程
可重入、跨线程互斥"要处理的那个跨线程场景**，不是同线程递归那个安全分支。

### 3.2 为什么"跨线程互斥"这半句话目前没有被这次实现真正满足

`:217` 的读不受任何同步保护，其观测到的值完全取决于 T5 这次读指令执行的
物理时刻恰好落在 T3（或任何其他曾经持有过这把锁的线程）的写操作序列的哪个
间隙——在 x86-64、8 字节自然对齐访问、`std::thread::id` 在 libstdc++ 下是单个
`pthread_t`（`unsigned long`）的情况下，这次读**不会读到撕裂的垃圾值**（跟
S-009 §24.4 对 `_curTick` 的论证同一个硬件事实），但**会读到一个新旧不确定、
真实存在过的值**——问题不在"读到垃圾"，在"读到一个曾经合法、但现在已经不是
事实的值，而这个值恰好等于读线程自己的 id"这个特定情形：

1. 假设 T5 在更早的某个时间点，曾经合法持有过这同一个 `Consumer` 的锁
   （完全可能——T5 所在的 L2 域完全可以在模拟过程中反复往同一个 `Switch` 的
   buffer 里 enqueue，每次都是一次独立的 `lock()`/`unlock()` 循环），那次
   循环里 `:222` 把 `m_wakeup_mutex_owner` 写成了 T5，随后 T5 自己的
   `unlock()`（`:231`）把它写回默认值——这两次写都是 T5 自己线程上的操作，
   T5 自己后续的读**保证**能看到 T5 自己最近的写（同线程程序序，不需要
   同步），所以 T5 单独看不会出问题。
2. 但在 T5 那次 `unlock()` 之后、T5 这次新的 `lock()` 之前，**这把锁的真正
   互斥体 `m_wakeup_mutex` 是空闲的**，任何其他线程（比如 T3）都可以自由地
   拿到它——T3 拿到之后（`m_wakeup_mutex.lock()` 成功，但 `:222` 的写还没
   来得及执行/还没被 T5 的核心观测到）到 T3 真正把 `m_wakeup_mutex_owner`
   写成 T3 之间，存在一个**物理上确实存在、但极窄的时间窗口**：这段时间里
   共享内存位置上实际保存的值仍然是"上一个把它清空之前的值"或者尚未被
   T3 的写覆盖——如果 T5 这次的 `:217` 读恰好落在这个窗口内，且**巧的是
   T3 覆盖的正是 T5 自己历史上写过的那个位置**（这不需要巧合到"读到 T3 的
   id"，只需要"T5 自己核心上缓存的这个地址的旧值，恰好是 T5 自己上次写的
   T5 这个值，还没被 T3 新的写通过缓存一致性协议使其失效"），T5 就会在
   **T3 真实持有真锁期间**，把 `m_wakeup_mutex_owner == self` 判成真，走
   `:218` 的重入快速路径——**从未调用 `m_wakeup_mutex.lock()`**，直接以为
   自己已经持锁，开始并发执行本该互斥的 `wakeup()`/buffer 操作。

这不是"数据竞争在 C++ 标准意义上是 UB，所以理论上任何事都可能发生"这种
泛泛而谈——是这份实现具体的读写时序能够构造出的一个**有物理意义的、真实
存在窗口**的场景：**一个曾经合法持有过这个 `Consumer` 锁的线程，在窗口期内
误判自己"仍然/又一次"持锁，而实际的当前持有者是另一个线程**。跟 S-009 §24.4
`_curTick` 那种"读到的值无论新旧,只要在 quantum 误差范围内都算业务上正确"
完全不是一回事——这里读到"错误答案"（自己已持锁，但实际没有）会直接破坏
`Consumer.hh` 注释里承诺的互斥不变式，两个线程会同时执行本应互斥的
`wakeup()`/`MessageBuffer` 操作，跟 S-003 §8.x 当年修的"同域快速路径绕过锁"
是同一等级的问题（真实数据损坏风险,不是精度放宽）。

### 3.3 现在还不知道的事：这个窗口在这份代码库的实际调用模式下是否真的可达

§3.2 是"这段代码的读写时序允许这个场景发生"的静态论证，**不等于**已经证明
它在本项目实际的调用模式下会被触发,也不等于排除了某种未被注意到的额外约束
使其不可达。已知的 `lock()`/`unlock()` 调用点（`grep` 确认，只有三处）：

| 调用点 | 线程 | 场景 |
|---|---|---|
| `Consumer::processCurrentEvent()`（`Consumer.cc:187/200`） | 这个 Consumer 自己的域线程 | 处理自己的 wakeup 事件，`lock()`/`unlock()` 包住整个方法体 |
| `Consumer::processKick()`（`Consumer.cc:206/210`） | 同上 | 处理一次性 kick 事件 |
| `MessageBuffer::enqueue()`（`MessageBuffer.cc:246` 起，经 `std::unique_lock<Consumer>`） | **可能是任意域的线程**——只要该域的 controller 往这个 buffer 的目标 consumer 塞消息 | §3.1 抓到的场景 |

§3.2 描述的窗口需要"同一个线程 T 曾经合法持有过这个 consumer 的锁,之后
释放,现在又要再次获取"——这在 `MessageBuffer::enqueue()` 这条跨域路径上
是否真实可能反复发生（同一个 controller 反复往同一个下游 `Switch`/其他
controller 的同一个 buffer 塞消息,这是 Ruby 协议消息传递的日常模式,直觉上
大概率是),还是有某种拓扑/调用约束使得每个跨域线程对每个特定 consumer 只会
在极特殊条件下重复获取——需要读 SLICC 生成代码和网络拓扑配置进一步确认,
**这份 spec 没有做这一步**。

## 4. 待确认的开放问题（下一步审计需要做的事）

1. **可达性**：§3.3 表格里 `MessageBuffer::enqueue()` 这条跨域路径,在实际
   跑的协议（`MESI_Three_Level`）和拓扑下,是否存在"同一个跨域线程重复对
   同一个 consumer 加锁/解锁"的模式——如果存在,§3.2 的窗口就是可达的,不是
   纯理论;如果能证明每个跨域线程对每个 consumer 只会 lock 一次（比如某种
   一次性握手协议）,风险会小很多,但目前没有证据支持这个假设,不能预设。
2. **触发概率量级**：即使可达,窗口本身极窄（一次写操作的缓存一致性传播
   延迟,纳秒级),多久能实际观测到一次错误后果（不是"TSan 报告一次数据竞争",
   是"真的发生了双重进入,产生了可观测的状态损坏"）需要类似 S-003 §8.4-8.6
   那种专门设计的压力测试,不是靠现有的 FS A/B 窗口顺带发现——这次 66 次
   TSan 报告只证明竞争在发生,不证明发生过窗口内误判。
3. **修法方向**（都还没有设计细节,列出来供讨论,不是结论）：
   - 把 `m_wakeup_mutex_owner` 换成某种可以原子读写的表示（`std::thread::id`
     本身不保证是无锁原子友好类型,需要换成例如线程本地 id 的哈希值或者
     `gettid()` 返回的整数,配合 `std::atomic` 的 acquire/release 语序,让
     `:217` 的读和 `:222`/`:231` 的写之间建立真正的 happens-before）——
     这个方向不改变"同线程重入走快路径、跨线程走真锁"这个设计,只是把
     owner 字段本身做对。
   - 换成标准库的 `std::recursive_mutex`,彻底放弃手写的 owner/depth 记账,
     用标准库保证正确性——代价是可能失去当前设计里"已经持锁时重入不用碰
     底层 mutex"这个优化（`std::recursive_mutex` 内部也要做一次原子操作
     确认是否是当前持有者,不是纯粹零开销,但至少标准保证正确)。
   - 保留当前设计,但证明 §4.1 的可达性问题的答案是"不可达"(如果调用模式
     确实排除了这个场景,那么现状可能不需要改,只是需要把这个论证明确写
     进 `Consumer.hh` 的注释,而不是现在这样完全没提这两个字段的并发语义)。

## 5. §4.1 可达性审计结果：窗口可达，不是纯理论

（本节回答 §4 开放问题 1。审计只读代码，未跑测试、未改任何源文件。）

**结论：可达，且是稳态高频路径，不是边角情形。** 三条证据链：

1. **触发 `enqueue()` 的 SLICC 动作是普通 L3 miss 路径，跑一次workload就会
   命中很多次。** `a_issueFetchToMemory`（`src/mem/ruby/protocol/
   MESI_Two_Level-L2cache.sm:449`）在三个转换里被调用（`:957/967/978`），
   全部是 `NP`（块不在 L3）状态下响应任意一种 L1 请求
   （`L1_GETS`/`L1_GET_INSTR`/`L1_GETX`）——这是最普通的"L3 未命中，转发到
   目录"路径。而 `MESI_Two_Level-L2cache.sm` 正是 `MESI_Three_Level` 协议
   自己的共享 L3 controller 实现：`src/mem/ruby/protocol/
   MESI_Three_Level.slicc:6` 直接 `include "MESI_Two_Level-L2cache.sm"`，
   没有另外的三级专用 L3/L2 文件。也就是说这条路径在 `MESI_Three_Level`
   下和在两级协议下是同一份代码，跑真实 workload 时会持续触发，不是
   一次性握手。
2. **这个 fork 实际用的 FS 拓扑给每个 controller 一个私有、终生不变的
   router（`Switch`）**，不是共享中心交换机。`docs/refs/scripts/
   x86_fs_mesi3_parallel_eventq.py`（S-009/S-010 TSan 验证用的同一份配置
   脚本）第 172-190 行：`SimplePt2Pt` 给每个 controller 通过一条
   `ext_link` 配一个专属 router（"each controller a private router
   (Switch)"），`router_dom` 字典把每个 controller 的 `id()` 映射到它
   自己的域（L3 → `l3_dom`，每个目录 bank → `mem_dom0+j`），这个映射在
   配置阶段建好之后不会在运行中变化——`Switch` 对象是这个目录 bank
   自己域里唯一、终生持有的 router,不会被重建或换成另一个对象。所以
   L3 域的 `a_issueFetchToMemory` 每次转发到目录,`enqueue()` 打的都是
   *同一个* `Switch` 对象的 `Consumer` 子对象。
3. **一个域在整次仿真里固定绑在同一个宿主线程上**——`src/sim/
   simulate.cc:302`（`simulatorThreads->runUntilLocalExit()`）配合 S-005
   §10 描述的按 `eventq_index` 一次性设定线程亲和的机制,domain→host
   thread 的映射从 `simulate()` 开始就固定,运行中不会有别的线程来替
   班处理同一个域的事件。

三条放一起：**L3 域自己的宿主线程,在每一次 L3 miss 时,都会对同一个目录
`Switch` 对象做一次 `lock()`/`unlock()`**——这是稳态下的高频普通路径,
不需要凑巧的调用序列。同时,这个目录自己域的宿主线程（S-011 §3.1 抓到的
T3）也在独立地、频繁地对同一个对象做自己的 `wakeup()`/`enqueue()`/
`unlock()`。§3.2 描述的窗口（"曾经合法持锁的线程,在锁被另一个线程持有期间,
读到自己历史上写的 owner 值"）需要的前提——同一个跨域线程反复对同一个
consumer 加锁解锁——在这个拓扑/协议/线程绑定组合下是常态,不是特例。
S-010 §11.2 那 66 次 TSan 报告（两轮各三十多次）本身就是这个高频模式的
独立实测佐证,不只是"理论上能构造"。

**没有找到反证**——拓扑、协议或线程分配里都没有任何机制把"同一个跨域线程
对同一个 consumer 只加锁一次"这件事强制住。

**证据置信度**：
- `a_issueFetchToMemory` 触发频率、`MESI_Three_Level` 复用
  `MESI_Two_Level-L2cache.sm` 这条事实——**直接读源码确认**
  （`.slicc`/`.sm` 文件）。
- 每 controller 私有 router、`router_dom` 映射——**直接读配置脚本确认**
  （`x86_fs_mesi3_parallel_eventq.py:172-190`）。
- 域→宿主线程终生绑定——**直接读 `simulate.cc` + S-005 确认**，但未逐行
  追踪 `simulatorThreads` 类定义本身；结论和调用模式、S-005 的描述一致。
- "每次 fetch 打的都是同一个 Switch 对象"（不是运行中重建的新对象）——
  **推断**自配置脚本（router 在配置阶段建一次，之后不重建）,未在 C++
  运行时逐帧追踪对象生命周期,但 SimObject/Switch 模型不支持运行中替换
  router 这件事本身没有反例。

§4 开放问题 2（触发概率量级/是否真产生可观测状态损坏）和问题 3（修法方向）
仍未做，结论不变：**可达性已确认，但概率量级和修法都还没做**，是否继续
需要用户决定。

## 6. §4.2 压力测试设计（本会话，只设计，未实现未跑）

目标：不靠 TSan 的静态竞争报告（"这里存在数据竞争"），而是直接、廉价地
**观测**§3.2 描述的误判是否真的发生过（"这次真的有两个线程同时以为自己
持锁"），量化每次运行大概能撞见几次，仿照 S-003 §8.4-8.6 的做法——加一份
专门为了观测这一个竞态而写的临时诊断，跑完就知道结论，观测完就从树上
移掉，不作为修法混进生产代码。**这一节只是设计，本会话没有写代码、没有
编译、没有跑。**

### 6.1 检测手段：给 `UncontendedMutex` 加两个诊断专用的只读/探测接口

`m_wakeup_mutex` 的类型 `UncontendedMutex`（`src/base/uncontended_mutex.hh`）
内部用一个 `std::atomic<int> flag` 记录"0=无人持锁/1=恰好一个线程持锁/
>1=有人在等"（`:51-56`），`lock()`/`unlock()`（`:69-118`）都基于对这个
`flag` 的 `compare_exchange_strong`。这个 `flag` 本身是"谁真正持有这把
底层互斥体"的唯一真实来源（ground truth）——跟 `Consumer::
m_wakeup_mutex_owner` 那个无同步字段完全独立，不受它想验证的那个 bug
本身的影响。诊断只需要两个新接口，都不改变现有 `lock()`/`unlock()` 的
行为：

```cpp
// 诊断专用，非阻塞：仅当 flag 真的是 0（真正空闲）时才成功地把它设成 1
// 并返回 true；否则不改变 flag，返回 false。语义等价于标准 try_lock()，
// 且因为底层是 compare_exchange_strong（不是 _weak），不会有标准允许的
// "明明空闲却假失败"这种问题。
bool tryLock() { return testAndSet(0, 1); }

// 诊断专用，只读：不修改任何状态，只汇报当前 flag 是否非零，仅用于
// 日志打印，不能用于任何控制流判断（避免这次读本身引入新的竞争）。
bool isLockedForDiagnosticOnly() const
{ return flag.load(std::memory_order_acquire) != 0; }
```

在 `Consumer::lock()` 的快速路径分支（`Consumer.cc:217` 判断
`m_wakeup_mutex_owner == self` 为真、原本直接 `++depth; return;` 的地方）
插入一次 `tryLock()` 探测：

```cpp
if (m_wakeup_mutex_owner == self) {
#ifdef CONSUMER_LOCK_STRESS_TEST
    if (m_wakeup_mutex.tryLock()) {
        // 铁证：这次快速路径判断为真的时刻，底层真互斥体其实是空闲的——
        // 说明 self==owner 读到的是一个过期值，这个线程现在*没有*真正
        // 持锁，却准备直接跳过锁往下走。记录一次，立刻把探测用的临时锁
        // 释放掉（不能持有它——一旦持有就把本该复现的竞态本身修好了,
        // 探测代码绝不能改变bug路径本身的行为)。
        recordConfirmedMisjudgedReentry(this, self, /*owner_seen=*/self);
        m_wakeup_mutex.unlock();
    }
#endif
    ++m_wakeup_mutex_depth;
    return;
}
```

关键约束：**探测拿到锁之后必须立刻释放**，不能借这次机会真的把锁"补上"
——一旦补上，被观测的这次运行就不再会展现 bug 本身导致的后续行为（比如
两个线程真的并发跑 `wakeup()`），只是把窗口本身填住了，等于在观测的同时
把要观测的现象抹掉，跟 S-003 §8.5 用 lock-free 环形缓冲区、不加 mutex，
就是同一个"诊断不能改变被诊断对象的行为"的原则。

### 6.2 更严重的次生现象：探测顺带发现的"错误地对真互斥体调用真
`unlock()`"

设计过程中发现一个 §3 没提到、但从 6.1 的探测直接可以推出的更严重后果：
如果某次 `lock()` 走了快速路径且被 6.1 的探测证实是误判（`tryLock()`
成功），那么这次 `lock()`/`unlock()` 会话在 `depth` 减到 0 时，
`Consumer::unlock()`（`Consumer.cc:226-233`）仍然会去调用**真正的**
`m_wakeup_mutex.unlock()`——但这个线程这次会话里从来没有真正
`m_wakeup_mutex.lock()` 过（快速路径整个跳过了它）。如果这个时间点上
真互斥体恰好被*另一个*线程（比如 §3.1 里真正持锁的 T3）真实持有，这次
错误的 `unlock()` 会把 `flag` 强行清零、`notify_all()`，**在 T3 还在
临界区内部时就把它的锁从下面抽走**——这比"两个线程同时进入临界区"更严重
：不只是本线程越权进入，连**真正合法的持有者**的持锁状态都被第三方
破坏了，任何这时候在等这把锁的第四个线程都可能被提前放行。这是一个
理论上从代码读写时序就能推出、但目前没有实测证据的次生断言,加进 6.3
的检测点 2。

### 6.3 两个独立的检测点

1. **检测点 1（§6.1）**：`Consumer::lock()` 快速路径里的 `tryLock()`
   探测——直接证明"这次 self==owner 判断为真时，真互斥体实际空闲"，即
   §3.2 描述的误判本身发生了。计数器
   `g_confirmedMisjudgedReentries`（`std::atomic<uint64_t>`，全局或
   per-Consumer）。
2. **检测点 2（§6.2）**：只在检测点 1 已经在本次 `lock()`/`unlock()`
   会话里触发过的前提下（用一个线程局部标志位从检测点 1 打开、
   `unlock()` 里读完就关掉，避免误判正常会话），在 `Consumer::unlock()`
   真正调用 `m_wakeup_mutex.unlock()` 之前先用 `isLockedForDiagnosticOnly()`
   查一次：如果此刻 `flag != 0`（说明真的有其他线程正合法持有），记一次
   `g_confirmedThirdPartyUnlockCorruption`——这是比检测点 1 更严重、
   更值得优先报告的事件。

两个计数器都配一份和 S-003 §8.5 同款的 lock-free 环形缓冲区（单个
`fetch_add` 记序号，不加 mutex），只在计数器命中的那一刻把
`Consumer*`、线程 id、发生时刻所在域的 `curTick()`、观测到的
`m_wakeup_mutex_owner` 原始值按序号记下来，运行结束后一次性打印,不在
热路径上做任何 I/O。

### 6.4 复现命令：怎么让这个窗口尽量多撞几次

- **配置**：沿用 S-009/S-010 验证用的同一份
  `docs/refs/scripts/x86_fs_mesi3_parallel_eventq.py`（§5 已确认这份
  拓扑是"每 controller 私有终生 router"，检测点要观测的正是它）。
- **quantum**：这次目标是最大化竞态命中率，不是测性能，所以**不用**
  S-009 验证过的 Q=6660"安全"值——反而应该往小了调（比如退回 Q=300 甚至
  更小的历史安全值，或者专门试几档），quantum 越小，跨域屏障同步越
  频繁，§3.2 描述的"锁真正空闲的那个窄窗口"打开的次数越多，越容易撞上。
  这跟项目主线"抬高 Q 换性能"的目标（S-009）方向正好相反——这里是专门
  为了触发竞态反着调,不能顺手复用同一批"验证性能用"的参数当作"验证
  正确性"的参数。
- **workload**：需要持续、高频的 L3 miss/跨域目录流量（§5 已确认
  `a_issueFetchToMemory` 是触发路径），选一个访存密集、工作集超出私有
  L2 但对 L3 有压力的多线程 workload，跑得越久撞见的机会越多；具体选哪个
  benchmark 沿用这个 fork 已有的 FS 测试 workload 即可（S-006/S-008
  已经在用的那个），不需要新引入。
- **两种独立的 build 各跑一遍，互相印证**：
  1. 本诊断 + **不开** TSan 的普通优化 build——跑得快，能在同样墙钟时间
     里堆更多 tick、更多次交错，是主力统计来源。
  2. 本诊断 + **开** TSan 一起编译——TSan 本身会在 `Consumer.cc:217/231`
     报告数据竞争，把它和检测点 1/2 的计数放在同一次运行里对照，能
     直接回答一个目前完全没数据的问题："TSan 报的这一类竞争里，有多
     大比例真的造成了检测点 1/2 定义的语义违规，而不只是'理论上是竞争
     但两次访问碰巧没在关键时刻重叠'"——这是量化"竞态发生"到"竞态
     造成后果"这个转化率的直接办法。
- **时长**：本会话之前的 TSan A/B 用 `MAX_TICKS=1.3e9`；这里既然目标
  就是撞见这个窗口,不受性能验证"窗口固定才能比较"的约束,应该跑到比
  1.3e9 明显更长（预算允许的话），因为这是概率事件,时长越长期望命中
  次数越高。

### 6.5 要报告的数字，以及这次设计明确不做的事

跑完之后要报告：检测点 1/2 各自的命中次数（总数、每小时墙钟、每 1e9
tick）；同一次运行里 TSan 在 `Consumer.cc:217/222/231` 报告的次数，
和检测点 1 的命中数做比例对照；检测点 2 只要非零，就应该被当作立即
需要修的严重问题单独标出来，不与检测点 1 混在一起统计。

明确**不**在这次设计范围内：不去追查检测点 1/2 命中之后是否真的产生了
可观测的协议层状态损坏（比如消息重复发送、`stats.txt` 出现和已知基线
不一致的计数）——那需要再往下一层，比对触发窗口前后的 SLICC 消息流水账,
是检测点命中之后如果需要进一步定级才做的事,这次只回答"窗口本身有没有
真的被撞上、撞上频率量级",不回答"撞上之后有没有观测到下游真实损坏"。

**这份设计目前只是设计——插桩代码、`CONSUMER_LOCK_STRESS_TEST` 编译宏、
实际编译和运行都还没有做**，跟 S-011 §5 的自我约束一致：`Consumer.cc`
改错代价高，实现和跑之前建议先让用户确认这份设计本身（尤其是 6.1 的
`tryLock()`/`isLockedForDiagnosticOnly()` 两个新接口、6.2 指出的次生
风险论证）方向没问题，再动手写代码、编译、上机跑——这是一次新架构
的、大概率需要现场调试而不是照抄已有流程的压力测试,不建议不请示直接
往下做。

## 7. 决定：不实现 §6 的压力测试（用户决定，本会话）

用户看过 §6 的压力测试设计后，决定这次不需要实际实现/编译/运行它——§6 的
设计文本原样保留，作为"如果以后需要拿触发概率量级的实测数字，这是现成
可用的方案"的参考存档，但**本会话不会再往这个方向推进**。§4 开放问题 2
（触发概率量级）因此保持未回答状态，这是一个明确的、用户主动做出的范围
取舍，不是遗漏。本会话改为直接进入 §4 开放问题 3（修法方向），见 §8。

## 8. 修法设计（回答 §4 开放问题 3；只设计，未实现未编译未跑）

### 8.1 重新评估 §4 列的两个候选方向

§4 原来列了三个方向，第三个（"证明不可达，代码不用改，只需要在 `Consumer.hh`
补注释"）已经被 §5 排除——可达性已确认。剩下两个：

- **方向一**：把 `m_wakeup_mutex_owner` 换成可以原子读写的表示，配合正确的
  内存序，让 `:217` 的读和 `:222`/`:231` 的写之间建立真正的
  happens-before。
- **方向二**：整个换成 `std::recursive_mutex`，彻底放弃手写的 owner/depth
  记账。

**推荐方向一**，理由和具体设计见下；方向二作为一个正确但不推荐的备选保留。

### 8.2 为什么"把字段换成原子的"真的能修好这个 bug，而不只是消除 UB

先说清楚一个容易想岔的地方：这不是"只要包一层 `std::atomic` 让 TSan 不报警,
问题就算解决了"这种敷衍——§3.2 描述的具体误判场景（T5 读到自己历史上写过、
但已经被自己后续的 `unlock()` 覆盖掉的旧值）**恰好是 C++ 内存模型里
"单一原子对象的一致性（coherence）"这一条保证专门排除的场景**：C++ 标准
要求同一个原子对象上的修改构成一个全局全序（modification order）,并且
**任何线程自己对同一个原子对象的连续读,不能违反它自己已经观测过的顺序**
——具体到这里：T5 自己的 `unlock()`（写"none"）严格发生在 T5 早先的
`lock()`（写"T5"）之后（同线程程序序）,这个全序关系不需要任何额外的
acquire/release 就成立,是原子类型与生俱来的保证（普通非原子字段完全没有
这条保证,这正是当前实现出 bug 的根源）。所以：**只要把这个字段换成任意
`std::atomic<T>`（哪怕全用 `memory_order_relaxed`），T5 就不可能再读到
自己那个早已被自己覆盖掉的历史值**——§3.2 构造的具体窗口被直接堵死,这不是
"降低概率",是从内存模型层面排除这个特定失败模式。

真正需要 acquire/release（而不是 relaxed）的地方,是另一件事：保证"新的
真实持有者写 owner 之前的操作"和"下一个读到这个 owner 值、判断'不是我'
从而老老实实去 `mutex.lock()` 排队的线程"之间的顺序关系不出岔子——具体
到这份实现,写序需要满足两条（两条都只是同线程程序序,不需要额外同步)：

1. `lock()`：先 `m_wakeup_mutex.lock()` 真正拿到底层互斥体成功之后,再
   `owner.store(self, release)`——不能反过来。
2. `unlock()`：先 `owner.store(none, release)`,再 `m_wakeup_mutex.unlock()`
   ——同样不能反过来。第 2 条尤其关键：如果反过来（先真正释放、再清
   owner），会在"真锁已经空出来"和"owner 字段还显示旧持有者"之间开出一个
   新的窗口,旧持有者这时候回来做 `lock()` 会在 owner 还没清空前就命中
   快路径,而这时真锁可能已经被别的线程抢走——这就是把 §3.2 的同一个 bug
   在新实现里重新引入一遍,必须避免。

`m_wakeup_mutex_depth` **不需要改成原子**：一旦 owner 字段本身不可能再被
误判,`depth` 就只会被"当前真正持锁的那一个线程"触碰（要么是刚真正拿到锁
的线程,要么是这同一个线程自己后续的同线程递归调用),不存在跨线程并发写,
现状的 plain `unsigned` 继续成立。已经确认 `m_wakeup_mutex_owner`/
`m_wakeup_mutex_depth` 只在 `Consumer.cc:217-232` 这两个函数内部被touch,
`Consumer.hh` 里也没有别的地方读写它们（`grep -rn
"m_wakeup_mutex_owner\|m_wakeup_mutex_depth" src/mem/ruby/` 只命中
声明和这两个函数本身),修改范围就是这两个函数 + 一行字段声明,不涉及其他
调用点。

### 8.3 owner 字段用什么类型：不用 `std::thread::id`，复用这个文件里已有的
`curEventQueue()` 域身份

`std::atomic<std::thread::id>` 在标准上是合法的（`thread::id` 被要求
trivially copyable/standard-layout），但"是不是无锁原子"依赖具体库实现,
这份仓库没有现成的证据。**更好的选择是复用这份代码已经在用的身份标识**：
`Consumer.cc:65` 和 `:118` 已经用 `curEventQueue() == em->eventQueue()`
判断"当前线程是不是这个 `em` 自己域的线程"（`global_event.cc:142` 也是
同一个idiom）——`curEventQueue()`（`sim/eventq.hh:105/116`）是每个宿主
线程一份的 `__thread EventQueue*`,由 `simulate.cc:363` 在线程开始跑之前
设定一次,串行/并行模式下都会被设置（不只是并行模式专属),`MessageBuffer::
enqueue()` 对 `Consumer::lock()` 的调用在串行模式下也会执行（只是这时
必然是同线程重入或从来无竞争),所以这个值在两种模式下都可用。

用 `std::atomic<EventQueue*>` 代替 `std::atomic<std::thread::id>` 的好处：
- 指针大小的原子类型在所有主流平台上都毫无疑问是无锁的,不需要像
  `thread::id` 那样去查具体库实现是否无锁。
- 跟这份文件里判断"域身份"的既有写法完全一致,不是引入一个新概念,复查
  这处改动的人不需要同时学两套"我是谁"的表示。
- 比较开销比 `std::this_thread::get_id()`（每次都要构造一个 `thread::id`
  临时对象）更直接——直接读一次 `__thread` 变量。

### 8.4 改动草图（未编译，仅示意）

```cpp
// Consumer.hh
std::atomic<EventQueue*> m_wakeup_mutex_owner{nullptr};
unsigned m_wakeup_mutex_depth = 0;   // 不变，见 §8.2 的论证

// Consumer.cc
void
Consumer::lock()
{
    EventQueue *self = curEventQueue();
    if (m_wakeup_mutex_owner.load(std::memory_order_relaxed) == self) {
        ++m_wakeup_mutex_depth;
        return;
    }
    m_wakeup_mutex.lock();
    m_wakeup_mutex_owner.store(self, std::memory_order_release);
    m_wakeup_mutex_depth = 1;
}

void
Consumer::unlock()
{
    assert(m_wakeup_mutex_owner.load(std::memory_order_relaxed)
           == curEventQueue());
    if (--m_wakeup_mutex_depth == 0) {
        m_wakeup_mutex_owner.store(nullptr, std::memory_order_release);
        m_wakeup_mutex.unlock();
    }
}
```

（快路径的读用 `relaxed` 就够——§8.2 已经论证"排除自读旧值"这个性质是
`atomic` 本身自带的,不依赖内存序;真正需要 `release` 的只有两个写,保证
"我确实拿到/放弃了真锁"这件事对外可见的顺序,不需要给读也配对 `acquire`,
因为这次读不是用来同步除了 owner 本身以外的任何其他数据——受保护的真正
payload 一直是靠 `m_wakeup_mutex` 本身同步的,owner 字段只用来决定要不要
走快路径。）

### 8.5 为什么不推荐方向二（`std::recursive_mutex`）

`std::recursive_mutex` 完全正确,标准保证的语义直接覆盖这里要的"同线程
可重入、跨线程互斥",不需要任何这份设计里的原子序推理,实现和复查成本都
更低。**不推荐它做首选的唯一原因是性能**：`base/uncontended_mutex.hh`
开头的注释明确说明这个类存在的理由——"The std::mutex implementation is
slower than expected because of many mode checking and legacy support",
`UncontendedMutex` 用一次原子 CAS 换掉无竞争情况下的完整 mutex 调用。
`std::recursive_mutex`（通常包一层 `pthread_mutex_t` 并设
`PTHREAD_MUTEX_RECURSIVE` 属性）**每一次**调用（包括最常见的、无竞争的
首次加锁）都要走完整的库内部记账,这正是 `UncontendedMutex` 想避免的
开销,而这个 fork 的 Primary research goal 通篇是"real wall-clock
speedup"——在一个以性能为目标的项目里,选一个明知会在热路径（`Consumer::
lock()`/`unlock()` 在 Ruby 协议里是逐消息级别调用的频度）上引入已知
开销来源的修法,需要用实测数据证明方向一"不可行或收益不值得"才该退回来,
不是默认选项。方向二作为**如果方向一在实现/验证阶段遇到意外困难时的
备选**保留在这里,不是并列推荐。

### 8.6 验证计划（沿用 S-009/S-010 的方法，未执行）

1. **TSan A/B**：跟 S-009 §24/S-010 §11 同一份配置脚本
   （`docs/refs/scripts/x86_fs_mesi3_parallel_eventq.py`）、同样的
   `MAX_TICKS=1.3e9`、serial×2/spin×2 四份跑法,确认改动后
   `Consumer.cc:217/222/231` 不再出现在 TSan 报告里（S-010 §11.2 记录的
   66 次这一类应该归零）,同时确认没有引入新的警告类别。
2. **正确性 A/B（非 TSan 优化 build）**：serial 和 spin 各跑一次,`stats.txt`
   逐字节比对,确认这个改动不改变任何可观测的模拟行为——跟 S-009/S-010
   两次修法用的同一条验收线。
3. **明确的证据缺口（如实记录,不回避）**：以上两项只能证明"TSan 不再
   检测到这个位置的数据竞争"和"修法没有引入行为差异",**不能**证明
   "§3.2 描述的误判事件发生次数从某个非零值降到了零"——因为从未用 §6
   的探测实际数过它发生过几次（§7 已经记录用户决定不做这一步),没有
   "修之前 N 次、修之后 0 次"这种直接对照,只有"修法在内存模型层面能
   排出这个失败模式"的静态论证（§8.2）加上"TSan 这一类警告消失"的间接
   证据。如果以后需要更硬的证据,§6 的设计仍然可以拿来在修法前后各跑
   一次做直接对照,但这次不做。
4. **实现前建议再确认一次**：`std::atomic<EventQueue*>::is_lock_free()`
   在实际编译这个仓库用的工具链上是不是确实为真（预期是,指针大小的
   原子在 x86-64/libstdc++ 上没有已知反例,但目前只是预期,没有实际
   编译验证过）。

## 9. 检查点（已通过：用户明确要求"go ahead and implement §8"）

跟 S-009/S-010 的检查点约定一致：`Consumer.cc` 是 S-002 引入、S-003 修过一次
的核心同步原语,改错的代价比 S-009/S-010 那两处新加的锁都高,§8 的实现+验证
在动手前请示过用户,用户明确回复"go ahead and implement §8"——本节之后的
§10 是这次授权范围内做的事,不是不请示往下做。

## 10. §8 的实现 + 编译 + 验证（本会话，用户已授权）

### 10.1 实际改动

跟 §8.3/§8.4 的设计一致,改了 `src/mem/ruby/common/Consumer.hh` 和 `.cc`
两个文件（只有这两个文件被改动,`git status` 确认)：

- `Consumer.hh`：`m_wakeup_mutex_owner` 从 `std::thread::id` 换成
  `std::atomic<EventQueue *>{nullptr}`；删掉不再需要的 `#include <thread>`；
  在 `lock()`/`unlock()` 声明上方的注释里补充了这次改动的理由（指向本 spec
  §8.2 的论证)。
- `Consumer.cc`：`lock()`/`unlock()` 改用 `curEventQueue()` 代替
  `std::this_thread::get_id()`；快路径的读用 `memory_order_relaxed`；
  真正拿到/放弃 `m_wakeup_mutex` 之后写 owner 时用
  `memory_order_release`，写序保持 §8.2/§8.4 设计的顺序（先真锁后写
  owner；先清 owner 后真解锁)。

`m_wakeup_mutex_depth` 按 §8.2 的论证未改动（仍是普通 `unsigned`)。

### 10.2 实现前确认的一个开放项（§8.6 第 4 点）：`is_lock_free()`

用当前工具链（g++, `-std=c++20`)编译一个独立的最小复现:
`std::atomic<EventQueue*>::is_lock_free()` 返回 `true`，
`is_always_lock_free` 也是 `true`——§8.3 预期的"指针大小的原子在
x86-64/libstdc++ 上没有已知反例"在这份工具链上确认成立,不是假设。

### 10.3 构建

`taskset -c 0-53,56-91`（CLAUDE.md 规定的、避开 `54-55,92-111` 两个测试
臂专用核的构建核范围）：
- `scons build/X86_MESI_Three_Level/gem5.opt -j90`——成功,`scons: done
  building targets`。
- `scons --with-tsan build/X86_MESI_Three_Level_TSAN/gem5.opt -j90`——
  成功。

两次构建都只有正常的第三方库缺失警告（tcmalloc/libpng/HDF5/capstone），
没有跟这次改动相关的编译错误或警告。

### 10.4 TSan A/B（沿用 S-009 §24.5/§25.4、S-010 §11.2 的方法：
`MAX_TICKS=1.3e9`，serial×2 + spin×2，同时跑在不同隔离核上）

| 运行 | 核 | simInsts | TSan 警告数 |
|---|---|---:|---:|
| serial round1 | 54 | 926690 | 0 |
| serial round2 | 55 | 926690 | 0 |
| spin round1 | 92-99 | 926608 | 5 |
| spin round2 | 100-107 | 926608 | 5 |

**`Consumer::lock()`/`unlock()`（原来的 `:217/222/231`，S-010 §11.2 记过
66 次、S-009 §25.4 记过一轮 37/38 次）这次在四份日志里一次都没出现**——
不只是检查 `#0` 帧,`grep -n "Consumer.cc"` 整份日志逐行检查过,两份 spin
日志里 `Consumer.cc` 只作为调用栈中间帧（`commitTick`/
`scheduleEventAbsolute`/`consumeCurrentTick`/`processCurrentEvent`）出现
在另外 5 次警告里,这 5 次警告的 `#0` 帧全部落在
`EventQueue::getCurTick()`（`eventq.hh:875`）和
`Flags<unsigned short>::set()`（`flags.hh:116`）——跟 S-009 §24.4/24.5
记录的既有背景噪声完全同一类（项目自己"放宽跨域时序"的设计取舍，S-011
§1 已经论证过这两类和 `Consumer` 锁不是一回事,这次修法要处理的正是让
`Consumer` 这一类从背景噪声里消失,而不动 `_curTick`/`Flags` 那两类——
结果符合预期)。**这次修法的直接目标（消除 TSan 对 `Consumer::lock()`/
`unlock()` 的报告）达成**。

### 10.5 正确性验证：原计划的方法论有问题，改用更严格的对照，
且过程中发现一个跟这次修法无关的既有分歧（如实记录）

**§8.6 原计划第 2 点想做的验证是"serial 和 spin 各跑一次,`stats.txt`
逐字节比对"**，沿用 S-009 §24.3/S-010 §11.2 的做法。**跑出来发现这个
假设本身不成立**：用改完的二进制在 `MAX_TICKS=1.3e9`（非 TSan 优化
build)分别跑 serial（核 54）和 spin（核 92-99），`simInsts`
分别是 926690 和 926608——**不相同**，`diff` 排除 `host*` 字段后两份
`stats.txt` 有 2126 行不同,不是逐字节相同。

**这立刻需要回答一个问题：这是这次修法引入的回归,还是修法之前就存在的
分歧？** 用 `git stash` 把 `Consumer.hh`/`.cc` 临时还原成改动前的版本,
在同一个 `build/X86_MESI_Three_Level` 目录里重新编译（只重新编译
`Consumer.o` 及依赖它的目标文件,不是全量重建),用同一份检查点、同一个
脚本、同一个 `MAX_TICKS=1.3e9` 窗口,serial（核 54）+ spin（核 92-99）
各跑一次,再把 `Consumer.hh`/`.cc` 用 `git stash pop` 换回来:

| 对照 | simInsts (serial) | simInsts (spin) | serial-vs-spin diff 行数 |
|---|---:|---:|---:|
| 改动前（git stash 还原） | 926690 | 926608 | 2126 |
| 改动后（这次修法） | 926690 | 926608 | 2126 |

**两行完全一样**——serial-vs-spin 的分歧在改动前后是同一个分歧,不是这次
修法造成的。更关键的对照（这才是真正回答"这次修法有没有改变可观测行为"
的问题)：**同一个模式下,改动前 vs 改动后的 `stats.txt`（排除 `host*`
字段）逐字节相同**——`diff <(...orig-serial...) <(...fix-serial...)`
和 `diff <(...orig-spin...) <(...fix-spin...)` 两次 `diff` 输出都是空、
退出码 0。**这才是这次修法应该满足、也确实满足的正确性标准**：同一个
模式下,这次修法不改变任何可观测的模拟行为。

**§8.6 第 2 点原计划的表述需要在这里更正**：assume "serial vs spin 在这个
窗口下逐字节相同"这件事,套用的是 S-009 §24.5/§25.4 和 S-010 §11.2 的
先例——但那几次做 serial-vs-spin 逐字节比对的,**都是在 TSan build
（`X86_MESI_Three_Level_TSAN`）上跑的**,不是这次用的普通优化 build；
TSan build 因为插桩会慢一到两个数量级,线程实际的相对调度节奏跟不开
TSan 时完全不同。这次是第一次在**不开 TSan 的优化 build**上、在这么大
的窗口（1.3e9 ticks)做 serial-vs-spin 对照,得到的是一个新的、之前没被
观测到的结果：**serial 和 spin 在这个窗口下本来就不是逐字节相同的
（不管有没有这次修法),这跟 `Consumer::lock()` 的这个 owner 竞争无关
（有没有修法,分歧数字一模一样)**。

**这跟本项目"relaxed cross-domain timing, not lock-free structures"这个
主线设计取舍（CLAUDE.md "Primary research goal"一节）在方向上是一致的**
——parallel/spin 模式本来就不承诺和 serial 在细粒度时序上完全一致,S-009/
S-010 之前观测到的"逐字节相同"更可能是"这几次具体测的窗口/build 组合恰好
没有踩到某条会导致分歧的路径",而不是这个 fork 的架构性保证。**但这不
代表这个分歧不值得关注**——`INDEX.md` 里已经有一条记录"精确模式下一个
恒定的一 ruby 周期偏差未查明根因（S-006 §9.6 附近提到）"；本会话尝试
在 `S-006-fs-mode-migration.md` 里核实这条指引指向的具体章节,**没有
找到跟这句引述精确对应的文本**——不确定这是不是同一个现象,如实记录这个
不确定性,不去猜测二者是同一件事。**这是一个新的、独立于 S-011 的待办
项，超出这次"实现 §8"的范围，需要用户决定要不要单独立文调查**（本次
divergence 的性质：926690 vs 926608，差 82 条指令，2126 行 stats
不同——量级明显不是"一个 ruby 周期"这种量级的表述能覆盖的，两者恐怕
不是同一件事，但没有反查过 `S-006`/`S-008` 原始数据来确认)。

### 10.6 结论

**§8 的修法已实现、已编译（非 TSan + TSan 两个 build)、已验证**：
- TSan 不再报告 `Consumer::lock()`/`unlock()` 的数据竞争（§10.4，从
  S-010 §11.2 的 66 次/S-009 §25.4 的 37-38 次降到 0）。
- 这次修法在同一模式下不改变任何可观测的模拟行为（§10.5，改动前后
  `stats.txt` 逐字节相同)。
- 触发概率量级（§4 开放问题 2）仍未回答，用户已决定不做（§7)。
- **副产品发现**：serial 和 spin 在 `MAX_TICKS=1.3e9`、非 TSan
  build 下本来就有一个跟这次修法无关的既有分歧（§10.5），需要用户
  决定是否单独立文调查,不阻塞 S-011 本身的结论。
- 未做：把这次改动的诊断产物（`/tmp/s11-*` 系列目录/日志）之外的
  内容清理或提交；`git stash` 已经在验证结束后 `pop` 回来,工作树
  当前状态是"改动已应用",跟 `git status` 显示的一致。
