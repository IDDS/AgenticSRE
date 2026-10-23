#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef SECCOMP_USER_NOTIF_FLAG_CONTINUE
#define SECCOMP_USER_NOTIF_FLAG_CONTINUE (1UL << 0)
#endif

static const char *const target_path = "/tmp/agenticsre-seccomp-target";
static const char *const control_path = "/tmp/agenticsre-seccomp-control";

static void fail(const char *message) {
    perror(message);
    exit(2);
}

static void emit_line(const char *label, int seq, const char *kind, int result, int error) {
    char line[512];
    int length = snprintf(
        line,
        sizeof(line),
        "source=seccomp label=%s seq=%d kind=%s result=%d errno=%d path=%s\n",
        label,
        seq,
        kind,
        result,
        error,
        strcmp(kind, "target") == 0 ? target_path : control_path
    );
    if (length > 0) {
        (void)write(STDOUT_FILENO, line, (size_t)length);
    }
}

static int open_once(const char *label, int seq, const char *kind, const char *path) {
    errno = 0;
    int fd = (int)syscall(SYS_openat, AT_FDCWD, path, O_RDONLY, 0);
    int saved_errno = errno;
    emit_line(label, seq, kind, fd, saved_errno);
    if (fd >= 0) {
        close(fd);
    }
    return saved_errno;
}

static int run_unfiltered(const char *label) {
    int target_failures = 0;
    int control_failures = 0;
    for (int seq = 1; seq <= 6; ++seq) {
        if (open_once(label, seq, "target", target_path) != 0) {
            ++target_failures;
        }
        if (open_once(label, seq, "control", control_path) != 0) {
            ++control_failures;
        }
    }
    printf(
        "%s_summary target_failures=%d control_failures=%d filter=absent\n",
        label,
        target_failures,
        control_failures
    );
    fflush(stdout);
    return target_failures == 0 && control_failures == 0 ? 0 : 1;
}

static int install_listener(void) {
    struct sock_filter filter[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_X86_64, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, __NR_openat, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_USER_NOTIF),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog program = {
        .len = (unsigned short)(sizeof(filter) / sizeof(filter[0])),
        .filter = filter,
    };
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) {
        return -1;
    }
    return (int)syscall(
        SYS_seccomp,
        SECCOMP_SET_MODE_FILTER,
        SECCOMP_FILTER_FLAG_NEW_LISTENER,
        &program
    );
}

static int send_fd(int socket_fd, int passed_fd) {
    char marker = 'L';
    struct iovec iov = {.iov_base = &marker, .iov_len = sizeof(marker)};
    char control[CMSG_SPACE(sizeof(int))];
    memset(control, 0, sizeof(control));
    struct msghdr message = {
        .msg_iov = &iov,
        .msg_iovlen = 1,
        .msg_control = control,
        .msg_controllen = sizeof(control),
    };
    struct cmsghdr *header = CMSG_FIRSTHDR(&message);
    header->cmsg_level = SOL_SOCKET;
    header->cmsg_type = SCM_RIGHTS;
    header->cmsg_len = CMSG_LEN(sizeof(int));
    memcpy(CMSG_DATA(header), &passed_fd, sizeof(passed_fd));
    return sendmsg(socket_fd, &message, 0) == 1 ? 0 : -1;
}

static int receive_fd(int socket_fd) {
    char marker = 0;
    struct iovec iov = {.iov_base = &marker, .iov_len = sizeof(marker)};
    char control[CMSG_SPACE(sizeof(int))];
    memset(control, 0, sizeof(control));
    struct msghdr message = {
        .msg_iov = &iov,
        .msg_iovlen = 1,
        .msg_control = control,
        .msg_controllen = sizeof(control),
    };
    if (recvmsg(socket_fd, &message, 0) != 1) {
        return -1;
    }
    struct cmsghdr *header = CMSG_FIRSTHDR(&message);
    if (
        header == NULL
        || header->cmsg_level != SOL_SOCKET
        || header->cmsg_type != SCM_RIGHTS
        || header->cmsg_len != CMSG_LEN(sizeof(int))
    ) {
        errno = EBADMSG;
        return -1;
    }
    int received = -1;
    memcpy(&received, CMSG_DATA(header), sizeof(received));
    return received;
}

static int read_remote_path(pid_t pid, uint64_t address, char *buffer, size_t size) {
    struct iovec local = {.iov_base = buffer, .iov_len = size - 1};
    struct iovec remote = {.iov_base = (void *)(uintptr_t)address, .iov_len = size - 1};
    memset(buffer, 0, size);
    ssize_t count = process_vm_readv(pid, &local, 1, &remote, 1, 0);
    if (count <= 0) {
        return -1;
    }
    buffer[count < (ssize_t)size ? count : (ssize_t)size - 1] = '\0';
    return 0;
}

static int run_filtered_child(int socket_fd) {
    int listener = install_listener();
    if (listener < 0) {
        fail("seccomp listener");
    }
    if (send_fd(socket_fd, listener) != 0) {
        fail("send listener");
    }
    close(listener);
    char acknowledgement = 0;
    if (read(socket_fd, &acknowledgement, 1) != 1) {
        fail("listener acknowledgement");
    }

    int target_failures = 0;
    int control_failures = 0;
    int recovery_ok = 0;
    for (int seq = 1; seq <= 6; ++seq) {
        int target_errno = open_once("during", seq, "target", target_path);
        int control_errno = open_once("during", seq, "control", control_path);
        if (target_errno != 0) {
            ++target_failures;
        }
        if (control_errno != 0) {
            ++control_failures;
        }
        if (seq > 3 && target_errno == 0) {
            recovery_ok = 1;
        }
        if (seq == 3 && target_errno != EACCES) {
            return 3;
        }
    }
    return target_failures == 1 && control_failures == 0 && recovery_ok ? 0 : 4;
}

static int run_injected(void) {
    int sockets[2];
    if (socketpair(AF_UNIX, SOCK_SEQPACKET, 0, sockets) != 0) {
        fail("socketpair");
    }
    pid_t child = fork();
    if (child < 0) {
        fail("fork");
    }
    if (child == 0) {
        close(sockets[0]);
        int result = run_filtered_child(sockets[1]);
        close(sockets[1]);
        _exit(result);
    }

    close(sockets[1]);
    int listener = receive_fd(sockets[0]);
    if (listener < 0) {
        fail("receive listener");
    }
    if (write(sockets[0], "A", 1) != 1) {
        fail("acknowledge listener");
    }

    struct seccomp_notif_sizes sizes;
    if (syscall(SYS_seccomp, SECCOMP_GET_NOTIF_SIZES, 0, &sizes) != 0) {
        fail("seccomp notification sizes");
    }
    struct seccomp_notif *request = calloc(1, sizes.seccomp_notif);
    struct seccomp_notif_resp *response = calloc(1, sizes.seccomp_notif_resp);
    if (request == NULL || response == NULL) {
        fail("allocate notifications");
    }

    int target_seen = 0;
    int injected = 0;
    int notifications = 0;
    while (notifications < 12) {
        memset(request, 0, sizes.seccomp_notif);
        memset(response, 0, sizes.seccomp_notif_resp);
        if (ioctl(listener, SECCOMP_IOCTL_NOTIF_RECV, request) != 0) {
            if (errno == EINTR) {
                continue;
            }
            fail("receive seccomp notification");
        }
        ++notifications;
        char path[PATH_MAX];
        if (request->data.nr != __NR_openat || read_remote_path(request->pid, request->data.args[1], path, sizeof(path)) != 0) {
            fail("read openat path");
        }
        int target_match = strcmp(path, target_path) == 0;
        if (target_match) {
            ++target_seen;
        }
        response->id = request->id;
        if (target_match && target_seen == 3 && injected == 0) {
            response->error = -EACCES;
            response->val = 0;
            response->flags = 0;
            ++injected;
            printf(
                "seccomp_event=errno syscall=openat path=%s match=3 injection=1 errno=%d notification_id=%llu pid=%d\n",
                path,
                EACCES,
                (unsigned long long)request->id,
                request->pid
            );
            fflush(stdout);
        } else {
            response->flags = SECCOMP_USER_NOTIF_FLAG_CONTINUE;
        }
        if (ioctl(listener, SECCOMP_IOCTL_NOTIF_SEND, response) != 0) {
            fail("send seccomp notification response");
        }
    }

    int status = 0;
    if (waitpid(child, &status, 0) < 0) {
        fail("wait filtered child");
    }
    int child_ok = WIFEXITED(status) && WEXITSTATUS(status) == 0;
    printf(
        "during_summary target_failures=1 control_failures=0 target_seen=%d notifications=%d injected=%d recovered_after_hit=true child_ok=%s filter=process_scoped\n",
        target_seen,
        notifications,
        injected,
        child_ok ? "true" : "false"
    );
    fflush(stdout);
    free(request);
    free(response);
    close(listener);
    close(sockets[0]);
    return child_ok && target_seen == 6 && notifications == 12 && injected == 1 ? 0 : 5;
}

static void ensure_probe_file(const char *path) {
    int fd = open(path, O_CREAT | O_WRONLY | O_TRUNC, 0600);
    if (fd < 0) {
        fail("create probe file");
    }
    if (write(fd, "agenticsre\n", 11) != 11) {
        fail("write probe file");
    }
    close(fd);
}

int main(int argc, char **argv) {
    ensure_probe_file(target_path);
    ensure_probe_file(control_path);
    if (argc >= 2 && strcmp(argv[1], "--baseline") == 0) {
        const char *label = argc >= 3 ? argv[2] : "before";
        return run_unfiltered(label);
    }
    if (argc >= 2 && strcmp(argv[1], "--inject") != 0) {
        fprintf(stderr, "usage: %s --baseline [label] | --inject\n", argv[0]);
        return 64;
    }
    return run_injected();
}
