# S-010 — `AddrRangeMap` 缓存的跨域数据竞争：发现 + 审计 + 实现 + TSan 验证（已实现+验证，性能 A/B 待做）

**状态：已实现并通过 TSan 验证**。用户选了 §6 的选项 1（现在做），§7 的只读审计
确认了落点和范围，用户确认后（"go ahead and implement the lock"）在
`src/base/addr_range_map.hh` 里加了容器内部锁（§10），非 TSan 构建的正确性 A/B
和 TSan 扩时长 A/B（§11，沿用 S-009 §24.3/24.5 的方法论）都已跑完并且干净。
**仍未做**：热路径开销的性能 A/B（§9 原文列的问题，现在是唯一剩下的验证项）。

这份文档来自 [S-009](./S-009-raise-fs-quantum-past-iobus-edge-design.md) §24.5 的
TSan 扩时长 A/B——那一轮任务范围锁定在"经典 Port/南桥"路径（`BaseXBar`/
`PioDevice`），这个竞争跟那条线完全是不同子系统，S-009 检查点里明确写了"不顺手
处理，需要用户决定优先级"，用户选了选项 1。

## 1. 发现来源

[S-009 §24.5](./S-009-raise-fs-quantum-past-iobus-edge-design.md#245-扩时长-tsan-ab-结果maxticks13e9serial2--spin2同时跑在) 的
TSan 扩时长 A/B（`MAX_TICKS=1.3e9`，serial×2 + spin×2，`build/X86_MESI_Three_Level_TSAN`）
里，两份 spin 日志报告数量最多的一类竞争落在
`src/base/addr_range_map.hh:266`（`addNewEntryToCache`）和 `:291`（`find`）——
两份日志合计 **1500+ 次**（各 783/787 次命中 `:266`，112/123 次命中 `:291`），
远超其他所有类别之和（相比之下 S-009 §24.4/24.5 记录的 `EventQueue::_curTick`
竞争、`Consumer::lock()` 竞争都是个位数到几十次量级）。这次 A/B 本身的正确性
结论不受影响——四份 `stats.txt` 逐字节相同，`simInsts` 四份都是 926690，这个
竞争目前没有导致可观测的仿真结果分歧（原因见 §5，跟 S-009 §18.2/24.5 对
PIT/RTC/IDE 的判断是同一个教训：竞争"发生"和竞争"造成可观测后果"是两回事）。

## 2. 代码位置和竞争机制

`AddrRangeMap<V, max_cache_size>`（`src/base/addr_range_map.hh`）是一个按地址区间
查找的容器，内部一棵 `std::map<AddrRange, V> tree` 加一个**最近命中缓存**
`mutable std::list<iterator> cache`（`addr_range_map.hh:337`）。命中路径：

```cpp
// addr_range_map.hh:286-321，非 const 版本
iterator
find(const AddrRange &r, std::function<bool(const AddrRange)> cond)
{
    // 先查缓存
    for (auto c = cache.begin(); c != cache.end(); c++) {
        auto it = *c;
        if (cond(it->first)) {
            cache.splice(cache.begin(), cache, c);   // :295，命中时重排缓存
            return it;
        }
    }
    ...
    addNewEntryToCache(next);   // 未命中缓存但命中树时，写入缓存（:302/313）
    ...
}

// addr_range_map.hh:323-327，const 版本
const_iterator
find(const AddrRange &r, std::function<bool(const AddrRange)> cond) const
{
    return const_cast<AddrRangeMap *>(this)->find(r, cond);   // :326
}
```

`addNewEntryToCache`（`addr_range_map.hh:256-273`）在缓存满时会原地覆写
`*last`（`:266`）并 `splice`（`:268`）——**这是 `find()`（`:291` 读 `*c`）和
`addNewEntryToCache()`（`:266` 写 `*last`）两者都在改写同一个 `cache` 链表**，
TSan 报的两行正好对应这两处读写。

关键点：`contains(Addr)`（这是外部实际调用的入口，见 §3）**语义上是一个只读
查询**——它是 `const` 成员函数，调用方没有理由认为这次调用会修改任何状态。
但 `const` 版本靠 `const_cast` 剥掉常量性，转发到非 const 版本，非 const 版本
**无条件**把命中结果写入 `mutable cache`（LRU 式重排/写入，为了加速下一次查找）。
换句话说：`AddrRangeMap` 的"查找"接口对外表现为无副作用的读，内部实现是一个
无锁的、跨调用共享可变状态的写——这正是 S-004 §9.8 记录的 X86 TLB 竞争的同一个
模式（"语义只读的操作，内部靠 `mutable` 字段做缓存/记账，缓存写没有保护"），
不是巧合，是这个代码库里同一类反模式的第二个实例。

## 3. 命中路径：为什么是"每核每次内存访问"级别的频率

调用链（已读代码确认）：

```
RubyPort::MemResponsePort::isPhysMemAddress(pkt)   // RubyPort.cc:721-725
  → owner.system->isMemAddr(addr)                  // System::isMemAddr, system.cc:290-301
    → physmem.isMemAddr(addr)                      // PhysicalMemory::isMemAddr, physical.cc:272-276
      → addrMap.contains(addr) != addrMap.end()    // physical.hh:145 声明的 addrMap
        → AddrRangeMap::contains(Addr) const       // addr_range_map.hh:114-118
          → contains(RangeSize(r,1)) const         // :90-93
            → find(r, isSubset lambda) const       // :323-327，const_cast 落地
```

`RubyPort.cc` 里 `isPhysMemAddress` 被调用的地方（`RubyPort.cc:286/356/427/491/668`）
覆盖了 Sequencer 收到内存请求、发起 hit callback 等**几乎所有请求路径**——
S-009 §24.5 原文写的"每个核每一次内存访问都会走到这条判断"是对这条调用链
逐层跟下来的结论，不是猜测。

`PhysicalMemory`（`src/mem/physical.hh:136`）在系统里只构造一份，它内部的
`addrMap`（`physical.hh:145`：`AddrRangeMap<AbstractMemory*, 1>`，注意
`max_cache_size=1`——缓存只有一个槽位）被 `System` 持有，`System` 是
S-006 §11.1 域划分里域 0（uncore）拥有的对象，但**所有核域的线程都会同步调用
到它身上**（跟 S-009 §18.2 描述的经典 Port 链路"全程在发起请求的核自己的
域线程上同步执行，从未真正切线程"是同一个结构）。于是：

- 核域 i 的线程调用 `contains()`，缓存命中/未命中都可能触发 `cache` 的
  `splice`/覆写（`:266`/`:295`）。
- 核域 j 的线程**几乎同时**做同一件事，读写同一个 `cache` 链表。
- `max_cache_size=1` 意味着这个缓存**只有一个槽位**——不同核连续访问不同地址
  区间时，这个槽位会被频繁地互相覆写，读写重叠的概率比一个更大的缓存更高
  （槽位越少，同一块内存被并发读写的相对频率越高）。

这就是 S-009 §24.5 判断"量级和触发频率大概率比南桥/IOXBar 这条线更大"的依据：
IOXBar/PioDevice 只在核发起 PIO 请求时命中（偶发），这条路径在**普通内存
load/store**上命中（几乎每条访存指令）。

## 4. 和 S-004 §9.8 X86 TLB 修法的对照

S-004 §9.8 记录的 TLB 竞争（`mem_state.cc` 的 `unmapRegion`/`remapRegion` 跨域
`flushAll()` 撞上其他域自己的 TLB `lookup`/`insert`）最终的修法（S-004 附近
385-388 行）是给 `X86ISA::TLB` 加一个 `UncontendedMutex tlbLock`
（`src/base/uncontended_mutex.hh`——项目里给"真实但罕见竞争"用的既定轻量锁，
S-009 §24.1 给 `BaseXBar`/`PioDevice` 加锁时也复用了同一个类型），在
`lookup`/`insert`/`flushAll` 等入口整体 `lock_guard`。

`AddrRangeMap` 的情况结构上更简单（没有 TLB 那种"跨对象"的锁序问题——
竞争双方是同一个 `AddrRangeMap` 实例内部的 `cache` 字段，不涉及第二个对象），
类比的修法方向是同一套思路：给 `AddrRangeMap` 加一个 `mutable UncontendedMutex`
成员，在 `find()`（非 const 版本，真正读写 `cache` 的地方）入口
`lock_guard` 住。**这不是本文档要定稿的设计**，只是记录一个和 S-004/S-009
一致的、可信的修法方向，供用户决定要不要往下做时参考：

- 好处：落点单一（`AddrRangeMap::find` 一个函数），不需要像 S-009 §23 那样
  为多个设备类型分别找公共基类落点——`AddrRangeMap` 本身就是唯一的公共基础
  设施。
- 需要确认、当时**没有**验证过的点（§7 已经做完第 1 点，2/3 仍未做）：
  1. `AddrRangeMap` 是模板类，被多处实例化，需要过一遍所有实例化，确认哪些
     是跨域共享的、哪些是域私有的，不能假设只有这一处需要加锁，参照
     S-009 §24.1 对 `PioPort<Interrupts>` 那次"发现意料之外的第二个实例化"
     的教训——**§7 做了这一步**。
  2. 加锁后这条"每次访存都要走"的热路径的开销未测量——S-009 §22 对 TLB
     锁的热路径开销也是同样悬而未决，两者可以放在同一轮测量里一起做。
  3. `contains()`/`intersects()` 目前都是 inline 短函数，`lock_guard` 会
     把原本可能被内联优化掉的调用变成一个真实的锁操作，热路径影响需要
     实测，不能纸面判断"轻量锁所以没事"。

## 7. 全仓库 `AddrRangeMap` 实例化审计（比照 S-009 §23 的方法论，只读，已完成）

`grep -rn "AddrRangeMap" src` 排除 `addr_range_map.hh` 自己，命中 8 处实例化。
逐处确认"这个实例是被多个域的线程共同触达，还是只属于一个域自己的对象"：

| 实例 | 位置 | 归属对象 | 域局部性 | 结论 |
|---|---|---|---|---|
| `PhysicalMemory::addrMap` | `mem/physical.hh:145` | `System`（域 0），但**被所有核域线程同步调用**（`RubyPort::isPhysMemAddress` 链路，§3） | 跨域共享 | **真实竞争，已被 TSan 观测到**（§1），本文档的原始发现 |
| `BaseXBar::portMap` | `mem/xbar.hh:320` | `BaseXBar`（IOXBar/board iobus 等，域 0），同样被跨域调用 | 跨域共享 | **新发现，见 §7.2**——不是同一个容器实例，但是同一个 `AddrRangeMap` 模板类、同一种反模式，而且没有被 S-009 §24 的 `layerLock` 完整盖住 |
| `RiscvISA::PMAChecker::misaligned` | `arch/riscv/pma_checker.hh:78` | 每个 `BasePMAChecker*`，被 `mmu.hh`/`tlb.hh`/`pagetable_walker.hh` 各自的 TLB/MMU 持有一份 | 域私有（每个核域自己的 TLB 私有对象，S-006 §11.1 域映射下从不跨域） | 安全；且本项目建的是 X86（`MESI_Three_Level`），RISC-V 这条代码在当前 build 里不参与 |
| `NonCachingSimpleCPU::memBackdoors` | `cpu/simple/noncaching.hh:61` | 每个 CPU 实例私有 | 域私有 | 安全 |
| `DmaPort::memBackdoors` | `dev/dma_device.hh:65` | 每个 `DmaPort` 实例（IDE/NIC 等 DMA 设备各自持有），只被该设备自己驱动 DMA 状态机的域 0 线程触达（跟 S-009 §23.1 记录的 IDE DMA 状态机"完全在域 0 自己线程上跑"是同一个结构），不像 PIO 那样被核域线程同步穿透调用 | 域私有 | 安全（但依赖"没有代码从核域线程直接调用某个 `DmaPort` 的方法"这个假设——只查了声明和主要入口，没有逐行追完整调用图，比 `PhysicalMemory`/`BaseXBar` 两条置信度低一点） |
| `AbstractController::downstreamAddrMap` | `mem/ruby/slicc_interface/AbstractController.hh:481` | 每个 Ruby 控制器（SLICC state machine）实例私有，只在 `mapAddressToDownstreamMachine`（`AbstractController.cc:434`）里被**该控制器自己的 SLICC 动作**调用，动作在该控制器自己的域线程上跑 | 域私有 | 安全；且目前唯一的调用方是 CHI 协议的 `.sm` 文件，本项目用的是 `MESI_Three_Level`，这条路径当前 build 不会被触发 |
| `GPUComputeDriver::gpuVmas` | `gpu-compute/gpu_compute_driver.hh:162` | 每个 GPU driver 实例私有 | 域私有/不适用 | 安全；GPU-compute 是独立 build（配 `arch/amdgpu`），不在本项目的 X86/Ruby parallel-EventQueue 范围内 |
| SystemC TLM 桥 `backdoorMap` | `systemc/tlm_bridge/gem5_to_tlm.hh:189` | 每个 TLM 桥接端口实例私有 | 域私有/不适用 | 安全；SystemC 集成是独立 build，不在本项目范围内 |

**结论**：8 处实例化里，**2 处是跨域共享、真实存在竞争**（`PhysicalMemory::addrMap`
和新发现的 `BaseXBar::portMap`），其余 6 处域私有或不适用于本项目当前的 build/
协议组合。跟 S-009 §24.1 的教训一致——"过一遍所有实例化"这一步不能省，这次
省了会漏掉 `BaseXBar::portMap` 这个同样真实、而且和已经修过的 `layerLock` 是
"看起来已经处理过"却实际有漏洞的那种最容易被忽略的情况。

### 7.1 `BaseXBar::portMap`：竞争机制和 `PhysicalMemory::addrMap` 完全一样

`BaseXBar::portMap`（`AddrRangeMap<PortID, 3>`，`xbar.hh:320`）是 IOXBar/
board 主 iobus/`pci_bus` 等所有 `BaseXBar` 派生对象内部的路由表，`findPort()`
（`xbar.cc:334-`）调用 `portMap.contains(addr_range)`（`xbar.cc:341`）——跟
`PhysicalMemory::addrMap` 一样，`contains()` 是 `const`，内部靠 `const_cast`
落到会修改 `mutable cache` 的 `find()`，§2 描述的竞争机制原样适用。

### 7.2 `layerLock`（S-009 §24.1）没有盖住这个容器——四个调用入口，只有一个上锁

跟踪 `NoncoherentXBar` 里所有调用 `findPort()` 的入口（已读代码确认，
`noncoherent_xbar.cc`）：

| 入口 | 是否持有 `layerLock` | 代码位置 |
|---|---|---|
| `recvTimingReq` | **是**（S-009 §24.1 加的锁，函数开头 `lock_guard`） | `noncoherent_xbar.cc:104-113` |
| `recvTimingResp` | 持锁，但这个函数**不调用** `findPort()`（走 `routeTo` 反查，不需要重新路由） | `noncoherent_xbar.cc:180-` |
| `recvAtomicBackdoor` | **否** | `noncoherent_xbar.cc:264` 附近 |
| `recvFunctional` | **否** | `noncoherent_xbar.cc:325` |
| `recvMemBackdoorReq` | **否** | `noncoherent_xbar.cc:297` |

`layerLock` 保护的是 `reqLayers`/`respLayers`/`routeTo`（S-009 §24.1 原文），
**从设计时就没打算保护 `portMap`**——`recvTimingReq` 里对 `findPort()` 的调用
只是恰好被这把锁"顺路"覆盖了，因为它也在同一个已加锁的函数体内，不是因为
`portMap` 被认定需要保护。`recvAtomicBackdoor`/`recvFunctional`/
`recvMemBackdoorReq` 三个入口完全没有锁——如果一个核域线程发起 timing PIO
请求（`recvTimingReq`，持锁）同时另一个核域线程（或域 0 自己，比如某个
功能性访问/调试路径触发 `recvFunctional`）在跑 `recvFunctional`，两者都会
命中 `portMap.contains()`，其中至少一侧没有锁保护——这是教科书式的"部分
加锁"疏漏，跟 §2 描述的 `PhysicalMemory::addrMap` 是同一种底层机制，但是
一个新的、独立的暴露面。

**为什么 S-009 §24.5 的 TSan 扩时长 A/B 没有报出这一条**：那次测试是 FS
定窗跑正常的 timing 模拟，`recvAtomicBackdoor`/`recvFunctional` 相对
`recvTimingReq`是冷路径（原子模式一般不在这条 timing A/B 里触发；
`recvFunctional` 只在特定功能性访问/调试场景被调用），这次窗口大概率没有让
两者在时间上重叠——跟 §8（`PhysicalMemory::addrMap` 那条的同类分析）、以及
S-009 §18.2/24.5 反复出现的"没触发不等于没有"是同一个教训，这次连着两个
不同容器都验证了一遍。

### 7.3 这对修法方向的选择意味着什么

§4 原来给的修法方向是"在 `AddrRangeMap::find()` 内部加锁"，当时的理由只是
"落点单一"；§7.2 的发现把这个理由从"更省事"升级成"**必要**"——如果改成
S-009 §24 那种"在每个调用入口手动加锁"的做法（比如给 `recvAtomicBackdoor`/
`recvFunctional`/`recvMemBackdoorReq` 也手动套 `layerLock`），历史已经证明
这类逐入口加锁**会漏**（`layerLock` 本身就是先例）。而如果锁直接长在
`AddrRangeMap::find()`/`addNewEntryToCache()` 内部，`portMap`（`BaseXBar`）
和 `addrMap`（`PhysicalMemory`）两个完全不同的调用方**不需要各自记得加锁
就自动被覆盖**——这跟 S-009 §19.1 选择在 `PacketQueue::schedSendEvent` 这个
公共点而不是逐调用点做 snap 是同一个设计判断，这次有了更直接的反面证据支持。

`AddrRangeMap` 自己的锁和 `layerLock` 不冲突：`UncontendedMutex`
（`src/base/uncontended_mutex.hh`）不可重入，但 `recvTimingReq` 持有的是
`BaseXBar` 自己的 `layerLock`，`AddrRangeMap::find()` 加的是**另一个、属于
`AddrRangeMap` 实例自己的**锁对象——两者是不同的锁，`recvTimingReq` 走到
`portMap.contains()` 时是"持有 layerLock，再尝试获取 portMap 自己的锁"，
只要代码库里没有反过来"持有 AddrRangeMap 自己的锁时又去尝试获取
layerLock"的路径（`AddrRangeMap::find()` 内部只碰自己的 `tree`/`cache`，
从不回调外部对象，天然是叶子锁，不会形成这个反向路径），加锁顺序恒定为
`layerLock → AddrRangeMap 内部锁`，不会死锁——跟 S-004 §9.8 给 `tlbLock`
定性为"叶子锁"是同一个论证。

## 8. 为什么这次 A/B 没有暴露出可观测的错误结果

跟 S-009 §18.2/24.5 对 PIT/RTC/IDE 的判断同一个教训：这次窗口下四份
`stats.txt` 逐字节相同，**不代表这个竞争无害**，更可能的解释是：

- `cache` 只有 1 个槽位，竞争窗口极短（一次 `splice`/覆写就是几条指针赋值），
  两个线程真正同时命中同一块内存、且时序恰好在"读到一半、写也在改"的窗口内
  重叠的概率虽然远高于 IOXBar 场景，但造成**可观测**后果（比如 `find()`
  返回一个悬空或错误的 `iterator`，进而让 `isMemAddr` 判断出错）需要更巧的
  时序；`std::list` 的 `splice`/迭代器覆写在实践中往往不会立刻崩，可能只是
  短暂返回一个逻辑上过期但仍然有效的迭代器（这次缓存里唯一的元素本来就是
  同一棵 `tree` 里的有效 iterator，破坏 `cache` 本身不一定破坏 `tree`）。
- 这不是"没有 bug"的证明，是"这次测试窗口/负载没有触发可观测后果"——跟
  S-008 §15.3、S-009 §18.2/24.5 反复强调的教训完全一致：竞争已经被 TSan
  **直接观测到**（不是理论推测），是否会在某次运行里产生错误的物理地址
  判断（进而路由错一次内存请求）取决于两个线程的相对时序，不能因为这次
  没炸就下"安全"的结论。

## 9. 现状与下一步：审计已完成，实现前再确认一次

用户在这份文档最初版本的 §6（三选项：现在做/排在 S-009 后面/单独排期）里
选了选项 1。§7 是这个选择授权的"只读审计"部分，已经做完，结论：

- **修法落点确定**：给 `AddrRangeMap` 自己加一个 `mutable UncontendedMutex`
  成员，在 `find()`（真正读写 `cache` 的非 const 版本）入口 `lock_guard`
  住（§4 的原方向，§7.3 补强了"必须是容器内部加锁，不能是逐调用点加锁"
  这个论证）。
- **只有一处模板类改动，覆盖两个跨域竞争**：`PhysicalMemory::addrMap`（§3，
  本文档原始发现）和新发现的 `BaseXBar::portMap`（§7.1-7.2）都会被同一处
  `AddrRangeMap::find()` 内部锁自动覆盖，不需要分别处理。
- **锁序确认为安全**：`AddrRangeMap` 内部锁是叶子锁，跟 `layerLock`
  （S-009 §24.1）没有反向获取路径，不会死锁（§7.3）。
- **其余 6 处 `AddrRangeMap` 实例化确认域私有或不适用于本项目当前
  build**（§7 表格），不需要加锁，但如果以后本项目扩展到 RISC-V/
  GPU-compute/CHI 协议，需要重新核实这个结论是否还成立。

**还没有做、实现前需要确认的点**：

- 热路径开销未测量（§4 原文列的问题仍然成立，`contains()` 从可能被内联的
  纯函数变成一次真实的锁获取，`PhysicalMemory::addrMap` 这条路径是"每次
  访存"级别的调用频率，`BaseXBar::portMap` 是"每次经典 Port 请求"级别，
  开销需要跟 S-009 §22 遗留的 TLB 锁开销测量放在同一轮里一起测）。
- `src/base/addr_range_map.hh` 是被 RISC-V/GPU-compute/SystemC 共用的模板
  头文件——改这个文件会影响本项目 X86/Ruby 之外的其他 ISA/build 配置（哪怕
  §7 的审计确认这些配置里的用法目前是域私有、安全的，改动本身仍然触达那些
  代码路径，值得在动手前明确一次，而不是默认"反正审计过了就直接改"）。
- `DmaPort::memBackdoors` 那一条（§7 表格）置信度比另外几条低——只查了
  声明和主要入口，没有逐行追完整调用图；如果要对"域私有"这个结论要更高的
  把握，需要在实现前补一次更细的追踪，或者干脆一并纳入加锁范围（反正落点
  是 `AddrRangeMap` 内部，纳入不纳入这一处的开销都是同一把锁，代价很低）。

**下一步需要用户确认**：是否现在直接开始实现（改 `addr_range_map.hh` 加锁 +
TSan A/B + 性能 A/B，按 S-004 §9.8/S-009 §24 的验证方法论），还是先看一遍
§7 的审计结果再决定——尤其是 §7.2 的 `BaseXBar::portMap` 发现，这本来不在
"AddrRangeMap 审计"这个任务的预期范围内，是审计过程中顺带找到的一个和
S-009 §24 已完成工作有关的新缺口，值得单独确认优先级判断有没有变化。

用户确认后（"go ahead and implement the lock"），§10/§11 是这一步的实现和验证
记录。

## 10. 实现

改的文件只有 `src/base/addr_range_map.hh`。落点和 §9 定的方向一致（容器内部
加锁，不是逐调用点加锁），具体做法：

- 加 `#include "base/uncontended_mutex.hh"`，加一个私有成员
  `mutable UncontendedMutex cacheLock`（跟 S-004 §9.8 的 `tlbLock`、
  S-009 §24.1 的 `layerLock`/`pioLock` 同一个类型，项目里"真实但罕见竞争"
  的既定写法）。
- 原来的非 const `find()`（真正读写 `cache` 的那个）改名成私有的
  `findImpl()`，**不加锁**；新的 `find()` 只是 `lock_guard(cacheLock)` 之后
  调用 `findImpl()`——`contains()`/`intersects()` 都经这个 `find()`，自动
  获得保护。
- `insert()`/`erase(iterator)`/`erase(iterator,iterator)`/`clear()`
  四个直接读写 `tree`/`cache` 的方法各自 `lock_guard(cacheLock)`。
  **`insert()` 有个需要注意的坑**：它原来的实现调用公开的 `intersects(r)`
  做冲突检查，`intersects()` 会再调用一次已加锁的 `find()`——如果照抄这个
  调用链，`insert()` 自己持锁的同时再去拿同一把不可重入的
  `UncontendedMutex`，会自锁死锁。改法是让 `insert()` 直接调用 `findImpl()`
  （不过锁的版本），复用 `intersects()` 用的同一个 `isSubset`/`intersects`
  判断闭包，跳过公开的、会重新加锁的入口。
- 没有改 `begin()`/`end()`/`size()`/`empty()`——这几个只读 `tree`，§7 的
  审计已经确认对本项目关心的两个跨域共享实例（`PhysicalMemory::addrMap`、
  `BaseXBar::portMap`）来说，`tree` 的插入/删除只发生在配置期（单线程，
  并行域还没起来），迭代访问和并发写从不重叠，不需要跟着加锁——加了也没
  问题，但不在这次改动范围内，遵照"只改验证需要的地方"的原则。
- 锁序：`AddrRangeMap` 内部锁是叶子锁（`findImpl()`/`insert()` 内部只碰
  自己的 `tree`/`cache`，从不回调外部对象），跟 `layerLock` 之间的加锁
  顺序恒定为 `layerLock → AddrRangeMap 自己的锁`（`recvTimingReq` 持有
  `layerLock` 时调用 `findPort()` → `portMap.contains()`），没有反向路径，
  §7.3 的死锁分析原样成立。

**构建验证**：`scons build/X86_MESI_Three_Level/gem5.opt` 全量编译通过，
没有任何警告/报错落在 `addr_range_map.hh` 或它的任意一个调用方
（`PhysicalMemory`、`BaseXBar`、RISC-V PMA checker、`NonCachingSimpleCPU`、
`DmaPort`、`AbstractController`、GPU-compute、SystemC 桥接——§7 表格的
全部 8 个实例化）。

（题外的一次运维事故，跟这次改动本身无关，如实记一笔：第一次
`scons ... -j$(nproc)`（112）没有 `taskset`，把 `isolcpus=54-55,92-111`
隔离核也占满了，还撞上这台宿主机在高并发下"名义 112 核、实际交付吞吐
远低于此"的情况，导致交互终端卡到一秒一个字符——杀掉重开，改成
`taskset -c 0-47 ... -j16`，之后所有编译/测试都保持这个习惯：显式避开
`54-55,92-111`，`-j` 不用 `nproc`。）

## 11. 验证：正确性 + TSan A/B

沿用 S-009 §24.3/§24.5 的两阶段方法论——先小窗口correctness-only 探测，
确认没有死锁/崩溃/结果分歧，再扩时长找 TSan 报告。两阶段都用同一个检查点
`/workspace/gem5-ckpt/x86-threads3-roi-classic`，同一个驱动脚本
`docs/refs/scripts/x86_fs_mesi3_parallel_eventq.py`，`isolcpus` 隔离核
（serial→54/55，spin→92-99/100-107，跟 S-009 §24.3/24.5 完全同一套核分配）。

### 11.1 小窗口（MAX_TICKS=2e8，serial + spin 各一次，TSan build）

| 模式 | hostSeconds | simInsts |
|---|---:|---:|
| serial | 14.94 | 74062 |
| spin | 205.15 | 74062 |

`diff` 两份 `stats.txt`（排除宿主计时字段）**逐字节相同**，`simInsts` 跟
S-009 §24.3 记录的加锁前/后数值（74062）一致——加了 `cacheLock` 之后
Q=300 这个工作点上仍然时序中性。TSan 报了 2 次警告，都是 S-009 §24.4
记录的既有 `EventQueue::_curTick` 竞争（`eventq.hh:875`），**跟这次改动
无关**——`addr_range_map.hh`/`xbar.hh`（`portMap`）/`cacheLock` 一次都没
出现在报告里。

（进程的 `exit code 66`不是崩溃——是 TSan 检测到竞争后的默认退出码
惯例，S-009 §24.4 已经记录过这个行为；`run.log`/`stats.txt` 都确认仿真
本身正常跑满整个窗口才退出。）

### 11.2 扩时长（MAX_TICKS=1.3e9，serial×2+spin×2，同时跑在不同隔离核上）

跟 S-009 §24.5 同一个协议：

| 运行 | hostSeconds | simInsts | TSan 警告数 |
|---|---:|---:|---:|
| serial round1（核 54） | 154.07 | 926690 | 0 |
| serial round2（核 55） | 156.46 | 926690 | 0 |
| spin round1（核 92-99） | 1138.35 | 926690 | 37 |
| spin round2（核 100-107） | 641.58 | 926690 | 39 |

**正确性**：四份 `stats.txt`（排除宿主计时字段）两两 `diff`——serial r1
vs r2、spin r1 vs r2、serial r1 vs spin r1、serial r2 vs spin r2——**全部
逐字节相同**，`simInsts` 四份都是 926690，跟 S-009 §24.5 加锁前的数值
（同样是 926690）一致，没有死锁、没有崩溃、没有超时。

（spin round1/round2 的 `hostSeconds` 差了近一倍——1138s vs 642s——比
S-009 §24.5 记录的同类噪声（982s/961s，差 ~2%）大得多。这次没有像
S-009 §24.5 那样让 4 个任务从一开始就完全同时起跑（TSan 重编译 + 小窗口
探测占用了一部分时间错峰），加上这次是单独一台宿主机在跑，具体是否有
其他噪声源没有进一步拆解——**只如实记录量级，不当作性能结论**，跟 §9
里"热路径开销未测量"这个开放项是分开的两件事，不要混着看。）

**竞争扫描**：对两份 spin TSan 日志分别提取每一次报告的 `#0`
帧（真正发生读写竞争的代码位置，不是调用栈里路过的中间帧）：

```
gem5::EventQueue::getCurTick() const        src/sim/eventq.hh:875
gem5::EventQueue::setCurTick(unsigned long) src/sim/eventq.hh:862
gem5::Flags<unsigned short>::isSet(...)     src/base/flags.hh:83
gem5::Flags<unsigned short>::set(...)       src/base/flags.hh:116
gem5::ruby::Consumer::lock()                src/mem/ruby/common/Consumer.cc:217/222
gem5::ruby::Consumer::unlock()              src/mem/ruby/common/Consumer.cc:231
```

两份日志的全部 76 次警告，`#0` 帧只落在这 6 个位置——跟 S-009 §24.4/24.5
记录的既有背景噪声（`_curTick`、`Event::Flags`、Ruby 自己的
`Consumer::lock()`/`unlock()`）完全对应，**没有一次的 `#0` 帧落在
`AddrRangeMap::findImpl`/`addNewEntryToCache`，也没有一次落在
`BaseXBar` 的 `reqLayers`/`respLayers`/`routeTo` 处理代码里**。

`noncoherent_xbar.cc:171`（`NoncoherentXBar::recvTimingReq`）在 spin
round2 的一份报告里作为**调用栈中间帧**出现过一次——检查完整调用栈确认
它只是恰好调用了 `Layer::succeededTiming → occupyLayer → EventManager::
schedule → EventQueue::getCurTick()` 这条通用路径，实际竞争双方是
`_curTick` 字段，不是 `NoncoherentXBar` 自己的任何状态，跟 S-009 §24.5
记录的"`flags.hh:116` 报告里 `NoncoherentXBar` 只是路过"是同一个模式。

### 11.3 结论

`PhysicalMemory::addrMap` 和 `BaseXBar::portMap` 这两个 §1/§7.1-7.2
确认的跨域竞争，在加了 `cacheLock` 之后，**76 次 TSan 报告里一次都没有
再出现**，四份 1.3e9-tick 窗口的 `stats.txt` 逐字节相同——修法本身
（§10）在正确性上验证通过。**剩下唯一没做的验证项是热路径开销**（§9
原文列的问题，非 TSan opt build 下 `contains()`/`insert()` 多一次锁
获取对"每次访存"级别调用频率的实际影响，还没有测过），这是下一步。
