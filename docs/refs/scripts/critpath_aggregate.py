# Copyright (c) 2026 The Regents of the University of California
# SPDX-License-Identifier: BSD-3-Clause
"""
S-012 offline aggregation v1: turn a directory of per-domain
critpath-domain<N>.csv files (produced by critPathFlush(), see
src/base/critpath_trace.cc and design doc
docs/specs/S-012-eventq-critical-path-instrumentation-design.md §7) into
the "which domain arrives last at the quantum barrier" histogram that
answers unknown 1 of docs/specs/OPEN-ISSUES.md §A1, plus a partial answer
to unknown 3 (per-quantum eventCount imbalance) by crossing it with the
last-arriver result.

CSV schema (written by critPathFlush(), one header line then data rows):
    kind,tick,domainId,barrierPass,isLast,eventCount,dur_ns,lockTag

Step 4 (design §4.1) adds kind == "lockwait" rows: one per
UncontendedMutex slow-path acquisition on a tagged (LayerLock/PioLock/
CacheLock/ConsumerLock) instance. dur_ns there is the slow-path wait
time; lockTag is the CritPathLockTag ordinal (1=LayerLock, 2=PioLock,
3=CacheLock, 4=ConsumerLock -- 0/None never appears, design §4.1's
`tag != None` guard). This script's barrier-histogram path (unknown 1)
ignores lockwait rows and vice versa for the lock-wait summary (unknown
2, §7's second bullet) -- each is filtered by `kind` independently
rather than assuming a file only ever contains one kind.

Join key (design §3.4): (tick, barrierPass). Every domain's BarrierPass
record for the same quantum boundary and the same one-of-two-per-quantum
barrier carries the same tick (curEventQueue()->getCurTick(), shared
across domains because all barrierEvent[i] are scheduled at the same
`when`) and the same barrierPass (1 or 2). Exactly one domain has
isLast == 1 per (tick, barrierPass) group -- this script asserts that
invariant rather than assuming it, since a violation would mean the join
key logic (or the underlying single-true-return Barrier::wait() contract,
design §2.2) is broken.

Usage:
    python3 critpath_aggregate.py <outdir> [--pass 1|2]

<outdir> is the -d directory a critpath_trace=1 run wrote
critpath-domain*.csv into. --pass restricts the histogram to one of the
two per-quantum barrier passes (design §3.1: pass 1 = "ran the quantum's
domain-local events", pass 2 = "ran the global event body, domain 0
only"); default is both, reported separately.
"""

import argparse
import csv
import glob
import os
import sys
from collections import defaultdict


def load_records(outdir):
    """Yield (tick, domainId, barrierPass, isLast, eventCount, dur_ns) for
    every barrier-kind row across all critpath-domain*.csv files in outdir."""
    paths = sorted(glob.glob(os.path.join(outdir, "critpath-domain*.csv")))
    if not paths:
        sys.exit(f"no critpath-domain*.csv files found under {outdir}")

    for path in paths:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["kind"] != "barrier":
                    continue
                yield (
                    int(row["tick"]),
                    int(row["domainId"]),
                    int(row["barrierPass"]),
                    row["isLast"] == "1",
                    int(row["eventCount"]),
                    int(row["dur_ns"]),
                )


LOCK_TAG_NAMES = {1: "LayerLock", 2: "PioLock", 3: "CacheLock",
                  4: "ConsumerLock"}


def load_lockwait_records(outdir):
    """Yield (tick, domainId, lockTag, dur_ns) for every lockwait-kind row
    across all critpath-domain*.csv files in outdir."""
    paths = sorted(glob.glob(os.path.join(outdir, "critpath-domain*.csv")))
    if not paths:
        sys.exit(f"no critpath-domain*.csv files found under {outdir}")

    for path in paths:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["kind"] != "lockwait":
                    continue
                yield (
                    int(row["tick"]),
                    int(row["domainId"]),
                    int(row["lockTag"]),
                    int(row["dur_ns"]),
                )


def read_host_seconds(outdir):
    """Best-effort read of the (single, whole-run) hostSeconds stat from
    <outdir>/stats.txt, for normalizing lock-wait time (design §7's second
    bullet). Returns None if stats.txt is missing or the field isn't
    found -- normalization is then skipped, not fatal."""
    path = os.path.join(outdir, "stats.txt")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "hostSeconds":
                try:
                    return float(parts[1])
                except ValueError:
                    return None
    return None


def aggregate_lockwait(outdir):
    # per_domain_tag[(domainId, lockTag)] = [dur_ns, ...]
    per_domain_tag = defaultdict(list)
    for _tick, domain_id, lock_tag, dur_ns in load_lockwait_records(outdir):
        per_domain_tag[(domain_id, lock_tag)].append(dur_ns)
    return per_domain_tag


def print_lockwait_summary(per_domain_tag, host_seconds):
    if not per_domain_tag:
        print("\n=== lock-wait summary (unknown 2) ===\n"
              "  no lockwait records found (either critpath_trace was off, "
              "the run predates Step 4, or no tagged UncontendedMutex ever "
              "took its slow path in this window)")
        return

    domains = sorted({d for d, _t in per_domain_tag})
    domain_totals = defaultdict(int)
    print("\n=== lock-wait summary (unknown 2) ===")
    print(f"{'domain':>8} {'lockTag':>14} {'count':>10} "
          f"{'total_ns':>14} {'avg_ns':>10}")
    for domain_id in domains:
        for lock_tag in sorted(t for d, t in per_domain_tag if d == domain_id):
            durs = per_domain_tag[(domain_id, lock_tag)]
            total = sum(durs)
            domain_totals[domain_id] += total
            name = LOCK_TAG_NAMES.get(lock_tag, str(lock_tag))
            print(f"{domain_id:>8} {name:>14} {len(durs):>10} "
                  f"{total:>14} {total / len(durs):>10.1f}")

    print(f"\n{'domain':>8} {'total lock-wait ns':>20}"
          + ("  % of hostSeconds" if host_seconds else ""))
    for domain_id in domains:
        total = domain_totals[domain_id]
        line = f"{domain_id:>8} {total:>20}"
        if host_seconds:
            pct = 100.0 * total / (host_seconds * 1e9)
            line += f"  {pct:>16.3f}%"
        print(line)


def aggregate(outdir):
    # groups[(tick, pass)] = list of (domainId, isLast, eventCount, dur_ns)
    groups = defaultdict(list)
    for tick, domain_id, bpass, is_last, event_count, dur_ns in \
            load_records(outdir):
        groups[(tick, bpass)].append((domain_id, is_last, event_count, dur_ns))

    last_arriver_count = defaultdict(lambda: defaultdict(int))  # [pass][domain]
    last_arriver_events = defaultdict(list)  # [(pass, domain)] -> [eventCount,...]
    spread_ns = defaultdict(list)  # [pass] -> [spread_ns, ...] (one per quantum)
    bad_groups = []

    for (tick, bpass), rows in groups.items():
        last_rows = [r for r in rows if r[1]]
        if len(last_rows) != 1:
            bad_groups.append((tick, bpass, len(last_rows), len(rows)))
            continue
        last_domain = last_rows[0][0]
        last_arriver_count[bpass][last_domain] += 1
        last_arriver_events[(bpass, last_domain)].append(last_rows[0][2])

        # Design §3.1: for each non-last domain, dur_ns is (its own release
        # time minus its own arrival time) i.e. roughly (last arriver's
        # arrival time minus its own arrival time). The max over the
        # waiting domains is the earliest arriver's wait -- the spread
        # between the earliest and last arrival at this barrier pass.
        waits = [r[3] for r in rows if not r[1]]
        if waits:
            spread_ns[bpass].append(max(waits))

    return groups, last_arriver_count, last_arriver_events, spread_ns, bad_groups


def print_histogram(last_arriver_count, last_arriver_events, spread_ns,
                     only_pass=None):
    passes = [only_pass] if only_pass else sorted(last_arriver_count.keys())
    for bpass in passes:
        counts = last_arriver_count[bpass]
        total = sum(counts.values())
        print(f"\n=== barrier pass {bpass} "
              f"({'quantum-local events done' if bpass == 1 else 'global event body (domain 0 only)'}) "
              f"-- {total} quanta ===")
        print(f"{'domain':>8} {'last-arriver count':>20} {'share':>8} "
              f"{'avg eventCount when last':>26}")
        for domain_id in sorted(counts, key=lambda d: -counts[d]):
            n = counts[domain_id]
            share = 100.0 * n / total if total else 0.0
            events = last_arriver_events[(bpass, domain_id)]
            avg_events = sum(events) / len(events) if events else 0.0
            print(f"{domain_id:>8} {n:>20} {share:>7.1f}% {avg_events:>26.1f}")

        spreads = spread_ns[bpass]
        if spreads:
            spreads_sorted = sorted(spreads)
            n = len(spreads_sorted)
            p50 = spreads_sorted[n // 2]
            p95 = spreads_sorted[int(n * 0.95)]
            p99 = spreads_sorted[int(n * 0.99)]
            print(f"  cross-domain arrival spread (ns): "
                  f"min={spreads_sorted[0]} p50={p50} p95={p95} "
                  f"p99={p99} max={spreads_sorted[-1]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("outdir", help="-d directory from a critpath_trace=1 run")
    ap.add_argument("--pass", dest="only_pass", type=int, choices=(1, 2),
                     default=None,
                     help="restrict the histogram to one barrier pass "
                          "(default: report both separately)")
    args = ap.parse_args()

    groups, last_arriver_count, last_arriver_events, spread_ns, bad_groups = \
        aggregate(args.outdir)

    if bad_groups:
        print(f"WARNING: {len(bad_groups)} (tick, pass) groups did not have "
              f"exactly one isLast==1 row (single-true-return Barrier::wait() "
              f"invariant, design §2.2, appears violated). First few:",
              file=sys.stderr)
        for tick, bpass, n_last, n_total in bad_groups[:10]:
            print(f"  tick={tick} pass={bpass}: {n_last} isLast rows out of "
                  f"{n_total} domain rows", file=sys.stderr)

    n_quanta = len({tick for (tick, _bpass) in groups})
    print(f"loaded {len(groups)} (tick, pass) groups across {n_quanta} "
          f"distinct quantum-boundary ticks")

    print_histogram(last_arriver_count, last_arriver_events, spread_ns,
                     only_pass=args.only_pass)

    per_domain_tag = aggregate_lockwait(args.outdir)
    host_seconds = read_host_seconds(args.outdir)
    print_lockwait_summary(per_domain_tag, host_seconds)


if __name__ == "__main__":
    main()
