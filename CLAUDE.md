# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

gem5 is a modular computer-system architecture simulator (processor microarchitecture and full/multi-system
simulation), used in academic and industrial research. The codebase mixes C++ (the simulator core, ~most of
`src/`) with Python (SimObject parameter declarations, the build system, config scripts, and the `gem5`
standard library used to assemble simulations).

## Primary research goal (this fork)

This fork's main ongoing project is speeding up multi-core Ruby simulation by splitting gem5's single
`EventQueue` into one queue per cache/coherence domain (private per-core L1/L2 domains + a shared
LLC/directory domain), run on separate host threads, synchronized by a quantum barrier — following the
parti-gem5 approach (relaxed cross-domain timing, not lock-free structures). Target: real wall-clock speedup
over the existing single-`EventQueue` serial simulator while staying timing-accurate (or with a
well-quantified, deliberate relaxation).

Full design history, decisions, and empirical results live in `docs/specs/INDEX.md` — start there, not with
this file, for anything related to this work. New investigations get their own `docs/specs/S-NNN-slug.md`
(see the index for the numbering convention); don't append to an existing spec file for a new topic.

**Working style for this project** (empirically validated over many sessions, not just a preference):
- Report inconvenient measurements plainly, and write them into the relevant `docs/specs/S-NNN` file
  immediately, in the same session — not just mentioned in chat and left for later. Whenever a result
  contradicts a stated goal or an earlier claim (including one made earlier in the same session), correct
  the doc right away.
- Even after a broad go-ahead ("start implementing X"), pause and offer a checkpoint before starting a new
  sub-phase that's qualitatively riskier than what came before (new architecture territory, first-of-its-kind
  test, likely to need live debugging rather than following an existing plan) — broad authorization for the
  overall task doesn't imply authorization to run through every risky sub-phase unattended.
- Known operational pitfall specific to this project: never send `SIGUSR1`/`SIGUSR2` to a running
  parallel-`EventQueue` gem5 process (async stat dump on a non-main thread without the GIL segfaults it) —
  see `docs/specs/S-007-spin-barrier-and-milestone.md` §14 for the safe way to read a live tick instead.
- Host CPUs `54-55,92-111` are kernel-isolated (`isolcpus=`, see `/proc/cmdline`) for clean A/B timing.
  `54-55` is on NUMA node 0 (node 0 spans 0-55) — pin the serial arm there. `92-111` is on NUMA node 1
  (node 1 spans 56-111) — pin the parallel-spin arm there.
- `54-55` is reserved exclusively for the serial-arm test and `92-111` exclusively for the parallel-arm
  test — no other job may be scheduled or pinned to them. This includes build tools (e.g. `scons -j`) that
  would otherwise happily use every core on the box: constrain those to the unreserved cores (e.g. via
  `taskset`/`--cpu-list`), whose max available count is 90.

## Branches

**Remotes.** `origin` = `git@github.com:pkwxl/gem5-parq.git` (this fork's personal
repo — research backup + integration home). `upstream` = `https://github.com/gem5/gem5.git`
(read-only; source of `stable`/`develop`).

**Fork research workflow** (the full rationale is
[docs/decisions/0001-fork-branching-strategy.md](docs/decisions/0001-fork-branching-strategy.md) —
read it before starting a new investigation):
- `main` (tracks `origin/main`) is this fork's **research trunk** — the analogue of upstream `develop`.
  Keep it buildable and spec-consistent; **do not commit exploratory work directly on `main`.**
- **One branch per S-NNN investigation**, named `sNNN-slug` to match its `docs/specs/S-NNN-slug.md` spec,
  branched from `main`. **Claim the number on `main` first** (add the `INDEX.md` row, status `进行中`,
  commit) before branching, so parallel investigations don't collide on the next number.
- Give each investigation its own `git worktree` under `/workspace/gem5-wt/<branch>/`, with its `build/`
  symlinked into namespaced tmpfs `/workspace/shm/gem5/<branch>/build/` (volatile — builds regenerate;
  checkpoints still go to `-d /tmp/...`). One-time: `mkdir -p /workspace/shm/gem5`.
- Orthogonal investigations → independent branches; conflicting/same-subsystem ones (the S-009→S-014→S-015
  pattern over `xbar.cc`/`packet_queue.cc`) → chain off the parent branch or rebase after it merges.
- Conclude by merging to `main` with `--no-ff` (**never squash** — the step-by-step commit log is itself a
  research record); dead-end investigations still land their spec + INDEX row as the negative result.
- **Pushing is manual** — push `main` (and active `sNNN-*` branches) to `origin` by hand; nothing here
  auto-pushes.

**Upstream contribution** (unchanged): if asked to make a change intended for upstream, branch from
`upstream/develop` (not `main` or `stable`), keep fork-research commits out, and open the PR against
`gem5/gem5`'s `develop`. Check which branch you're on before starting work.

## Build

Build system is SCons, configured via `SConstruct`/`SConscript` files throughout the tree, with Kconfig-style
options layered on top (see `KCONFIG.md` for how Kconfig files interact with the `CONF` dict and generated
`config/*.hh` headers).

```sh
scons build/ALL/gem5.opt          # optimized build, all ISAs
scons build/X86/gem5.opt          # single-ISA build (faster); also ARM, RISCV, MIPS, POWER, SPARC, NULL
scons build/ALL/gem5.debug        # debug build (asserts, no optimization)
scons build/ALL/gem5.fast         # fully optimized, no debug symbols
scons build/ALL/gem5.opt -j$(nproc)
```

Valid `build_opts/` targets (pass as the ISA component of the path) include `ALL`, `ARM`, `MIPS`, `NULL`,
`POWER`, `RISCV`, `SPARC`, `X86`, `ARM_X86`, plus Ruby-protocol-specific variants like
`X86_MESI_Two_Level`, `ARM_MOESI_hammer`, `Garnet_standalone`, etc. `NULL` is used for tests/tools that don't
need a real ISA (fastest to build).

A build directory (`build/<ISA>/`) is fully self-contained per ISA/config combination; there's no need to
clean between switching ISAs, but full rebuilds after major changes can be slow — prefer single-ISA builds
(e.g. `X86` or `NULL`) while iterating, and only build `ALL` when it matters.

## Testing

See `TESTING.md` for full detail. Two independent layers:

**C++ unit tests (GoogleTest)** — colocated with source as `*.test.cc`:
```sh
scons build/ALL/unittests.opt                         # build+run all unit tests
scons build/ALL/base/bitunion.test.opt                 # build one test binary
./build/ALL/base/bitunion.test.opt                     # run it
./build/ALL/base/bitunion.test.opt --gtest_list_tests
./build/ALL/base/bitunion.test.opt --gtest_filter=BitUnionData.NormalBitfield
```

**Python unit tests** (`tests/gem5/pyunit`) — need a compiled gem5 binary:
```sh
scons build/ALL/gem5.opt -j<N>
./build/ALL/gem5.opt tests/run_pyunit.py
```

**System-level regression tests** (`tests/gem5/`) — run full simulations against reference outputs, driven by
`tests/main.py`:
```sh
cd tests
./main.py run                       # 'quick' suite (X86/ARM/RISC-V) — minimum before opening a PR
./main.py run <dir1> <dir2>         # restrict to specific test directories
./main.py run --length=long         # ~12h suite, run daily in CI
./main.py run --length=very-long    # multi-day suite
./main.py run -j6                   # parallel
./main.py list -q --suites          # list test UIDs (machine-readable)
```
Quick tests are the minimum bar for a PR. Run `long`/`very-long` mainly for significant/invasive changes.

**Checkpoints**: gem5 has no persistent config for the output directory — it's set per-invocation via
`-d`/`--outdir` (default `m5out`, relative to cwd; see `src/python/m5/main.py`). When running `gem5.opt` with
checkpointing enabled (e.g. `--checkpoint-at-end`, scripted `m5.checkpoint()` calls), always pass an explicit
`-d /tmp/<something>` rather than letting checkpoints land under the repo — `cpt.<tick>/` directories are
large binary state dumps (`m5.cpt` + `.pmem` files), not source, and shouldn't be committed. `cpt.*/` is
gitignored as a safety net, but prefer not to rely on that.

## Code style & formatting

- C++: 4-space indents, no tabs, 79-col lines, no trailing whitespace. `ClassNames` UpperCamelCase, member
  functions/lower-level identifiers lowerCamelCase, local variables snake_case, macros ALL_CAPS. Member
  variables with a public accessor are prefixed `_` (e.g. `_variableWithAccessor`). Return type of a function
  definition goes on its own line; opening brace of a function body goes on its own line; `if`/`for`/`while`
  keep the opening brace on the same line. Access specifiers (`public:`/`private:`) indented 2 spaces, members
  under them indented 4. Enforced/auto-fixed by `.clang-format` via `util/run-git-clang-format.py`.
- Python: formatted with **Black** (line-length 79, see `pyproject.toml`) and **isort** (profile "black",
  custom section ordering for `m5`/`_m5`/`gem5` imports — see `[tool.isort]` in `pyproject.toml`). Follow PEP 8
  naming, but match surrounding code's existing convention where it differs.
- `pre-commit` (`.pre-commit-config.yaml`) runs isort, black, yamlfmt, clang-format, and misc hygiene checks
  (trailing whitespace, large files, merge conflict markers, etc.) as a pre-commit hook, and the same checks
  gate CI. Install once with `pip install pre-commit && pre-commit install`; without it, style violations
  will only surface in CI.

## Commit message convention

Required for any commit intended for upstream (see `CONTRIBUTING.md` for full detail):
- Header: `<tag[,tag...]>: <short description>`, ≤65 characters. Tags identify the touched component(s) — see
  `MAINTAINERS.yaml` for the accepted tag list (e.g. `mem`, `cpu`, `arch-arm`, `sim`, `tests`, `configs`).
- Blank line, then an optional but encouraged free-form description, each line ≤72 characters.
- Prefer several small, focused commits/PRs over one large one.

## High-level architecture

**Source layout** (`src/`):
- `sim/` — simulation kernel: event queue (`eventq.hh`), `SimObject` base, drain/checkpoint machinery,
  process/system abstractions. This is the backbone everything else plugs into.
- `arch/<isa>/` — one directory per ISA (`arm`, `x86`, `riscv`, `mips`, `power`, `sparc`, `amdgpu`, plus
  `generic` for ISA-agnostic shared code, and `null` for ISA-less builds). ISA-specific decoders, ISA
  definition files (`.isa`), fault handling, registers, and page tables live here. Instruction semantics are
  described in `.isa` files and compiled by `arch/isa_parser/` (a Python DSL) into C++ at build time — expect
  generated code, not hand-written `.cc`, for actual instruction execution logic.
- `cpu/` — CPU timing models, largely ISA-independent, selected at config time: `simple/` (AtomicSimple,
  TimingSimple), `o3/` (out-of-order superscalar), `minor/` (in-order pipelined), plus `kvm/`,
  `checker/`, and shared infra (`exec_context.hh`, `static_inst.hh`, `pred/` branch predictors).
- `mem/` — memory system: caches, the "classic" coherent-memory-system objects, DRAM/NVM controllers, and
  `mem/ruby/` — an entirely separate, more detailed coherence-protocol simulator. Ruby protocols are written
  in **SLICC** (`.sm` files under `mem/ruby/protocol` and `build_opts`-selected protocol dirs) and compiled by
  a dedicated SLICC toolchain into C++ state machines; don't expect to trace Ruby coherence logic through
  plain C++ alone.
- `dev/` — simulated devices (disks, NICs, interrupt controllers, platform-specific glue for full-system
  boot).
- `gpu-compute/` — GPGPU compute-unit simulation (AMD GCN/RDNA-style), pairs with `arch/amdgpu`.
- `python/` — everything Python-facing: `python/m5/` is the low-level Python↔C++ bridge (`m5.objects`,
  `m5.params`, pybind11 embedding); `python/gem5/` is the **gem5 standard library** (`components/boards`,
  `components/cachehierarchies`, `components/memory`, `components/processors`, `resources/`, `simulate/`) —
  the higher-level, composable Python API most new simulation scripts should be built on rather than
  hand-assembling raw SimObjects.
- `systemc/` — SystemC/TLM integration; `sst/` — Structural Simulation Toolkit integration.

**SimObjects — the core C++/Python coupling.** Almost every configurable simulator component (a CPU, a cache,
a memory controller, a bus...) is declared twice: a `.py` file (e.g. `mem/AbstractMemory.py`) subclassing
`m5.SimObject.SimObject` / another SimObject class, declaring `Param.*` fields and children, and a matching
`.cc`/`.hh` pair implementing the C++ class. The build system generates the Python↔C++ glue (param structs,
pybind11 bindings) from the `.py` declaration. When adding or changing a simulated component's configurable
parameters, both the `.py` declaration and the C++ constructor/param usage need to stay in sync. SConscript
files register which `.py`/`.cc` files participate in a given build (gated by Kconfig options), so a new
SimObject usually needs a `SConscript` entry (or `Kconfig` entry) as well as the `.py`/`.cc`/`.hh` files
themselves.

**Config scripts** (`configs/`) are plain Python that build up a `System` graph of SimObjects and hand it to
`m5.simulate()`. `configs/example/` holds runnable examples (SE-mode `se.py`, full-system `fs.py`, the newer
`gem5_library/` examples built on the `gem5` stdlib). `configs/common/` holds shared option-parsing/setup
helpers reused across examples. Prefer the `gem5` standard library (`src/python/gem5/components/...`) for new
scripts over replicating patterns from the older example scripts, unless matching an existing script's style.

**Kconfig** (see `KCONFIG.md` for full mechanics): options are declared close to the code they gate, included
via `rsource` (a path-relative kconfiglib extension), and become both build-time C++ config
(`env['CONF']['FOO']` → `#include "config/foo.hh"`) and Python (`m5.defines.buildEnv`). `build_opts/<NAME>`
files are pre-made default configurations (defconfigs) — `scons build/<NAME>/...` applies the corresponding
`build_opts` file the first time that build directory is configured.

## Notes for making changes

- A change to a `.py` SimObject file, an `.isa` file, or a SLICC `.sm` file triggers generated-code
  regeneration on the next build — these are not passive data files.
- ISA-specific work belongs under `arch/<isa>/`; keep ISA-agnostic logic in `arch/generic/` or the relevant
  ISA-independent module (`cpu/`, `mem/`) instead of duplicating it per-ISA.
- Ruby (`mem/ruby/`) and the "classic" memory system (`mem/` proper) are largely parallel, independent
  implementations of cache/coherence — check which one a given config/test is actually using before assuming
  a memory-system fix needs to apply to both.
