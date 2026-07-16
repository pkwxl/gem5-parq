# S-005: 宿主机线程绑核（CPU affinity）

> **状态**：已实现（`Root.eventq_host_cpus`）并做了 A/B 实测——方向正确
> （中位数改善 ~5%，极差近乎减半）但 n=3、区间有重叠，只能算指向性证据。
> 是 [S-007](./S-007-spin-barrier-and-milestone.md) 自旋屏障的前置条件。
> 对应原单体设计文档第 10 节，内容原样保留。

---

## 10. 宿主机线程调度：per-EventQueue 线程绑核（CPU affinity）

### 10.1 动机与规格

用户给出的规格即 PDES 的标准宿主机调度纪律，针对的是 quantum 屏障
同步模型的两个已实测痛点：

1. **木桶效应（straggler）**：每个 quantum 末尾所有线程在全局屏障
   会合（`global_event.cc` 的 `BarrierEvent`），任何一个线程被 OS
   抢占/迁移，其他所有线程都在屏障上空等。我们自己的数据就是证据：
   同一并行 repro 的 wall-clock 曾在 56-620s 间摆动（S-003 §8.7 的 7 连跑），
   当时已用线程级 CPU 采样确认是宿主机争抢、不是死锁。
2. **缓存迁移损耗**：线程被 OS 从物理核 A 挪到 B，L1/L2 热数据全部
   作废。绑核后线程独占一个核的私有缓存。

规格的三件套及其在本沙箱的可行性（实测）：

| 措施 | 可行性 |
|------|--------|
| 线程绑核（`pthread_setaffinity_np`） | ✅ 可行，`sched_setaffinity` 未被 seccomp 拦截 |
| 避免超配（线程数 ≤ 宿主硬件线程数） | ✅ 6-8 线程 vs 112 物理核，天然满足；另加了告警 |
| 核隔离（`isolcpus`/`nohz_full`） | ❌ 宿主启动参数，容器内无法设置 |
| 避免 HT 兄弟核互抢 | 不适用——本宿主 `Thread(s) per core: 1`，无超线程 |
| NUMA 绑定 | 部分可行：可选同一 node 的核（node0=0-55, node1=56-111）；`numactl` 未安装 |

关键局限要如实记录：**绑核只约束我们自己的线程落在哪，不能阻止
其他容器的负载落到同一批核上**（没有 isolcpus 就没有"独占"）。
本宿主 loadavg 高且波动大，所以合理预期是**方差收窄**为主、
中位数改善为辅。

### 10.2 实现（完全镜像 sim_quantum 的参数管线）

- `Root.eventq_host_cpus`（`VectorParam.Int`，默认空 = 不绑核，
  `src/sim/Root.py`）→ `root.cc` 构造函数写入全局
  `eventqHostCpus`（`eventq.hh/.cc`，紧挨 `simQuantum`）。
- `SimulatorThreads::runUntilLocalExit()`（`simulate.cc`）在
  spawn 从属线程处绑核：queue i 的线程绑 `eventqHostCpus[i]`
  （`threads.back().native_handle()`）；**主线程（驱动 queue 0、
  同样参与每个 quantum 屏障）也绑**（`pthread_self()`）。
  `#if defined(__linux__)` 保护；绑失败只 `warn`（目标核可能在
  容器 cpuset 之外），成功打 `inform`。
- 校验：列表非空但长度 < EventQueue 数 → `fatal`；EventQueue 数
  超过宿主硬件线程数 → `warn`（超配告警，与绑核无关也生效）。
- 默认空列表时**一行为都不改变**——基线中性是结构性的。
- 配置入口：`se.py --eventq-host-cpus=0,1,...`（在
  `--parallel-l2-eventq` 块内）；FS 脚本
  `x86_fs_mesi3_parallel_eventq.py` 用 `HOST_PIN_CPUS` 环境变量
  （沿用该脚本 `PARALLEL_EVENTQ`/`SIM_QUANTUM_TICKS` 的风格）。

### 10.3 复验与 A/B 实测

- **串行基线逐字节不变**：`simTicks=31555788000`（参数默认空，
  不触碰任何路径）。
- **绑核生效确认**：pinned 各跑的日志均有 6/6 条
  `eventq N: pinned thread to host CPU M` inform。
- **A/B**（SE repro，Q=500 即 `--sim-quantum=0.5ns`，
  `--abs-max-tick=2e9`，6 线程，ABAB 交错消load漂移，绑
  node0 的空闲核 8-13；所有跑均正确退出在 tick 2e9）：

| 组 | wall-clock（3 跑） | 中位数 | 极差 |
|----|--------------------|--------|------|
| 不绑核 | 96.8 / 98.9 / 127.0 s | 98.9 s | 30.2 s |
| 绑核 8-13 | 85.8 / 93.9 / 103.2 s | 93.9 s | 17.4 s |

**如实解读**：中位数改善 ~5%，极差几乎减半——方向与预期一致
（方差收窄为主），但 n=3、两组区间有重叠（绑核最慢 103.2s >
不绑核最快 96.8s），只能算**指向性证据，不算结论**。共享宿主上
别的容器仍可落在同一批核上，这是没有 isolcpus 的结构性天花板。
真机部署指引（isolcpus + nohz_full + 物理核 1:1 + 同 NUMA node +
numactl 绑内存）记录于此，供有独占宿主时复测。

### 10.4 与自旋屏障的关系

绑核是 S-004 §9.3 出路 2 / S-004 §9.4 候选出路 3（自旋屏障替换 cv 屏障，
~21-26µs/quantum → 期望 ~2µs）的**前置条件**：自旋 + 超配/被抢占
= 灾难（自旋线程烧掉被等线程需要的核），绑核 + 无超线程 + 核数
富余则自旋是安全的。本节落地后，自旋屏障成为已解锁的下一步。


---

**上一篇**：[S-004：第一次真实加速比测量](./S-004-first-speedup-measurement-and-fixes.md)
**下一篇**：[S-006：迁移到 FS 模式](./S-006-fs-mode-migration.md)
**返回**：[INDEX.md](./INDEX.md)
