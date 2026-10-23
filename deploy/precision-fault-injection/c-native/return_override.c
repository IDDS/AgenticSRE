#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

typedef int (*stable_operation_fn)(int);

static atomic_int call_count = 0;
static atomic_int injection_count = 0;

static const char *env_string(const char *name, const char *fallback) {
    const char *value = getenv(name);
    return value && *value ? value : fallback;
}

static int env_int(const char *name, int fallback) {
    const char *value = getenv(name);
    if (!value || !*value) {
        return fallback;
    }
    char *end = NULL;
    long parsed = strtol(value, &end, 10);
    return end && *end == '\0' ? (int)parsed : fallback;
}

int stable_operation(int value) {
    static stable_operation_fn original = NULL;
    if (!original) {
        original = (stable_operation_fn)dlsym(RTLD_NEXT, "stable_operation");
        if (!original) {
            fprintf(stderr, "instrumentation_error=dlsym_failed\n");
            return value + 1;
        }
    }
    int current_call = atomic_fetch_add(&call_count, 1) + 1;
    int hit_count = env_int("AGENTICSRE_FAULT_HIT", 3);
    int max_hits = env_int("AGENTICSRE_FAULT_MAX_HITS", 1);
    int return_value = env_int("AGENTICSRE_FAULT_RETURN", -77);
    int errno_value = env_int("AGENTICSRE_FAULT_ERRNO", EIO);
    int delay_ms = env_int("AGENTICSRE_FAULT_DELAY_MS", 350);
    const char *action = env_string("AGENTICSRE_FAULT_ACTION", "return_override");
    if (current_call >= hit_count && atomic_load(&injection_count) < max_hits) {
        int previous = atomic_fetch_add(&injection_count, 1);
        if (previous < max_hits) {
            if (strcmp(action, "return_override") == 0) {
                fprintf(
                    stderr,
                    "instrumentation_event=return_override symbol=stable_operation call=%d injection=%d return=%d\n",
                    current_call,
                    previous + 1,
                    return_value
                );
                return return_value;
            }
            if (strcmp(action, "errno") == 0) {
                errno = errno_value;
                fprintf(
                    stderr,
                    "instrumentation_event=errno symbol=stable_operation call=%d injection=%d errno=%d return=%d\n",
                    current_call,
                    previous + 1,
                    errno_value,
                    return_value
                );
                return return_value;
            }
            if (strcmp(action, "delay") == 0) {
                fprintf(
                    stderr,
                    "instrumentation_event=delay symbol=stable_operation call=%d injection=%d delay_ms=%d\n",
                    current_call,
                    previous + 1,
                    delay_ms
                );
                usleep((useconds_t)delay_ms * 1000U);
                return original(value);
            }
            fprintf(stderr, "instrumentation_error=unsupported_action action=%s\n", action);
        }
    }
    return original(value);
}
