/*
 * Copyright (c) 2026 The Regents of the University of California
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "base/critpath_trace.hh"

#include "base/cprintf.hh"
#include "base/output.hh"
#include "sim/cur_tick.hh"

namespace gem5
{

bool g_critPathTraceEnabled = false;
size_t g_critPathTraceReserve = 0;

thread_local uint32_t critPathDomainId = 0;
thread_local std::vector<CritPathRecord> critPathBuffer;
thread_local uint64_t critPathEventCount = 0;

void
critPathRecordBarrierPass(const CritPathBarrierCtx &ctx, bool isLast,
                           CritPathClock::duration dur)
{
    CritPathRecord r;
    r.tick = ctx.tick;
    r.domainId = critPathDomainId;
    r.kind = CritPathRecordKind::BarrierPass;
    r.barrierPass = ctx.pass;
    r.isLast = isLast;
    r.eventCount = critPathEventCount;
    r.dur = dur;
    critPathBuffer.push_back(r);

    critPathEventCount = 0;
}

void
critPathRecordLockWait(CritPathLockTag tag, CritPathClock::duration dur)
{
    CritPathRecord r;
    r.tick = curTick();
    r.domainId = critPathDomainId;
    r.kind = CritPathRecordKind::LockWait;
    r.dur = dur;
    r.lockTag = tag;
    critPathBuffer.push_back(r);
}

void
critPathReserve()
{
    if (g_critPathTraceReserve > 0)
        critPathBuffer.reserve(g_critPathTraceReserve);
}

void
critPathFlush()
{
    if (critPathBuffer.empty())
        return;

    const std::string name =
        csprintf("critpath-domain%d.csv", critPathDomainId);
    OutputStream *os = simout.create(name, false, true);
    std::ostream *s = os->stream();

    *s << "kind,tick,domainId,barrierPass,isLast,eventCount,dur_ns,"
          "lockTag\n";
    for (const auto &r : critPathBuffer) {
        *s << (r.kind == CritPathRecordKind::BarrierPass ?
                   "barrier" : "lockwait")
           << "," << r.tick
           << "," << r.domainId
           << "," << (unsigned)r.barrierPass
           << "," << (r.isLast ? 1 : 0)
           << "," << r.eventCount
           << "," << std::chrono::duration_cast<
                        std::chrono::nanoseconds>(r.dur).count()
           << "," << (unsigned)r.lockTag
           << "\n";
    }

    simout.close(os);
    critPathBuffer.clear();
}

} // namespace gem5
