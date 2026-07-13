"""
Multi-core shared-memory SE-mode benchmark, parametrized to run either as a
single event queue (gem5's normal mode) or as one EventQueue per CPU
(gem5's classic multi-eventq parallel mode), so the two can be compared for
real wall-clock speedup.

Workload: tests/test-progs/threads/bin/x86/linux/threads -- a small
pthread/std::thread program that splits an array-add across
thread::hardware_concurrency() threads (== number of simulated CPUs in SE
mode) and validates the result. Shared, coherent memory across CPUs, so
cross-CPU/cross-queue traffic is real, not synthetic.

NOTE: with --parallel, this reproducibly aborts with
`eventq.hh:759: Assertion 'when >= getCurTick()' failed` at any
--sim-quantum, because classic (non-Ruby, non-KVM) BaseCache/CoherentXBar
port timing has no notion of an eventq boundary. See
../gem5-parallel-eventq-speedup-experiment.md for the full writeup -- the
crash itself is the finding, not a bug in this script.

Usage:
  gem5.opt parallel_bench.py [--num-cpus N] [--num-values N]
                              [--parallel] [--sim-quantum TIME]
"""

import argparse
import os
import sys

import m5
from m5.objects import *
from m5.ticks import fromSeconds

GEM5_ROOT = "/workspace/gem5"
sys.path.append(os.path.join(GEM5_ROOT, "configs"))
from common.FileSystemConfig import config_filesystem

parser = argparse.ArgumentParser()
parser.add_argument("--num-cpus", type=int, default=4)
parser.add_argument("--num-values", type=int, default=200000)
parser.add_argument(
    "--parallel",
    action="store_true",
    help="Give each CPU (+ its private L1s) its own EventQueue/thread. "
    "Shared uncore (L2/membus/mem_ctrl) stays on EventQueue 0.",
)
parser.add_argument("--sim-quantum", type=str, default="1us")
parser.add_argument(
    "--binary",
    type=str,
    default=os.path.join(
        GEM5_ROOT, "tests/test-progs/threads/bin/x86/linux/threads"
    ),
)
args = parser.parse_args()


class L1Cache(Cache):
    assoc = 2
    tag_latency = 2
    data_latency = 2
    response_latency = 2
    mshrs = 4
    tgts_per_mshr = 20
    size = "32KiB"


class L2Cache(Cache):
    assoc = 8
    tag_latency = 12
    data_latency = 12
    response_latency = 12
    mshrs = 20
    tgts_per_mshr = 12
    size = "1MiB"


system = System()
system.clk_domain = SrcClockDomain()
system.clk_domain.clock = "2GHz"
system.clk_domain.voltage_domain = VoltageDomain()

system.mem_mode = "timing"
system.mem_ranges = [AddrRange("512MiB")]

system.cpu = [X86TimingSimpleCPU() for _ in range(args.num_cpus)]

system.l2bus = L2XBar()
system.membus = SystemXBar()

for i, cpu in enumerate(system.cpu):
    cpu.icache = L1Cache()
    cpu.dcache = L1Cache()
    cpu.icache.cpu_side = cpu.icache_port
    cpu.dcache.cpu_side = cpu.dcache_port
    cpu.icache.mem_side = system.l2bus.cpu_side_ports
    cpu.dcache.mem_side = system.l2bus.cpu_side_ports

    cpu.createInterruptController()
    cpu.interrupts[0].pio = system.membus.mem_side_ports
    cpu.interrupts[0].int_requestor = system.membus.cpu_side_ports
    cpu.interrupts[0].int_responder = system.membus.mem_side_ports

    if args.parallel:
        # 0 is reserved for the shared uncore below; each CPU (and its
        # private, never-shared L1s) gets its own queue/OS thread.
        cpu.eventq_index = i + 1
        cpu.icache.eventq_index = i + 1
        cpu.dcache.eventq_index = i + 1

system.l2cache = L2Cache()
system.l2cache.cpu_side = system.l2bus.mem_side_ports
system.l2cache.mem_side = system.membus.cpu_side_ports

system.system_port = system.membus.cpu_side_ports

system.mem_ctrl = MemCtrl()
system.mem_ctrl.dram = DDR3_1600_8x8()
system.mem_ctrl.dram.range = system.mem_ranges[0]
system.mem_ctrl.port = system.membus.mem_side_ports

process = Process()
process.cmd = [args.binary, str(args.num_values)]
for cpu in system.cpu:
    cpu.workload = process
    cpu.createThreads()

system.workload = SEWorkload.init_compatible(args.binary)
config_filesystem(system)

root = Root(full_system=False, system=system)

if args.parallel:
    m5.ticks.fixGlobalFrequency()
    root.sim_quantum = fromSeconds(m5.util.convert.anyToLatency(args.sim_quantum))

m5.instantiate()

print(
    f"Beginning simulation! num_cpus={args.num_cpus} "
    f"num_values={args.num_values} parallel={args.parallel} "
    f"sim_quantum={args.sim_quantum if args.parallel else 'n/a'}"
)
exit_event = m5.simulate()
print(f"Exiting @ tick {m5.curTick()} because {exit_event.getCause()}")
