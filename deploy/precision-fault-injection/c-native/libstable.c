#include <unistd.h>

__attribute__((noinline)) int stable_operation(int value) {
    usleep(10000);
    return value + 1;
}
