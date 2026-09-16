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

DROPIN_DIR="/etc/systemd/system/minas.boot_check.service.d"
DROPIN_FILE="$DROPIN_DIR/90-keep-ssh.conf"
TMP_FILE="$DROPIN_DIR/.90-keep-ssh.conf.tmp.$$"

mkdir -p "$DROPIN_DIR"
trap 'rm -f "$TMP_FILE"' EXIT HUP INT TERM

{
    echo '[Service]'
    echo '# Xiaomi boot_check stops Dropbear when the signed RPMB ssh_en flag is absent.'
    echo '# Restore the SSH socket immediately after the vendor boot check completes.'
    printf 'ExecStartPost=%s start dropbear.socket\n' "$SYSTEMCTL"
} > "$TMP_FILE"

chmod 0644 "$TMP_FILE"
mv -f "$TMP_FILE" "$DROPIN_FILE"
trap - EXIT HUP INT TERM

"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable dropbear.socket >/dev/null
"$SYSTEMCTL" start dropbear.socket

DROPIN_PATHS="$($SYSTEMCTL show minas.boot_check.service -p DropInPaths --value)"
EXEC_START_POST="$($SYSTEMCTL show minas.boot_check.service -p ExecStartPost --value)"

case "$DROPIN_PATHS" in
    *"$DROPIN_FILE"*) ;;
    *)
        echo "错误：systemd 未加载修复配置。" >&2
        exit 14
        ;;
esac

case "$EXEC_START_POST" in
    *"dropbear.socket"*) ;;
    *)
        echo "错误：ExecStartPost 未正确加载。" >&2
        exit 15
        ;;
esac

if [ "$("$SYSTEMCTL" is-enabled dropbear.socket)" != "enabled" ]; then
    echo "错误：dropbear.socket 未启用。" >&2
    exit 16
fi

if [ "$("$SYSTEMCTL" is-active dropbear.socket)" != "active" ]; then
    echo "错误：dropbear.socket 未运行。" >&2
    exit 17
fi

echo "修复已安装：$DROPIN_FILE"
echo "dropbear.socket：enabled / active"
echo "设备尚未重启。"
REMOTE_SCRIPT

echo "完成。重启后可使用以下命令验证："
echo "  ssh root@$NAS_IP 'systemctl is-active dropbear.socket'"
