/*
 * Copyright (c) 2026 The Regents of the University of California
 * SPDX-License-Identifier: BSD-3-Clause
 */

/**
 * @file
 * S-012 critical-path instrumentation: shared types/state (design doc:
 * docs/specs/S-012-eventq-critical-path-instrumentation-design.md).
 *
 * As of Step 4, Barrier::wait() (src/base/barrier.hh) appends
 * BarrierPass records, EventQueue::serviceOne() (src/sim/eventq.cc)
 * drives the per-quantum event counter (design §4.2/§3.3),
 * UncontendedMutex::lock()'s slow path (src/base/uncontended_mutex.hh)
 * appends LockWait records for the four tagged cross-domain instances
 * (design §4.1), and critPathFlush() is wired to each domain's thread
 * exit / atexit (Step 3). With critpath_trace off (the default), the
 * added per-thread cost is: the one-time write of critPathDomainId at
 * thread entry (§4.3), one predictable bool check per barrier wait, one
 * predictable bool check per serviced event, and one predictable bool
 * check (`tag != None`, itself almost always false since only 4 tagged
 * instances exist) per UncontendedMutex slow-path acquisition.
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
    // Events this domain ran (serviceOne() calls that actually invoked
    // process(), design §3.3) since the previous BarrierPass record on
    // this domain; critPathEventCount is reset to 0 right after being
    // copied in here.
    uint64_t eventCount = 0;

    // For BarrierPass: time spent blocked in wait() (0 if isLast).
    // For LockWait: time spent in the UncontendedMutex slow path.
    CritPathClock::duration dur{};

    // LockWait fields (kind == LockWait; design §3.2).
    CritPathLockTag lockTag = CritPathLockTag::None;
};

/**
 * Per-call context that globalBarrier() (design §4.2, src/sim/
 * global_event.hh) passes into Barrier::wait() so the record it
 * produces can be joined across domains. Barrier itself has no notion
 * of "domain" or "quantum" -- it just carries what the caller already
 * knows. Domain id is not part of this context: it is read from the
 * thread-local critPathDomainId at record time instead of being passed
 * down, since it never changes for the life of the thread (§4.3).
 */
struct CritPathBarrierCtx
{
    Tick tick;       // curEventQueue()->getCurTick() at the call site.
    uint8_t pass;    // 1 or 2 -- design §3.1.
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
 * Events this domain has run since the last BarrierPass record (design
 * §3.3). Only serviceOne()'s "actually ran process()" path increments
 * this; critPathRecordBarrierPass() reads and resets it.
 */
extern thread_local uint64_t critPathEventCount;

/**
 * Call from EventQueue::serviceOne() (design §3.3) right after an event
 * that wasn't squashed has been processed. A no-op branch (predictable,
 * off the hot path's critical dependency chain) when tracing is off.
 */
inline void
critPathCountEvent()
{
    if (critPathTracing())
        ++critPathEventCount;
}

/**
 * Record one BarrierPass entry for the calling thread's domain (design
 * §3.1/§4.2): called from Barrier::wait() when it was given a non-null
 * CritPathBarrierCtx and tracing is on. Reads critPathDomainId and
 * critPathEventCount (resetting the latter to 0) as a side effect --
 * this is the one designated point where the per-quantum event counter
 * gets consumed.
 */
void critPathRecordBarrierPass(const CritPathBarrierCtx &ctx, bool isLast,
                                CritPathClock::duration dur);

/**
 * Record one LockWait entry for the calling thread's domain (design
 * §3.2/§4.1): called from UncontendedMutex::lock()'s slow path when
 * the mutex carries a non-None tag and tracing is on. Reads
 * critPathDomainId and, unlike critPathRecordBarrierPass(), reads the
 * join-key tick itself via curTick() (design §3.4) rather than taking
 * it from the caller -- UncontendedMutex lives in base/ and can't
 * include sim/eventq.hh (circular: eventq.hh already includes
 * uncontended_mutex.hh for its own untagged mutexes), so unlike
 * globalBarrier() (sim/global_event.hh) it cannot read
 * curEventQueue()->getCurTick() itself either. curTick() (sim/
 * cur_tick.hh, already an accepted base/ dependency -- see base/
 * trace.hh, base/stats/storage.hh) is the same thread-local value.
 */
void critPathRecordLockWait(CritPathLockTag tag, CritPathClock::duration dur);

/**
 * Write this thread's critPathBuffer to
 * "<outdir>/critpath-domain<critPathDomainId>.csv" and clear it. A
 * no-op if the buffer is empty. Wired to each domain's thread-exit /
 * atexit (src/sim/simulate.cc, Step 3).
 */
void critPathFlush();

/**
 * Buffer-capacity budget (in records), configured via the Root param
 * critpath_trace_reserve (design §4.4's "预留容量" follow-up, landed
 * in Step 4 -- see S-012 §13.6/§14). 0 (the default) means no
 * reservation: critPathBuffer grows the normal std::vector way,
 * exactly as it did through Step 3.
 */
extern size_t g_critPathTraceReserve;

/**
 * Reserve capacity for this thread's critPathBuffer if a nonzero
 * budget was configured. Call once at thread entry, right after
 * critPathDomainId is set (§4.3) and before any record can be pushed.
 * A no-op when g_critPathTraceReserve is 0.
 */
void critPathReserve();

} // namespace gem5

#endif // __BASE_CRITPATH_TRACE_HH__
