# Copyright (c) 2012-2013 ARM Limited
# All rights reserved.
#
# The license below extends only to copyright in the software and shall
# not be construed as granting a license to any other intellectual
# property including but not limited to intellectual property relating
# to a hardware implementation of the functionality of the software
# licensed hereunder.  You may use the software subject to the license
# terms below provided that you ensure that this notice is replicated
# unmodified and in its entirety in all distributions of the software,
# modified or unmodified, in source code or in binary form.
#
# Copyright (c) 2006-2008 The Regents of The University of Michigan
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met: redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer;
# redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution;
# neither the name of the copyright holders nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

# Simple test script
#
# "m5 test.py"

import argparse
import os
import sys

import m5
import m5.ticks
from m5.defines import buildEnv
from m5.objects import *
from m5.params import NULL
from m5.util import (
    addToPath,
    fatal,
    warn,
)

from gem5.isas import ISA

addToPath("../../")

from common import (
    CacheConfig,
    CpuConfig,
    MemConfig,
    ObjectList,
    Options,
    Simulation,
)
from common.Caches import *
from common.cpu2000 import *
from common.FileSystemConfig import config_filesystem
from ruby import Ruby


def get_processes(args):
    """Interprets provided args and returns a list of processes"""

    multiprocesses = []
    inputs = []
    outputs = []
    errouts = []
    pargs = []

    workloads = args.cmd.split(";")
    if args.input != "":
        inputs = args.input.split(";")
    if args.output != "":
        outputs = args.output.split(";")
    if args.errout != "":
        errouts = args.errout.split(";")
    if args.options != "":
        pargs = args.options.split(";")

    idx = 0
    for wrkld in workloads:
        process = Process(pid=100 + idx)
        process.executable = wrkld
        process.cwd = os.getcwd()
        process.gid = os.getgid()

        if args.env:
            with open(args.env) as f:
                process.env = [line.rstrip() for line in f]

        if len(pargs) > idx:
            process.cmd = [wrkld] + pargs[idx].split()
        else:
            process.cmd = [wrkld]

        if len(inputs) > idx:
            process.input = inputs[idx]
        if len(outputs) > idx:
            process.output = outputs[idx]
        if len(errouts) > idx:
            process.errout = errouts[idx]

        multiprocesses.append(process)
        idx += 1

    if args.smt:
        cpu_type = ObjectList.cpu_list.get(args.cpu_type)
        assert ObjectList.is_o3_cpu(cpu_type), "SMT requires an O3CPU"
        return multiprocesses, idx
    else:
        return multiprocesses, 1


warn(
    "The se.py script is deprecated. It will be removed in future releases of "
    " gem5."
)

parser = argparse.ArgumentParser()
Options.addCommonOptions(parser)
Options.addSEOptions(parser)

if "--ruby" in sys.argv:
    Ruby.define_options(parser)

    # EXPERIMENTAL: see docs/specs/parallel-eventq-lockfree-l2-design.md.
    # Splits a MESI_Two_Level + Crossbar SE-mode system into one EventQueue
    # per L1 plus one shared EventQueue for L2/directory/memory, following
    # the parti-gem5 N+1-thread partitioning. Only "option 2" from the
    # design doc (reuse Crossbar.py, give the shared xbar router its own
    # extra EventQueue) is implemented so far.
    parser.add_argument(
        "--parallel-l2-eventq",
        action="store_true",
        help="Run with per-L1 + shared-L2 EventQueues instead of one "
        "single EventQueue. Requires --ruby --topology=Crossbar "
        "--num-l2caches=1 --num-dirs=1 and the MESI_Two_Level protocol.",
    )
    parser.add_argument(
        "--sim-quantum",
        type=str,
        default="10us",
        help="Simulation quantum for --parallel-l2-eventq PDES mode. "
        "Default: %(default)s",
    )
    parser.add_argument(
        "--eventq-host-cpus",
        type=str,
        default=None,
        help="Comma-separated host CPU ids to pin the per-EventQueue "
        "simulation threads to (index i pins the thread driving event "
        "queue i), e.g. 0,1,2,3,4,5. Requires --parallel-l2-eventq.",
    )
    parser.add_argument(
        "--eventq-barrier-mode",
        type=str,
        default="cv",
        choices=["cv", "spin", "hybrid"],
        help="Per-quantum global barrier mechanism for --parallel-l2-eventq. "
        "'cv' (default) uses the condition-variable barrier; 'spin' busy-waits "
        "(only sensible with --eventq-host-cpus pinning); 'hybrid' spins "
        "--eventq-barrier-spin-iters times then sleeps.",
    )
    parser.add_argument(
        "--eventq-barrier-spin-iters",
        type=int,
        default=0,
        help="Hybrid barrier: spin iterations before falling back to the "
        "condition variable.",
    )

args = parser.parse_args()

multiprocesses = []
numThreads = 1

if args.bench:
    apps = args.bench.split("-")
    if len(apps) != args.num_cpus:
        print("number of benchmarks not equal to set num_cpus!")
        sys.exit(1)

    for app in apps:
        try:
            if ObjectList.cpu_list.get_isa(args.cpu_type) == ISA.ARM:
                exec(
                    "workload = %s('arm_%s', 'linux', '%s')"
                    % (app, args.arm_iset, args.spec_input)
                )
            else:
                # TARGET_ISA has been removed, but this is missing a ], so it
                # has incorrect syntax and wasn't being used anyway.
                exec(
                    "workload = %s(buildEnv['TARGET_ISA', 'linux', '%s')"
                    % (app, args.spec_input)
                )
            multiprocesses.append(workload.makeProcess())
        except:
            print(
                f"Unable to find workload for ISA: {app}",
                file=sys.stderr,
            )
            sys.exit(1)
elif args.cmd:
    multiprocesses, numThreads = get_processes(args)
else:
    print("No workload specified. Exiting!\n", file=sys.stderr)
    sys.exit(1)


(CPUClass, test_mem_mode, FutureClass) = Simulation.setCPUClass(args)
CPUClass.numThreads = numThreads

# Check -- do not allow SMT with multiple CPUs
if args.smt and args.num_cpus > 1:
    fatal("You cannot use SMT with multiple CPUs!")

np = args.num_cpus
mp0_path = multiprocesses[0].executable
system = System(
    cpu=[CPUClass(cpu_id=i) for i in range(np)],
    mem_mode=test_mem_mode,
    mem_ranges=[AddrRange(args.mem_size)],
    cache_line_size=args.cacheline_size,
)

if numThreads > 1:
    system.multi_thread = True

# Create a top-level voltage domain
system.voltage_domain = VoltageDomain(voltage=args.sys_voltage)

# Create a source clock for the system and set the clock period
system.clk_domain = SrcClockDomain(
    clock=args.sys_clock, voltage_domain=system.voltage_domain
)

# Create a CPU voltage domain
system.cpu_voltage_domain = VoltageDomain()

# Create a separate clock domain for the CPUs
system.cpu_clk_domain = SrcClockDomain(
    clock=args.cpu_clock, voltage_domain=system.cpu_voltage_domain
)

# If elastic tracing is enabled, then configure the cpu and attach the elastic
# trace probe
if args.elastic_trace_en:
    CpuConfig.config_etrace(CPUClass, system.cpu, args)

# All cpus belong to a common cpu_clk_domain, therefore running at a common
# frequency.
for cpu in system.cpu:
    cpu.clk_domain = system.cpu_clk_domain

if ObjectList.is_kvm_cpu(CPUClass) or ObjectList.is_kvm_cpu(FutureClass):
    if buildEnv["USE_X86_ISA"]:
        system.kvm_vm = KvmVM()
        system.m5ops_base = max(0xFFFF0000, Addr(args.mem_size).getValue())
        for process in multiprocesses:
            process.useArchPT = True
            process.kvmInSE = True
    else:
        fatal("KvmCPU can only be used in SE mode with x86")

# Sanity check
if args.simpoint_profile:
    if not ObjectList.is_noncaching_cpu(CPUClass):
        fatal("SimPoint/BPProbe should be done with an atomic cpu")
    if np > 1:
        fatal("SimPoint generation not supported with more than one CPUs")

for i in range(np):
    if args.smt:
        system.cpu[i].workload = multiprocesses
    elif len(multiprocesses) == 1:
        system.cpu[i].workload = multiprocesses[0]
    else:
        system.cpu[i].workload = multiprocesses[i]

    if args.simpoint_profile:
        system.cpu[i].addSimPointProbe(args.simpoint_interval)

    if args.checker:
        system.cpu[i].addCheckerCpu()

    if args.bp_type:
        bpClass = ObjectList.bp_list.get(args.bp_type)
        system.cpu[i].branchPred = bpClass()

    if args.indirect_bp_type:
        indirectBPClass = ObjectList.indirect_bp_list.get(
            args.indirect_bp_type
        )
        system.cpu[i].branchPred.indirectBranchPred = indirectBPClass()

    system.cpu[i].createThreads()

if args.ruby:
    Ruby.create_system(args, False, system)
    assert args.num_cpus == len(system.ruby._cpu_ports)

    system.ruby.clk_domain = SrcClockDomain(
        clock=args.ruby_clock, voltage_domain=system.voltage_domain
    )
    for i in range(np):
        ruby_port = system.ruby._cpu_ports[i]

        # Create the interrupt controller and connect its ports to Ruby
        # Note that the interrupt controller is always present but only
        # in x86 does it have message ports that need to be connected
        system.cpu[i].createInterruptController()

        # Connect the cpu's cache ports to Ruby
        ruby_port.connectCpuPorts(system.cpu[i])

    if args.parallel_l2_eventq:
        if (
            args.topology != "Crossbar"
            or args.num_l2caches != 1
            or args.num_dirs != 1
            or buildEnv["PROTOCOL"] != "MESI_Two_Level"
        ):
            fatal(
                "--parallel-l2-eventq only supports the MESI_Two_Level "
                "protocol with --topology=Crossbar --num-l2caches=1 "
                "--num-dirs=1"
            )

        # Domain assignment: one EventQueue per L1 (index == cpu id),
        # one shared EventQueue for L2 + directory + memory, and (since
        # Crossbar.py routes every link through one shared central "xbar"
        # router that can't be pinned to either side, see design doc
        # section 6.3 option 2) one extra EventQueue just for that xbar.
        l2_domain = args.num_cpus
        xbar_domain = args.num_cpus + 1

        domain_of_cntrl = {}
        for i in range(args.num_cpus):
            domain_of_cntrl[getattr(system.ruby, "l1_cntrl%d" % i)] = i
        domain_of_cntrl[system.ruby.l2_cntrl0] = l2_domain
        domain_of_cntrl[system.ruby.dir_cntrl0] = l2_domain
        for mem_ctrl in system.mem_ctrls:
            domain_of_cntrl[mem_ctrl] = l2_domain

        for cntrl, domain in domain_of_cntrl.items():
            for obj in cntrl.descendants():
                obj.eventq_index = domain

        domain_of_router = {}
        for ext_link in system.ruby.network.ext_links:
            domain_of_router[ext_link.int_node] = domain_of_cntrl[
                ext_link.ext_node
            ]
        for router in system.ruby.network.routers:
            router.eventq_index = domain_of_router.get(router, xbar_domain)

        # Each CPU joins its own L1's domain (design doc section 9.6,
        # parti-gem5's original core+L1-per-thread shape). This makes the
        # CPU<->sequencer edges -- both the mandatory-queue enqueue and
        # the port-level response callback, the latter of which bypasses
        # the Consumer lock system entirely and crashed at tick 5.08e9 in
        # section 9.4 -- same-domain, and lifts the exact-mode quantum
        # bound from 1 ruby cycle to the network link latency. The cost:
        # syscall emulation and fault fixup now run concurrently across
        # domains, serialized by Process::seEmulLock.
        for i in range(args.num_cpus):
            for obj in system.cpu[i].descendants():
                obj.eventq_index = i
else:
    MemClass = Simulation.setMemClass(args)
    system.membus = SystemXBar()
    system.system_port = system.membus.cpu_side_ports
    CacheConfig.config_cache(args, system)
    MemConfig.config_mem(args, system)
    config_filesystem(system, args)

system.workload = SEWorkload.init_compatible(mp0_path)

if args.wait_gdb:
    system.workload.wait_for_remote_gdb = True

root = Root(full_system=False, system=system)

if args.ruby and args.parallel_l2_eventq:
    m5.ticks.fixGlobalFrequency()
    quantum = m5.ticks.fromSeconds(
        m5.util.convert.anyToLatency(args.sim_quantum)
    )
    # Correctness precondition (design doc sections 2.5/9.6): with no
    # quantum snap on cross-domain arrival ticks, the quantum must not
    # exceed the minimum cross-domain communication latency. With CPUs
    # assigned to their L1 domains, the only cross-domain edges left in
    # this topology are the Crossbar's internal links (router <-> central
    # xbar), whose latency is --link-latency ruby-clock cycles.
    ruby_period = m5.ticks.fromSeconds(
        m5.util.convert.anyToLatency(args.ruby_clock)
    )
    max_quantum = args.link_latency * ruby_period
    if quantum > max_quantum:
        fatal(
            "--sim-quantum (%d ticks) must not exceed the minimum "
            "cross-domain link latency (--link-latency=%d ruby cycles "
            "= %d ticks)",
            quantum,
            args.link_latency,
            max_quantum,
        )
    root.sim_quantum = quantum
    if args.eventq_host_cpus is not None:
        root.eventq_host_cpus = [
            int(c) for c in args.eventq_host_cpus.split(",")
        ]
    root.eventq_barrier_mode = args.eventq_barrier_mode
    root.eventq_barrier_spin_iters = args.eventq_barrier_spin_iters

Simulation.run(args, root, system, FutureClass)
