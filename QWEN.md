# QWEN.md — gem5 research fork

## First read

- `CLAUDE.md` is the primary instruction file (249 lines covering architecture, branch strategy, build, test, formatting, measurement). Read it first.
- `docs/decisions/0001-fork-branching-strategy.md` — full rationale for the branch+worktree workflow below.

## This repo

gem5 (computer architecture simulator). This fork's project: **parallel EventQueue** for multi-core Ruby speedup using the **parti-gem5 approach**.

---

## Core Concepts

### What is gem5?

gem5 is an open-source computer system architecture simulator used for system architecture research. It supports multiple ISAs (X86, ARM, RISC-V, etc.), multiple memory systems ("classic" caches in `src/mem/` and detailed Ruby coherence protocol simulator in `src/mem/ruby/`), and diverse workload configurations from simple SE (simple emulation) mode to full-system (FS) OS boot.

### Research Fork Focus

Split the single global EventQueue into per-cache-domain queues running on separate host threads, synchronized by quantum barriers. This follows the **parti-gem5** approach: relax cross-domain timing constraints intentionally to achieve true parallelism while quantifying and measuring the precision trade-off.

**Primary research goal**: Measure real wall-clock speedup over current serial gem5 when running multi-core Ruby simulations, using three-arm methodology.

---

## Build System (SCons)

```bash
scons build/<ISA>/gem5.opt -j<N>              # e.g. X86, ARM, NULL, X86_MESI_Three_Level
scons build/ALL/unittests.opt                  # C++ unit tests
```

- Builds are self-contained per ISA in `build/`
- Configuration via Kconfig-style `CONF` dict in SCons (enabled with `rsource` keyword)
- Generated code: `.isa` parsers, SLICC `.sm` state machines, SimObject pybind11 bindings

### Key Build Flags

| Flag | Effect |
|------|--------|
| `PARALLEL_EVENTQ=1` | Enable multi-queue parallel simulation (per-domain EventQueues) |
| `PARALLEL_EVENTQ=0` | Use serial single-EventQueue mode |

---

## Two Memory Systems — Know Which One You're In

| System | Location | Description |
|--------|----------|-------------|
| **Classic** | `src/mem/` | Traditional gem5 caches/coherence |
| **Ruby** | `src/mem/ruby/` | Separate, detailed protocol simulator |

Check which one a config/test uses before assuming a fix applies to both.

---

## Testing Methodology

### Three-Arm Measurement (Never Two)

1. **Baseline** — pre-fork commit (unchanged)
2. **Current Serial** — `PARALLEL_EVENTQ=0`
3. **Current Parallel** — `PARALLEL_EVENTQ=1`

Derive:
- `overhead_ratio = baseline / serial`
- `real_speedup = baseline / parallel`
- `internal_speedup = serial / parallel`

### Test Categories

| Layer | Command |
|---|---|---|
| C++ unit tests (GoogleTest) | `scons build/ALL/unittests.opt` → `./build/ALL/base/bitunion.test.opt --gtest_filter=...` |
| Python unit tests | `./build/ALL/gem5.opt tests/run_pyunit.py` |
| System-level (quick) | `cd tests && ./main.py run` |
| System-level (long, very-long) | `./main.py run --length={long|very-long}` |
| Rerun failed | `./main.py rerun` |

---

## Development Workflow

### Git Branching

- **`origin = pkwxl/gem5-gem5-parq`** (read-write remote)
- **`upstream = gem5/gem5`** (read-only upstream)

Branch strategy:
- `main` — research trunk (current fork state)
- `sNNN-slug` branches — individual investigations off `main`
- Never rebase/force-push research branches
- Merge with `--no-ff`, never squash

### Worktrees and Builds

```bash
# Create new worktree for a branch
cd /workspace
git worktree add gem5-wt/<branch> <branch>

# Build in tmpfs (volatile)
/workspace/shm/gem5/<branch>/build/
```

Paths used in this sandbox:
- Worktrees: `/workspace/gem5-wt/`
- tmpfs builds: `/workspace/shm/gem5/<branch>/build/`
- Checkpoint output: `-d /tmp/<something>` (never let `m5out` land under repo)

### CPU Isolation

Current isolation plan:
- **Serial** / **Parallel** arm cores: single source of truth is `util/roles/reserved-cores`
  (`SERIAL_ARM_CPUS` / `PARALLEL_ARM_CPUS`; builds go to `BUILD_CPUS`). Never hard-code them.
- Before occupying them, run the `check-cores` skill — container `ps` cannot see other
  containers' jobs pinned to those cores.
- Build uses: `0-53,56-91` (excluding isolation set)

Use `taskset -c <cpu-list>` for builds.

---

## Critical Gotchas

| Issue | Impact | Mitigation |
|-------|--------|------------|
| **No SIGUSR1/SIGUSR2** to parallel EventQueue process | Segfault | Kill with SIGTERM/SIGINT only |
| Checkpoints under `m5out/` repo path | Loss on rebuild/restart | Always use `-d /tmp/<name>` |
| Quantum grid arithmetic errors | Assert crash in `occupyLayer`, `schedSendEvent`, `BridgeBase` | Use `crossDomainSnap()` at all cross-domain schedule points |

---

## Key Files and Directories

| Path | Purpose |
|------|---------|
| `CLAUDE.md` | Primary instruction file for research fork |
| `AGENTS.md` | Instruction file for OpenCode sessions |
| `KCONFIG.md` | Kconfig-style configuration system |
| `TESTING.md` | Testing infrastructure documentation |
| `docs/specs/INDEX.md` | Research specs index (S-001 to S-018+) |
| `src/mem/ruby/` | Ruby memory system (parallel EventQueue focus) |
| `src/sim/eventq.cc`, `eventq.hh` | EventQueue implementation |

---

## Current Research Status

### Completed Work

| Spec | Status | Description |
|------|--------|-------------|
| S-001 | Design | parti-gem5 proposal, topology, lock model, open issues |
| S-002 | Implemented+validated | Per-consumer shared wakeup mutex, 15% serial overhead |
| S-003 | Fixed | First multithreaded Ruby crashes, Consumer fix |
| S-004 | SE route stopped at 9.9x | SE天花板: 125x→1x via CPU入域/Ruby加锁/ASan bug fixes |
| S-005 | Implemented+A/B | Host thread CPU affinity (PDES scheduling discipline) |
| S-006 | APIC wall fixed | FS migration, quantum grid anchor bug, checkpoint restore |
| S-007 | **Milestone** | Spin barrier mode: serial/spin 1.41x in SE+FS |
| S-008 | Measurement done | FS current work point: parallel at 0.33× serial (~3× slower) |
| S-009 | **Implemented+validated** | Raise quantum past IO bus edge → Q=6660, measured ~0.91x with short window |
| S-010 | Implemented+TSan validated | `AddrRangeMap` LRU cache race fix+verification |
| S-011 | Implemented+verified | Consumer lock owner field race → atomic<EventQueue*> |
| S-012 | Fully implemented (Step 1-5) | Critical path instrumentation; discovered domain imbalance |
| S-013 | Partial positive result | Balanced checkpoint created, but domain 2 persistently low (unexplained) |
| S-014 | Implemented+verified | `occupyLayer` snap fix; long window exposed gaps in prior validation |
| S-015 | Implemented+merged | `PacketQueue::retry()` cross-domain race → pqLock + pseudo-retry tolerance |

### Active / In Progress

| Spec | Status | Notes |
|------|--------|-------|
| S-016 | Fixed+merged | Serial hang in `NoncoherentXBar` reentrancy (layerLock self-deadlock) |
| S-017 | Spec drafted, not run | Three-arm hostSeconds on balanced workload (pending execution) |
| S-018 | Pending body | O3 CPU restore of balanced checkpoint |

---

## Performance Notes

### Quantum Settings

Current experimental values from S-009/S-014:
| Parameter | Value | Notes |
|-----------|-------|-------|
| `SIM_QUANTUM_TICKS` | 6660 | Quantum boundary (grid anchor) |
| Short window (original validation) | 2e8 ticks | Only 74588 instructions — likely still in guest startup phase |
| Long window (verified fix) | 1.3e9 ticks | Multi-billion tick runs for full validation |

### Observed Metrics

- **Serial overhead**: ~15% vs baseline SCons build
- **S-012 Step 5 (balanced workload)**: 8 domains share quantum finish (6.9%-16.6% each), lock wait <0.15%
- **S-014/S-015 long-window fix**: No more `assert(when >= getCurTick())` or ` PacketQueue::sendDeferredPacket()` crashes

---

## Reference: Research Specs Index

See `docs/specs/INDEX.md` for full spec details. Summary:

- **S-001 to S-004**: Foundation, first measurements, SE mode天花板
- **S-005 to S-007**: Host thread affinity, FS migration, milestone达成 (spin barrier 1.41x)
- **S-008 to S-011**: Root cause analysis & fixes: quantum too small, IO bus edge, AddrRangeMap/Consumer races
- **S-012 to S-018**: Performance measurement infrastructure, domain imbalance investigation, long-window validation,serial hang fix

---

## Common Commands Reference

```bash
# Build a specific ISA with optimisation
scons build/X86_MESI_Three_Level/gem5.opt -j12

# Run quick system tests
cd tests && ./main.py run

# Compile C++ unit tests and run one set
scons build/ALL/base/bitunion.test.opt
./build/ALL/base/bitunion.test.opt --gtest_filter=BitUnionData.NormalBitfield

# Python unit tests
./build/ALL/gem5.opt tests/run_pyunit.py

# Run with quantum override (for experimentation)
./build/X86_MESI_Three_Level/gem5.opt configs/example/se.py \
    --parallel-eventq-sim-quantum=10000
```

---

## How to Add New Research Specs

1. **Add index entry** in `docs/specs/INDEX.md` (next S-NNN)
2. **Create spec file**: `docs/specs/S-NNN-slug.md`
3. **Document**: problem, design, implementation, validation method, results
4. **Commit to main**: merge with `--no-ff`, never squash

See existing specs for template format.

---

## Notes for AI Assistants

- This is a research fork — focus is on parallel EventQueue via parti-gem5
- All work uses the three-arm methodology (never compare two points alone)
- Cross-domain timing is relaxed intentionally but must be quantified
- Use `crossDomainSnap()` at all cross-domain schedule entry points
- Never modify checkpoints under repo path (`m5out/`)
- Never send SIGUSR1/SIGUSR2 to parallel EventQueue processes

---

**Last updated**: 2026-07-21 based on S-001 through S-018 specs and current codebase state.
