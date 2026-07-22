# Project Summary

## Overall Goal
Speed up multi-core Ruby simulation by splitting gem5's single `EventQueue` into one queue per cache/coherence domain, run on separate host threads, synchronized by a quantum barrier, while maintaining timing accuracy or with a well-quantified relaxation.

## Key Knowledge
- **Languages**: C++ (simulator core), Python (SimObject parameter declarations, build system, config scripts, and gem5 standard library).
- **Measurement Methodology**: Use a three-arm comparison (Baseline, Current Serial, Current Parallel) to measure overhead ratio, real speedup, and internal speedup.
- **Branching Strategy**: 
  - `main`: Research trunk, tracks `origin/main`.
  - Investigation branches: Named `sNNN-slug`, branched from `main`.
- **Build System**: Uses SCons with Kconfig-style options.
  - Example build commands: `scons build/ALL/gem5.opt` (optimized build), `scons build/X86/gem5.opt` (single-ISA build), `scons build/ALL/gem5.debug` (debug build).
- **Testing**: 
  - C++ unit tests (GoogleTest).
  - Python unit tests.
  - System-level regression tests driven by `tests/main.py`.
- **Code Style & Formatting**: 
  - C++: 4-space indents, no tabs, 79-col lines, no trailing whitespace.
  - Python: Formatted with Black and isort, following PEP 8 naming conventions.
- **Commit Message Convention**: 
  - Header: `<tag[,tag...]>: <short description>`, ≤65 characters.
  - Description: Optional, free-form, each line ≤72 characters.

## Recent Actions
- Read and analyzed `CLAUDE.md` to understand the project's goals, branching strategy, build system, testing procedures, code style, and commit message conventions.
- Captured the primary research goal, measurement methodology, working style, and high-level architecture of the project.

## Current Plan
1. [IN PROGRESS] Continue exploring the codebase to understand existing functions, utilities, and patterns.
2. [TODO] Gather more specific requirements and preferences from the user.
3. [TODO] Identify the next task or area to focus on (e.g., adding a new feature, fixing a bug, improving performance).
4. [TODO] Develop a detailed plan for the next steps, including what to change, which files to modify, and how to verify the changes.

---

## Summary Metadata
**Update time**: 2026-07-21T06:49:13.834Z 
