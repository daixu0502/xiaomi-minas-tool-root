"""No live Docker, firewall, root credential or network changes in these tests."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

TOOL = Path(__file__).resolve().parents[1]
ROOT = TOOL.parents[1]
spec = importlib.util.spec_from_file_location('bridge_repair', TOOL / 'src/docker_bridge_repair.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def network(name='bridge', ident='a' * 64, internal=False, options=None, subnet='172.17.0.0/16'):
    return {'Id': ident, 'Name': name, 'Driver': 'bridge', 'Internal': internal,
            'Options': options or {}, 'IPAM': {'Config': [{'Subnet': subnet}]}}


class FakeNAS:
    def __init__(self, directory, native=False, legacy=True):
        self.directory = Path(directory)
        self.uci = '1' if native else '0'
        self.helper_enabled = legacy
        self.helper_active = legacy
        self.calls = []
        self.containers = ['b' * 64, 'c' * 64]
        self.live = set(self.containers)
        self.networks = [network(), network('apps', 'd' * 64, subnet='172.18.0.0/16')]
        self.tables = {'nat': [['-P', 'POSTROUTING', 'ACCEPT']], 'filter': [['-P', 'FORWARD', 'ACCEPT']]}
        self.runtime = self.directory / 'daemon.json'
        self.forward = self.directory / 'ip_forward'
        self.config = self.directory / 'mi_docker'
        self.config.write_text('fixture configuration\n')
        self.forward.write_text('1\n')
        self.runtime.write_text(json.dumps({'iptables': native}))
        self.fail_start = False
        self.fail_arm = False
        self.fail_restart_once = False
        self.changes = ''
        self.tables['filter'].append(['-A', 'FORWARD', '-s', '192.0.2.0/24', '-j', 'DROP'])
        # A similar, unrelated comment must never be selected for cleanup.
        self.tables['nat'].append(['-A', 'POSTROUTING', '-s', '198.51.100.0/24', '-m', 'comment', '--comment', m.TAG + '-other', '-j', 'ACCEPT'])
        if legacy:
            self.tables['nat'].append(['-A', 'POSTROUTING', '-s', '172.18.0.0/16', '!', '-o', 'br-' + 'd' * 12,
                                       '-m', 'comment', '--comment', m.TAG, '-j', 'MASQUERADE'])
        if native:
            self.native_rules()

    def native_rules(self):
        for values in self.tables.values():
            values[:] = [r for r in values if not (r[:2] == ['-A', 'FORWARD'] and '-j' in r and r[r.index('-j') + 1] == 'DOCKER')]
        self.tables['filter'].append(['-A', 'FORWARD', '-j', 'DOCKER'])
        for item in self.networks:
            for rule in m.requirements(item):
                if ['-A', *rule] not in self.tables['nat']:
                    self.tables['nat'].append(['-A', *rule])
                bridge = rule[5]
                for rule in (['FORWARD', '-i', bridge, '!', '-o', bridge, '-j', 'ACCEPT'],
                             ['FORWARD', '-o', bridge, '-m', 'conntrack', '--ctstate', 'RELATED,ESTABLISHED', '-j', 'ACCEPT']):
                    if ['-A', *rule] not in self.tables['filter']:
                        self.tables['filter'].append(['-A', *rule])

    def run(self, args, check=True, timeout=45):
        args = [str(a) for a in args]
        self.calls.append(args)
        out, code = '', 0
        command = Path(args[0]).name
        if command == 'uci':
            if 'get' in args:
                out, code = (self.uci + '\n', 0) if self.uci is not None else ('', 1)
            elif 'set' in args:
                self.uci = args[-1].split('=', 1)[1]
            elif 'delete' in args:
                self.uci = None
            elif 'changes' in args:
                out = self.changes
        elif command == 'iptables':
            table, operation = args[4:6]
            rule = args[6:]
            if operation == '-S':
                out = '\n'.join(shlex.join(r) for r in self.tables[table])
            elif operation == '-C':
                code = 0 if ['-A', *rule] in self.tables[table] else 1
            elif operation == '-D':
                self.tables[table].remove(['-A', *rule])
            elif operation == '-A':
                self.tables[table].append(['-A', *rule])
            elif operation == '-P':
                self.tables[table] = [r for r in self.tables[table] if r[:2] != ['-P', rule[0]]]
                self.tables[table].append(['-P', *rule])
            else:
                raise AssertionError(args)
        elif command == 'docker':
            if args[1] != '--host=unix:///var/run/docker.sock':
                raise AssertionError('Non-local Docker access')
            action = args[2:]
            if action[0] == 'ps':
                out = '\n'.join(sorted(self.live))
            elif action[:2] == ['network', 'ls']:
                out = '\n'.join(n['Id'] for n in self.networks)
            elif action[:2] == ['network', 'inspect']:
                out = json.dumps(self.networks)
            elif action[0] == 'inspect':
                out = json.dumps([{'Mounts': [{'Source': '/nas/pool0/u123/data'}]}])
            elif action[0] == 'stop':
                self.live -= set(action[3:])
            elif action[0] == 'start':
                if self.fail_start:
                    self.fail_start = False
                    code = 1
                else:
                    self.live.update(action[1:])
            elif action[0] != 'info':
                raise AssertionError(args)
        elif command == 'systemctl':
            if args[1] == 'is-active':
                code = 0 if self.helper_active else 3
            elif args[1] == 'is-enabled':
                out = 'enabled\n' if self.helper_enabled else 'not-found\n'
                code = 0 if self.helper_enabled else 1
            elif args[1] == 'disable':
                self.helper_active = self.helper_enabled = False
            elif args[1] == 'enable':
                self.helper_enabled = True
            elif args[1] == 'start':
                self.helper_active = True
            elif args[1] == 'restart':
                if self.fail_restart_once:
                    self.fail_restart_once = False
                    code = 1
                else:
                    enabled = self.uci != '0'
                    self.runtime.write_text(json.dumps({'iptables': enabled}))
                    if enabled:
                        self.forward.write_text('1\n')
                        self.native_rules()
            elif args[1] != 'stop':
                raise AssertionError(args)
        elif command == 'systemd-run':
            code = int(self.fail_arm)
        else:
            raise AssertionError(args)
        result = subprocess.CompletedProcess(args, code, out, 'simulated failure' if code else '')
        if check and code:
            raise RuntimeError('simulated failure')
        return result


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.nas = FakeNAS(self.temp.name)
        self.base = Path(self.temp.name) / 'repair'
        (self.base / 'transactions').mkdir(parents=True)
        self.patches = [patch.object(m, 'BASE', self.base), patch.object(m, 'CONFIG', self.nas.config),
                        patch.object(m, 'RUNTIME', self.nas.runtime), patch.object(m, 'FORWARD', self.nas.forward),
                        patch.object(m, 'run', self.nas.run), patch.object(m.os.path, 'ismount', return_value=True)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def apply(self):
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path == Path('/usr/sbin/dockerd_prepare.sh'):
                return 'config_load mi_docker'
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read), contextlib.redirect_stdout(io.StringIO()):
            m.apply(yes=True)

    def state(self):
        folders = list((self.base / 'transactions').iterdir())
        self.assertEqual(len(folders), 1)
        folder = folders[0]
        return folder, json.loads((folder / 'state.json').read_text())

    def test_apply_restores_restart_no_containers_and_sets_native(self):
        self.apply()
        folder, state = self.state()
        self.assertEqual(state['phase'], 'pending')
        self.assertEqual(self.nas.uci, '1')
        self.assertEqual(self.nas.live, set(self.nas.containers))
        self.assertTrue(m.audit()['healthy'])
        self.assertFalse(m.audit()['needsChange'])
        self.assertTrue(m.ID.fullmatch(folder.name))
        self.assertEqual((folder / 'state.json').stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.base / 'repair.py').is_file())

    def test_watchdog_armed_before_any_network_change(self):
        self.apply()
        arm = next(i for i, c in enumerate(self.nas.calls) if c[0] == 'systemd-run')
        change = next(i for i, c in enumerate(self.nas.calls) if c[:2] == ['uci', 'set'])
        stop = next(i for i, c in enumerate(self.nas.calls) if 'stop' in c and Path(c[0]).name == 'docker')
        self.assertLess(arm, change)
        self.assertLess(arm, stop)

    def test_only_exact_legacy_comment_deleted(self):
        self.apply()
        deletes = [c for c in self.nas.calls if '-D' in c]
        self.assertEqual(len(deletes), 1)
        self.assertIn(m.TAG, deletes[0])
        self.assertTrue(any(m.TAG + '-other' in r for r in self.nas.tables['nat']))
        self.assertTrue(any('192.0.2.0/24' in r for r in self.nas.tables['filter']))
        self.assertFalse(any(flag in c for c in self.nas.calls for flag in ('-F', '--flush')))

    def test_timer_failure_does_not_change_uci_or_containers(self):
        self.nas.fail_arm = True
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.nas.uci, '0')
        self.assertTrue(self.nas.helper_active)
        self.assertEqual(self.nas.live, set(self.nas.containers))
        self.assertEqual(self.state()[1]['phase'], 'not-started')

    def test_failed_restart_rolls_back(self):
        self.nas.fail_restart_once = True
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.nas.uci, '0')
        self.assertTrue(self.nas.helper_active)
        self.assertEqual(self.state()[1]['phase'], 'rolled-back')

    def test_container_start_failure_rolls_back(self):
        self.nas.fail_start = True
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.nas.uci, '0')
        self.assertEqual(self.nas.live, set(self.nas.containers))

    def test_rollback_restores_scoped_state(self):
        self.nas.forward.write_text('0\n')
        self.apply()
        folder, state = self.state()
        with contextlib.redirect_stdout(io.StringIO()):
            m.rollback(folder, state)
        self.assertEqual(self.nas.uci, '0')
        self.assertEqual(self.nas.forward.read_text(), '0\n')
        self.assertTrue(self.nas.helper_enabled)
        self.assertTrue(any(m.owned(r) for r in self.nas.tables['nat']))

    def test_rollback_removes_previously_absent_uci_option(self):
        self.nas.uci = None
        self.apply()
        folder, state = self.state()
        with contextlib.redirect_stdout(io.StringIO()):
            m.rollback(folder, state)
        self.assertIsNone(self.nas.uci)

    def test_no_nat_service_is_created_when_legacy_is_absent(self):
        self.nas.helper_active = self.nas.helper_enabled = False
        self.apply()
        self.assertFalse(any(c[:2] == ['systemctl', 'enable'] or c[:2] == ['systemctl', 'disable'] for c in self.nas.calls))

    def test_healthy_does_not_accept_docker_user_hook_alone(self):
        self.nas.uci = '1'
        self.nas.runtime.write_text('{"iptables":true}')
        self.nas.native_rules()
        for rule in self.nas.tables['filter']:
            if rule == ['-A', 'FORWARD', '-j', 'DOCKER']:
                rule[-1] = 'DOCKER-USER'
        self.assertFalse(m.audit()['healthy'])

    def test_secure_directory_rejects_symlink_and_writable_state(self):
        # Point at a real root-owned ancestor before introducing mocked unsafe leaf metadata.
        from types import SimpleNamespace
        unsafe = SimpleNamespace(st_mode=0o040777, st_uid=0)
        with patch.object(Path, 'lstat', return_value=unsafe), self.assertRaisesRegex(RuntimeError, '不安全'):
            m.secure_dir(Path('/'))
        unsafe.st_mode = 0o120777
        with patch.object(Path, 'lstat', return_value=unsafe), self.assertRaisesRegex(RuntimeError, '不安全'):
            m.secure_dir(Path('/'))

    def test_confirm_cancels_timer_and_old_watchdog_cannot_rollback(self):
        self.apply()
        folder, state = self.state()
        with contextlib.redirect_stdout(io.StringIO()):
            m.confirm(folder, state)
            m.rollback(folder, state)
        self.assertEqual(state['phase'], 'confirmed')
        self.assertEqual(self.nas.uci, '1')
        self.assertTrue(any(c[0:2] == ['systemctl', 'stop'] and c[-1].endswith('.timer') for c in self.nas.calls))

    def test_confirm_rejects_stopped_original_container(self):
        self.apply()
        folder, state = self.state()
        self.nas.live.clear()
        with self.assertRaises(RuntimeError):
            m.confirm(folder, state)
        self.assertEqual(state['phase'], 'pending')

    def test_pending_transaction_blocks_second_apply(self):
        self.apply()
        self.nas.uci = '0'
        with self.assertRaisesRegex(RuntimeError, '待确认'):
            self.apply()

    def test_unmounted_pool_prevents_mutation(self):
        with patch.object(m.os.path, 'ismount', return_value=False), self.assertRaisesRegex(RuntimeError, '未挂载'):
            self.apply()
        self.assertFalse(any(c[:2] == ['uci', 'set'] for c in self.nas.calls))

    def test_rollback_refuses_to_start_containers_without_pool(self):
        self.apply()
        folder, state = self.state()
        count = len(self.nas.calls)
        with patch.object(m.os.path, 'ismount', return_value=False), self.assertRaisesRegex(RuntimeError, '未挂载'):
            m.rollback(folder, state)
        self.assertEqual(len(self.nas.calls), count)

    def test_uncommitted_uci_change_rejected(self):
        self.nas.changes = 'mi_docker.globals.bip=fixture'
        with self.assertRaisesRegex(RuntimeError, '尚未提交'):
            self.apply()

    def test_internal_ipv6_and_explicit_no_nat_are_not_repaired(self):
        self.assertEqual(m.requirements(network(internal=True)), [])
        self.assertEqual(m.requirements(network(options={'com.docker.network.bridge.enable_ip_masquerade': 'false'})), [])
        self.assertEqual(m.requirements(network(subnet='fd00::/64')), [])

    def test_network_name_does_not_become_shell_command(self):
        self.assertTrue(m.requirements(network(name='apps; dangerous')))
        with self.assertRaisesRegex(RuntimeError, '网卡'):
            m.requirements(network(options={'com.docker.network.bridge.name': 'br-foo;id'}))

    def test_healthy_apply_is_readonly_and_does_not_make_state_directory(self):
        self.nas.uci = '1'
        self.nas.runtime.write_text('{"iptables":true}')
        self.nas.native_rules()
        self.nas.helper_active = self.nas.helper_enabled = False
        self.nas.tables['nat'] = [r for r in self.nas.tables['nat'] if not m.owned(r)]
        with patch.object(m.os, 'geteuid', return_value=0), patch.object(m, 'secure_dir') as mkdir, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(['--apply', '--yes']), 0)
            mkdir.assert_not_called()
        self.assertFalse(any(c[:2] == ['systemctl', 'restart'] for c in self.nas.calls))

    def test_missing_outbound_filter_rule_is_not_healthy(self):
        self.nas.uci = '1'
        self.nas.runtime.write_text('{"iptables":true}')
        self.nas.native_rules()
        self.nas.tables['filter'] = [r for r in self.nas.tables['filter'] if '-i' not in r]
        self.assertFalse(m.audit()['healthy'])
        self.assertTrue(m.audit()['missingForward'])

    def test_check_never_writes_files_or_restarts(self):
        before = set(Path(self.temp.name).rglob('*'))
        with patch.object(m.os, 'geteuid', return_value=0), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(['--check']), 2)
        self.assertEqual(before, set(Path(self.temp.name).rglob('*')))
        self.assertFalse(any(c[:2] == ['uci', 'set'] or c[:2] == ['systemctl', 'restart'] for c in self.nas.calls))

    def test_reject_nonroot_and_invalid_transaction_ids(self):
        with patch.object(m.os, 'geteuid', return_value=123), self.assertRaisesRegex(RuntimeError, 'root'):
            m.main(['--check'])
        for ident in ('../../etc', '/data/something', 'pending; reboot', ''):
            with self.assertRaisesRegex(RuntimeError, '编号'):
                m.transaction(ident)

    def test_single_file_wrapper_matches_reviewed_python(self):
        script = (ROOT / 'fix-docker-bridge.sh').read_text()
        embedded = script.split("<<'MINAS_DOCKER_BRIDGE_PY'\n", 1)[1].split('\nMINAS_DOCKER_BRIDGE_PY\n', 1)[0]
        self.assertEqual(embedded, (TOOL / 'src/docker_bridge_repair.py').read_text().rstrip())

    def test_watchdog_waits_for_inflight_apply_instead_of_losing_rollback(self):
        with patch.object(m.os, 'geteuid', return_value=0), patch.object(m, 'secure_dir'), \
                patch.object(m, 'transaction', return_value=(self.base, {'phase': 'confirmed'})), \
                patch.object(m.fcntl, 'flock') as lock, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(['--rollback', '20261015-120000-abcdefgh']), 0)
            self.assertEqual(lock.call_args.args[1], m.fcntl.LOCK_EX)


if __name__ == '__main__':
    unittest.main()
