"""Pure shell regression tests; network commands use isolated fixtures."""
import json
import os
import pty
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

TOOL=Path(__file__).resolve().parents[1]
ROOT=TOOL.parents[1]
ENTRY=ROOT/'xiaomi-storage-open-ssh-wsl.sh'
MOCK=r'''#!/usr/bin/python3
import json, os, pathlib, sys
r=pathlib.Path(os.environ['SSH_CLIENT_TEST_ROOT']); args=sys.argv[1:]; name=pathlib.Path(sys.argv[0]).name
cfg=json.loads((r/'config.json').read_text())
with (r/'calls').open('a') as f: f.write(json.dumps([name,args])+'\n')
if name=='ssh':
    command=' '.join(args)
    if '__MINAS_ROOT_COMMAND_OK__' in command:
        if cfg.get('mi_shell'): print('[MINAS]')
        elif cfg.get('ready',True) or (r/'ready').exists(): print('__MINAS_ROOT_COMMAND_OK__\n0')
        else: sys.exit(255)
    elif command.endswith("find /home -maxdepth 1 -type d -name 'u[0-9]*'"):
        print('\n'.join('/home/'+u for u in cfg.get('users',['u123','u456'])))
    elif args[-2:]==['/bin/sh','-s']: (r/'payload').write_bytes(sys.stdin.buffer.read())
    elif 'test -s /data/ssh-autostart-fix' in command: sys.exit(cfg.get('audit_exit',0))
    else: sys.exit(98)
elif name=='powershell.exe':
    sys.stdout.buffer.write('C:\\Users\\测试用户\\AppData\\Local'.encode())
    sys.stderr.buffer.write('本地化提示'.encode('gbk'))
elif name=='wslpath': print(str(r/'测试用户'))
elif name=='openssl':
    if args[0]=='x509': sys.stdin.buffer.read()
    print('subject=CN=nas.example.test' if args[0]=='x509' else 'fixture')
elif name=='sleep': pass
elif name=='curl':
    url=next(a for a in args if a.startswith(('https://','http://')))
    out=args[args.index('-o')+1] if '-o' in args else None; data=''
    if url.endswith('/get_pool_info'): data=json.dumps({'data':{'username':'u123','password':'test-only"\\password'}})
    elif '-T' in args:
        (r/'payload').write_bytes(pathlib.Path(args[args.index('-T')+1]).read_bytes()); data='201'
    elif '__X2' in url: (r/'ready').touch()
    elif url.endswith('.probe') and '-X' not in args: data='0\n' if cfg.get('probe_ready',True) else 'not-root\n'
    elif '-X' not in args: sys.exit(97)
    if '-X' in args and args[args.index('-X')+1]=='DELETE': data=''
    if out and out!='/dev/null': pathlib.Path(out).write_text(data)
    elif '-w' in args: print(data,end='')
'''

class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ssh-shell-client-'); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.bin=self.root/'bin'; self.bin.mkdir()
        for name in ['ssh','powershell.exe','wslpath','openssl','curl','sleep']:
            p=self.bin/name; p.write_text(MOCK); p.chmod(0o755)
        self.env={**os.environ,'SSH_CLIENT_TEST_ROOT':str(self.root),'PATH':str(self.bin)+':'+str(TOOL/'.tools')+':'+os.environ['PATH']}
        self.key=self.root/'key'
        subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(self.key)],check=True)
        self.certs=self.root/'测试用户/minasCert'; self.certs.mkdir(parents=True)
        for name in ['ca_chain.pem','fixture_cert.pem','fixture_private_key.pem']: (self.certs/name).write_text('test-only')
    def run_client(self,*args,cfg=None,entry=ENTRY,input=None):
        (self.root/'config.json').write_text(json.dumps(cfg or {}))
        return subprocess.run(['bash',str(entry),'192.0.2.1','--identity',str(self.key),*args],input=input,env=self.env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=15)
    def calls(self,name):
        p=self.root/'calls'
        return [args for command,args in map(json.loads,p.read_text().splitlines()) if command==name] if p.exists() else []
    def test_existing_ssh_uses_selected_user_and_installs_boot(self):
        result=self.run_client('--user','u456'); self.assertEqual(result.returncode,0,result.stderr)
        payload=(self.root/'payload').read_text()
        self.assertIn('--recover --worker "$stage/restore-ssh.sh" --user u456',payload)
        self.assertNotIn('python',payload.lower()); self.assertFalse(self.calls('curl'))
    def test_multiple_accounts_require_user_without_terminal(self):
        result=self.run_client(); self.assertNotEqual(result.returncode,0); self.assertIn('--user',result.stderr)
        self.assertFalse((self.root/'payload').exists()); self.assertFalse(self.calls('curl'))
    def test_interactive_multiple_accounts_accept_number_after_invalid_choice(self):
        (self.root/'config.json').write_text('{}')
        master,slave=pty.openpty()
        try:
            process=subprocess.Popen(['bash',str(ENTRY),'192.0.2.1','--identity',str(self.key)],
                                     stdin=slave,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=self.env)
            os.write(master,b'0\n2\n'); stdout,stderr=process.communicate(timeout=10)
            self.assertEqual(process.returncode,0,stderr.decode())
            self.assertIn('--user u456',(self.root/'payload').read_text())
        finally:
            os.close(master); os.close(slave)
    def test_unique_account_is_automatic(self):
        result=self.run_client(cfg={'users':['u123']}); self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('--user u123',(self.root/'payload').read_text())
    def test_invalid_user_is_not_sent_to_remote(self):
        for user in ['u999','u1;id']:
            with self.subTest(user=user):
                result=self.run_client('--user',user); self.assertNotEqual(result.returncode,0)
                self.assertFalse((self.root/'payload').exists())
    def test_check_rejects_mi_shell_with_exit_zero(self):
        result=self.run_client('--check',cfg={'mi_shell':True}); self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'payload').exists())
    def test_check_does_not_generate_missing_key(self):
        self.key.unlink(); Path(str(self.key)+'.pub').unlink()
        result=self.run_client('--check'); self.assertEqual(result.returncode,0,result.stderr); self.assertFalse(self.key.exists())
    def test_installer_does_not_add_keys(self):
        result=self.run_client(entry=ROOT/'install-ssh-autostart-fix.sh'); self.assertEqual(result.returncode,0,result.stderr)
        payload=(self.root/'payload').read_text(); self.assertIn('--install-boot-only',payload)
        self.assertNotIn('--public-key "$stage/public.key"',payload); self.assertNotIn('python',payload.lower())
    def test_public_without_private_is_not_overwritten(self):
        self.key.unlink(); original=Path(str(self.key)+'.pub').read_bytes()
        result=self.run_client('--user','u123'); self.assertNotEqual(result.returncode,0)
        self.assertEqual(Path(str(self.key)+'.pub').read_bytes(),original)
    def test_legacy_preserves_utf8_paths_and_cleans_upload(self):
        self.assertIsNotNone(shutil.which('jq',path=self.env['PATH']),'jq required for protocol tests')
        result=self.run_client('--legacy-bootstrap',cfg={'ready':False}); self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('UTF8Encoding',self.calls('powershell.exe')[0][-1])
        self.assertEqual(self.calls('wslpath')[0],['-u','C:\\Users\\测试用户\\AppData\\Local'])
        from urllib.parse import unquote
        calls=self.calls('curl'); trigger=next(a for args in calls for a in args if '__X2' in a)
        self.assertIn('";sh $(printf',unquote(trigger)); self.assertIn('\\057\\156\\141\\163',unquote(trigger))
        self.assertTrue(any('DELETE' in args for args in calls))
        self.assertFalse(any('test-only' in arg for args in calls for arg in args))
    def test_probe_does_not_generate_key_or_install_boot(self):
        self.key.unlink(); Path(str(self.key)+'.pub').unlink()
        result=self.run_client('--probe-bootstrap','--cert-dir',str(self.certs)); self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(self.key.exists()); self.assertNotIn('--recover',(self.root/'payload').read_text())
        self.assertEqual(sum('DELETE' in args for args in self.calls('curl')),2)
    def test_failed_boot_audit_does_not_report_complete(self):
        result=self.run_client('--user','u123',cfg={'audit_exit':1}); self.assertNotEqual(result.returncode,0)
        self.assertNotIn('SSH 已开启，纯 Shell 开机修复已安装',result.stdout)
    def portainer_server(self,exit_code=0,writable=True):
        fixture=self; requests=[]; commands={}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self): self.respond()
            def do_POST(self): self.respond()
            def respond(self):
                body=self.rfile.read(int(self.headers.get('Content-Length',0)))
                data=json.loads(body) if body else None; requests.append((self.path,data,self.headers.get('Authorization')))
                raw=False
                if self.path=='/api/auth':
                    fixture.assertEqual(data,{'Username':'admin','Password':'fixture-password'})
                    result={'jwt':'fixture.token.value'}
                else:
                    fixture.assertEqual(self.headers.get('Authorization'),'Bearer fixture.token.value')
                    if self.path=='/api/endpoints': result=[{'Id':3}]
                    elif self.path.endswith('/containers/json?all=1'): result=[{'Id':'a'*64,'Names':['/test']}]
                    elif self.path.endswith('/containers/'+'a'*64+'/json'):
                        result={'State':{'Running':True},'Mounts':[{'Type':'bind','Source':'/','RW':writable,'Destination':'/nas/home'}]}
                    elif self.path.endswith('/exec'):
                        fixture.assertTrue(data['Tty']); fixture.assertEqual(data['User'],'0')
                        fixture.assertEqual(data['Cmd'][:4],['chroot','/nas/home','/bin/sh','-c'])
                        identifier=f'{len(commands)+1:064x}'; commands[identifier]=data['Cmd']; result={'Id':identifier}
                    elif self.path.endswith('/start'):
                        fixture.assertEqual(data,{'Detach':False,'Tty':True}); raw=True
                        command=commands[self.path.split('/')[-2]][-1]
                        if command.startswith('find /home'): result='/home/u123\n/home/u456\n'
                        else:
                            fixture.assertIn('--offline',command); fixture.assertNotIn('python',command.lower())
                            (fixture.root/'payload').write_text(command); (fixture.root/'ready').touch(); result='queued\n'
                    elif self.path.endswith('/json'): result={'Running':False,'ExitCode':exit_code}
                    else: raise AssertionError(self.path)
                encoded=result.encode() if raw else json.dumps(result).encode()
                self.send_response(200); self.send_header('Content-Length',str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
        server=HTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        (self.bin/'curl').unlink()
        return f'http://127.0.0.1:{server.server_port}',requests,commands
    def test_portainer_tty_exec_uses_inspected_bind_and_verifies_real_ssh(self):
        url,requests,commands=self.portainer_server()
        result=self.run_client('--portainer','--portainer-url',url,'--portainer-user','admin','--user','u456',
                               input='fixture-password\n',cfg={'ready':False})
        self.assertEqual(result.returncode,0,result.stderr); self.assertEqual(len(commands),2)
        self.assertIn('--user u456',(self.root/'payload').read_text())
        self.assertNotIn('fixture-password',result.stdout+result.stderr)
    def test_portainer_exec_failure_is_not_reported_as_success(self):
        url,_,_=self.portainer_server(exit_code=1)
        result=self.run_client('--portainer','--portainer-url',url,'--portainer-user','admin','--user','u123',input='fixture-password\n')
        self.assertNotEqual(result.returncode,0); self.assertIn('容器恢复命令失败',result.stderr)
    def test_portainer_rejects_readonly_root_mount(self):
        url,_,commands=self.portainer_server(writable=False)
        result=self.run_client('--portainer','--portainer-url',url,'--portainer-user','admin','--user','u123',input='fixture-password\n')
        self.assertNotEqual(result.returncode,0); self.assertEqual(commands,{})

class PackagingTests(unittest.TestCase):
    def test_three_entries_share_exact_worker(self):
        worker=(TOOL/'src/restore-ssh.sh').read_bytes()
        for name in ['xiaomi-storage-open-ssh-wsl.sh','install-ssh-autostart-fix.sh']:
            script=(ROOT/name).read_text()
            embedded=script.split("<<'MINAS_SSH_REPAIR_SH_SOURCE'\n",1)[1].split('\nMINAS_SSH_REPAIR_SH_SOURCE',1)[0]
            self.assertEqual((embedded+'\n').encode(),worker); self.assertNotIn('python',script.lower())
        plugin=ROOT.parent/'ssh-recovery-plugin/payload/system/restore-ssh.sh'
        if plugin.is_file(): self.assertEqual(plugin.read_bytes(),worker)
    def test_shell_sources_parse(self):
        for script in ROOT.glob('*.sh'): subprocess.run(['bash','-n',str(script)],check=True)
        subprocess.run(['/bin/sh','-n',str(TOOL/'src/restore-ssh.sh')],check=True)
    def test_help_needs_no_network_or_runtime_python(self):
        result=subprocess.run(['bash',str(ENTRY),'--help'],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr); self.assertIn('2.1.0',result.stdout)

if __name__=='__main__': unittest.main()
