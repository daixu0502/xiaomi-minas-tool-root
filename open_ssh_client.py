#!/usr/bin/env python3
"""Client bootstrap for an administrator's own Xiaomi NAS; never prints credentials."""
import argparse
import base64
import http.client
import getpass
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from urllib.parse import urlsplit
from urllib.request import Request,urlopen

HERE = Path(__file__).resolve().parent
MARKER = '__MINAS_ROOT_COMMAND_OK__'


def checked(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs).stdout


def ssh_args(ip, identity):
    return ['ssh', '-i', str(identity), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=5', 'root@' + ip]


def command_ready(ip, identity):
    try:
        result = subprocess.run(ssh_args(ip, identity) + ['printf "' + MARKER + '\\n"; id -u'],
                                capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.decode(errors='replace').replace('\r', '').splitlines() == [MARKER, '0']


def public_identity(identity):
    identity.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    pub = Path(str(identity) + '.pub')
    if not identity.exists():
        if pub.exists():
            raise RuntimeError('仅发现公钥而没有对应私钥，请使用 --identity 指定现有密钥，避免覆盖')
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(identity)], check=True)
    # Derive from the actual private key, rather than trusting a stale .pub file.
    key = checked(['ssh-keygen', '-y', '-f', str(identity)], text=True).strip()
    if not re.fullmatch(r'(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/=]+', key):
        raise RuntimeError('无法解析此 SSH 公钥')
    return key + '\n'


def recovery_tar(public):
    memory = io.BytesIO()
    with tarfile.open(fileobj=memory, mode='w:gz') as archive:
        files = {name: (HERE / name).read_bytes() for name in ('recover-ssh.py', 'restore-ssh.sh')}
        files['recovery.pub'] = public.encode()
        for name, data in files.items():
            info = tarfile.TarInfo(name); info.mode = 0o600; info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return memory.getvalue()


def root_payload(user, public):
    if not re.fullmatch(r'u[0-9]+', user):
        raise RuntimeError('NAS 返回的账户名称无效')
    encoded = base64.b64encode(recovery_tar(public)).decode()
    return ('#!/bin/sh\nset -eu\numask 077\nstage=$(mktemp -d /tmp/minas-ssh-open.XXXXXX)\n'
            'trap \'rm -rf -- "$stage"\' EXIT\nbase64 -d <<\'REPAIR_BUNDLE\' | tar -xz -C "$stage"\n' +
            encoded + '\nREPAIR_BUNDLE\n/usr/bin/python3 "$stage/recover-ssh.py" --local-recover --user ' + user +
            ' --public-key "$stage/recovery.pub" --install-boot\n').encode()


def container_repair_command(destination,user,public):
    if not re.fullmatch(r'u[0-9]+',user) or not destination.startswith('/') or '..' in destination.split('/'):
        raise RuntimeError('容器宿主机映射或 NAS 账户无效')
    blob=base64.b64encode(recovery_tar(public)).decode()
    program=('import base64,io,tarfile,tempfile,subprocess,shutil\n'
             'from pathlib import Path\n'
             'stage=Path(tempfile.mkdtemp(prefix="nas-ssh-emergency-"))\n'
             'try:\n'
             ' with tarfile.open(fileobj=io.BytesIO(base64.b64decode('+repr(blob)+'))) as tf:\n'
             '  assert set(tf.getnames())=={"recover-ssh.py","restore-ssh.sh","recovery.pub"}\n'
             '  tf.extractall(stage,**({"filter":"data"} if hasattr(tarfile,"data_filter") else {}))\n'
             ' subprocess.run(["/usr/bin/python3",str(stage/"recover-ssh.py"),"--local-recover","--offline","--user",'+repr(user)+',"--public-key",str(stage/"recovery.pub")],check=True)\n'
             'finally: shutil.rmtree(stage)\n')
    return ['chroot',destination,'/usr/bin/python3','-c',program]


class Portainer:
    def __init__(self,url,username,password):
        parts=urlsplit(url)
        if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise RuntimeError('Portainer URL 无效；不要把凭据放进 URL')
        self.url=url.rstrip('/');self.token=''
        self.token=self.call('/api/auth',{'Username':username,'Password':password})['jwt']

    def call(self,path,data=None,raw=False):
        headers={'Content-Type':'application/json'}
        if self.token:headers['Authorization']='Bearer '+self.token
        body=None if data is None else json.dumps(data).encode()
        request=Request(self.url+path,data=body,headers=headers)
        with urlopen(request,timeout=45) as response:content=response.read(4*1024*1024)
        return content if raw else json.loads(content)

    def execute(self,endpoint,container,command):
        prefix='/api/endpoints/'+str(endpoint)+'/docker'
        identifier=self.call(prefix+'/containers/'+container+'/exec',{'AttachStdout':True,'AttachStderr':True,'User':'0','Cmd':command})['Id']
        content=self.call(prefix+'/exec/'+identifier+'/start',{'Detach':False,'Tty':False},raw=True)
        output=b''
        while len(content)>=8 and content[0] in (0,1,2) and content[1:4]==b'\0\0\0':
            size=int.from_bytes(content[4:8],'big');output+=content[8:8+size];content=content[8+size:]
        output+=content
        state=self.call(prefix+'/exec/'+identifier+'/json')
        if state.get('Running') or state.get('ExitCode')!=0:
            raise RuntimeError('容器恢复命令失败：'+output.decode(errors='replace')[-2000:])
        return output.decode(errors='replace')


def portainer_bootstrap(args,ip,public):
    print('目标 Portainer：'+(args.portainer_url or 'http://'+ip+':9001'))
    username=args.portainer_user or input('Portainer 用户名：').strip()
    password=getpass.getpass('Portainer 密码（不保存）：')
    api=Portainer(args.portainer_url or 'http://'+ip+':9001',username,password);password=''
    endpoints=api.call('/api/endpoints')
    endpoint=args.endpoint or (endpoints[0]['Id'] if len(endpoints)==1 else None)
    if endpoint not in [item['Id'] for item in endpoints]:raise RuntimeError('请用 --endpoint 指定 Portainer 环境 ID')
    prefix='/api/endpoints/'+str(endpoint)+'/docker'
    containers=api.call(prefix+'/containers/json?all=1')
    matching=[item for item in containers if '/'+args.container in item.get('Names',[]) or item.get('Id')==args.container]
    if len(matching)!=1:raise RuntimeError('请用 --container 指定已映射 NAS / 的运行中应急容器名称')
    container=matching[0]['Id'];details=api.call(prefix+'/containers/'+container+'/json')
    mounts=[m for m in details.get('Mounts',[]) if m.get('Type')=='bind' and m.get('Source')=='/' and m.get('RW')]
    if len(mounts)!=1 or not details.get('State',{}).get('Running'):raise RuntimeError('应急容器必须运行，且将宿主机 / 以可写方式绑定到容器目录')
    destination=mounts[0]['Destination']
    raw=api.execute(endpoint,container,['chroot',destination,'/usr/bin/python3','-c','from pathlib import Path; import json; print(json.dumps([p.name for p in Path("/home").glob("u[0-9]*") if p.is_dir()]))'])
    users=json.loads(raw.strip());user=args.user or (users[0] if len(users)==1 else None)
    if user not in users or not re.fullmatch(r'u[0-9]+',user or ''):raise RuntimeError('请用 --user 指定 NAS 管理账户（u 开头）')
    result=api.execute(endpoint,container,container_repair_command(destination,user,public))
    print(result.strip());print('等待宿主机一次性 Cron 激活（通常在下一分钟），随后验证真实 SSH 命令。')
    return user


def cert_directory(explicit):
    if explicit:
        return explicit
    if Path('/proc/sys/fs/binfmt_misc/WSLInterop').exists():
        raw = checked(['cmd.exe', '/c', 'echo', '%LOCALAPPDATA%'], text=True).strip()
        return Path(checked(['wslpath', raw], text=True).strip()) / 'minasCert'
    raise RuntimeError('请通过 --cert-dir 指定客户端 minasCert 证书目录')


def certificates(directory):
    pairs = [(p, directory / (p.name.removesuffix('_cert.pem') + '_private_key.pem'))
             for p in directory.glob('*_cert.pem')]
    pairs = [(p, k) for p, k in pairs if k.is_file()]
    if len(pairs) != 1 or not (directory / 'ca_chain.pem').is_file():
        raise RuntimeError('证书目录必须包含一个匹配的客户端证书/私钥和 ca_chain.pem；请在小米客户端重新登录后检查')
    return directory / 'ca_chain.pem', *pairs[0]


def discover_cn(ip):
    pem = checked(['openssl', 's_client', '-connect', ip + ':443', '-servername', 'x'], input=b'', timeout=15)
    subject = checked(['openssl', 'x509', '-noout', '-subject', '-nameopt', 'RFC2253'], input=pem).decode().strip().removeprefix('subject=')
    match = re.search(r'(?:^|,)CN=([A-Za-z0-9.-]+)(?:,|$)', subject)
    if not match:
        raise RuntimeError('未能提取有效 NAS 证书名称')
    return match.group(1)


class NasTLS:
    def __init__(self, ip, cn, paths):
        self.ip, self.cn = ip, cn
        ca, cert, key = paths
        self.context = ssl.create_default_context(cafile=str(ca))
        self.context.load_cert_chain(str(cert), str(key))

    def request(self, port, method, path, body=None, credentials=None):
        connection = http.client.HTTPSConnection(self.cn, port, context=self.context, timeout=20)
        raw = socket.create_connection((self.ip, port), timeout=10)
        connection.sock = self.context.wrap_socket(raw, server_hostname=self.cn)
        headers = {'Content-Type': 'application/json' if port == 443 else 'application/octet-stream'}
        if credentials:
            headers['Authorization'] = 'Basic ' + base64.b64encode(credentials.encode()).decode()
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read(2 * 1024 * 1024)
        finally:
            connection.close()


def webdav_account(data):
    if isinstance(data, dict):
        if isinstance(data.get('username'), str) and isinstance(data.get('password'), str):
            return data['username'], data['password']
        for value in data.values():
            found = webdav_account(value)
            if found:
                return found
    if isinstance(data, list):
        for value in data:
            found = webdav_account(value)
            if found:
                return found
    return None


def bootstrap(api, public, probe=False):
    status, body = api.request(443, 'POST', '/cgi-bin/luci/filemgr/get_pool_info', b'{}')
    if status != 200:
        raise RuntimeError('客户端认证失败，HTTP ' + str(status))
    account = webdav_account(json.loads(body))
    if not account or not re.fullmatch(r'u[0-9]+', account[0]):
        raise RuntimeError('未获取到有效 NAS WebDAV 账户')
    user, password = account
    name = 'ssh-repair-' + uuid.uuid4().hex + '.sh'
    path = '/pool0/data/' + name
    credentials = user + ':' + password
    result_path='/pool0/data/'+name+'.probe'
    result_host='/nas/pool0/'+user+'/data/'+name+'.probe'
    payload=(('#!/bin/sh\nset -eu\numask 077\nid -u > '+result_host+'\nchown '+user+':'+user+' '+result_host+'\n').encode()
             if probe else root_payload(user, public))
    status, _ = api.request(5000, 'PUT', path, payload, credentials)
    if status not in (200, 201, 204):
        raise RuntimeError('恢复程序上传失败，HTTP ' + str(status))
    # Preserve the existing authenticated legacy bootstrap route. A successful
    # HTTP response is never treated as proof that the firmware executed it.
    host_path = '/nas/pool0/' + user + '/data/' + name
    escaped = ''.join('\\%03o' % byte for byte in host_path.encode())
    from urllib.parse import quote
    command = 'sh $(printf \'' + escaped + '\')'
    trigger = '/pool0/video/__X2' + quote('";' + command + ';"', safe='') + '__.ts'
    try:
        try:
            api.request(5000, 'GET', trigger, credentials=credentials)
        except (TimeoutError, OSError, http.client.HTTPException):
            pass  # The root process may outlive the WebDAV request; verify SSH.
        if probe:
            for _ in range(3):
                status,body=api.request(5000,'GET',result_path,credentials=credentials)
                if status==200 and body.strip()==b'0':return
                time.sleep(1)
            raise RuntimeError('该固件未通过旧证书引导探测；请保留插件或 Portainer 恢复入口')
    finally:
        # Uploaded material contains a public key only, but is still temporary.
        try:
            api.request(5000, 'DELETE', path, credentials=credentials)
            if probe:api.request(5000,'DELETE',result_path,credentials=credentials)
        except (OSError, http.client.HTTPException):
            pass


def main():
    parser = argparse.ArgumentParser(description='小米 NAS SSH 开启/修复 2.0.0，兼容旧固件与 mi-shell 固件')
    parser.add_argument('ip', nargs='?'); parser.add_argument('--identity', type=Path, default=Path.home() / '.ssh/id_ed25519')
    parser.add_argument('--cert-dir', type=Path); parser.add_argument('--check', action='store_true')
    parser.add_argument('--user', help='已有 SSH 时选择要保存恢复材料的 NAS 账户（u 开头）')
    parser.add_argument('--probe-bootstrap',action='store_true',help='仅用临时 UID 标记检查证书引导路径，不修改 SSH、账户或启动配置')
    parser.add_argument('--portainer',action='store_true',help='通过现有 Portainer 与宿主机根目录映射容器恢复；适用于旧引导被固件阻止时')
    parser.add_argument('--portainer-url');parser.add_argument('--portainer-user')
    parser.add_argument('--endpoint',type=int);parser.add_argument('--container',default='test')
    parser.add_argument('--legacy-bootstrap', action='store_true', help='即使 SSH 可用也验证原有的客户端认证引导路径')
    args = parser.parse_args(); ip = str(ipaddress.IPv4Address(args.ip or input('NAS IPv4 地址：').strip()))
    if args.probe_bootstrap:
        paths=certificates(cert_directory(args.cert_dir));bootstrap(NasTLS(ip,discover_cn(ip),paths),'',probe=True)
        print('已验证当前固件的证书引导 root 命令路径；临时标记已清理，未修改 SSH 设置。');return
    if args.check:
        if not command_ready(ip, args.identity):
            raise RuntimeError('SSH 未通过真实系统命令检查：可能未开启、公钥未安装，或仍为 mi-shell')
        print('SSH 公钥认证与 root 系统命令均正常。'); return
    public = public_identity(args.identity)
    portainer_user=None
    if args.portainer:
        portainer_user=portainer_bootstrap(args,ip,public)
    elif not args.legacy_bootstrap and command_ready(ip, args.identity):
        # Discover the NAS account without logging any user config or secrets.
        raw = checked(ssh_args(ip, args.identity) + ["find /home -maxdepth 1 -type d -name 'u[0-9]*'"], timeout=15).decode()
        users = [Path(p).name for p in raw.splitlines() if re.fullmatch(r'/home/u[0-9]+', p)]
        selected = args.user or (users[0] if len(users) == 1 else None)
        if selected not in users:
            raise RuntimeError('NAS 存在多个管理账户，请通过 SSH 恢复插件分别配置，或使用 --legacy-bootstrap 获取已登录客户端账户')
        subprocess.run(ssh_args(ip, args.identity) + ['/bin/sh', '-s'], input=root_payload(selected, public), check=True, timeout=360)
    else:
        print('尝试通过已登录小米客户端的证书引导；不会修改 RPMB。')
        paths = certificates(cert_directory(args.cert_dir))
        bootstrap(NasTLS(ip, discover_cn(ip), paths), public)
    for _ in range(45 if args.portainer else 20):
        if command_ready(ip, args.identity):
            if portainer_user:
                subprocess.run(ssh_args(ip,args.identity)+['/bin/sh','-s'],input=root_payload(portainer_user,public),check=True,timeout=360)
                if not command_ready(ip,args.identity):raise RuntimeError('开机修复安装后的 SSH 系统命令复查失败')
            print('SSH 已开启，已验证 root 系统命令；开机恢复已安装。请保留登录私钥。'); return
        time.sleep(2)
    raise RuntimeError('未通过真实 SSH 命令验证。固件可能已阻止原有引导路径，请使用 SSH 恢复插件或 Portainer 离线应急包；HTTP 成功不代表 SSH 已恢复。')


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('失败：' + str(error), file=sys.stderr)
        sys.exit(1)
