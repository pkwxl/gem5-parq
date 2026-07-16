/*
 * threads_balanced.cpp -- balanced-workload variant of
 * tests/test-progs/threads/src/threads.cpp, written for docs/specs/S-013.
 *
 * The original `threads` benchmark only parallelizes the array_add step;
 * array init and result validation are fully serial and run on whichever
 * core hosts the process's original (non-spawned) thread context. Under
 * S-012's critical-path tracing, that serial ~400k-element work landing on
 * one core (plus no explicit thread pinning) made that core's domain the
 * last-arriving domain at ~50% of quantum barriers -- a workload-shape
 * artifact, not a coherence-topology finding (S-012 §13.5).
 *
 * This variant parallelizes all three phases (init, compute, validate)
 * with the same block-cyclic partition, so each thread only ever touches
 * its own index range across all phases (no inter-thread synchronization
 * needed between phases -- a thread's own init always precedes its own
 * compute/validate because they're sequential in that thread's code), and
 * pins each thread to a specific vCPU explicitly so the domain<->core
 * mapping is deterministic instead of left to guest scheduler placement.
 *
 * Usage: threads_balanced [num_values] [chunk_size]
 *   chunk_size=1 (default) reproduces the original worst-case false-sharing
 *   element-interleaved distribution -- every cache line touched by all
 *   `threads` threads.
 */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <algorithm>
#include <atomic>
#include <iostream>
#include <pthread.h>
#include <sched.h>
#include <thread>

using namespace std;

void
pin_to_cpu(unsigned cpu)
{
    cpu_set_t cpuset;
    CPU_ZERO(&cpuset);
    CPU_SET(cpu, &cpuset);
    pthread_setaffinity_np(pthread_self(), sizeof(cpu_set_t), &cpuset);
}

/*
 * Elements are handed out in blocks of chunk_size, round-robin across
 * threads (block-cyclic distribution) -- same scheme as the original
 * array_add, reused here for all three phases via `body`.
 */
template <typename F>
void
for_each_chunk(int tid, int threads, int num_values, int chunk_size, F body)
{
    for (int base = tid * chunk_size; base < num_values;
         base += threads * chunk_size) {
        int end = min(base + chunk_size, num_values);
        for (int i = base; i < end; i++) {
            body(i);
        }
    }
}

void
worker(int *a, int *b, int *c, int tid, int threads, int num_values,
       int chunk_size, atomic<int> *num_valid)
{
    pin_to_cpu(tid);

    for_each_chunk(tid, threads, num_values, chunk_size, [&](int i) {
        a[i] = i;
        b[i] = num_values - i;
        c[i] = 0;
    });

    for_each_chunk(tid, threads, num_values, chunk_size, [&](int i) {
        c[i] = a[i] + b[i];
    });

    int local_valid = 0;
    for_each_chunk(tid, threads, num_values, chunk_size, [&](int i) {
        if (c[i] == num_values) {
            local_valid++;
        } else {
            cerr << "c[" << i << "] is wrong.";
            cerr << " Expected " << num_values;
            cerr << " Got " << c[i] << "." << endl;
        }
    });
    num_valid->fetch_add(local_valid, memory_order_relaxed);
}

int
main(int argc, char *argv[])
{
    unsigned num_values;
    unsigned chunk_size = 1;
    if (argc == 1) {
        num_values = 100;
    } else if (argc == 2) {
        num_values = atoi(argv[1]);
        if ((int)num_values <= 0) {
            cerr << "Usage: " << argv[0]
                 << " [num_values] [chunk_size]" << endl;
            return 1;
        }
    } else if (argc == 3) {
        num_values = atoi(argv[1]);
        chunk_size = atoi(argv[2]);
        if ((int)num_values <= 0 || (int)chunk_size <= 0) {
            cerr << "Usage: " << argv[0]
                 << " [num_values] [chunk_size]" << endl;
            return 1;
        }
    } else {
        cerr << "Usage: " << argv[0]
             << " [num_values] [chunk_size]" << endl;
        return 1;
    }

    unsigned cpus = thread::hardware_concurrency();

    cout << "Running on " << cpus << " cores. ";
    cout << "with " << num_values << " values, chunk_size " << chunk_size;
    cout << endl;

    int *a, *b, *c;
    a = new int[num_values];
    b = new int[num_values];
    c = new int[num_values];

    if (!(a && b && c)) {
        cerr << "Allocation error!" << endl;
        return 2;
    }

    atomic<int> num_valid(0);
    thread **workers = new thread*[cpus];

    for (unsigned i = 0; i < cpus; i++) {
        workers[i] = new thread(worker, a, b, c, (int)i, (int)cpus,
                                 (int)num_values, (int)chunk_size,
                                 &num_valid);
    }

    cout << "Waiting for other threads to complete" << endl;

    for (unsigned i = 0; i < cpus; i++) {
        workers[i]->join();
        delete workers[i];
    }
    delete[] workers;

    cout << "Validating..." << flush;

    if ((unsigned)num_valid.load() == num_values) {
        cout << "Success!" << endl;
        return 0;
    } else {
        return 2;
    }
}
