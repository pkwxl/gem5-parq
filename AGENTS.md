# AGENTS.md — gem5 research fork

## First read

- `CLAUDE.md` is the primary instruction file (249 lines covering architecture, branch strategy, build, test, formatting, measurement). Read it first.
- `docs/decisions/0001-fork-branching-strategy.md` — full rationale for the branch+worktree workflow below.

## This repo

gem5 (computer architecture simulator). This fork's project: parallel EventQueue for multi-core Ruby speedup. Research branches off `main`; upstream contributions branch from `upstream/develop`.

## Build (SCons — not CMake/Make)

```
scons build/<ISA>/gem5.opt -j<N>    # e.g. X86, ARM, NULL, X86_MESI_Three_Level
scons build/ALL/unittests.opt        # C++ unit tests
```

Build dirs are self-contained per ISA; `build_opts/` has defconfigs. Kconfig options at `env['CONF']['FOO']` → `#include "config/foo.hh"`.

## Generated code (editing these triggers codegen — not passive data)

- `.isa` files → C++ via `arch/isa_parser/`
- SLICC `.sm` files → C++ state machines
- SimObject `.py` declarations → pybind11 bindings + param structs (both `.py` and `.cc`/`.hh` must stay in sync)
- New SimObject usually needs a `SConscript` entry too

## Two memory systems — know which one you are in

- `src/mem/` = "classic" caches/coherence
- `src/mem/ruby/` = separate, more detailed protocol simulator

Check which one a config/test uses before assuming a fix applies to both.

## Research workflow

- `origin = pkwxl/gem5-parq`, `upstream = gem5/gem5` (read-only). `main` is research trunk.
- `git push` is **denied** by `.claude/settings.json` — manual only.
- Each S-NNN investigation → `sNNN-slug` branch off `main`. Claim number on `main` first (add INDEX.md row, commit).
- Worktrees: `/workspace/gem5-wt/<branch>/`. Builds: `/workspace/shm/gem5/<branch>/build/` (tmpfs, volatile). Checkpoints: `-d /tmp/...`.
- Merge `--no-ff`, never squash. Dead-end branches still land spec + INDEX row.
- Upstream contributions branch from `upstream/develop`, keep fork commits out.

## Measurement (3 arms, never 2)

1. **Baseline** — pre-fork commit
2. **Current Serial** — `PARALLEL_EVENTQ=0`
3. **Current Parallel** — `PARALLEL_EVENTQ=1`

Derive: Overhead ratio = 2/1, Real speedup = 1/3, Internal speedup = 2/3.

Reserved isolcpus cores: values live only in `util/roles/reserved-cores` (`SERIAL_ARM_CPUS` / `PARALLEL_ARM_CPUS` / `BUILD_CPUS`) — read them there, never hard-code. No other job may run on the arm cores; pin every parallel job to `BUILD_CPUS`. `ps` inside this container cannot see outside occupancy — run the `check-cores` skill first.

## Critical gotchas

- **No SIGUSR1/SIGUSR2** to a parallel-EventQueue process (segfault).
- **Checkpoints**: always `-d /tmp/<something>`, never let `m5out` land under the repo.
- Report inconvenient measurements into the relevant `docs/specs/S-NNN` file same session.
- Offer checkpoint before starting a qualitatively riskier sub-phase.

## Testing

| Layer | Command |
|---|---|
| C++ unit tests (GoogleTest) | `scons build/ALL/unittests.opt` |
| Python unit tests | `./build/ALL/gem5.opt tests/run_pyunit.py` |
| System-level regression (quick) | `cd tests && ./main.py run` |

## Formatting

- Python: Black (79 chars), isort (profile "black", custom m5/gem5 sections).
- C++: `util/run-git-clang-format.py` (enforces `.clang-format`).
- All enforced via pre-commit: `pip install pre-commit && pre-commit install`.

## When you learn something new

If you discover a gotcha or workflow detail that is not here or in CLAUDE.md, add it.
