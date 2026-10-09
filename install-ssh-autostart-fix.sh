#!/usr/bin/env bash
set -Eeuo pipefail

DEFAULT_NAS_IP="10.31.0.66"
NAS_IP="${1:-$DEFAULT_NAS_IP}"

usage() {
    cat <<'EOF'
用法：
  bash install-ssh-autostart-fix.sh [小米智能存储IP]

示例：
  bash install-ssh-autostart-fix.sh 10.31.0.66

请先运行开启 SSH 的脚本，确认可以使用 root 密钥登录设备。
脚本不会重启设备，也不修改 RPMB。
开机后最多等待 5 分钟，确认小米开机检查结束再恢复 SSH，随后有限复查 12 次。
EOF
}

if [[ "$NAS_IP" == "-h" || "$NAS_IP" == "--help" ]]; then
    usage
    exit 0
fi

if [[ ! "$NAS_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    echo "错误：无效的 IPv4 地址：$NAS_IP" >&2
    usage >&2
    exit 2
fi

IFS='.' read -r -a OCTETS <<< "$NAS_IP"
for OCTET in "${OCTETS[@]}"; do
    if (( 10#$OCTET > 255 )); then
        echo "错误：无效的 IPv4 地址：$NAS_IP" >&2
        exit 2
    fi
done

if ! command -v ssh >/dev/null 2>&1; then
    echo "错误：未找到 ssh 命令。" >&2
    exit 3
fi

SSH_TARGET="root@$NAS_IP"
SSH_OPTIONS=(
    -o BatchMode=yes
    -o ConnectTimeout=10
    -o StrictHostKeyChecking=accept-new
)

echo "正在连接 $SSH_TARGET 并安装开机 SSH 修复……"

ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" /bin/sh -s <<'REMOTE_SCRIPT'
set -eu

if [ "$(id -u)" != "0" ]; then
    echo "错误：远端必须以 root 身份运行。" >&2
    exit 10
fi

SYSTEMCTL="$(command -v systemctl || true)"
if [ -z "$SYSTEMCTL" ]; then
    echo "错误：设备上未找到 systemctl。" >&2
    exit 11
fi

if ! "$SYSTEMCTL" cat minas.boot_check.service >/dev/null 2>&1; then
    echo "错误：设备上不存在 minas.boot_check.service，可能不是受支持的固件。" >&2
    exit 12
fi

if ! "$SYSTEMCTL" cat dropbear.socket >/dev/null 2>&1; then
    echo "错误：设备上不存在 dropbear.socket。" >&2
    exit 13
fi

if ! command -v crond >/dev/null 2>&1; then
    echo "错误：设备上未找到 crond。" >&2
    exit 14
fi

DROPIN_DIR="/etc/systemd/system/minas.boot_check.service.d"
DROPIN_FILE="$DROPIN_DIR/90-keep-ssh.conf"
CRON_FILE="/etc/cron.d/keep-ssh"
TMP_DROPIN="$DROPIN_DIR/.90-keep-ssh.conf.tmp.$$"
TMP_CRON="/etc/cron.d/.keep-ssh.tmp.$$"
WORKER_DIR="/data/ssh-autostart-fix"
WORKER_FILE="$WORKER_DIR/restore-ssh.sh"
TMP_WORKER="$WORKER_DIR/.restore-ssh.tmp.$$"

for tool in flock timeout logger systemd-run; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "错误：设备缺少 $tool。" >&2
        exit 14
    fi
done

mkdir -p "$DROPIN_DIR" /etc/cron.d "$WORKER_DIR"
chmod 0755 "$WORKER_DIR"
cleanup() {
    rm -f "$TMP_DROPIN" "$TMP_CRON" "$TMP_WORKER"
}
trap cleanup EXIT HUP INT TERM

cat > "$TMP_WORKER" <<'SSH_RESTORE_WORKER'
#!/bin/sh
# Bounded boot recovery, not a permanent watchdog. Never changes RPMB or passwords.
set -u
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
WAIT_SECONDS=300
POLL_SECONDS=2
RECHECK_SECONDS=5
RECHECK_COUNT=12
BOOT_UNIT=minas.boot_check.service
SOCKET_UNIT=dropbear.socket

log() {
    logger -t ssh-autostart-fix -- "$*"
}
seconds() {
    read -r elapsed rest < /proc/uptime
    printf '%s\n' "${elapsed%%.*}"
}
boot_finished() {
    active= sub= start=0 end=0 result= job=
    state=$(timeout 8 /usr/bin/systemctl show "$BOOT_UNIT" \
        -p ActiveState -p SubState -p ExecMainStartTimestampMonotonic \
        -p ExecMainExitTimestampMonotonic -p Result -p Job 2>/dev/null) || return 1
    while IFS='=' read -r key value; do
        case "$key" in
            ActiveState) active=$value ;;
            SubState) sub=$value ;;
            ExecMainStartTimestampMonotonic) start=$value ;;
            ExecMainExitTimestampMonotonic) end=$value ;;
            Result) result=$value ;;
            Job) job=$value ;;
        esac
    done <<EOF_STATE
$state
EOF_STATE
    case "$start:$end" in *[!0-9:]*|:*|*:) return 1 ;; esac
    [ "$start" -gt 0 ] && [ "$end" -ge "$start" ] || return 1
    [ -z "$job" ] || [ "$job" = "0" ] || return 1
    case "$active:$sub" in
        active:exited|inactive:dead|failed:failed) return 0 ;;
        *) return 1 ;;
    esac
}
socket_active() {
    timeout 8 /usr/bin/systemctl is-active --quiet "$SOCKET_UNIT"
}
wait_for_boot() {
    while ! boot_finished; do
        if [ "$(seconds)" -ge "$deadline" ]; then
            log "ERROR: boot check did not finish within ${WAIT_SECONDS}s; SSH recovery aborted"
            return 1
        fi
        sleep "$POLL_SECONDS"
    done
}

# Read-only probe used during installation/tests; it does not acquire the boot lock.
if [ "${1:-}" = "--check" ]; then
    if boot_finished && socket_active; then
        printf '%s\n' 'boot-check finished; dropbear.socket active'
        exit 0
    fi
    exit 1
fi

exec 9>/run/ssh-autostart-fix.lock
flock -n 9 || exit 0
deadline=$(( $(seconds) + WAIT_SECONDS ))
log "waiting for $BOOT_UNIT to finish (limit ${WAIT_SECONDS}s)"
wait_for_boot || exit 1
log "boot check finished: state=$active/$sub result=$result; checking SSH"
i=0
while [ "$i" -le "$RECHECK_COUNT" ]; do
    # If the vendor check runs again during this short window, wait for it again.
    wait_for_boot || exit 1
    if ! socket_active; then
        if timeout 10 /usr/bin/systemctl start "$SOCKET_UNIT"; then
            log "dropbear.socket restored after boot check (check $i/$RECHECK_COUNT)"
        else
            log "WARNING: could not start dropbear.socket (check $i/$RECHECK_COUNT)"
        fi
    fi
    if [ "$i" -eq "$RECHECK_COUNT" ]; then
        if socket_active; then
            log "SSH recovery verified; bounded rechecks finished"
            exit 0
        fi
        log "ERROR: SSH remains inactive after bounded rechecks"
        exit 1
    fi
    i=$((i + 1))
    sleep "$RECHECK_SECONDS"
done
SSH_RESTORE_WORKER

/bin/sh -n "$TMP_WORKER"

{
    echo '[Service]'
    echo '# Restore SSH after the Xiaomi boot check when this drop-in is loaded.'
    printf 'ExecStartPost=%s start dropbear.socket\n' "$SYSTEMCTL"
} > "$TMP_DROPIN"

{
    echo 'SHELL=/bin/sh'
    echo 'PATH=/usr/sbin:/usr/bin:/sbin:/bin'
    echo 'MAILTO=""'
    echo
    echo '# Wait for the real boot-check completion, then perform bounded SSH rechecks.'
    echo '@reboot root /bin/sh /data/ssh-autostart-fix/restore-ssh.sh'
} > "$TMP_CRON"

chmod 0644 "$TMP_DROPIN" "$TMP_CRON"
chmod 0755 "$TMP_WORKER"
mv -f "$TMP_WORKER" "$WORKER_FILE"
mv -f "$TMP_DROPIN" "$DROPIN_FILE"
mv -f "$TMP_CRON" "$CRON_FILE"

"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable dropbear.socket >/dev/null
"$SYSTEMCTL" start dropbear.socket
"$SYSTEMCTL" reload crond.service >/dev/null 2>&1 || true

DROPIN_PATHS="$($SYSTEMCTL show minas.boot_check.service -p DropInPaths --value)"
EXEC_START_POST="$($SYSTEMCTL show minas.boot_check.service -p ExecStartPost --value)"

case "$DROPIN_PATHS" in
    *"$DROPIN_FILE"*) ;;
    *)
        echo "错误：systemd 未加载辅助 drop-in。" >&2
        exit 15
        ;;
esac

case "$EXEC_START_POST" in
    *"dropbear.socket"*) ;;
    *)
        echo "错误：ExecStartPost 未正确加载。" >&2
        exit 16
        ;;
esac

if ! grep -Fq '@reboot root' "$CRON_FILE" ||
   ! grep -Fq '/data/ssh-autostart-fix/restore-ssh.sh' "$CRON_FILE"; then
    echo "错误：Cron 开机修复未正确安装。" >&2
    exit 17
fi

if [ "$("$SYSTEMCTL" is-enabled dropbear.socket)" != "enabled" ]; then
    echo "错误：dropbear.socket 未启用。" >&2
    exit 18
fi

if [ "$("$SYSTEMCTL" is-active dropbear.socket)" != "active" ]; then
    echo "错误：dropbear.socket 未运行。" >&2
    exit 19
fi

echo "Cron 开机修复：$CRON_FILE"
echo "状态等待与有限复查脚本：$WORKER_FILE"
echo "systemd 辅助配置：$DROPIN_FILE"
echo "dropbear.socket：enabled / active"
systemd-run --quiet --collect --unit="ssh-autostart-fix-check-$(date +%s)" /bin/sh "$WORKER_FILE"
echo "已启动一次有限复查；使用 journalctl -t ssh-autostart-fix 查看结果。"
echo "设备尚未重启；下次开机在小米开机检查结束后恢复 SSH，不再依赖固定 30 秒等待。"
REMOTE_SCRIPT

echo "安装完成。下次重启后可运行以下命令验证："
echo "  ssh root@$NAS_IP 'systemctl is-active dropbear.socket'"
