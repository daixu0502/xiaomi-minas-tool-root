import base64
import importlib.util
import io
from pathlib import Path
import subprocess
import tarfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('bootstrap',ROOT/'open_ssh_client.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class BootstrapTests(unittest.TestCase):
    def test_successful_exit_from_mi_shell_is_not_a_successful_command(self):
        with patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],0,b'[MINAS]',b'')):
            self.assertFalse(m.command_ready('192.0.2.1',Path('/tmp/key')))
        with patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],0,(m.MARKER+'\n0\n').encode(),b'')):
            self.assertTrue(m.command_ready('192.0.2.1',Path('/tmp/key')))
    def test_offline_or_timeout_is_not_success(self):
        with patch.object(m.subprocess,'run',side_effect=subprocess.TimeoutExpired('ssh',15)):
            self.assertFalse(m.command_ready('192.0.2.1',Path('/tmp/key')))
    def test_payload_rejects_shell_metacharacters_in_account(self):
        with self.assertRaises(RuntimeError):m.root_payload('u1;id','public fixture')
    def test_payload_contains_only_public_material_and_uses_bounded_root_tool(self):
        data=m.root_payload('u123','ssh-ed25519 fixture\n').decode()
        self.assertIn('--local-recover --user u123',data)
        self.assertNotIn('rpmb set',data)
        encoded=data.split("<<'REPAIR_BUNDLE' | tar -xz -C \"$stage\"\n",1)[1].split('\nREPAIR_BUNDLE')[0]
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(encoded))) as tf:
            self.assertEqual(set(tf.getnames()),{'recover-ssh.py','restore-ssh.sh','recovery.pub'})
            self.assertEqual(tf.extractfile('recovery.pub').read(),b'ssh-ed25519 fixture\n')
    def test_generated_single_file_bundle_matches_reviewed_sources(self):
        script=(ROOT/'xiaomi-storage-open-ssh-wsl.sh').read_text()
        encoded=script.split("<<'NAS_SSH_CLIENT_BUNDLE' | tar -xz -C \"$stage\"\n",1)[1].split('\nNAS_SSH_CLIENT_BUNDLE')[0]
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(encoded))) as tf:
            for name in ['open_ssh_client.py','recover-ssh.py','restore-ssh.sh']:
                self.assertEqual(tf.extractfile(name).read(),(ROOT/name).read_bytes())
    def test_installer_embedded_worker_and_engine_match_sources(self):
        script=(ROOT/'install-ssh-autostart-fix.sh').read_text()
        self.assertEqual(script.split("<<'SSH_RESTORE_WORKER'\n",1)[1].split('\nSSH_RESTORE_WORKER')[0].strip(),(ROOT/'restore-ssh.sh').read_text().strip())
        self.assertEqual(script.split("<<'SSH_RECOVERY_PYTHON'\n",1)[1].split('\nSSH_RECOVERY_PYTHON')[0].strip(),(ROOT/'recover-ssh.py').read_text().strip())
    def test_nested_webdav_response(self):
        self.assertEqual(m.webdav_account({'data':[{'username':'u123','password':'test-only'}]}),('u123','test-only'))
    def test_portainer_command_uses_inspected_mount_and_no_shell_interpolation(self):
        command=m.container_repair_command('/nas/home','u123','ssh-ed25519 fixture\n')
        self.assertEqual(command[:4],['chroot','/nas/home','/usr/bin/python3','-c'])
        compile(command[4],'<container>','exec')
        self.assertIn('"--offline"',command[4])
        with self.assertRaises(RuntimeError):m.container_repair_command('/nas/../etc','u123','fixture')
    def test_portainer_exec_decodes_docker_frames_and_checks_exit_status(self):
        client=object.__new__(m.Portainer)
        data=b'{"state":"pending"}\n';frame=bytes([1,0,0,0])+len(data).to_bytes(4,'big')+data
        with patch.object(client,'call',side_effect=[{'Id':'exec-test'},frame,{'Running':False,'ExitCode':0}]):
            self.assertEqual(client.execute(3,'container-test',['id']),data.decode())
    def test_portainer_does_not_accept_credentials_in_url(self):
        with self.assertRaises(RuntimeError):m.Portainer('http://user:password@example.test','test','test-only')


if __name__=='__main__':unittest.main()
