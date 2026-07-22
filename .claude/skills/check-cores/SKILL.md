---
name: check-cores
description: 实验开跑前实测保留核（A/B 计时专用的隔离核）当前是否真的空闲。按核采样 /proc/stat 若干秒，逐核给出忙碌率与判定。Experimenter 的 Phase A 必做项；任何要占用保留核的动作之前都应先跑一次。
---

# check-cores — 保留核空闲实测

## 何时跑

- **Experimenter 每次实验的 Phase A 必做**（`docs/roles/experimenter/PROTOCOL.md` §2）。
- 任何角色在准备长时间占核之前，想确认自己不会撞到别人。

## 为什么需要它

容器里的 `ps`/`top` **只看得见本容器的进程**。宿主机或另一个容器把任务绑到我们的
隔离核上时，从这里完全看不见——而这恰恰会静默污染一整轮 A/B 计时。

`/proc/stat` 没有被 PID namespace 隔离：从容器内读到的是**宿主机全部逻辑核**的累计
时间。所以按核采样能看见我们看不见的那些负载。这是本 skill 存在的全部理由。

## 怎么跑

```sh
python3 .claude/skills/check-cores/sample-cores.py            # 用配置里的采样时长
python3 .claude/skills/check-cores/sample-cores.py --seconds 3   # 快速探一眼
python3 .claude/skills/check-cores/sample-cores.py --json      # 机器可读
```

退出码：`0` = 两条臂都空闲；`1` = 至少一条不可用。

核号、采样时长、忙碌率阈值全部来自 `util/roles/reserved-cores`（决策 0004 的单点
定义）。**脚本和本文件都不写具体核号**——换机器只改那一个文件。

## 怎么读结果

- 判定为「空闲」= 该核在整个采样窗口里忙碌率低于 `IDLE_MAX_BUSY_PCT`。本机的保留核
  同时设了 `nohz_full`/`rcu_nocbs`，真空闲时连时钟中断都没有，所以干净的核会是
  实打实的 `0.000%`，而不是一个小的非零值。
- 判定为「忙」时**不要**自行找替代核开跑——把实测数字原样贴进 Checkpoint 1，由用户
  决定是等待、换核还是取消。协议不允许 Experimenter 临场改计划。

## 两条必须连同结论一起说出来的限制

1. **占用率是证据，不是归属**：能证明「有人在用这个核」，但证明不了是谁——本容器
   看不到外面的进程。反过来，一个核显示空闲，也不能证明没有别人**已经把它绑定**
   但此刻恰好没跑。
2. **不保鲜**：检查通过和真正开跑之间有时间差，这个门无法由钩子验证新鲜度。所以
   它是纪律不是机制——`role-gate.py` 不会因为你没跑过它而拦你。
