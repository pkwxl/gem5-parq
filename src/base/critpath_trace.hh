/*
 * Copyright (c) 2026 The Regents of the University of California
 * SPDX-License-Identifier: BSD-3-Clause
 */

/**
 * @file
 * S-012 critical-path instrumentation scaffolding (design doc:
 * docs/specs/S-012-eventq-critical-path-instrumentation-design.md).
 *
 * This header only declares the shared types/state; nothing appends to
 * critPathBuffer yet (that lands with the Barrier::wait() and
 * UncontendedMutex slow-path instrumentation described in the design's
 * §4.1/§4.2 -- separate, later steps). With critpath_trace off (the
 * default), the only per-thread cost this scaffolding adds is the
 * one-time write of critPathDomainId at thread entry (§4.3).
 */

#ifndef __BASE_CRITPATH_TRACE_HH__
#define __BASE_CRITPATH_TRACE_HH__

#include <chrono>
#include <cstdint>
#include <vector>

#include "base/types.hh"

namespace gem5
{

/**
 * Identifies which cross-domain UncontendedMutex instance a LockWait
 * record came from (design §2.5/§4.1). `None` is the default tag for
 * every UncontendedMutex instance not explicitly listed there (e.g.
 * EventQueue::service_mutex) -- those are excluded from tracing by
 * design (§6), not just untagged.
 */
enum class CritPathLockTag : uint8_t
{
    None = 0,
    LayerLock,
    PioLock,
    CacheLock,
    ConsumerLock,
};

enum class CritPathRecordKind : uint8_t
{
    BarrierPass,
    LockWait,
};

using CritPathClock = std::chrono::steady_clock;

/**
 * One instrumentation record. Appended only by the thread that owns the
 * critPathBuffer it lives in (design §4.4: per-domain buffer, single
 * writer, no lock needed). `tick` is the quantum-boundary tick shared
 * across domains for the same barrier pass (design §3.4) -- the join
 * key used by offline analysis, not a timestamp.
 */
struct CritPathRecord
{
    Tick tick = 0;
    uint32_t domainId = 0;
    CritPathRecordKind kind = CritPathRecordKind::BarrierPass;

    // BarrierPass fields (kind == BarrierPass; design §3.1).
    uint8_t barrierPass = 0;  // 1 or 2: which of the two per-quantum
                              // globalBarrier() calls this is.
    bool isLast = false;

    // For BarrierPass: time spent blocked in wait() (0 if isLast).
    // For LockWait: time spent in the UncontendedMutex slow path.
    CritPathClock::duration dur{};

    // LockWait fields (kind == LockWait; design §3.2).
    CritPathLockTag lockTag = CritPathLockTag::None;
};

/**
 * Global enable switch. Set once from Root::Root() (design §4.5) before
 * any eventq thread is spawned, and never written again -- safe to read
 * from any thread without synchronization.
 */
extern bool g_critPathTraceEnabled;

inline bool
critPathTracing()
{
    return g_critPathTraceEnabled;
}

inline CritPathClock::time_point
critPathNow()
{
    return CritPathClock::now();
}

/**
 * Domain id of the calling thread, set once at thread entry (design
 * §4.3) and never written again for the lifetime of the thread.
 */
extern thread_local uint32_t critPathDomainId;

/** Per-domain, single-writer record buffer (design §4.4). */
extern thread_local std::vector<CritPathRecord> critPathBuffer;

/**
 * Write this thread's critPathBuffer to
 * "<outdir>/critpath-domain<critPathDomainId>.csv" and clear it. A
 * no-op if the buffer is empty -- true for every domain until later
 * steps wire up the call sites that actually append records.
 *
 * Not yet called from any production code path (design §4.4's flush
 * call sites land with the instrumentation that produces records to
 * flush); exists now so that step lands with the rest of the
 * scaffolding in one place.
 */
void critPathFlush();

} // namespace gem5

#endif // __BASE_CRITPATH_TRACE_HH__
