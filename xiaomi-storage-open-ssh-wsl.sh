#!/usr/bin/env bash
# Xiaomi NAS SSH bootstrap 2.0.0; readable standalone source.
# Generated from the three adjacent source files by build_release.py.
set -euo pipefail
umask 077
for tool in python3 cat ssh ssh-keygen openssl; do
 command -v "$tool" >/dev/null || { echo "缺少命令：$tool" >&2; exit 2; }
done
stage=$(mktemp -d /tmp/minas-ssh-client.XXXXXX)
chmod 0700 "$stage"
trap 'rm -rf -- "$stage"' EXIT

# ---- open_ssh_client.py (plain source) ----
cat > "$stage/open_ssh_client.py" <<'MINAS_SSH_CLIENT_PY_SOURCE'
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
MINAS_SSH_CLIENT_PY_SOURCE

# ---- recover-ssh.py (plain source) ----
cat > "$stage/recover-ssh.py" <<'MINAS_SSH_RECOVERY_PY_SOURCE'
#!/usr/bin/python3
"""Bounded root operations for SSH recovery, not a shell or generic file API."""
import base64
import argparse
import contextlib
import fcntl
import hashlib
import json
import io
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zipfile

sys.dont_write_bytecode = True
BASE = Path('/data/.minas-privileged')
STATE = BASE/'sshrecovery'
WORKER = Path('/data/ssh-autostart-fix/restore-ssh.sh')
CRON = Path('/etc/cron.d/keep-ssh')
DROPIN = Path('/etc/systemd/system/minas.boot_check.service.d/90-keep-ssh.conf')
SOCKET = 'dropbear.socket'
BOOT = 'minas.boot_check.service'
SHELL_DROPIN = Path('/etc/systemd/system/dropbear@.service.d/90-normal-shell.conf')
ROOT_TOOL = Path('/data/ssh-autostart-fix/recover-ssh.py')
LOGIN_BACKUP = ROOT_TOOL.parent/'login-authorized-keys'
PENDING_CRON = Path('/etc/cron.d/ssh-recovery-once')
PENDING_RESULT = ROOT_TOOL.parent/'emergency-result.json'


class RecoveryError(Exception):
    pass


def require(ok, message):
    if not ok: raise RecoveryError(message)


def safe(path):
    """All privileged targets must remain root-owned, without symlink redirects."""
    for node in [path]+list(path.parents):
        require(not node.is_symlink(), '恢复路径包含符号链接，已停止操作')
        if node.exists():
            s=node.stat()
            require(s.st_uid==0 and not s.st_mode & 0o022, '恢复路径权限异常，已停止操作')


def atomic(path, data, mode=0o600):
    safe(path)
    fd,name=tempfile.mkstemp(prefix='.sshrecovery-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as out:
            out.write(data);out.flush();os.fsync(out.fileno());os.fchmod(out.fileno(),mode)
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def public_key(text):
    require(isinstance(text,str) and 0<len(text)<=16384, '公钥长度无效')
    lines=[s.strip() for s in text.splitlines() if s.strip()]
    require(len(lines)==1, '每次只能导入一条公钥，不接受 authorized_keys 选项')
    parts=lines[0].split()
    require(len(parts)>=2 and parts[0] in ('ssh-ed25519','ssh-rsa','ecdsa-sha2-nistp256','ecdsa-sha2-nistp384','ecdsa-sha2-nistp521'), '不支持的公钥格式')
    try:
        raw=base64.b64decode(parts[1],validate=True);position=0
        def field():
            nonlocal position
            size=struct.unpack('>I',raw[position:position+4])[0];position+=4
            value=raw[position:position+size];position+=size
            if len(value)!=size:raise ValueError()
            return value
        algorithm=field().decode('ascii')
        if algorithm!=parts[0]:raise ValueError()
        if algorithm=='ssh-ed25519':
            if len(field())!=32:raise ValueError()
        elif algorithm=='ssh-rsa':
            exponent,modulus=field(),field()
            if not 1<=len(exponent)<=8 or int.from_bytes(exponent,'big')<3 or not 2048<=int.from_bytes(modulus,'big').bit_length()<=8192:raise ValueError()
        else:
            curve=field().decode('ascii');point=field()
            if curve!=algorithm.removeprefix('ecdsa-sha2-') or len(point)!={'nistp256':65,'nistp384':97,'nistp521':133}[curve] or point[0]!=4:raise ValueError()
        if position!=len(raw):raise ValueError()
    except (ValueError,UnicodeError,struct.error,KeyError) as exc:
        raise RecoveryError('公钥内容损坏或密钥长度不安全') from exc
    canonical=parts[0]+' '+base64.b64encode(raw).decode()+' ssh-recovery'
    fingerprint='SHA256:'+base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip('=')
    return canonical,fingerprint


def command(args, timeout=15):
    result=subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=timeout,env=root_environment())
    require(result.returncode==0, '系统操作失败，请检查日志与设备固件支持情况')
    return result.stdout.strip()


def root_environment():
    return {'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','HOME':pwd.getpwuid(0).pw_dir,'LC_ALL':'C','OPENSSL_CONF':'/dev/null'}


def private_public(text):
    require(isinstance(text,str) and 0<len(text)<=65536, '私钥长度无效')
    require(text.strip().startswith('-----BEGIN OPENSSH PRIVATE KEY-----') and text.strip().endswith('-----END OPENSSH PRIVATE KEY-----'), '私钥仅支持 OpenSSH Ed25519；其他密钥请上传 .pub 文件')
    try:
        raw=base64.b64decode(''.join(text.strip().splitlines()[1:-1]),validate=True)
        prefix=b'openssh-key-v1\0'
        if not raw.startswith(prefix):raise ValueError()
        position=len(prefix)
        def field():
            nonlocal position
            size=struct.unpack('>I',raw[position:position+4])[0];position+=4
            value=raw[position:position+size];position+=size
            if len(value)!=size:raise ValueError()
            return value
        cipher,kdf,options=field(),field(),field()
        require(cipher==b'none' and kdf==b'none' and not options,'不支持加密私钥，请上传对应 .pub 文件')
        count=struct.unpack('>I',raw[position:position+4])[0];position+=4
        if count!=1:raise ValueError()
        pub,private=field(),field()
        if position!=len(raw) or len(private)<8 or private[:4]!=private[4:8]:raise ValueError()
        offset=8
        def part():
            nonlocal offset
            size=struct.unpack('>I',private[offset:offset+4])[0];offset+=4
            value=private[offset:offset+size];offset+=size
            if len(value)!=size:raise ValueError()
            return value
        algorithm,point,secret,comment=part(),part(),part(),part()
        if algorithm!=b'ssh-ed25519' or len(point)!=32 or len(secret)!=64 or secret[32:]!=point:raise ValueError()
        if private[offset:]!=bytes(range(1,len(private)-offset+1)):raise ValueError()
        string=lambda v:struct.pack('>I',len(v))+v
        if pub!=string(algorithm)+string(point):raise ValueError()
        # Derive the public key from the seed using OpenSSL via stdin only.
        result=subprocess.run(['/usr/bin/openssl','pkey','-inform','DER','-pubout','-outform','DER'],
                              input=bytes.fromhex('302e020100300506032b657004220420')+secret[:32],capture_output=True,timeout=10,env=root_environment())
        if result.returncode or result.stdout!=bytes.fromhex('302a300506032b6570032100')+point:raise ValueError()
        return 'ssh-ed25519 '+base64.b64encode(pub).decode()
    except (ValueError,struct.error) as exc:raise RecoveryError('私钥内容损坏或公私钥不匹配') from exc


def unit_state(unit):
    out=command(['/usr/bin/systemctl','show',unit,'-p','LoadState','-p','ActiveState','-p','SubState',
                 '-p','ExecMainStartTimestampMonotonic','-p','ExecMainExitTimestampMonotonic','-p','Job'])
    return dict(line.split('=',1) for line in out.splitlines() if '=' in line)


def boot_finished():
    s=unit_state(BOOT)
    if s.get('LoadState')=='not-found':return None
    start=int(s.get('ExecMainStartTimestampMonotonic','0'));end=int(s.get('ExecMainExitTimestampMonotonic','0'))
    return start>0 and end>=start and s.get('Job','') in ('','0') and (s.get('ActiveState'),s.get('SubState')) in (('active','exited'),('inactive','dead'),('failed','failed'))


class Recovery:
    def __init__(self,user,state=STATE,root_home=None,host_key=Path('/etc/dropbear/dropbear_rsa_host_key'),shell_paths=None):
        self.user=user;self.state=state;self.host_key=host_key
        self.root_home=Path(root_home or pwd.getpwuid(0).pw_dir)
        self.key=state/'keys'/(user+'.pub');self.host_backup=state/'host/dropbear_rsa_host_key'
        self.logfile=state/'events.json'
        self.settings_file=state/'keys'/(user+'.settings.json')
        self.generated=state/'tmp'/('generated-'+user)
        self.shell_dropin,self.normal_shell,self.limited_shell=shell_paths or (SHELL_DROPIN,Path('/usr/bin/bash.bash'),Path('/usr/sbin/mi-shell'))

    def restore_shell(self):
        # New firmware launches mi-shell despite root's configured login shell.
        # Bind Bash only inside each Dropbear service; retain the vendor binary.
        if not self.limited_shell.exists():return False
        source=self.normal_shell.resolve(strict=True);target=self.limited_shell.resolve(strict=True)
        for path in (source,target,self.shell_dropin):safe(path)
        require(source.is_file() and source.stat().st_mode & 0o111 and target.is_file(),'固件的 SSH 命令解释器不可用')
        require(all(re.fullmatch(r'/[A-Za-z0-9_./@-]+',str(p)) for p in (source,target)),'SSH 命令解释器路径异常')
        data=('[Service]\nBindReadOnlyPaths='+str(source)+':'+str(target)+'\n').encode()
        if self.shell_dropin.exists():
            require(self.shell_dropin.read_bytes()==data,'已有不同的 SSH shell 配置，已停止覆盖')
            return True
        self.shell_dropin.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
        atomic(self.shell_dropin,data,0o644)
        try:command(['/usr/bin/systemctl','daemon-reload'])
        except Exception:
            self.shell_dropin.unlink()
            raise
        self.log('已恢复新 SSH 会话的系统命令执行；固件 mi-shell 文件保留')
        return True

    def login_status(self):
        folder=self.root_home/'.ssh';auth=folder/'authorized_keys'
        result={'rootHome':str(self.root_home),'homeExists':self.root_home.is_dir(),
                'sshDirectoryExists':folder.is_dir(),'authorizedKeysExists':auth.is_file(),
                'savedKeyInstalled':False,'permissionsSafe':False,'ready':False,'reason':'home_missing'}
        if not result['homeExists']:return result
        if not result['sshDirectoryExists']:result['reason']='ssh_missing';return result
        if not result['authorizedKeysExists']:result['reason']='authorization_missing';return result
        try:
            safe(auth)
            require(folder.stat().st_mode & 0o077==0 and auth.stat().st_mode & 0o077==0,'登录目录或公钥文件权限异常')
            result['permissionsSafe']=True
            if self.key.exists():
                wanted=public_key(self.key.read_text())[0].split()[:2]
                result['savedKeyInstalled']=any(line.split()[:2]==wanted for line in auth.read_text().splitlines())
                result['ready']=result['savedKeyInstalled']
                result['reason']='ready' if result['ready'] else 'saved_key_not_installed'
            else:
                result['reason']='no_saved_key'
        except (RecoveryError,OSError,ValueError):result['reason']='permissions_or_content'
        return result

    def emergency_bundle(self):
        memory=io.BytesIO();files={'recover-ssh.py':Path(__file__).read_bytes(),
                                 'restore-ssh.sh':(BASE/'ssh-recovery-boot.sh').read_bytes(),
                                 'manifest.json':json.dumps({'version':'1.1.0','user':self.user}).encode()}
        if self.key.exists():files['recovery.pub']=(public_key(self.key.read_text())[0]+'\n').encode()
        files['README.txt']=('SSH 应急包 1.1.0\n仅包含恢复程序、可选登录公钥，不包含私钥或 NAS 主机密钥。\n'
            '请提前下载到电脑并保留 Portainer 管理入口。\n'
            'SSH 与插件都无法使用时，将本包解压到 NAS 用户共享文件夹 data/ssh-emergency。\n'
            '在容器里将 NAS 的 / 绑定到 /nas，使用 root Console 执行：\n'
            'chroot /nas /usr/bin/python3 /nas/pool0/'+self.user+'/data/ssh-emergency/recover-ssh.py --local-recover --offline\n'
            '脚本先修复文件，再通过一次性宿主机 Cron 加载配置和开启 SSH。需宿主机 crond 正常运行。\n'
            '查看结果：cat /nas/data/ssh-autostart-fix/emergency-result.json\n'
            '也可在已有宿主机 root 终端执行同一程序并去掉 --offline。\n'
            '若未包含 recovery.pub，执行时用 --public-key 指定自己的公钥文件。\n'
            '固件若移除 Python、Cron、Docker 或所有管理入口，本包不能保证恢复；没有预留隐藏账户或免认证端口。\n').encode()
        files['SHA256SUMS']=(''.join(hashlib.sha256(data).hexdigest()+'  '+name+'\n' for name,data in files.items())).encode()
        with zipfile.ZipFile(memory,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,data in files.items():archive.writestr(name,data)
        self.log('已导出离线应急包；不包含任何私钥')
        return base64.b64encode(memory.getvalue()).decode()

    def settings(self):
        try:value=json.loads(self.settings_file.read_bytes())
        except (OSError,ValueError):value={}
        return {'generateWithoutPublicKey':value.get('generateWithoutPublicKey') is True}

    def save_settings(self,value):
        require(isinstance(value,bool),'生成设置必须为开关值')
        atomic(self.settings_file,(json.dumps({'generateWithoutPublicKey':value})+'\n').encode())
        self.log('已'+('开启' if value else '关闭')+'无公钥时生成登录密钥；此操作不生成密钥')

    def clear_generated(self):
        safe(self.generated)
        if self.generated.exists():shutil.rmtree(self.generated)

    def generated_info(self):
        safe(self.generated)
        try:info=json.loads((self.generated/'meta.json').read_bytes())
        except (OSError,ValueError):return {'ready':False}
        if float(info.get('expires',0))<=time.time():
            self.clear_generated();return {'ready':False}
        return {'ready':(self.generated/'private.key').is_file(),'expires':info['expires']}

    def generate_login_key(self):
        self.clear_generated()
        seed=os.urandom(32)
        result=subprocess.run(['/usr/bin/openssl','pkey','-inform','DER','-pubout','-outform','DER'],
                              input=bytes.fromhex('302e020100300506032b657004220420')+seed,capture_output=True,timeout=10,env=root_environment())
        require(result.returncode==0 and len(result.stdout)==44,'无法生成安全的 Ed25519 登录密钥')
        point=result.stdout[-32:];string=lambda v:struct.pack('>I',len(v))+v
        pub=string(b'ssh-ed25519')+string(point)
        check=os.urandom(4)
        private=check+check+string(b'ssh-ed25519')+string(point)+string(seed+point)+string(b'ssh-recovery')
        padding=8-len(private)%8;private+=bytes(range(1,padding+1))
        raw=b'openssh-key-v1\0'+string(b'none')+string(b'none')+string(b'')+struct.pack('>I',1)+string(pub)+string(private)
        encoded=base64.b64encode(raw).decode()
        private_text='-----BEGIN OPENSSH PRIVATE KEY-----\n'+'\n'.join(encoded[i:i+70] for i in range(0,len(encoded),70))+'\n-----END OPENSSH PRIVATE KEY-----\n'
        public,fingerprint=public_key(private_public(private_text))
        self.generated.mkdir(mode=0o700)
        try:
            atomic(self.generated/'private.key',private_text.encode())
            atomic(self.generated/'public.pub',(public+'\n').encode())
            atomic(self.generated/'meta.json',json.dumps({'expires':time.time()+900}).encode())
            atomic(self.key,(public+'\n').encode())
        except Exception:
            self.clear_generated();raise
        self.log('已生成登录密钥：'+fingerprint+'；私钥仅允许一次下载，15 分钟后过期')

    def download_generated(self):
        require(self.generated_info()['ready'],'下载已使用或过期，请勿重复请求')
        memory=io.BytesIO()
        with zipfile.ZipFile(memory,'w',zipfile.ZIP_DEFLATED) as archive:
            for source,name in [('private.key','ssh-login.key'),('public.pub','ssh-login.pub')]:
                path=self.generated/source;safe(path)
                entry=zipfile.ZipInfo(name);entry.external_attr=0o600<<16
                archive.writestr(entry,path.read_bytes())
        result=base64.b64encode(memory.getvalue()).decode()
        self.clear_generated();self.log('登录密钥包已领取，NAS 临时私钥已删除；请妥善保存下载文件')
        return result

    def log(self,message):
        try:events=json.loads(self.logfile.read_bytes())
        except (OSError,ValueError):events=[]
        events.append({'time':int(time.time()),'user':self.user,'message':message})
        atomic(self.logfile,(json.dumps(events[-80:],ensure_ascii=False)+'\n').encode())

    def save_public(self,text):
        canonical,fingerprint=public_key(text)
        self.clear_generated()
        atomic(self.key,(canonical+'\n').encode())
        self.log('已保存恢复公钥：'+fingerprint)
        return fingerprint

    def save_private(self,text):
        return self.save_public(private_public(text))

    def restore_public(self):
        if not self.key.exists():return False
        canonical,_=public_key(self.key.read_text())
        safe(self.root_home)
        if not self.root_home.exists():
            try:self.root_home.mkdir(mode=0o700)
            except OSError as exc:raise RecoveryError('无法创建 root 登录目录：'+str(exc)) from exc
        require(self.root_home.is_dir(),'root 登录目录不是目录，已停止操作')
        folder=self.root_home/'.ssh';safe(folder)
        folder.mkdir(mode=0o700,exist_ok=True);folder.chmod(0o700)
        target=folder/'authorized_keys';safe(target)
        data=target.read_bytes() if target.exists() else b''
        require(len(data)<=1024*1024, 'authorized_keys 文件过大，已停止操作')
        wanted=canonical.split()[1]
        present=any(wanted in line.split() for line in data.decode('utf-8',errors='replace').splitlines())
        if not present:data=data.rstrip(b'\n')+(b'\n' if data else b'')+canonical.encode()+b'\n'
        atomic(target,data)
        return True

    def restore_host(self):
        safe(self.host_key);safe(self.host_backup)
        if self.host_key.exists():
            require(0<self.host_key.stat().st_size<=65536, '主机密钥异常，未覆盖现有文件')
            if not self.host_backup.exists():atomic(self.host_backup,self.host_key.read_bytes(),0o400)
            return 'preserved'
        self.host_key.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
        if self.host_backup.exists():
            atomic(self.host_key,self.host_backup.read_bytes());return 'restored'
        command(['/usr/sbin/dropbearkey','-t','rsa','-s','2048','-f',str(self.host_key)],timeout=25)
        self.host_key.chmod(0o600);atomic(self.host_backup,self.host_key.read_bytes(),0o400)
        return 'generated'

    def status(self):
        socket=unit_state(SOCKET);boot=boot_finished()
        fingerprint=''
        if self.key.exists():_,fingerprint=public_key(self.key.read_text())
        try:events=json.loads(self.logfile.read_bytes())
        except (OSError,ValueError):events=[]
        host_valid=False
        if self.host_key.is_file():
            try:host_valid=bool(command(['/usr/sbin/dropbearkey','-y','-f',str(self.host_key)],timeout=5))
            except (RecoveryError,OSError,subprocess.TimeoutExpired):pass
        return dict(version='1.1.0',loginAuthorization=self.login_status(),hostKeyValid=host_valid,sshActive=socket.get('ActiveState')=='active',socketSupported=socket.get('LoadState')!='not-found',port=22,
                    bootFinished=boot,keySaved=bool(fingerprint),fingerprint=fingerprint,hostKey=self.host_key.is_file(),hostBackup=self.host_backup.is_file(),
                    bootService=dict(worker=WORKER.is_file(),cron=CRON.is_file(),dropin=DROPIN.is_file()),
                    events=[e for e in reversed(events) if e.get('user')==self.user][:20],settings=self.settings(),generated=self.generated_info())

    def recover(self):
        require(unit_state(SOCKET).get('LoadState')!='not-found','固件没有 dropbear.socket，不支持恢复')
        require(boot_finished() is not False,'小米开机检查尚未结束，请稍后重试')
        if not self.key.exists() and self.settings()['generateWithoutPublicKey']:
            self.generate_login_key()
        host='untouched';key=False
        if self.key.exists():
            if str(self.root_home)==pwd.getpwuid(0).pw_dir:self.root_home=prepare_root_account()
            host=self.restore_host();key=self.restore_public()
        normal_shell=self.restore_shell()
        command(['/usr/bin/systemctl','reset-failed',SOCKET])
        command(['/usr/bin/systemctl','start',SOCKET])
        require(unit_state(SOCKET).get('ActiveState')=='active','SSH 未恢复，请检查系统日志')
        message='SSH 已开启；'+('恢复公钥已写入。' if key else '未保存公钥，未改动任何密钥文件。')
        if normal_shell:message+='新建 SSH 会话可执行系统命令。'
        if not key and not self.host_key.exists():message+='主机密钥仍缺失；若无法登录，请配置恢复材料后再处理。'
        if self.generated_info()['ready']:message+='新登录密钥可在 15 分钟内下载一次。'
        if host=='generated':message+='主机密钥已重新生成，下次连接需核对新指纹。'
        self.log(message)
        return message

    def install_boot(self,overwrite=False):
        require(unit_state(BOOT).get('LoadState')!='not-found' and unit_state(SOCKET).get('LoadState')!='not-found','设备缺少受支持的开机检查或 SSH 单元')
        asset=BASE/'ssh-recovery-boot.sh';safe(asset)
        data={WORKER:asset.read_bytes(),ROOT_TOOL:Path(__file__).read_bytes(),
              DROPIN:b'[Service]\n# Restore SSH after the Xiaomi boot check when this drop-in is loaded.\nExecStartPost=/usr/bin/systemctl start dropbear.socket\n',
              CRON:b'SHELL=/bin/sh\nPATH=/usr/sbin:/usr/bin:/sbin:/bin\nMAILTO=""\n\n# Wait for the real boot-check completion, then perform bounded SSH rechecks.\n@reboot root /bin/sh /data/ssh-autostart-fix/restore-ssh.sh\n'}
        for target,value in data.items():
            safe(target)
            require(overwrite or not target.exists() or target.read_bytes()==value,'已有不同的 SSH 修复代码；确认替换后再操作')
        command(['/bin/sh','-n',str(asset)])
        previous={p:p.read_bytes() if p.exists() else None for p in data}
        try:
            for target,value in data.items():
                target.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
                atomic(target,value,0o755 if target in (WORKER,ROOT_TOOL) else 0o644)
            snapshot_login(self.root_home)
            self.restore_shell()
            command(['/usr/bin/systemctl','daemon-reload'])
            command(['/usr/bin/systemctl','enable',SOCKET])
            command(['/usr/bin/systemctl','reload','crond.service'])
        except Exception:
            for target,value in previous.items():
                if value is None:target.unlink(missing_ok=True)
                else:atomic(target,value,0o755 if target in (WORKER,ROOT_TOOL) else 0o644)
            subprocess.run(['/usr/bin/systemctl','daemon-reload'],capture_output=True,timeout=15)
            raise
        self.log('已安装新旧固件开机修复：等待开机检查、补回缺失登录材料、修复 mi-shell、有限复查')
        return '开机 SSH 修复服务已安装；不重启 NAS，下一次开机生效。'


def init_recovery_state():
    # Shell CGI checks executability before sudo; the shared entry directory
    # must remain traversable. Recovery state and key directories stay private.
    for path,mode in [(BASE,0o755)]+[(p,0o700) for p in (STATE,STATE/'keys',STATE/'host',STATE/'tmp',ROOT_TOOL.parent)]:
        safe(path);path.mkdir(mode=mode,parents=True,exist_ok=True);path.chmod(mode)


def prepare_root_account(passwd_file=Path('/etc/passwd'),fallback=Path('/home/rootx')):
    safe(passwd_file)
    lines=passwd_file.read_text().splitlines();matches=[i for i,line in enumerate(lines) if line.startswith('root:')]
    require(len(matches)==1,'root 账户记录异常')
    i=matches[0];fields=lines[i].split(':');require(len(fields)==7 and fields[2]=='0','root 账户记录异常')
    current=Path(fields[5]);require(current.is_absolute(),'root 主目录无效')
    if current==Path('/root'):
        safe(fallback);fallback.mkdir(mode=0o700,exist_ok=True)
        old=current/'.ssh/authorized_keys'
        if old.is_file():
            safe(old);data=old.read_bytes();require(len(data)<=1024*1024,'登录公钥文件过大')
            folder=fallback/'.ssh';safe(folder);folder.mkdir(mode=0o700,exist_ok=True);folder.chmod(0o700)
            target=folder/'authorized_keys';safe(target)
            existing=target.read_bytes() if target.exists() else b''
            extra=[line for line in data.splitlines() if line and line not in existing.splitlines()]
            if extra:atomic(target,existing.rstrip(b'\n')+(b'\n' if existing else b'')+b'\n'.join(extra)+b'\n')
        fields[5]=str(fallback);current=fallback
    if fields[6].endswith('/mi-shell'):fields[6]='/bin/sh'
    replacement=':'.join(fields)
    if lines[i]!=replacement:
        lines[i]=replacement;atomic(passwd_file,('\n'.join(lines)+'\n').encode(),stat.S_IMODE(passwd_file.stat().st_mode))
    return current


def snapshot_login(root_home):
    auth=root_home/'.ssh/authorized_keys'
    if auth.is_file():
        safe(auth);data=auth.read_bytes();require(len(data)<=1024*1024,'登录公钥文件过大')
        safe(LOGIN_BACKUP);LOGIN_BACKUP.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        atomic(LOGIN_BACKUP,data)


def repair_boot_materials():
    init_recovery_state();home=prepare_root_account();recovery=Recovery('u0',state=STATE,root_home=home)
    if LOGIN_BACKUP.is_file():
        safe(LOGIN_BACKUP);data=LOGIN_BACKUP.read_bytes();require(len(data)<=1024*1024,'公钥恢复副本过大')
        safe(home);home.mkdir(mode=0o700,exist_ok=True)
        folder=home/'.ssh';safe(folder);folder.mkdir(mode=0o700,exist_ok=True);folder.chmod(0o700)
        auth=folder/'authorized_keys';safe(auth)
        if not auth.exists():atomic(auth,data)
        if not recovery.host_key.exists():recovery.restore_host()
    recovery.restore_shell()


def activate_pending():
    init_recovery_state();safe(PENDING_CRON);safe(PENDING_RESULT)
    with (ROOT_TOOL.parent/'activate.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        PENDING_CRON.unlink(missing_ok=True)
        try:
            deadline=time.monotonic()+300
            while boot_finished() is False:
                require(time.monotonic()<deadline,'等待开机检查超时');time.sleep(2)
            repair_boot_materials();command(['/usr/bin/systemctl','daemon-reload'])
            command(['/usr/bin/systemctl','enable',SOCKET]);command(['/usr/bin/systemctl','reset-failed',SOCKET])
            command(['/usr/bin/systemctl','start',SOCKET])
            require(unit_state(SOCKET).get('ActiveState')=='active','SSH socket 尚未就绪')
            result={'ok':True,'state':'ready','message':'SSH 服务已开启，请用公钥重新连接验证系统命令。'}
        except Exception as error:result={'ok':False,'state':'failed','error':str(error)}
        atomic(PENDING_RESULT,(json.dumps(result,ensure_ascii=False)+'\n').encode())
        return result


def root_cli(argv=None):
    parser=argparse.ArgumentParser(description='NAS root SSH 修复；支持旧固件和 mi-shell 固件，不提供远程免认证入口。')
    modes=parser.add_mutually_exclusive_group(required=True)
    for mode in ('local-recover','boot-repair','prepare-boot','activate-pending','diagnose'):modes.add_argument('--'+mode,action='store_true')
    parser.add_argument('--user');parser.add_argument('--public-key',type=Path)
    parser.add_argument('--offline',action='store_true');parser.add_argument('--install-boot',action='store_true')
    args=parser.parse_args(argv);require(os.geteuid()==0,'此工具必须在 NAS root 终端或宿主机 chroot 中运行')
    os.umask(0o077)
    if args.diagnose:
        print(json.dumps(Recovery('u0').status(),ensure_ascii=False));return
    init_recovery_state();safe(STATE/'operations.lock')
    with (STATE/'operations.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return root_command(args)


def root_command(args):
    if args.activate_pending:print(json.dumps(activate_pending(),ensure_ascii=False));return
    if args.prepare_boot:
        init_recovery_state();snapshot_login(Path(pwd.getpwuid(0).pw_dir));repair_boot_materials();return
    if args.boot_repair:repair_boot_materials();return
    adjacent=Path(__file__).resolve().parent;manifest=adjacent/'manifest.json'
    user=args.user
    if not user and manifest.is_file():user=json.loads(manifest.read_bytes()).get('user')
    require(isinstance(user,str) and re.fullmatch(r'u[0-9]+',user),'请通过 --user 指定 NAS 管理账户，或使用带 manifest.json 的应急包')
    # Both offline and online paths use the same bounded filesystem repairs.
    init_recovery_state();home=prepare_root_account();recovery=Recovery(user,state=STATE,root_home=home)
    key=args.public_key or adjacent/'recovery.pub'
    if key.is_file():
        require(key.stat().st_size<=16384,'公钥文件过大');recovery.save_public(key.read_text())
    if recovery.key.exists():recovery.restore_public();recovery.restore_host()
    require((home/'.ssh/authorized_keys').is_file(),'没有登录授权，请提供 --public-key 公钥文件')
    atomic(ROOT_TOOL,Path(__file__).read_bytes(),0o700);snapshot_login(home)
    if args.offline:
        cron=b'SHELL=/bin/sh\nPATH=/usr/sbin:/usr/bin:/sbin:/bin\n* * * * * root /usr/bin/python3 /data/ssh-autostart-fix/recover-ssh.py --activate-pending >/dev/null 2>&1\n'
        safe(PENDING_CRON);require(not PENDING_CRON.exists() or PENDING_CRON.read_bytes()==cron,'已有不同的一次性恢复任务')
        atomic(PENDING_RESULT,b'{"ok":false,"state":"pending"}\n');atomic(PENDING_CRON,cron,0o644)
        print(json.dumps({'ok':True,'state':'pending','message':'文件已准备；等待宿主机 Cron 激活，尚未验证 SSH。','result':str(PENDING_RESULT)},ensure_ascii=False));return
    recovery.restore_shell();command(['/usr/bin/systemctl','daemon-reload'])
    result=activate_pending();require(result['ok'],result.get('error','SSH 激活失败'))
    if args.install_boot:
        asset=adjacent/'restore-ssh.sh';require(asset.is_file(),'缺少 restore-ssh.sh')
        atomic(BASE/'ssh-recovery-boot.sh',asset.read_bytes(),0o755);recovery.install_boot(True)
    print(json.dumps(result,ensure_ascii=False))


def prune():
    require(os.geteuid()==0,'仅允许系统清理过期临时密钥')
    safe(STATE)
    with (STATE/'operations.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        for folder in (STATE/'tmp').glob('generated-u*'):
            if re.fullmatch(r'generated-u[0-9]+',folder.name):
                Recovery(folder.name.removeprefix('generated-'),state=STATE).generated_info()


def api(user,request):
    require(os.geteuid()==0,'需要安装插件的受限权限组件')
    safe(BASE);safe(STATE)
    import privileged_runtime as runtime
    runtime.authorize(user,'sshrecovery')
    require(request.get('pluginUser')==user,'禁止访问其他用户的恢复材料')
    home=Path('/home')/user/'plugin/sshrecovery'
    require((home/'INFO').is_file(),'插件未安装')
    recovery=Recovery(user)
    with (STATE/'operations.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        action=request.get('action')
        extra={}
        if action=='status':message=''
        elif action=='recover':message=recovery.recover()
        elif action=='import_public':message='恢复公钥已保存：'+recovery.save_public(request.get('key'))
        elif action=='import_private':message='已提取并校验公钥，私钥未写入磁盘：'+recovery.save_private(request.get('key'))
        elif action=='save_settings':
            recovery.save_settings(request.get('generateWithoutPublicKey'));message='生成选项已保存；未执行密钥生成。'
        elif action=='download_keys':
            extra={'keyArchive':recovery.download_generated(),'downloadName':'ssh-login-keys.zip'};message='密钥包已领取，请保存下载文件。'
        elif action=='download_emergency':
            extra={'emergencyArchive':recovery.emergency_bundle(),'downloadName':'nas-ssh-emergency.zip'};message='应急包已生成，请保存到电脑。包内不包含任何私钥。'
        elif action=='forget_key':
            recovery.key.unlink(missing_ok=True);recovery.clear_generated();recovery.log('已移除插件内保存的公钥；现有 authorized_keys 未修改');message='保存的恢复公钥已移除，当前登录公钥未删除。'
        elif action=='install_boot':message=recovery.install_boot(request.get('overwrite') is True)
        else:raise RecoveryError('不支持的操作')
        return dict(ok=True,message=message,**recovery.status(),**extra)


if __name__=='__main__':
    try:root_cli()
    except Exception as error:
        print(json.dumps({'ok':False,'error':str(error)},ensure_ascii=False));sys.exit(1)
MINAS_SSH_RECOVERY_PY_SOURCE

# ---- restore-ssh.sh (plain source) ----
cat > "$stage/restore-ssh.sh" <<'MINAS_SSH_RESTORE_SH_SOURCE'
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
if [ -f /data/ssh-autostart-fix/recover-ssh.py ]; then
    if ! timeout 30 /usr/bin/python3 /data/ssh-autostart-fix/recover-ssh.py --boot-repair; then
        log 'ERROR: SSH login material or mi-shell compatibility repair failed'
        exit 1
    fi
fi
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
MINAS_SSH_RESTORE_SH_SOURCE

python3 -B "$stage/open_ssh_client.py" "$@"
