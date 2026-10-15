"""Exercise shell filesystem repairs in a temporary NAS tree, never the host."""
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]

class MaterialsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ssh-shell-materials-'); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.work=self.root/'data/ssh-autostart-fix'; self.work.mkdir(parents=True)
        self.etc=self.root/'etc'; self.etc.mkdir()
        self.home=self.root/'home/rootx'; self.home.parent.mkdir()
        self.old=self.root/'root'; self.old.mkdir()
        self.bin=self.root/'bin'; self.bin.mkdir()
        self.passwd=self.etc/'passwd'
        self.passwd.write_text('root:x:0:0:root:/home/rootx:/bin/sh\nu123:x:1001:1001:user:/home/u123:/bin/false\n')
        (self.bin/'bash.bash').write_text('original bash'); (self.bin/'bash.bash').chmod(0o755)
        (self.bin/'mi-shell').write_text('original vendor shell'); (self.bin/'mi-shell').chmod(0o755)
        keygen=self.bin/'dropbearkey'
        keygen.write_text('#!/bin/sh\nwhile [ "$#" -gt 0 ]; do if [ "$1" = -f ]; then shift; file=$1; fi; shift; done\n[ -s "$file" ] || printf host-key > "$file"\n')
        keygen.chmod(0o755)
        source=(ROOT/'restore-ssh.sh').read_text().rsplit('\ncase "${1:-}" in',1)[0]
        mapping=dict([('/usr/bin/bash.bash',self.bin/'bash.bash'),('/usr/sbin/mi-shell',self.bin/'mi-shell'),
                        ('/usr/sbin/dropbearkey',keygen),('/bin/bash',self.bin/'bash'),
                        ('/data',self.root/'data'),('/etc',self.etc),('/home/rootx',self.home),('/home',self.root/'home'),('/root',self.old)])
        source=re.sub('|'.join(re.escape(k) for k in sorted(mapping,key=len,reverse=True)),lambda match:str(mapping[match[0]]),source)
        # Production path ownership rules reject /tmp; exercise repairs inside
        # our fixture with equivalent leaf-symlink protection and test safe below.
        self.library=source+'\nsafe() { [ ! -L "$1" ] || fail "fixture symlink"; }\n'
        self.original_library=source
    def run_shell(self,body,original_safe=False):
        rows=self.passwd.read_text().splitlines()
        fields=rows[0].split(':')
        if fields[5]=='/root': fields[5]=str(self.old)
        if fields[5]=='/home/rootx': fields[5]=str(self.home)
        if fields[6]=='/usr/sbin/mi-shell': fields[6]=str(self.bin/'mi-shell')
        rows[0]=':'.join(fields); self.passwd.write_text('\n'.join(rows)+'\n')
        return subprocess.run(['/bin/sh','-s'],input=(self.original_library if original_safe else self.library)+'\n'+body,
                              capture_output=True,text=True,timeout=10)
    def assert_ok(self,result): self.assertEqual(result.returncode,0,result.stderr)
    def test_migrates_root_home_and_preserves_other_accounts_and_keys(self):
        self.passwd.write_text('root:original-password:0:0:root:/root:/usr/sbin/mi-shell\nu123:x:1001:1001:user:/home/u123:/bin/false\n')
        (self.old/'.ssh').mkdir(); (self.old/'.ssh/authorized_keys').write_text('old-key\nshared-key\n')
        (self.home/'.ssh').mkdir(parents=True); (self.home/'.ssh/authorized_keys').write_text('new-key\nshared-key')
        self.assert_ok(self.run_shell('prepare_account'))
        text=self.passwd.read_text()
        self.assertIn('root:original-password:0:0:root:'+str(self.home)+':/bin/sh',text)
        self.assertIn('u123:x:1001:1001:user:/home/u123:/bin/false',text)
        self.assertEqual(set((self.home/'.ssh/authorized_keys').read_text().splitlines()),{'new-key','shared-key','old-key'})
        self.assertEqual((self.home/'.ssh').stat().st_mode & 0o777,0o700)
        self.assertEqual((self.home/'.ssh/authorized_keys').stat().st_mode & 0o777,0o600)
    def test_missing_home_is_created_and_missing_auth_is_restored(self):
        (self.work/'login-authorized-keys').write_text('saved-public-key\n')
        self.assert_ok(self.run_shell('prepare_account'))
        self.assertEqual((self.home/'.ssh/authorized_keys').read_text(),'saved-public-key\n')
    def test_existing_and_empty_auth_are_never_overwritten_by_snapshot(self):
        (self.home/'.ssh').mkdir(parents=True); (self.work/'login-authorized-keys').write_text('revoked-key\n')
        for value in ['current-key\n','']:
            with self.subTest(value=value):
                (self.home/'.ssh/authorized_keys').write_text(value)
                self.assert_ok(self.run_shell('prepare_account'))
                self.assertEqual((self.home/'.ssh/authorized_keys').read_text(),value)
    def test_shell_fix_is_a_systemd_bind_and_vendor_binary_stays_intact(self):
        self.assert_ok(self.run_shell('repair_shell'))
        conf=self.etc/'systemd/system/dropbear@.service.d/90-normal-shell.conf'
        self.assertEqual(conf.read_text(),'[Service]\nBindReadOnlyPaths='+str(self.bin/'bash.bash')+':'+str(self.bin/'mi-shell')+'\n')
        self.assertEqual((self.bin/'mi-shell').read_text(),'original vendor shell')
    def test_no_mi_shell_requires_no_bash_or_dropin(self):
        (self.bin/'mi-shell').unlink(); (self.bin/'bash.bash').unlink()
        self.assert_ok(self.run_shell('repair_shell'))
        self.assertFalse((self.etc/'systemd').exists())
    def test_missing_bash_fails_before_writing_dropin(self):
        (self.bin/'bash.bash').unlink()
        result=self.run_shell('repair_shell'); self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.etc/'systemd').exists())
    def test_custom_shell_dropin_is_preserved(self):
        conf=self.etc/'systemd/system/dropbear@.service.d/90-normal-shell.conf'
        conf.parent.mkdir(parents=True); conf.write_text('[Service]\n# custom shell configuration\n')
        original=conf.read_bytes(); result=self.run_shell('repair_shell')
        self.assertNotEqual(result.returncode,0); self.assertEqual(conf.read_bytes(),original)
    def test_host_key_is_backed_up_restored_and_not_rotated(self):
        host=self.etc/'dropbear/dropbear_rsa_host_key'; host.parent.mkdir(); host.write_text('original-host-identity')
        self.assert_ok(self.run_shell('repair_host'))
        self.assertEqual((self.work/'host/dropbear_rsa_host_key').read_text(),'original-host-identity')
        host.unlink(); self.assert_ok(self.run_shell('repair_host'))
        self.assertEqual(host.read_text(),'original-host-identity')
    def test_install_snapshot_and_shared_boot_paths(self):
        self.assert_ok(self.run_shell('prepare_account'))
        (self.home/'.ssh/authorized_keys').write_text('current-key\n')
        worker=self.root/'worker-source'; worker.write_text('#!/bin/sh\nexit 0\n')
        self.assert_ok(self.run_shell('prepare_account\ninstall_boot '+str(worker)))
        self.assertEqual((self.work/'restore-ssh.sh').read_bytes(),worker.read_bytes())
        self.assertEqual((self.work/'login-authorized-keys').read_text(),'current-key\n')
        self.assertIn('@reboot root /bin/sh '+str(self.work/'restore-ssh.sh'),(self.etc/'cron.d/keep-ssh').read_text())
    def test_symlink_target_is_rejected_by_production_guard(self):
        link=self.root/'unsafe'; link.symlink_to(self.passwd)
        result=self.run_shell('safe '+str(link),original_safe=True)
        self.assertNotEqual(result.returncode,0); self.assertIn('符号链接',result.stderr)

if __name__=='__main__': unittest.main()
