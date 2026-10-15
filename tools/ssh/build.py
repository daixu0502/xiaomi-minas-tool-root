#!/usr/bin/env python3
"""Build standalone shell entry points from reviewed, secret-free source files."""
import argparse
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROJECT = ROOT.parent


def build_ssh_entrypoint():
    """Generate two pure shell entry points from the same reviewed worker."""
    client = (HERE / 'src/open-ssh-client.sh').read_text(encoding='utf-8')
    worker = (HERE / 'src/restore-ssh.sh').read_text(encoding='utf-8').rstrip() + '\n'
    shared = PROJECT / 'ssh-recovery-plugin/payload/system/restore-ssh.sh'
    if shared.is_file() and shared.read_text(encoding='utf-8') != worker:
        raise ValueError('Shared SSH worker differs from the plugin; synchronize the reviewed sources first')
    marker = 'MINAS_SSH_REPAIR_SH_SOURCE'
    if marker in worker.splitlines():
        raise ValueError('Source conflicts with heredoc delimiter: ' + marker)
    start = client.index('write_repair_sources() {')
    end = client.index('\nmake_payload() {', start)
    embedded = ('write_repair_sources() {\n'
                '    cat > "$STAGE/restore-ssh.sh" <<\'' + marker + '\'\n' +
                worker + marker + '\n}\n')
    standalone = client[:start] + embedded + client[end:]
    (ROOT / 'xiaomi-storage-open-ssh-wsl.sh').write_text(standalone, encoding='utf-8', newline='\n')
    installer = standalone.replace('then main "$@"; fi', 'then main --install-boot-only "$@"; fi')
    (ROOT / 'install-ssh-autostart-fix.sh').write_text(installer, encoding='utf-8', newline='\n')


def build():
    # Shared worker is shipped by the plugin and both standalone shell entries.
    plugin_worker = PROJECT / 'ssh-recovery-plugin/payload/system/restore-ssh.sh'
    if plugin_worker.is_file():
        (HERE / 'src/restore-ssh.sh').write_bytes(plugin_worker.read_bytes().replace(b'\r\n', b'\n'))
    build_ssh_entrypoint()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-entrypoint-only', action='store_true',
                        help='Only regenerate the two pure shell SSH entry points from their adjacent sources')
    args = parser.parse_args()
    if args.ssh_entrypoint_only:
        build_ssh_entrypoint()
    else:
        build()
