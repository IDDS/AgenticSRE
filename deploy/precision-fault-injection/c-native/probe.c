#include <errno.h>
#include <stdio.h>
#include <time.h>
#include <unistd.h>

extern int stable_operation(int value);

static long elapsed_ms(struct timespec start, struct timespec end) {
    return (end.tv_sec - start.tv_sec) * 1000L + (end.tv_nsec - start.tv_nsec) / 1000000L;
}

int main(int argc, char **argv) {
    const char *source = argc > 1 ? argv[1] : "native";
    setvbuf(stdout, NULL, _IOLBF, 0);
    setvbuf(stderr, NULL, _IOLBF, 0);
    for (int seq = 1; seq <= 1200; ++seq) {
        struct timespec started;
        struct timespec finished;
        errno = 0;
        clock_gettime(CLOCK_MONOTONIC, &started);
        int result = stable_operation(seq);
        int saved_errno = errno;
        clock_gettime(CLOCK_MONOTONIC, &finished);
        printf(
            "source=%s seq=%d result=%d errno=%d latency_ms=%ld pid=%d\n",
            source,
            seq,
            result,
            saved_errno,
            elapsed_ms(started, finished),
            getpid()
        );
        usleep(100000);
    }
    return 0;
}
