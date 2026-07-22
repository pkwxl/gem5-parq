#!/usr/bin/env python3
"""按核采样 /proc/stat，判定保留核当前是否真的空闲。

数值全部来自 `util/roles/reserved-cores`（决策 0004 的单点定义），本脚本不含
任何硬编码核号。用法：

    python3 .claude/skills/check-cores/sample-cores.py [--seconds N] [--json]

为什么是采样而不是查进程：容器内 `ps` 只看得见本容器的进程，宿主机和别的容器
把任务绑到隔离核上时完全不可见。而 `/proc/stat` **没有被 PID namespace 隔离**，
从容器里读到的是宿主机全部逻辑核的累计时间——这正是我们需要的证据面。

代价（必须连同结论一起报告，不要省略）：占用率是**证据**不是**归属**——它能
说明"这个核上有人在跑"，但说不出是谁；反过来，此刻空闲也不保证下一秒仍空闲。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

CONF_REL = "util/roles/reserved-cores"


def repo_root() -> Path:
    # .claude/skills/check-cores/sample-cores.py → 仓库根
    return Path(__file__).resolve().parents[3]


def load_conf() -> dict[str, str]:
    path = repo_root() / CONF_REL
    conf: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, _, v = line.partition("=")
            conf[k.strip()] = v.strip().strip("\"'")
    missing = {"SERIAL_ARM_CPUS", "PARALLEL_ARM_CPUS"} - conf.keys()
    if missing:
        sys.exit(f"{path} 缺少键：{sorted(missing)}")
    return conf


def expand(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return sorted(set(out))


def snapshot() -> dict[int, tuple[int, int]]:
    """每个逻辑核 → (总 jiffies, 空闲 jiffies)。空闲 = idle + iowait。

    steal（被 hypervisor 抢走）**算忙**：那段时间同样拿不到 CPU，对 A/B 计时的
    污染和别人在跑没有区别。
    """
    out: dict[int, tuple[int, int]] = {}
    for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
        if not line.startswith("cpu") or line.startswith("cpu "):
            continue
        head, *rest = line.split()
        vals = [int(v) for v in rest]
        total = sum(vals)
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        out[int(head[3:])] = (total, idle)
    return out


def fmt_list(cores: list[int]) -> str:
    """把核号压成 taskset 的 cpu-list 写法：[100,101,102,105] → 100-102,105。"""
    if not cores:
        return "无"
    out, start, prev = [], cores[0], cores[0]
    for c in cores[1:] + [None]:
        if c is not None and c == prev + 1:
            prev = c
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        if c is not None:
            start = prev = c
    return ",".join(out)


def exit_code(result: dict[str, dict]) -> int:
    """0 = 两条臂都全空闲；1 = 至少一条臂一个空闲核都没有；2 = 部分空闲。"""
    if all(r["idle"] for r in result.values()):
        return 0
    if any(r["free_count"] == 0 for r in result.values()):
        return 1
    return 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=None, help="采样时长，默认取配置值")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    conf = load_conf()
    secs = args.seconds if args.seconds is not None else float(conf.get("IDLE_SAMPLE_SECONDS", 10))
    thresh = float(conf.get("IDLE_MAX_BUSY_PCT", 0.1))

    arms = [
        ("串行臂", "SERIAL_ARM_CPUS", conf["SERIAL_ARM_CPUS"]),
        ("并行臂", "PARALLEL_ARM_CPUS", conf["PARALLEL_ARM_CPUS"]),
    ]

    a = snapshot()
    time.sleep(secs)
    b = snapshot()

    result: dict[str, dict] = {}
    for label, key, spec in arms:
        cores = {}
        for c in expand(spec):
            if c not in a or c not in b:
                cores[c] = None            # 这个核在 /proc/stat 里不存在
                continue
            dt = b[c][0] - a[c][0]
            di = b[c][1] - a[c][1]
            cores[c] = 100.0 * (dt - di) / dt if dt > 0 else 0.0
        free = sorted(c for c, v in cores.items() if v is not None and v < thresh)
        result[key] = {
            "label": label,
            "spec": spec,
            "cores": cores,
            "free": free,
            "free_count": len(free),
            "idle": len(free) == len(cores),
        }

    if args.json:
        print(json.dumps(
            {"seconds": secs, "threshold_pct": thresh, "conf": CONF_REL, "arms": result},
            ensure_ascii=False, indent=2,
        ))
        return exit_code(result)

    print(f"保留核空闲检查 — 采样 {secs:g}s，阈值 <{thresh}%，数值出处 {CONF_REL}")
    for key, r in result.items():
        print(f"\n{r['label']}  {key}={r['spec']}")
        for c, pct in sorted(r["cores"].items()):
            if pct is None:
                print(f"  cpu{c:<4} —        /proc/stat 里没有这个核（配置与本机不符）")
            else:
                print(f"  cpu{c:<4} {pct:7.3f}%  {'空闲' if pct < thresh else '忙  ← 有人在用'}")
        print(f"  空闲核：{fmt_list(r['free'])}  共 {r['free_count']}/{len(r['cores'])} 个")

    print(
        "\n占用状态怎么用（决策 0004 §3.1）：**忙是粘性的**——测到忙就假定它一直忙，"
        "直到下一次实测到它空闲为止；没测到忙就按不忙处理，不要假设看不见的占用。\n"
        "只要本条臂的空闲核数量满足计划要求，就可以用这些空闲核开跑，**默认不必顾虑**"
        "对同机其他实验的影响（除非计划或用户特别要求不打扰）。空闲核不够则停下走"
        "Checkpoint 2。实际使用的核号必须写进 Checkpoint 1 和 spec；同机有别的实验在跑"
        "时，也要一并记为本次数字的已知扰动源（共享 LLC/内存带宽，绑核不隔离这两样）。\n"
        "另注：占用率是证据不是归属——容器内看不到是谁在用（`ps` 只看得见本容器的进程）。"
    )
    return exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
