typedef unsigned int __u32;

#define SEC(name) __attribute__((section(name), used))
#define BPF_MAP_TYPE_ARRAY 2
#define __uint(name, value) int (*name)[value]
#define __type(name, value) value *name

struct bpf_sock_addr {
    __u32 user_family;
    __u32 user_ip4;
    __u32 user_ip6[4];
    __u32 user_port;
};

struct connection_config {
    __u32 target_ip4;
    __u32 target_port;
    __u32 max_failures;
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct connection_config);
} config_map SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u32);
} failure_map SEC(".maps");

static void *(*bpf_map_lookup_elem)(const void *map, const void *key) = (void *)1;

SEC("cgroup/connect4")
int agenticsre_connect4(struct bpf_sock_addr *context) {
    __u32 key = 0;
    struct connection_config *config = bpf_map_lookup_elem(&config_map, &key);
    __u32 *failure_count = bpf_map_lookup_elem(&failure_map, &key);

    if (!config || !failure_count) {
        return 1;
    }
    if (context->user_ip4 != config->target_ip4 || context->user_port != config->target_port) {
        return 1;
    }

    __u32 previous = __sync_fetch_and_add(failure_count, 1);
    if (previous < config->max_failures) {
        return 0;
    }
    return 1;
}

char LICENSE[] SEC("license") = "GPL";
