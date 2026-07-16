/*
 * Copyright (c) 2026 The Regents of the University of California
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "base/critpath_trace.hh"

#include "base/cprintf.hh"
#include "base/output.hh"

namespace gem5
{

bool g_critPathTraceEnabled = false;

thread_local uint32_t critPathDomainId = 0;
thread_local std::vector<CritPathRecord> critPathBuffer;

void
critPathFlush()
{
    if (critPathBuffer.empty())
        return;

    const std::string name =
        csprintf("critpath-domain%d.csv", critPathDomainId);
    OutputStream *os = simout.create(name, false, true);
    std::ostream *s = os->stream();

    *s << "kind,tick,domainId,barrierPass,isLast,dur_ns,lockTag\n";
    for (const auto &r : critPathBuffer) {
        *s << (r.kind == CritPathRecordKind::BarrierPass ?
                   "barrier" : "lockwait")
           << "," << r.tick
           << "," << r.domainId
           << "," << (unsigned)r.barrierPass
           << "," << (r.isLast ? 1 : 0)
           << "," << std::chrono::duration_cast<
                        std::chrono::nanoseconds>(r.dur).count()
           << "," << (unsigned)r.lockTag
           << "\n";
    }

    simout.close(os);
    critPathBuffer.clear();
}

} // namespace gem5
