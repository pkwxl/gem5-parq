"""
Hybrid-fidelity multi-eventq experiment: each CPU + its private L1+L2 stays
in full timing mode on its own EventQueue; the only thing shared across
CPUs -- one LLC + one MemCtrl -- lives on EventQueue 0 and is reached
through mem.ThreadBridge, gem5's official (and only) supported way to cross
an EventQueue boundary in the classic memory system. ThreadBridge only
implements recvAtomic/recvFunctional; recvTimingReq panics by design. This
script exists to answer empirically: can a CPU's L2 (running in timing
mode) actually be pointed at a ThreadBridge at all, or does the very first
miss that reaches the shared LLC panic immediately?

Usage:
  gem5.opt parallel_bench_hybrid.py [--num-cpus N] [--num-values N]
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
parser.add_argument("--num-cpus", type=int, default=2)
parser.add_argument("--num-values", type=int, default=1000)
parser.add_argument("--parallel", action="store_true")
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
    size = "256KiB"


class LLCache(Cache):
    assoc = 16
    tag_latency = 30
    data_latency = 30
    response_latency = 30
    mshrs = 32
    tgts_per_mshr = 12
    size = "2MiB"


system = System()
system.clk_domain = SrcClockDomain()
system.clk_domain.clock = "2GHz"
system.clk_domain.voltage_domain = VoltageDomain()
system.mem_mode = "timing"
system.mem_ranges = [AddrRange("512MiB")]

system.cpu = [X86TimingSimpleCPU() for _ in range(args.num_cpus)]

# Shared LLC + membus + mem_ctrl: EventQueue 0.
system.membus = SystemXBar()
system.llcbus = L2XBar()  # fan-in: llc.cpu_side is a single (non-vector) port
system.llc = LLCache()
system.llcbus.mem_side_ports = system.llc.cpu_side
system.llc.mem_side = system.membus.cpu_side_ports
system.mem_ctrl = MemCtrl()
system.mem_ctrl.dram = DDR3_1600_8x8()
system.mem_ctrl.dram.range = system.mem_ranges[0]
system.mem_ctrl.port = system.membus.mem_side_ports
system.system_port = system.membus.cpu_side_ports

system.bridge = [ThreadBridge() for _ in range(args.num_cpus)]

for i, cpu in enumerate(system.cpu):
    cpu.icache = L1Cache()
    cpu.dcache = L1Cache()
    cpu.icache.cpu_side = cpu.icache_port
    cpu.dcache.cpu_side = cpu.dcache_port

    cpu.l2bus = L2XBar()
    cpu.icache.mem_side = cpu.l2bus.cpu_side_ports
    cpu.dcache.mem_side = cpu.l2bus.cpu_side_ports

    cpu.l2cache = L2Cache()
    cpu.l2cache.cpu_side = cpu.l2bus.mem_side_ports

    # Private hierarchy -> ThreadBridge (living on the LLC's queue) -> LLC.
    cpu.l2cache.mem_side = system.bridge[i].in_port
    system.bridge[i].out_port = system.llcbus.cpu_side_ports

    cpu.createInterruptController()
    cpu.interrupts[0].pio = system.membus.mem_side_ports
    cpu.interrupts[0].int_requestor = system.membus.cpu_side_ports
    cpu.interrupts[0].int_responder = system.membus.mem_side_ports

    if args.parallel:
        cpu.eventq_index = i + 1
        cpu.icache.eventq_index = i + 1
        cpu.dcache.eventq_index = i + 1
        cpu.l2bus.eventq_index = i + 1
        cpu.l2cache.eventq_index = i + 1
        # bridge stays on queue 0 (same as the LLC it reaches into), per
        # the ThreadBridge docstring's own example.

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
