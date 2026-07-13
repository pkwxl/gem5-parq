"""
Best-case multi-eventq experiment: N fully independent {CPU, private L1I/D,
private membus, private MemCtrl, private SE process} subsystems that never
talk to each other (disjoint SimObject graphs, disjoint physical memory).
This is the "keep local events local, there is nothing to dispatch" end of
the spectrum -- used to measure the ceiling of gem5's multi-eventq
mechanism when cross-queue traffic is truly zero, for comparison against
the coherent-shared-memory case in parallel_bench.py.

Build the workload binary first (statically linked, same arch as the host/
gem5.opt build): `gcc -O1 -static -o work work.c`

Usage:
  gem5.opt parallel_bench_independent.py [--num-sys N] [--n SIZE]
      [--reps R] [--parallel] [--sim-quantum TIME] [--binary PATH]
"""

import argparse
import os
import sys

import m5
from m5.objects import *
from m5.ticks import fromSeconds

parser = argparse.ArgumentParser()
parser.add_argument("--num-sys", type=int, default=4)
parser.add_argument("--n", type=int, default=4096, help="array size for work")
parser.add_argument("--reps", type=int, default=100, help="sweeps over the array")
parser.add_argument(
    "--parallel",
    action="store_true",
    help="Give each independent system its own EventQueue/thread.",
)
parser.add_argument("--sim-quantum", type=str, default="1us")
parser.add_argument(
    "--binary",
    type=str,
    default=os.path.join(os.path.dirname(os.path.realpath(__file__)), "work"),
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


root = Root(full_system=False)
systems = []

for i in range(args.num_sys):
    s = System()
    s.clk_domain = SrcClockDomain()
    s.clk_domain.clock = "2GHz"
    s.clk_domain.voltage_domain = VoltageDomain()
    s.mem_mode = "timing"
    s.mem_ranges = [AddrRange("256MiB")]

    s.cpu = X86TimingSimpleCPU()
    s.cpu.icache = L1Cache()
    s.cpu.dcache = L1Cache()
    s.cpu.icache.cpu_side = s.cpu.icache_port
    s.cpu.dcache.cpu_side = s.cpu.dcache_port

    s.membus = SystemXBar()
    s.cpu.icache.mem_side = s.membus.cpu_side_ports
    s.cpu.dcache.mem_side = s.membus.cpu_side_ports

    s.cpu.createInterruptController()
    s.cpu.interrupts[0].pio = s.membus.mem_side_ports
    s.cpu.interrupts[0].int_requestor = s.membus.cpu_side_ports
    s.cpu.interrupts[0].int_responder = s.membus.mem_side_ports

    s.system_port = s.membus.cpu_side_ports

    s.mem_ctrl = MemCtrl()
    s.mem_ctrl.dram = DDR3_1600_8x8()
    s.mem_ctrl.dram.range = s.mem_ranges[0]
    s.mem_ctrl.port = s.membus.mem_side_ports

    process = Process()
    process.cmd = [args.binary, str(args.n), str(args.reps)]
    s.cpu.workload = process
    s.cpu.createThreads()
    s.workload = SEWorkload.init_compatible(args.binary)

    if args.parallel:
        # Every child inherits eventq_index from its parent by default
        # (Param.UInt32(Parent.eventq_index, ...)), so setting it once here
        # is enough to move this whole independent subsystem to its own
        # queue/thread.
        s.eventq_index = i

    systems.append(s)

root.systems = systems

if args.parallel:
    m5.ticks.fixGlobalFrequency()
    root.sim_quantum = fromSeconds(m5.util.convert.anyToLatency(args.sim_quantum))

m5.instantiate()

print(
    f"Beginning simulation! num_sys={args.num_sys} n={args.n} reps={args.reps} "
    f"parallel={args.parallel} "
    f"sim_quantum={args.sim_quantum if args.parallel else 'n/a'}"
)
exit_event = m5.simulate()
print(f"Exiting @ tick {m5.curTick()} because {exit_event.getCause()}")
