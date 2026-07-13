#include <stdio.h>
#include <stdlib.h>

/* Simple CPU/memory-bound kernel for SE-mode gem5 benchmarking:
 * repeatedly sweeps an array doing dependent reads+writes, so it can't be
 * optimized away and generates a steady stream of L1/L2/memory traffic. */
int main(int argc, char **argv) {
    long n = (argc > 1) ? atol(argv[1]) : 4096;
    long reps = (argc > 2) ? atol(argv[2]) : 100;

    int *a = malloc(n * sizeof(int));
    if (!a) return 1;
    for (long i = 0; i < n; i++) a[i] = (int)(i % 97);

    long sum = 0;
    for (long r = 0; r < reps; r++) {
        for (long i = 0; i < n; i++) {
            a[i] = a[i] * 3 + 1;
            sum += a[i];
        }
    }

    printf("done: n=%ld reps=%ld sum=%ld\n", n, reps, sum);
    free(a);
    return 0;
}
