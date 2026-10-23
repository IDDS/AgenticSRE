ARG TARGET_BASE_IMAGE=agenticsre-offline:validation-amd64
FROM ${TARGET_BASE_IMAGE}

ENV container=docker \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        coreutils \
        curl \
        findutils \
        fuse-overlayfs \
        gawk \
        grep \
        gzip \
        hostname \
        iproute2 \
        iptables \
        kmod \
        procps \
        sed \
        systemd \
        systemd-sysv \
        tar \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && systemctl mask \
        dev-hugepages.mount \
        sys-fs-fuse-connections.mount \
        systemd-remount-fs.service

# The locally cached builder image lets this test run when Docker Hub is
# unavailable. Remove its application and Python runtime so the target still
# represents a customer host that only has systemd and basic Linux commands.
RUN rm -rf /app /usr/local/lib/python3.12 /usr/local/bin/python /usr/local/bin/python3 /usr/local/bin/python3.12 \
    && ! command -v python \
    && ! command -v docker

COPY tests/offline_install_target/run_inside_target.sh /usr/local/sbin/run-agenticsre-offline-e2e
RUN chmod 0755 /usr/local/sbin/run-agenticsre-offline-e2e

STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
