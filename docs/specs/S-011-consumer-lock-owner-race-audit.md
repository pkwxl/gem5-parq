# S-011 — 审计草案：`Consumer` 自己的 per-consumer 锁 owner 字段无同步读写
（发现 + 精确复现窗口分析，未做全量调用点排查，未修）

**状态：草案**。这份 spec 只做完了"把问题精确定位到一个具体的、有窗口边界的时序
竞争"这一步（§3），**没有**做 S-009 §23/S-010 §7 那种"全部调用点排查、确认修法
范围"级别的完整审计，也没有设计或实现任何修法。按用户的要求单独立文（不并进
S-009），因为这是完全不同的子系统——S-009 管的是经典 Port/南桥，这里是 Ruby
自己的 `Consumer`/`MessageBuffer` 同步机制（S-002 引入，S-003 修过一次）。

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

## 5. 这份 spec 到此为止

跟 S-009/S-010 的检查点约定一致：`Consumer.cc` 是 S-002 引入、S-003 修过一次
的核心同步原语,改错的代价（悄无声息的状态损坏,可能要类似 S-003 §8.4-8.6
那种专门构造的压力测试才能复现）比 S-009/S-010 那两处新加的锁都高——**不建议
不请示直接往下做**。§4 列的三个问题,哪个先做（先查可达性、先设计修法、还是
先到此为止只留档等以后有余力再看）需要用户决定。
