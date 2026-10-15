#!/usr/bin/env python3
"""Build standalone shell entry points from reviewed, secret-free source files."""
import base64
import io
from pathlib import Path
import tarfile

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent


def build_bridge_entrypoint():
    source = (HERE / 'docker_bridge_repair.py').read_text(encoding='utf-8').rstrip()
    wrapper = ('#!/bin/sh\n# Generated from docker_bridge_repair.py; edit the Python source and rebuild.\n'
               'set -eu\numask 077\ncommand -v python3 >/dev/null 2>&1 || { echo "缺少 python3" >&2; exit 2; }\n'
               'worker=$(mktemp /tmp/minas-docker-bridge.XXXXXXXX)\n'
               'trap \'rm -f -- "$worker"\' EXIT\n'
               'cat > "$worker" <<\'MINAS_DOCKER_BRIDGE_PY\'\n' + source + '\nMINAS_DOCKER_BRIDGE_PY\n'
               'python3 -B "$worker" "$@"\n')
    (HERE / 'fix-docker-bridge.sh').write_text(wrapper, encoding='utf-8', newline='\n')


def build():
    build_bridge_entrypoint()
    plugin = PROJECT / 'ssh-recovery-plugin/payload/system'
    if plugin.is_dir():
        for source, target in [('ssh-recovery-helper', 'recover-ssh.py'), ('restore-ssh.sh', 'restore-ssh.sh')]:
            (HERE / target).write_bytes((plugin / source).read_bytes().replace(b'\r\n', b'\n'))
    helper = (HERE / 'recover-ssh.py').read_text(encoding='utf-8')
    worker = (HERE / 'restore-ssh.sh').read_text(encoding='utf-8').rstrip()
    installer = HERE / 'install-ssh-autostart-fix.sh'
    if not installer.exists():
        installer.write_text((PROJECT / installer.name).read_text(encoding='utf-8'), encoding='utf-8')
    text = installer.read_text(encoding='utf-8')
    start = "<<'SSH_RESTORE_WORKER'\n"
    first, rest = text.split(start, 1)
    _, last = rest.split('\nSSH_RESTORE_WORKER', 1)
    text = first + start + worker + '\nSSH_RESTORE_WORKER' + last
    begin, end = '# BEGIN GENERATED SSH REPAIR TOOL', '# END GENERATED SSH REPAIR TOOL'
    block = (begin + '\n' + 'tool_tmp="$WORKER_DIR/.recover-ssh.py.tmp.$$"\n'
             'cat > "$tool_tmp" <<\'SSH_RECOVERY_PYTHON\'\n' + helper.rstrip() + '\nSSH_RECOVERY_PYTHON\n'
             'chmod 0700 "$tool_tmp"\nmv -f "$tool_tmp" "$WORKER_DIR/recover-ssh.py"\n'
             '/usr/bin/python3 "$WORKER_DIR/recover-ssh.py" --prepare-boot\n' + end)
    if begin in text:
        text = text.split(begin, 1)[0] + block + text.split(end, 1)[1]
    else:
        text = text.replace('/bin/sh -n "$TMP_WORKER"', block + '\n\n/bin/sh -n "$TMP_WORKER"', 1)
    marker = '# Verify a real command: mi-shell can return exit code 0 without executing it.'
    if marker not in text:
        point = 'ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" /bin/sh -s'
        check = (marker + '\nprobe=$(ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \'printf "__MINAS_ROOT_COMMAND_OK__\\n"; id -u\')\n'
                 'if [ "$probe" != "$(printf \'__MINAS_ROOT_COMMAND_OK__\\n0\')" ]; then\n'
                 '    echo "错误：SSH 未执行真实 root 命令。请先用 SSH 恢复插件、开启脚本或 Portainer 应急包恢复命令行。" >&2\n'
                 '    exit 4\nfi\n\n')
        text = text.replace(point, check + point, 1)
    text = text.replace('DEFAULT_NAS_IP="10.31.0.66"', 'DEFAULT_NAS_IP=""')
    key = 'if [[ ! "$NAS_IP" =~'
    if '请输入 NAS IPv4 地址' not in text:
        text = text.replace(key, 'if [[ -z "$NAS_IP" ]]; then read -r -p "请输入 NAS IPv4 地址: " NAS_IP; fi\n\n' + key, 1)
    installer.write_text(text, encoding='utf-8', newline='\n')
    if plugin.is_dir():
        (PROJECT / installer.name).write_text(text, encoding='utf-8', newline='\n')
    memory = io.BytesIO()
    with tarfile.open(fileobj=memory, mode='w:gz') as tf:
        for name in ('open_ssh_client.py', 'recover-ssh.py', 'restore-ssh.sh'):
            data = (HERE / name).read_bytes().replace(b'\r\n', b'\n')
            info = tarfile.TarInfo(name); info.size = len(data); info.mode = 0o600
            tf.addfile(info, io.BytesIO(data))
    blob = base64.encodebytes(memory.getvalue()).decode()
    wrapper = ('#!/usr/bin/env bash\n# Xiaomi NAS SSH bootstrap 2.0.0; old firmware and mi-shell firmware.\n'
               'set -euo pipefail\nfor tool in python3 tar base64 ssh ssh-keygen openssl; do\n'
               ' command -v "$tool" >/dev/null || { echo "缺少命令：$tool" >&2; exit 2; }\ndone\n'
               'stage=$(mktemp -d /tmp/minas-ssh-client.XXXXXX)\nchmod 0700 "$stage"\n'
               'trap \'rm -rf -- "$stage"\' EXIT\nbase64 -d <<\'NAS_SSH_CLIENT_BUNDLE\' | tar -xz -C "$stage"\n' +
               blob + 'NAS_SSH_CLIENT_BUNDLE\npython3 "$stage/open_ssh_client.py" "$@"\n')
    (HERE / 'xiaomi-storage-open-ssh-wsl.sh').write_text(wrapper, encoding='utf-8', newline='\n')
    if plugin.is_dir():
        (PROJECT / 'xiaomi-storage-open-ssh-wsl.sh').write_text(wrapper, encoding='utf-8', newline='\n')


if __name__ == '__main__':
    build()
