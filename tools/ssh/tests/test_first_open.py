"""Upstream-style first opening; all NAS, TLS, WebDAV and SSH calls are fakes."""
import json
import os
import base64
from http.server import BaseHTTPRequestHandler,HTTPServer
import shutil
import threading
from pathlib import Path
import subprocess
import tempfile
import unittest

TOOL=Path(__file__).resolve().parents[1]
ROOT=TOOL.parents[1]
ENTRY=ROOT/'xiaomi-storage-open-ssh-wsl.sh'
MOCK=r'''#!/usr/bin/python3
import json,os,pathlib,sys
r=pathlib.Path(os.environ['FIRST_OPEN_TEST_ROOT']);args=sys.argv[1:];name=pathlib.Path(sys.argv[0]).name
cfg=json.loads((r/'config.json').read_text())
with (r/'calls').open('a') as f:f.write(json.dumps([name,args])+'\n')
if name=='openssl':
 if args[0]=='x509':sys.stdin.buffer.read();print('subject=CN=nas.example.test')
 else:print('test-certificate')
elif name=='curl':
 url=next(a for a in args if a.startswith(('http://','https://')))
 if url.endswith('/get_pool_info'):
  print(json.dumps({'data':{'username':cfg.get('user','u123'),'password':'test-only"\\password'}}))
 elif '-T' in args:
  (r/'webdav.conf').write_text(pathlib.Path(args[args.index('--config')+1]).read_text())
  (r/'bootstrap').write_bytes(pathlib.Path(args[args.index('-T')+1]).read_bytes())
  print(cfg.get('upload_status','201'),end='')
 elif '__X2' in url:
  if not cfg.get('blocked'):(r/'triggered').touch()
 elif '-X' in args and args[args.index('-X')+1]=='DELETE':pass
 else:sys.exit(97)
elif name=='ssh':
 command=' '.join(args)
 if '__MINAS_ROOT_COMMAND_OK__' in command:
  if not (r/'triggered').exists():sys.exit(255)
  print('[MINAS]' if cfg.get('limited') else '__MINAS_ROOT_COMMAND_OK__\n0')
 elif args[-2:]==['/bin/sh','-s']:
  (r/'boot-payload').write_bytes(sys.stdin.buffer.read());sys.exit(cfg.get('boot_exit',0))
 elif 'test -s /data/ssh-autostart-fix' in command:sys.exit(cfg.get('audit_exit',0))
 else:sys.exit(98)
elif name=='cmd.exe':print('C:\\Users\\测试用户\\AppData\\Local\r')
elif name=='wslpath':print(r/'win')
elif name=='ip':pass
elif name=='sleep':pass
'''


class FirstOpenTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.bin=self.root/'bin';self.bin.mkdir()
        for name in ('openssl','curl','ssh','cmd.exe','wslpath','ip','sleep'):
            p=self.bin/name;p.write_text(MOCK);p.chmod(0o755)
        self.key=self.root/'login-key'
        subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(self.key)],check=True)
        self.certs=self.root/'win/minasCert';self.certs.mkdir(parents=True)
        for name in ('ca_chain.pem','fixture_cert.pem','fixture_private_key.pem'):(self.certs/name).write_text('synthetic')
        self.env={**os.environ,'FIRST_OPEN_TEST_ROOT':str(self.root),'PATH':str(self.bin)+':'+str(TOOL/'.tools')+':'+os.environ['PATH']}

    def run_client(self,cfg=None,automatic_cert=False,ip='192.0.2.1',input=None):
        (self.root/'config.json').write_text(json.dumps(cfg or {}))
        args=['bash',str(ENTRY)]+([ip] if ip else [])+['--identity',str(self.key)]
        if not automatic_cert:args+=['--cert-dir',str(self.certs)]
        return subprocess.run(args,input=input,env=self.env,capture_output=True,text=True,timeout=20)

    def calls(self):return [json.loads(line) for line in (self.root/'calls').read_text().splitlines()]

    def test_banner_prints_install_commands_before_help_without_running_them(self):
        result=subprocess.run(['bash',str(ENTRY),'--help'],env=self.env,capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('sudo apt update',result.stdout)
        self.assertIn('sudo apt install openssh-client curl jq openssl coreutils dnsutils iproute2',result.stdout)
        self.assertLess(result.stdout.index('兼容补丁 2.1.2'),result.stdout.index('sudo apt update'))
        self.assertIn('不是在 NAS 上执行',result.stdout)
        self.assertFalse((self.root/'calls').exists())

    def test_missing_jq_still_shows_install_hint_before_failure(self):
        for command in ('mktemp','rm','ssh-keygen'):
            (self.bin/command).symlink_to(shutil.which(command))
        result=subprocess.run(['/bin/bash',str(ENTRY),'192.0.2.1'],env={**self.env,'PATH':str(self.bin)},capture_output=True,text=True,timeout=5)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("命令 'jq' 未找到",result.stderr)
        self.assertIn('sudo apt install openssh-client curl jq',result.stdout)

    def test_webdav_always_precedes_first_ssh_and_boot_installation(self):
        result=self.run_client();self.assertEqual(result.returncode,0,result.stderr)
        calls=self.calls();first_ssh=next(i for i,(name,args) in enumerate(calls) if name=='ssh')
        trigger=next(i for i,(name,args) in enumerate(calls) if name=='curl' and any('__X2' in a for a in args))
        self.assertLess(trigger,first_ssh)
        payload=(self.root/'boot-payload').read_text()
        self.assertIn('--install-boot-only --worker',payload)
        worker=payload.split("<<'MINAS_REMOTE_WORKER'\n",1)[1].split('\nMINAS_REMOTE_WORKER',1)[0]+'\n'
        self.assertEqual(worker.encode(),(TOOL/'src/restore-ssh.sh').read_bytes())
        bootstrap=(self.root/'bootstrap').read_text()
        self.assertIn('if [ -e /usr/sbin/mi-shell ]',bootstrap)
        self.assertNotIn('mitee_tool',bootstrap)
        self.assertNotIn('test-only',result.stdout+result.stderr)
        self.assertFalse(any('test-only' in arg for _,args in calls for arg in args))
        self.assertTrue(any(name=='curl' and 'DELETE' in args for name,args in calls))
        self.assertFalse(any('portainer' in arg.lower() for _,args in calls for arg in args))

    def test_blocked_entry_does_not_install_boot_or_claim_success(self):
        result=self.run_client({'blocked':True});self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'boot-payload').exists())
        self.assertIn('未安装开机修复',result.stderr)

    def test_real_curl_reads_config_and_sends_escaped_basic_credentials(self):
        result=self.run_client();self.assertEqual(result.returncode,0,result.stderr)
        config=self.root/'webdav.conf'
        self.assertTrue(config.read_text().startswith('user = "'))
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                received.append(self.headers.get('Authorization'))
                self.send_response(204);self.end_headers()
        server=HTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            curl=shutil.which('curl',path=os.environ['PATH']);self.assertIsNotNone(curl)
            request=subprocess.run([curl,'--silent','--show-error','--noproxy','*','--max-time','5','--config',str(config),
                                    '--output','/dev/null','http://127.0.0.1:'+str(server.server_port)+'/'],
                                   capture_output=True,text=True,timeout=10)
            self.assertEqual(request.returncode,0,request.stderr)
            wanted='Basic '+base64.b64encode(b'u123:test-only"\\password').decode()
            self.assertEqual(received,[wanted])
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_mi_shell_exit_zero_is_not_real_root_success(self):
        result=self.run_client({'limited':True});self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'boot-payload').exists())

    def test_upload_failure_does_not_trigger_or_install(self):
        result=self.run_client({'upload_status':'403'});self.assertNotEqual(result.returncode,0)
        self.assertFalse(any(name=='ssh' for name,_ in self.calls()))
        self.assertFalse((self.root/'triggered').exists())

    def test_install_and_audit_failures_are_reported_separately(self):
        for key in ('boot_exit','audit_exit'):
            with self.subTest(key=key):
                result=self.run_client({key:1});self.assertNotEqual(result.returncode,0)
                self.assertIn('SSH 已开启，但开机修复',result.stderr)
                self.assertNotIn('SSH 连接成功，开机修复已安装',result.stdout)

    def test_windows_certificate_path_preserves_unicode(self):
        result=self.run_client(automatic_cert=True);self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(any(name=='cmd.exe' and '65001' in ' '.join(args) for name,args in self.calls()))
        self.assertTrue(any(name=='wslpath' and '测试用户' in args[0] for name,args in self.calls()))

    def test_manual_ip_has_no_hardcoded_reverse_dns_check(self):
        result=self.run_client(ip=None,input='invalid\n192.0.2.1\n');self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('小米智能存储 IP: 192.0.2.1',result.stdout)

    def test_missing_private_key_never_overwrites_existing_public(self):
        self.key.unlink();pub=Path(str(self.key)+'.pub');before=pub.read_bytes()
        result=self.run_client();self.assertNotEqual(result.returncode,0)
        self.assertEqual(pub.read_bytes(),before);self.assertFalse((self.root/'bootstrap').exists())

    def test_bootstrap_firmware_branch_preserves_vendor_binary(self):
        self.assertEqual(self.run_client().returncode,0)
        bootstrap=(self.root/'bootstrap').read_text()
        start=bootstrap.index('if [ -e /usr/sbin/mi-shell ]; then')
        branch=bootstrap[start:bootstrap.index('\nsystemctl enable dropbear.socket',start)]
        firmware=self.root/'firmware';firmware.mkdir();(firmware/'etc').mkdir()
        normal=firmware/'bash.bash';normal.write_text('original bash');normal.chmod(0o755)
        limited=firmware/'mi-shell'
        branch=branch.replace('/usr/bin/bash.bash',str(normal)).replace('/usr/sbin/mi-shell',str(limited)).replace('/etc/systemd',str(firmware/'etc/systemd')).replace('systemctl daemon-reload',':')
        # Old firmware: no mi-shell and no Bash needed.
        subprocess.run(['/bin/sh','-s'],input=branch,text=True,check=True)
        self.assertFalse((firmware/'etc/systemd').exists())
        limited.write_text('vendor mi-shell');limited.chmod(0o755)
        subprocess.run(['/bin/sh','-s'],input=branch,text=True,check=True)
        conf=firmware/'etc/systemd/system/dropbear@.service.d/90-normal-shell.conf'
        self.assertEqual(conf.read_text(),'[Service]\nBindReadOnlyPaths='+str(normal)+':'+str(limited)+'\n')
        self.assertEqual(limited.read_text(),'vendor mi-shell')
        conf.write_text('[Service]\n# custom config\n')
        result=subprocess.run(['/bin/sh','-s'],input=branch,text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0);self.assertIn('custom config',conf.read_text())


if __name__=='__main__':unittest.main()
