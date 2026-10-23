#!/usr/bin/env bash
# AgenticSRE customer-site offline installer.
#
# This script never uses a package manager and never accesses the network. It
# installs the Docker static runtime bundled by build_offline_bundle.sh only
# when a usable Docker Engine is not already present.

set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ENV="${SCRIPT_DIR}/BUNDLE.env"
COMPOSE_FILE_SOURCE="${SCRIPT_DIR}/docker-compose.offline.yaml"
IMAGE_ARCHIVE="${SCRIPT_DIR}/images/agenticsre-image.tar.gz"
CHECKSUM_FILE="${SCRIPT_DIR}/SHA256SUMS"

INSTALL_ROOT="${AGENTICSRE_INSTALL_ROOT:-/opt/agenticsre}"
STATE_ROOT="${AGENTICSRE_STATE_ROOT:-/var/lib/agenticsre}"
CONFIG_SOURCE=""
BACKUP_FILE=""
RESTORE_FILE=""
ACTION="install"
HEALTH_TIMEOUT="${AGENTICSRE_HEALTH_TIMEOUT:-180}"
INSTALL_DOCKER="auto"
DOCKER_STORAGE_DRIVER="${AGENTICSRE_DOCKER_STORAGE_DRIVER:-}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { printf "%b[INFO]%b %s\n" "${GREEN}" "${NC}" "$*"; }
warn()  { printf "%b[WARN]%b %s\n" "${YELLOW}" "${NC}" "$*" >&2; }
step()  { printf "\n%b==>%b %s\n" "${BLUE}" "${NC}" "$*"; }
die()   { printf "%b[ERROR]%b %s\n" "${RED}" "${NC}" "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
用法：
  sudo ./install-offline.sh [选项]

默认动作是在完全断网的 Linux 客户机上安装/升级 AgenticSRE。

选项：
  --config FILE       使用预先填写的客户配置文件
  --install-root DIR  程序元数据目录（默认 /opt/agenticsre）
  --state-root DIR    持久化数据目录（默认 /var/lib/agenticsre）
  --verify-only       只校验交付包、架构和基础条件，不修改系统
  --status            查看服务状态与健康状态
  --stop              停止服务，保留镜像、配置和全部数据
  --restart           重启并执行健康检查
  --data-check        校验 ChromaDB/SQLite 与 JSON 持久化数据
  --backup [FILE]     停写后一致性备份数据库与配置
  --restore FILE      校验并恢复备份，失败自动回滚
  --skip-docker-install
                      未安装 Docker 时直接报错，不安装包内 Docker
  -h, --help          显示帮助

环境变量：
  AGENTICSRE_INSTALL_ROOT, AGENTICSRE_STATE_ROOT,
  AGENTICSRE_HEALTH_TIMEOUT, AGENTICSRE_DOCKER_STORAGE_DRIVER
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --config) [ "$#" -ge 2 ] || die "--config 缺少文件参数"; CONFIG_SOURCE="$2"; shift 2 ;;
        --install-root) [ "$#" -ge 2 ] || die "--install-root 缺少目录参数"; INSTALL_ROOT="$2"; shift 2 ;;
        --state-root) [ "$#" -ge 2 ] || die "--state-root 缺少目录参数"; STATE_ROOT="$2"; shift 2 ;;
        --verify-only) ACTION="verify"; shift ;;
        --status) ACTION="status"; shift ;;
        --stop) ACTION="stop"; shift ;;
        --restart) ACTION="restart"; shift ;;
        --data-check) ACTION="data-check"; shift ;;
        --backup)
            ACTION="backup"
            if [ "$#" -ge 2 ] && [[ "$2" != --* ]]; then BACKUP_FILE="$2"; shift 2; else shift; fi
            ;;
        --restore) [ "$#" -ge 2 ] || die "--restore 缺少备份文件"; ACTION="restore"; RESTORE_FILE="$2"; shift 2 ;;
        --skip-docker-install) INSTALL_DOCKER="never"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数：$1（运行 --help 查看用法）" ;;
    esac
done

[[ "${HEALTH_TIMEOUT}" =~ ^[1-9][0-9]*$ ]] || die "AGENTICSRE_HEALTH_TIMEOUT 必须是正整数"
[[ -z "${DOCKER_STORAGE_DRIVER}" || "${DOCKER_STORAGE_DRIVER}" =~ ^(overlay2|vfs)$ ]] \
    || die "AGENTICSRE_DOCKER_STORAGE_DRIVER 仅允许 overlay2 或 vfs"
required_host_commands=(
    awk basename bash cat chmod date dirname grep gzip hostname install mkdir
    mktemp mv nohup od readlink sed sleep tail tar touch tr uname
)
missing_host_commands=()
for command_name in "${required_host_commands[@]}"; do
    command -v "${command_name}" >/dev/null 2>&1 || missing_host_commands+=("${command_name}")
done
[ "${#missing_host_commands[@]}" -eq 0 ] || die "宿主机缺少最小系统命令：${missing_host_commands[*]}"
for root_pair in "INSTALL_ROOT=${INSTALL_ROOT}" "STATE_ROOT=${STATE_ROOT}"; do
    root_name="${root_pair%%=*}"
    root_value="${root_pair#*=}"
    [[ "${root_value}" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "${root_name} 必须是不含空格的绝对路径"
    [ "${root_value}" != "/" ] || die "${root_name} 不能是根目录"
done
[ "${INSTALL_ROOT}" != "${STATE_ROOT}" ] || die "INSTALL_ROOT 与 STATE_ROOT 不能相同"
case "${INSTALL_ROOT}/" in "${STATE_ROOT}/"*) die "INSTALL_ROOT 不能位于 STATE_ROOT 内" ;; esac
case "${STATE_ROOT}/" in "${INSTALL_ROOT}/"*) die "STATE_ROOT 不能位于 INSTALL_ROOT 内" ;; esac

read_bundle_value() {
    local key="$1"
    awk -F= -v wanted="${key}" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' "${BUNDLE_ENV}"
}

normalize_arch() {
    case "$1" in
        x86_64|amd64) printf 'amd64\n' ;;
        aarch64|arm64) printf 'arm64\n' ;;
        *) return 1 ;;
    esac
}

host_os="${AGENTICSRE_TEST_UNAME_S:-$(uname -s)}"
host_machine="${AGENTICSRE_TEST_UNAME_M:-$(uname -m)}"
[ "${host_os}" = "Linux" ] || die "仅支持 Linux 客户机，当前系统为 ${host_os}"
host_arch="$(normalize_arch "${host_machine}")" || die "不支持的 CPU 架构：${host_machine}"

if [[ "${ACTION}" =~ ^(status|stop|restart|data-check|backup|restore)$ ]]; then
    BUNDLE_VERSION=""
    DEPLOYMENT_MODE=""
    REQUIRES_INTERNET=""
    BUNDLE_ARCH="${host_arch}"
    IMAGE_REF=""
    DOCKER_VERSION=""
    COMPOSE_VERSION=""
else
    [ -f "${BUNDLE_ENV}" ] || die "交付包缺少 BUNDLE.env"
    [ -f "${COMPOSE_FILE_SOURCE}" ] || die "交付包缺少 docker-compose.offline.yaml"
    [ -f "${IMAGE_ARCHIVE}" ] || die "交付包缺少 images/agenticsre-image.tar.gz"
    [ -f "${CHECKSUM_FILE}" ] || die "交付包缺少 SHA256SUMS"

    BUNDLE_VERSION="$(read_bundle_value BUNDLE_VERSION)"
    DEPLOYMENT_MODE="$(read_bundle_value DEPLOYMENT_MODE)"
    REQUIRES_INTERNET="$(read_bundle_value REQUIRES_INTERNET)"
    BUNDLE_ARCH="$(read_bundle_value TARGET_ARCH)"
    IMAGE_REF="$(read_bundle_value IMAGE_REF)"
    DOCKER_VERSION="$(read_bundle_value DOCKER_VERSION)"
    COMPOSE_VERSION="$(read_bundle_value COMPOSE_VERSION)"

    [[ "${BUNDLE_VERSION}" =~ ^[A-Za-z0-9._-]+$ ]] || die "BUNDLE_VERSION 非法"
    [ "${DEPLOYMENT_MODE}" = "strict-offline" ] || die "拒绝部署：交付包未声明 DEPLOYMENT_MODE=strict-offline"
    [ "${REQUIRES_INTERNET}" = "0" ] || die "拒绝部署：交付包声明需要互联网"
    for payload_flag in CONTAINS_APPLICATION_IMAGE CONTAINS_DOCKER_RUNTIME CONTAINS_COMPOSE CONTAINS_EMBEDDING_MODEL; do
        [ "$(read_bundle_value "${payload_flag}")" = "1" ] || die "拒绝部署：交付包缺少离线能力标记 ${payload_flag}=1"
    done
    [[ "${IMAGE_REF}" =~ ^[A-Za-z0-9._/:@-]+$ ]] || die "IMAGE_REF 非法"
    [[ "${DOCKER_VERSION}" =~ ^[0-9][0-9A-Za-z._-]+$ ]] || die "DOCKER_VERSION 非法"
    [[ "${COMPOSE_VERSION}" =~ ^v?[0-9][0-9A-Za-z._-]+$ ]] || die "COMPOSE_VERSION 非法"
    [ "${BUNDLE_ARCH}" = "${host_arch}" ] || die "交付包架构为 ${BUNDLE_ARCH}，客户机架构为 ${host_arch}，不能部署"
fi

verify_checksums() {
    step "校验交付包完整性"
    if command -v sha256sum >/dev/null 2>&1; then
        (cd "${SCRIPT_DIR}" && sha256sum -c SHA256SUMS)
    elif command -v shasum >/dev/null 2>&1; then
        (cd "${SCRIPT_DIR}" && shasum -a 256 -c SHA256SUMS)
    else
        die "客户机缺少 sha256sum/shasum，无法校验交付包完整性"
    fi
    info "交付包 SHA-256 校验通过"
}

announce_offline_contract() {
    step "确认严格离线部署契约"
    info "现场安装不调用包管理器，不下载依赖，不构建或拉取镜像"
    info "应用镜像来源=docker load；Compose 拉取策略=never；内置数据库=ChromaDB/SQLite + JSON"
}

docker_ready() {
    command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}

compose_ready() {
    docker compose version >/dev/null 2>&1
}

need_root() {
    [ "${EUID}" -eq 0 ] || die "该操作需要 root 权限，请使用 sudo 运行"
}

install_docker_runtime() {
    local runtime_archive="${SCRIPT_DIR}/runtime/docker-${DOCKER_VERSION}.tgz"
    local compose_binary="${SCRIPT_DIR}/runtime/docker-compose-linux-${BUNDLE_ARCH}"
    local have_systemd=0
    [ -f "${runtime_archive}" ] || die "交付包缺少 ${runtime_archive#${SCRIPT_DIR}/}"
    [ -f "${compose_binary}" ] || die "交付包缺少 ${compose_binary#${SCRIPT_DIR}/}"
    if command -v systemctl >/dev/null 2>&1 && systemctl show-environment >/dev/null 2>&1; then
        have_systemd=1
    else
        warn "现场没有可用 systemd，将使用包内 dockerd 直接启动；首次部署仍可完成"
    fi
    if [ -e /etc/systemd/system/docker.service ] || [ -e /usr/lib/systemd/system/docker.service ] || [ -e /lib/systemd/system/docker.service ]; then
        if [ "${have_systemd}" -eq 1 ]; then
            die "检测到已有 docker.service，但 Docker 不可用；为避免覆盖客户运行时，请先由现场管理员处理"
        fi
        warn "检测到现有 docker.service，但 systemd 不可用；不会覆盖该单元，将使用兼容性直接启动"
    fi
    for existing_binary in containerd containerd-shim-runc-v2 ctr docker dockerd docker-init docker-proxy runc; do
        if command -v "${existing_binary}" >/dev/null 2>&1; then
            die "检测到已有 ${existing_binary}，拒绝用包内运行时覆盖；请使用 --skip-docker-install 并修复现有 Docker"
        fi
    done

    step "安装交付包内 Docker Engine ${DOCKER_VERSION} 与 Compose ${COMPOSE_VERSION}"
    local stage
    stage="$(mktemp -d /tmp/agenticsre-docker.XXXXXX)"
    trap 'rm -rf "${stage:-}"' RETURN
    tar -xzf "${runtime_archive}" -C "${stage}"
    [ -d "${stage}/docker" ] || die "Docker 静态包结构无效"
    for binary in containerd containerd-shim-runc-v2 ctr docker dockerd docker-init docker-proxy runc; do
        [ -f "${stage}/docker/${binary}" ] || die "Docker 静态包缺少 ${binary}"
        install -m 0755 "${stage}/docker/${binary}" "/usr/local/bin/${binary}"
    done
    install -d -m 0755 /usr/local/lib/docker/cli-plugins
    install -m 0755 "${compose_binary}" /usr/local/lib/docker/cli-plugins/docker-compose

    install -d -m 0755 /etc/docker
    if [ ! -f /etc/docker/daemon.json ]; then
        if [ -n "${DOCKER_STORAGE_DRIVER}" ]; then
            cat > /etc/docker/daemon.json <<EOF
{
  "storage-driver": "${DOCKER_STORAGE_DRIVER}",
  "features": {"containerd-snapshotter": false},
  "bridge": "none",
  "iptables": false,
  "ip-forward": false,
  "ip-masq": false,
  "log-driver": "json-file",
  "log-opts": {"max-size": "20m", "max-file": "5"}
}
EOF
        else
            cat > /etc/docker/daemon.json <<'EOF'
{
  "bridge": "none",
  "iptables": false,
  "ip-forward": false,
  "ip-masq": false,
  "log-driver": "json-file",
  "log-opts": {"max-size": "20m", "max-file": "5"}
}
EOF
        fi
    else
        warn "保留现有 /etc/docker/daemon.json，不覆盖客户已有 Docker 策略"
    fi

    if [ "${have_systemd}" -eq 1 ]; then
        install -d -m 0755 /etc/systemd/system
        install -m 0644 /dev/null /etc/systemd/system/docker.service
        cat > /etc/systemd/system/docker.service <<'EOF'
[Unit]
Description=Docker Application Container Engine
Documentation=https://docs.docker.com
After=local-fs.target

[Service]
# The installer performs its own `docker info` readiness loop. Type=simple is
# compatible with older/restricted systemd environments and avoids relying on
# sd_notify plumbing that is frequently unavailable on isolated appliances.
Type=simple
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=HOME=/root
WorkingDirectory=/
ExecStart=/usr/local/bin/dockerd --host=unix:///var/run/docker.sock
ExecReload=/bin/kill -s HUP $MAINPID
StandardOutput=append:/var/log/agenticsre-docker.log
StandardError=append:/var/log/agenticsre-docker.log
TimeoutStartSec=0
Restart=always
RestartSec=2
LimitNOFILE=infinity

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        systemctl enable docker
        systemctl start docker || warn "Docker systemd 启动命令返回失败，将在短时轮询后尝试直接启动"
    fi

    local waited=0 direct_pid=""
    if [ "${have_systemd}" -eq 1 ]; then
        until docker info >/dev/null 2>&1 || [ "${waited}" -ge 12 ]; do
            sleep 2
            waited=$((waited + 2))
        done
    fi
    if ! docker info >/dev/null 2>&1; then
        [ "${have_systemd}" -eq 1 ] && warn "systemd 未能托管 Docker，启用兼容性直接启动；日志：/var/log/agenticsre-docker.log"
        if [ "${have_systemd}" -eq 1 ]; then
            systemctl stop docker >/dev/null 2>&1 || true
        fi
        nohup /usr/local/bin/dockerd --host=unix:///var/run/docker.sock \
            </dev/null >>/var/log/agenticsre-docker.log 2>&1 &
        direct_pid="$!"
        printf '%s\n' "${direct_pid}" > /run/agenticsre-dockerd.pid
        until docker info >/dev/null 2>&1 || [ "${waited}" -ge 72 ]; do
            if ! kill -0 "${direct_pid}" >/dev/null 2>&1; then
                die "Docker 直接启动进程已退出，请查看 /var/log/agenticsre-docker.log"
            fi
            sleep 2
            waited=$((waited + 2))
        done
    fi
    docker info >/dev/null 2>&1 || die "Docker 服务启动超时，请查看 /var/log/agenticsre-docker.log 或 journalctl -u docker"
    compose_ready || die "Docker Compose 插件安装后仍不可用"
    info "Docker 离线安装完成"
    trap - RETURN
    rm -rf "${stage}"
}

compose_cmd() {
    docker compose \
        --project-name agenticsre \
        --env-file "${RUNTIME_ENV}" \
        --project-directory "${CURRENT_RELEASE}" \
        -f "${CURRENT_RELEASE}/docker-compose.offline.yaml" "$@"
}

health_check() {
    step "等待 AgenticSRE 健康检查"
    local waited=0
    while [ "${waited}" -lt "${HEALTH_TIMEOUT}" ]; do
        if docker exec agenticsre python -c \
            'import json,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:8080/api/health", timeout=3)); assert d.get("status") == "ok"' \
            >/dev/null 2>&1 \
            && docker exec agenticsre-skill-runtime python -c \
            'import json,os,urllib.request; p=os.environ.get("SKILL_RUNTIME_PORT", "8090"); d=json.load(urllib.request.urlopen("http://127.0.0.1:"+p+"/api/health", timeout=3)); assert d.get("status") == "ok"' \
            >/dev/null 2>&1; then
            info "Dashboard 与 Skill Runtime 均已就绪"
            return 0
        fi
        sleep 3
        waited=$((waited + 3))
    done
    warn "健康检查超时；最近日志如下："
    docker logs --tail 80 agenticsre 2>&1 || true
    docker logs --tail 80 agenticsre-skill-runtime 2>&1 || true
    return 1
}

runtime_value() {
    local key="$1"
    awk -F= -v wanted="${key}" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' "${RUNTIME_ENV}"
}

load_installation() {
    INSTALLATION_ENV="${INSTALL_ROOT}/installation.env"
    [ -f "${INSTALLATION_ENV}" ] || die "尚未安装 AgenticSRE：${INSTALLATION_ENV} 不存在"
    INSTALL_ROOT="$(installation_value INSTALL_ROOT)"
    STATE_ROOT="$(installation_value STATE_ROOT)"
    CURRENT_RELEASE="$(installation_value CURRENT_RELEASE)"
    RUNTIME_ENV="$(installation_value RUNTIME_ENV)"
    for installed_path in "${INSTALL_ROOT}" "${STATE_ROOT}" "${CURRENT_RELEASE}" "${RUNTIME_ENV}"; do
        [[ "${installed_path}" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "installation.env 包含非法路径"
    done
    [ -f "${RUNTIME_ENV}" ] || die "运行配置不存在：${RUNTIME_ENV}"
    docker_ready || die "Docker 服务不可用"
}

installation_value() {
    local key="$1" value
    value="$(awk -F= -v wanted="${key}" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' "${INSTALLATION_ENV}")"
    # Accept metadata generated by earlier revisions, which used single quotes.
    value="${value#\'}"
    value="${value%\'}"
    printf '%s\n' "${value}"
}

data_check() {
    local data_dir="${1:-${STATE_ROOT}/data}"
    local image_ref="${2:-}"
    [ -n "${image_ref}" ] || image_ref="$(runtime_value AGENTICSRE_IMAGE)"
    [[ "${image_ref}" =~ ^[A-Za-z0-9._/:@-]+$ ]] || die "持久化配置中的 AGENTICSRE_IMAGE 非法"
    [ -d "${data_dir}" ] || die "持久化数据目录不存在：${data_dir}"
    step "校验嵌入式数据库与 JSON 状态"
    docker run --rm --network none --entrypoint python \
        --volume "${data_dir}:/app/data:ro,z" \
        "${image_ref}" /app/scripts/validate_persistent_data.py --path /app/data || return 1
    info "持久化数据一致性校验通过"
}

service_is_running() {
    [ "$(docker inspect --format '{{.State.Running}}' agenticsre 2>/dev/null || true)" = "true" ]
}

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

create_stopped_backup() {
    local destination="$1"
    local destination_dir temp_file checksum
    [[ "${destination}" = /* ]] || destination="$(pwd)/${destination}"
    case "${destination}" in
        "${STATE_ROOT}/config"/*|"${STATE_ROOT}/data"/*)
            die "备份文件不能写入正在备份的 config/data 目录"
            ;;
    esac
    destination_dir="$(dirname "${destination}")"
    mkdir -p "${destination_dir}"
    [ ! -e "${destination}" ] && [ ! -e "${destination}.sha256" ] || die "备份目标已存在，拒绝覆盖：${destination}"
    temp_file="${destination}.part"
    [ ! -e "${temp_file}" ] || die "备份临时文件已存在：${temp_file}"
    tar -czf "${temp_file}" -C "${STATE_ROOT}" config data
    checksum="$(sha256_file "${temp_file}")"
    mv "${temp_file}" "${destination}"
    printf '%s  %s\n' "${checksum}" "$(basename "${destination}")" > "${destination}.sha256"
    chmod 0600 "${destination}" "${destination}.sha256"
}

backup_data() {
    local destination was_running=0
    destination="${BACKUP_FILE:-${STATE_ROOT}/backups/agenticsre-backup-$(date +%Y%m%d-%H%M%S).tar.gz}"
    [[ "${destination}" != *$'\n'* ]] || die "备份路径不能包含换行"
    service_is_running && was_running=1
    step "停止写入以创建一致性备份"
    compose_cmd stop
    if ! data_check; then
        [ "${was_running}" -eq 0 ] || compose_cmd up -d --no-build --pull never
        die "持久化数据校验失败，未生成备份"
    fi
    create_stopped_backup "${destination}"
    if [ "${was_running}" -eq 1 ]; then
        compose_cmd up -d --no-build --pull never
        health_check || die "备份已生成，但服务恢复启动失败"
    fi
    info "一致性备份已生成：${destination}"
    info "校验文件：${destination}.sha256"
}

check_restore_archive() {
    local archive="$1" entry normalized entry_type
    [ -f "${archive}" ] || die "备份文件不存在：${archive}"
    if [ -f "${archive}.sha256" ]; then
        local expected actual
        expected="$(awk '{print $1; exit}' "${archive}.sha256")"
        actual="$(sha256_file "${archive}")"
        [ "${expected}" = "${actual}" ] || die "备份 SHA-256 校验失败"
    else
        warn "备份没有 .sha256 侧车文件，仅执行内容结构和数据一致性校验"
    fi
    while IFS= read -r entry; do
        normalized="${entry#./}"
        case "${normalized}" in
            config|config/|config/*|data|data/|data/*) ;;
            *) die "备份包含越界路径：${entry}" ;;
        esac
        [[ "/${normalized}/" != *"/../"* && "${normalized}" != /* ]] || die "备份包含不安全路径：${entry}"
    done < <(tar -tzf "${archive}")
    # Only regular files and directories are valid. Links and device nodes can
    # escape the staging directory even if the textual path looks harmless.
    while IFS= read -r entry_type; do
        case "${entry_type}" in
            -|d) ;;
            *) die "备份包含非普通文件/目录项（类型 ${entry_type}）" ;;
        esac
    done < <(tar -tvzf "${archive}" | awk '{print substr($1,1,1)}')
}

rewrite_deployment_values() {
    local image_ref="$1" pair key value
    for pair in \
        "AGENTICSRE_IMAGE=${image_ref}" \
        "AGENTICSRE_ENV_FILE=${RUNTIME_ENV}" \
        "AGENTICSRE_DATA_DIR=${STATE_ROOT}/data" \
        "AGENTICSRE_LOG_DIR=${STATE_ROOT}/logs" \
        "AGENTICSRE_KUBECONFIG_FILE=${STATE_ROOT}/kube/config" \
        "AGENTICSRE_SSH_DIR=${STATE_ROOT}/ssh" \
        "AGENTICSRE_OFFLINE_MODE=true"; do
        key="${pair%%=*}"
        value="${pair#*=}"
        if grep -q "^${key}=" "${RUNTIME_ENV}"; then
            sed -i "s|^${key}=.*|${key}=${value}|" "${RUNTIME_ENV}"
        else
            printf '%s=%s\n' "${key}" "${value}" >> "${RUNTIME_ENV}"
        fi
    done
    chmod 0600 "${RUNTIME_ENV}"
}

restore_data() {
    local archive="${RESTORE_FILE}" was_running=0 stamp stage rollback old_data old_config
    check_restore_archive "${archive}"
    stamp="$(date +%Y%m%d-%H%M%S)"
    stage="$(mktemp -d "${STATE_ROOT}/.restore-${stamp}.XXXXXX")"
    tar -xzf "${archive}" --no-same-owner -C "${stage}"
    [ -d "${stage}/data" ] && [ -f "${stage}/config/agenticsre.env" ] || die "备份缺少 data/ 或 config/agenticsre.env"
    step "在隔离暂存目录预校验待恢复数据"
    data_check "${stage}/data"

    service_is_running && was_running=1
    step "预校验通过，停止写入并切换持久化数据"
    compose_cmd stop
    rollback="${STATE_ROOT}/backups/pre-restore-${stamp}.tar.gz"
    create_stopped_backup "${rollback}"
    old_data="${STATE_ROOT}/.pre-restore-data-${stamp}"
    old_config="${STATE_ROOT}/.pre-restore-config-${stamp}"
    mv "${STATE_ROOT}/data" "${old_data}"
    mv "${STATE_ROOT}/config" "${old_config}"
    mv "${stage}/data" "${STATE_ROOT}/data"
    mv "${stage}/config" "${STATE_ROOT}/config"
    RUNTIME_ENV="${STATE_ROOT}/config/agenticsre.env"
    # The installed release image is authoritative across machine/path changes.
    rewrite_deployment_values "$(awk -F= '$1 == "IMAGE_REF" {print $2; exit}' "${CURRENT_RELEASE}/BUNDLE.env")"

    if data_check && compose_cmd up -d --no-build --pull never && health_check; then
        rm -rf "${old_data}" "${old_config}" "${stage}"
        if [ "${was_running}" -eq 0 ]; then
            compose_cmd stop
            info "恢复前服务处于停止状态，验证后已恢复为停止状态"
        fi
        info "备份恢复完成：${archive}"
        info "恢复前自动备份：${rollback}"
        return 0
    fi

    warn "恢复后验证失败，正在回滚到恢复前数据"
    compose_cmd down >/dev/null 2>&1 || true
    mv "${STATE_ROOT}/data" "${STATE_ROOT}/failed-restore-data-${stamp}"
    mv "${STATE_ROOT}/config" "${STATE_ROOT}/failed-restore-config-${stamp}"
    mv "${old_data}" "${STATE_ROOT}/data"
    mv "${old_config}" "${STATE_ROOT}/config"
    RUNTIME_ENV="${STATE_ROOT}/config/agenticsre.env"
    [ "${was_running}" -eq 0 ] || compose_cmd up -d --no-build --pull never
    die "恢复失败且已回滚；失败数据保留在 ${STATE_ROOT}/failed-restore-*-${stamp}"
}

# Once an upgrade stops the old writer, any unexpected shell failure must not
# leave the customer with a stopped service or a half-switched database. The
# normal validation path clears this guard explicitly.
UPGRADE_GUARD_ACTIVE=0
emergency_upgrade_rollback() {
    local exit_code=$?
    trap - EXIT
    if [ "${UPGRADE_GUARD_ACTIVE:-0}" = "1" ]; then
        set +e
        warn "升级过程意外中断，正在恢复升级前程序、配置和数据"
        compose_cmd down >/dev/null 2>&1 || true
        if [ -n "${PRE_UPGRADE_DATA_BACKUP:-}" ] && [ -f "${PRE_UPGRADE_DATA_BACKUP}" ]; then
            local emergency_stamp emergency_state
            emergency_stamp="$(date +%Y%m%d-%H%M%S)-$$"
            emergency_state="${STATE_ROOT}/failed-upgrade-interrupted-${emergency_stamp}"
            mkdir -p "${emergency_state}"
            [ ! -d "${STATE_ROOT}/data" ] || mv "${STATE_ROOT}/data" "${emergency_state}/"
            [ ! -d "${STATE_ROOT}/config" ] || mv "${STATE_ROOT}/config" "${emergency_state}/"
            tar -xzf "${PRE_UPGRADE_DATA_BACKUP}" --no-same-owner -C "${STATE_ROOT}" || true
            RUNTIME_ENV="${STATE_ROOT}/config/agenticsre.env"
        fi
        if [ -n "${PREVIOUS_RUNTIME_ENV_BACKUP:-}" ] && [ -f "${PREVIOUS_RUNTIME_ENV_BACKUP}" ]; then
            mkdir -p "$(dirname "${RUNTIME_ENV}")"
            install -m 0600 "${PREVIOUS_RUNTIME_ENV_BACKUP}" "${RUNTIME_ENV}" || true
        fi
        if [ -n "${PREVIOUS_RELEASE:-}" ] && [ -d "${PREVIOUS_RELEASE}" ]; then
            ln -sfn "${PREVIOUS_RELEASE}" "${CURRENT_RELEASE}" || true
            if [ "${PREVIOUS_WAS_RUNNING:-0}" -eq 1 ]; then
                compose_cmd up -d --no-build --pull never >/dev/null 2>&1 || true
            fi
        fi
    fi
    exit "${exit_code}"
}
trap emergency_upgrade_rollback EXIT

if [[ "${ACTION}" =~ ^(install|verify)$ ]]; then
    verify_checksums
    announce_offline_contract
fi

if [ "${ACTION}" = "verify" ]; then
    [ -f "${SCRIPT_DIR}/runtime/docker-${DOCKER_VERSION}.tgz" ] || die "缺少 Docker Engine 离线包"
    [ -f "${SCRIPT_DIR}/runtime/docker-compose-linux-${BUNDLE_ARCH}" ] || die "缺少 Compose 离线二进制"
    info "严格离线包预检通过：version=${BUNDLE_VERSION}, arch=${BUNDLE_ARCH}, image=${IMAGE_REF}"
    exit 0
fi

if [[ "${ACTION}" =~ ^(status|stop|restart|data-check|backup|restore)$ ]]; then
    need_root
    load_installation
    case "${ACTION}" in
        status)
            compose_cmd ps
            health_check
            exit $?
            ;;
        stop)
            compose_cmd down
            info "服务已停止；配置与数据保留在 ${STATE_ROOT}"
            exit 0
            ;;
        restart)
            compose_cmd restart
            health_check
            exit $?
            ;;
        data-check)
            data_check
            exit $?
            ;;
        backup)
            backup_data
            exit $?
            ;;
        restore)
            restore_data
            exit $?
            ;;
    esac
fi

need_root

if [ -e /usr/local/sbin/agenticsrectl ] && [ ! -L /usr/local/sbin/agenticsrectl ]; then
    die "/usr/local/sbin/agenticsrectl 已存在且不是符号链接，拒绝覆盖"
fi

if command -v docker >/dev/null 2>&1 && ! docker_ready; then
    if command -v systemctl >/dev/null 2>&1; then
        warn "检测到 Docker 命令但服务不可用，尝试启动现有服务"
        systemctl start docker >/dev/null 2>&1 || true
        waited=0
        until docker_ready || [ "${waited}" -ge 30 ]; do
            sleep 2
            waited=$((waited + 2))
        done
    fi
    docker_ready || die "客户机已有 Docker，但 daemon 不可用；为避免覆盖客户安装，请先修复现有 Docker"
fi

if ! docker_ready; then
    [ "${INSTALL_DOCKER}" != "never" ] || die "Docker 不可用，且已指定 --skip-docker-install"
    install_docker_runtime
else
    info "复用客户机现有 Docker Engine：$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown)"
fi

if ! compose_ready; then
    compose_binary="${SCRIPT_DIR}/runtime/docker-compose-linux-${BUNDLE_ARCH}"
    [ -f "${compose_binary}" ] || die "Docker Compose 不可用且交付包中没有 Compose 二进制"
    install -d -m 0755 /usr/local/lib/docker/cli-plugins
    install -m 0755 "${compose_binary}" /usr/local/lib/docker/cli-plugins/docker-compose
    compose_ready || die "Docker Compose 安装失败"
fi

step "加载 AgenticSRE 离线镜像"
docker load -i "${IMAGE_ARCHIVE}"
docker image inspect "${IMAGE_REF}" >/dev/null 2>&1 || die "镜像加载后未找到 ${IMAGE_REF}"
image_arch="$(docker image inspect "${IMAGE_REF}" --format '{{.Architecture}}')"
[ "${image_arch}" = "${BUNDLE_ARCH}" ] || die "镜像架构 ${image_arch} 与交付包架构 ${BUNDLE_ARCH} 不一致"

step "创建稳定的安装与持久化目录"
RELEASE_DIR="${INSTALL_ROOT}/releases/${BUNDLE_VERSION}-${BUNDLE_ARCH}"
CURRENT_RELEASE="${INSTALL_ROOT}/current"
RUNTIME_ENV="${STATE_ROOT}/config/agenticsre.env"
PREVIOUS_RELEASE=""
PREVIOUS_RUNTIME_ENV_BACKUP=""
PRE_UPGRADE_DATA_BACKUP=""
PREVIOUS_WAS_RUNNING=0
if [ -L "${CURRENT_RELEASE}" ]; then
    PREVIOUS_RELEASE="$(readlink -f "${CURRENT_RELEASE}" 2>/dev/null || true)"
fi
mkdir -p "${RELEASE_DIR}" "${STATE_ROOT}/config" "${STATE_ROOT}/data" \
    "${STATE_ROOT}/logs" "${STATE_ROOT}/kube" "${STATE_ROOT}/ssh" "${STATE_ROOT}/backups"
install -m 0644 "${COMPOSE_FILE_SOURCE}" "${RELEASE_DIR}/docker-compose.offline.yaml"
install -m 0644 "${BUNDLE_ENV}" "${RELEASE_DIR}/BUNDLE.env"
if [ -e "${CURRENT_RELEASE}" ] && [ ! -L "${CURRENT_RELEASE}" ]; then
    die "${CURRENT_RELEASE} 已存在且不是符号链接；为避免覆盖未知目录，部署已停止"
fi
touch "${STATE_ROOT}/kube/config"
chmod 0600 "${STATE_ROOT}/kube/config"
chmod 0700 "${STATE_ROOT}/ssh"

if [ -n "${PREVIOUS_RELEASE}" ] && [ -f "${RUNTIME_ENV}" ]; then
    PREVIOUS_RUNTIME_ENV_BACKUP="${STATE_ROOT}/backups/pre-upgrade-${BUNDLE_VERSION}-${BUNDLE_ARCH}.env"
    [ ! -e "${PREVIOUS_RUNTIME_ENV_BACKUP}" ] || PREVIOUS_RUNTIME_ENV_BACKUP="${PREVIOUS_RUNTIME_ENV_BACKUP}.$(date +%Y%m%d-%H%M%S)"
    install -m 0600 "${RUNTIME_ENV}" "${PREVIOUS_RUNTIME_ENV_BACKUP}"

    service_is_running && PREVIOUS_WAS_RUNNING=1
    step "停止旧版写入并创建升级前数据快照"
    compose_cmd stop
    if ! data_check "${STATE_ROOT}/data" "${IMAGE_REF}"; then
        [ "${PREVIOUS_WAS_RUNNING}" -eq 0 ] || compose_cmd up -d --no-build --pull never
        die "升级前数据校验失败，已取消升级"
    fi
    PRE_UPGRADE_DATA_BACKUP="${STATE_ROOT}/backups/pre-upgrade-${BUNDLE_VERSION}-${BUNDLE_ARCH}-$(date +%Y%m%d-%H%M%S).tar.gz"
    if ! create_stopped_backup "${PRE_UPGRADE_DATA_BACKUP}"; then
        [ "${PREVIOUS_WAS_RUNNING}" -eq 0 ] || compose_cmd up -d --no-build --pull never
        die "无法创建升级前一致性快照，已取消升级"
    fi
    UPGRADE_GUARD_ACTIVE=1
    info "升级前数据快照：${PRE_UPGRADE_DATA_BACKUP}"
fi

if [ -n "${CONFIG_SOURCE}" ]; then
    [ -f "${CONFIG_SOURCE}" ] || die "客户配置文件不存在：${CONFIG_SOURCE}"
    install -m 0600 "${CONFIG_SOURCE}" "${RUNTIME_ENV}"
elif [ ! -f "${RUNTIME_ENV}" ]; then
    generated_password="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
    cat > "${RUNTIME_ENV}" <<EOF
# AgenticSRE customer-site persistent configuration.
AGENTICSRE_IMAGE=${IMAGE_REF}
AGENTICSRE_ENV_FILE=${RUNTIME_ENV}
AGENTICSRE_DATA_DIR=${STATE_ROOT}/data
AGENTICSRE_LOG_DIR=${STATE_ROOT}/logs
AGENTICSRE_KUBECONFIG_FILE=${STATE_ROOT}/kube/config
AGENTICSRE_SSH_DIR=${STATE_ROOT}/ssh
AGENTICSRE_OFFLINE_MODE=true
SKILL_RUNTIME_PORT=8090

# LLM: isolated sites should use an on-prem OpenAI-compatible endpoint.
LLM_API_KEY=
LLM_BASE_URL=http://127.0.0.1:8000/v1
LLM_MODEL=deepseek-v4-pro

# Web authentication is enabled by default for customer deployments.
AGENTICSRE_AUTH_ENABLED=true
AGENTICSRE_AUTH_USERNAME=admin
AGENTICSRE_AUTH_PASSWORD=${generated_password}
AGENTICSRE_AUTH_TOKEN=

AGENTICSRE_NO_PROXY=localhost,127.0.0.1,::1,.svc,.cluster.local
EOF
    chmod 0600 "${RUNTIME_ENV}"
    warn "未提供 --config，已生成安全默认配置；LLM 暂未启用"
    warn "首次登录：admin / ${generated_password}"
else
    info "保留现有持久化配置：${RUNTIME_ENV}"
fi

# Deployment-owned values must match the actual installation paths and image.
rewrite_deployment_values "${IMAGE_REF}"

cat > "${INSTALL_ROOT}/installation.env" <<EOF
INSTALL_ROOT=${INSTALL_ROOT}
STATE_ROOT=${STATE_ROOT}
CURRENT_RELEASE=${CURRENT_RELEASE}
RUNTIME_ENV=${RUNTIME_ENV}
EOF
chmod 0600 "${INSTALL_ROOT}/installation.env"

step "启动 AgenticSRE（全程不构建、不拉取镜像）"
ln -sfn "${RELEASE_DIR}" "${CURRENT_RELEASE}"
compose_cmd up -d --no-build --pull never --remove-orphans

deployment_verified=true
health_check || deployment_verified=false
if [ "${deployment_verified}" = "true" ]; then
    data_check || deployment_verified=false
fi
if [ "${deployment_verified}" != "true" ]; then
    if [ -n "${PREVIOUS_RELEASE}" ] && [ -f "${PREVIOUS_RELEASE}/docker-compose.offline.yaml" ] \
        && [ -n "${PREVIOUS_RUNTIME_ENV_BACKUP}" ] && [ -n "${PRE_UPGRADE_DATA_BACKUP}" ]; then
        rollback_stamp="$(date +%Y%m%d-%H%M%S)"
        failed_upgrade_state="${STATE_ROOT}/failed-upgrade-${BUNDLE_VERSION}-${rollback_stamp}"
        warn "新版本未通过服务/数据验证，正在回滚程序、配置和数据库"
        compose_cmd down >/dev/null 2>&1 || true
        mkdir -p "${failed_upgrade_state}"
        mv "${STATE_ROOT}/data" "${STATE_ROOT}/config" "${failed_upgrade_state}/"
        tar -xzf "${PRE_UPGRADE_DATA_BACKUP}" --no-same-owner -C "${STATE_ROOT}"
        RUNTIME_ENV="${STATE_ROOT}/config/agenticsre.env"
        install -m 0600 "${PREVIOUS_RUNTIME_ENV_BACKUP}" "${RUNTIME_ENV}"
        ln -sfn "${PREVIOUS_RELEASE}" "${CURRENT_RELEASE}"
        if data_check; then
            if [ "${PREVIOUS_WAS_RUNNING}" -eq 0 ]; then
                UPGRADE_GUARD_ACTIVE=0
                die "新版本部署失败，已恢复升级前程序、配置和数据；旧服务仍保持停止"
            fi
            if compose_cmd up -d --no-build --pull never && health_check; then
                UPGRADE_GUARD_ACTIVE=0
                die "新版本部署失败，已自动恢复上一版本及数据库；失败现场保留在 ${failed_upgrade_state}"
            fi
        fi
        UPGRADE_GUARD_ACTIVE=0
        die "新版本部署失败，上一版本自动恢复也未通过校验；快照与失败现场均已保留"
    fi
    die "首次部署未通过服务/数据验证，服务和数据已保留以便现场排障"
fi
UPGRADE_GUARD_ACTIVE=0

cat > "${INSTALL_ROOT}/agenticsrectl" <<EOF
#!/usr/bin/env bash
if [ "\$#" -eq 0 ]; then set -- --status; fi
exec "${CURRENT_RELEASE}/install-offline.sh" "\$@"
EOF
# Keep a copy of the exact installer so the control wrapper remains usable after
# the USB/release archive is removed.
install -m 0755 "${SCRIPT_DIR}/install-offline.sh" "${RELEASE_DIR}/install-offline.sh"
chmod 0755 "${INSTALL_ROOT}/agenticsrectl"
ln -sfn "${INSTALL_ROOT}/agenticsrectl" /usr/local/sbin/agenticsrectl

offline_audit_file="${STATE_ROOT}/offline-install-audit.env"
cat > "${offline_audit_file}" <<EOF
AUDIT_VERSION=1
RESULT=PASS
INSTALLED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
BUNDLE_VERSION=${BUNDLE_VERSION}
BUNDLE_ARCH=${BUNDLE_ARCH}
DEPLOYMENT_MODE=${DEPLOYMENT_MODE}
REQUIRES_INTERNET=${REQUIRES_INTERNET}
IMAGE_REF=${IMAGE_REF}
IMAGE_LOAD_METHOD=docker-load
COMPOSE_BUILD_POLICY=disabled
COMPOSE_PULL_POLICY=never
INSTALLER_NETWORK_FETCHES=0
BUNDLE_MANIFEST_SHA256=$(sha256_file "${CHECKSUM_FILE}")
IMAGE_ARCHIVE_SHA256=$(sha256_file "${IMAGE_ARCHIVE}")
PERSISTENCE_CHECK=PASS
HEALTH_CHECK=PASS
EOF
chmod 0600 "${offline_audit_file}"

host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "${host_ip}" ] || host_ip="127.0.0.1"
printf '\n'
printf '%bAgenticSRE 离线部署成功%b\n' "${GREEN}" "${NC}"
printf '  Dashboard: http://%s:8080\n' "${host_ip}"
printf '  持久化配置: %s\n' "${RUNTIME_ENV}"
printf '  持久化数据: %s/data\n' "${STATE_ROOT}"
printf '  离线安装审计: %s\n' "${offline_audit_file}"
printf '  查看状态: sudo agenticsrectl --status\n'
printf '  数据校验: sudo agenticsrectl --data-check\n'
printf '  数据备份: sudo agenticsrectl --backup\n'
printf '  查看日志: docker logs -f agenticsre\n'
printf '  停止服务: sudo agenticsrectl --stop\n'
