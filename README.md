# 小米智能存储 SSH 开启与开机恢复

版本 **2.1.0**。[仓库](https://github.com/daixu0502/xiaomi-minas-tool-root)。SSH 入口基于 Scruel 的 [minas-enable-ssh_wsl.sh](https://github.com/scruel/xiaomi-storage-minas-tool/blob/main/minas-enable-ssh_wsl.sh) 认证和 WebDAV 引导流程改写，客户端及 NAS 开机恢复均不依赖 Python。源码直接以明文 heredoc 内嵌，无静态 Base64 包。

## 快速使用（WSL Ubuntu）

已有可执行真实 root 命令的 SSH 时：

```bash
bash xiaomi-storage-open-ssh-wsl.sh 192.168.31.100 --user u123456789
# 只安装或更新开机修复，保留当前登录公钥：
bash install-ssh-autostart-fix.sh 192.168.31.100
```

开启脚本默认同时安装开机修复。唯一 NAS 账户自动选择；多个账户时可输入编号或完整账户名，无交互终端需传 `--user`。账户用于保存恢复公钥，不修改其他账户的配置。用 `--identity /home/你的WSL用户/.ssh/某私钥` 选择现有登录密钥。

如果旧版已提示“NAS 存在多个管理账户”，当次 SSH 实际已经通过 root 命令检查，直接指定 `--user` 即可，不需要强制旧证书路径。

### 依赖

已有 SSH 路径需要 Bash、OpenSSH、awk、sort、sed 等基本工具。旧证书及 Portainer 路径另需 curl、jq；旧证书路径还需 openssl、timeout。Ubuntu 缺少时可安装：

```bash
sudo apt update
sudo apt install openssh-client curl jq openssl coreutils
```

自动查询 Windows 证书目录使用 WSL 自带的 `wslpath` 和 Windows `powershell.exe`，明确输出 UTF-8，支持中文用户名。NAS 使用已有的 `/bin/sh`、systemd、Cron、flock、timeout、stat、awk、Dropbear 等原厂组件；mi-shell 固件还需原有 Bash。

### 未开启过 SSH 的设备

先登录小米 Windows 客户端，让证书写入 `%LOCALAPPDATA%/minasCert`，再运行开启脚本。默认先检查 SSH，不可用才走客户端证书引导。可显式指定证书目录：

```bash
bash xiaomi-storage-open-ssh-wsl.sh 192.168.31.100 \
  --cert-dir /mnt/c/Users/Windows用户名/AppData/Local/minasCert
# 只探测旧入口：临时写入并清理 UID 标记，不修改 SSH 或开机设置：
bash xiaomi-storage-open-ssh-wsl.sh 192.168.31.100 --probe-bootstrap
```

脚本适配新固件的 SSH 修复，不代表能在所有新固件上首次取得 root 权限。当前已测试设备的新固件曾封堵旧引导，需已有 SSH 恢复插件或 Portainer 入口。`--legacy-bootstrap` 仅强制测试旧路径。HTTP 成功、mi-shell 退出码 0 均不能代表 SSH 已恢复。

### Portainer 应急路径（纯 Shell）

```bash
bash xiaomi-storage-open-ssh-wsl.sh 192.168.31.100 --portainer \
  --portainer-url http://192.168.31.100:9001 --portainer-user admin \
  --endpoint 3 --container test --user u123456789
```

目标容器必须运行，并以可写 bind 方式绑定 NAS `/`（例如容器内 `/nas/home`）。脚本通过 Inspect 读取实际映射路径，不创建新容器。Portainer 密码交互输入；凭据只放在当前用户 0700 临时目录中的 0600 文件，不放进 curl 命令参数，完成后清理；HTTPS 验证证书，HTTP 仅用于可信局域网。

容器中通过 chroot 准备 Shell 修复文件；宿主机一次性 Cron 在下一分钟开始激活，最多等待开机检查 300 秒。任务开始即移除一次性 Cron，失败不形成永久重试。状态为 `/data/ssh-autostart-fix/shell-activation.status` 的 `pending`、`ready` 或 `failed`，日志使用 `ssh-autostart-fix` 标签。最终仍须通过客户端真实 root SSH 命令检查。

## 三个入口使用同一份开机修复代码

- `xiaomi-storage-open-ssh-wsl.sh`：开启/修复 SSH，默认安装开机恢复。
- `install-ssh-autostart-fix.sh`：已有正常 SSH 时安装/更新相同开机恢复，不新增公钥。
- SSH 恢复插件 **1.1.7**：“安装 / 更新开机修复”使用同一份 `restore-ssh.sh`。

插件源文件 `ssh-recovery-plugin/payload/system/restore-ssh.sh` 与本目录 `restore-ssh.sh` 保持完全一致；两个单文件脚本直接嵌入它。构建与测试校验逐字节一致。插件网页后台仍使用原有 Python API；其安装的开机 worker 与电脑端脚本均为 Shell。

共同逻辑：等待 `minas.boot_check.service` 实际结束（最多 300 秒），恢复缺失的登录授权，兼容旧 `/root` 迁移到 `/home/rootx` 和新版 mi-shell，通过 Dropbear 私有挂载空间绑定原厂 Bash，最后每 5 秒复查 SSH，最多 12 次，随后退出。保留原厂 mi-shell 二进制、root 密码和其他账户。

现有 `authorized_keys` 包括空文件都不会被旧快照覆盖；只在缺失时补回。更换或撤销公钥后重新运行安装脚本同步快照。删除 `/data/ssh-autostart-fix/login-authorized-keys` 可停止该缺失授权恢复。私钥只保存在电脑端，不上传 NAS；不存在私钥但已存在 `.pub` 时停止，避免误覆盖。

### NAS 写入位置

| 位置 | 用途 |
|---|---|
| `/home/rootx/.ssh/authorized_keys` | 合并新公钥，保留原有授权；旧 `/root` 的原有授权同步保留 |
| `/etc/passwd` | 只调整 root 主目录，必要时将 mi-shell 登录 shell 改为 `/bin/sh`；原始副本在下述私有目录 |
| `/data/ssh-autostart-fix/restore-ssh.sh` | 三个入口共用的纯 Shell 修复程序与开机 worker |
| `/data/ssh-autostart-fix/login-authorized-keys` | 当前登录授权快照；仅补缺失文件 |
| `/data/ssh-autostart-fix/host/dropbear_rsa_host_key` | 缺失时恢复 SSH 主机身份；已有密钥不会轮换 |
| `/data/ssh-autostart-fix/passwd.before-shell-repair` | 首次修改前的 root 账户文件备份，0600 |
| `/data/.minas-privileged/sshrecovery/keys/<账户>.pub` | 与插件共用的恢复公钥；仅开启脚本按所选账户写入 |
| `/data/.minas-privileged/sshrecovery/operations.lock` | 与插件共用的互斥锁 |
| `/etc/systemd/system/dropbear@.service.d/90-normal-shell.conf` | 新固件服务私有 Bash 映射 |
| `/etc/systemd/system/minas.boot_check.service.d/90-keep-ssh.conf` | 开机检查完成后启动 SSH |
| `/etc/cron.d/keep-ssh` | 开机运行有限复查 |
| `/etc/cron.d/minas-shell-ssh-activate` | 仅 Portainer 离线路径的一次性激活任务 |
| `/run/ssh-autostart-fix.lock` | 本次开机 worker 的互斥锁 |

修复不修改 RPMB、root 密码，不重启 NAS、Docker 或已有 SSH 会话；新 SSH 会话使用更新后的 shell。不会写入参与原厂校验的插件源码目录。固件更新若删除配置或入口，仍可能需要通过保留的管理入口重新执行。

### 检查与日志

```bash
bash xiaomi-storage-open-ssh-wsl.sh 192.168.31.100 --check
ssh root@192.168.31.100 id
ssh root@192.168.31.100 '/bin/sh /data/ssh-autostart-fix/restore-ssh.sh --check'
ssh root@192.168.31.100 'journalctl -b -t ssh-autostart-fix --no-pager'
```

`--check` 不安装开机修复、不生成密钥。测试通过不替代实际重启验证；本次更新不主动重启设备。

## 文件整理

保留当前两个单文件入口、Shell 客户端源码、共享 worker、构建脚本、测试、固件参考和独立 Docker 工具。已删除被纯 Shell 替代的 `open_ssh_client.py`、`recover-ssh.py`；本地 `offline/` 是此前导出的旧版应急材料，继续本地保存，不作为新版入口、不上传。`.tools/` 仅为本地测试依赖，不收入源码或安装包。

## Docker bridge 网络出口修复

适用于使用 `/etc/config/mi_docker` → `/usr/sbin/dockerd_prepare.sh` → `/tmp/dockerd/daemon.json` 的小米智能存储固件；已在 Docker **20.10.17** 上检查。不是通用 Linux／小米路由器 Docker 安装器，也不安装 Docker 或获取 root。

之前的出口异常来自 `mi_docker.globals.iptables=0`：Docker 不生成 bridge 所需 NAT、转发及端口映射规则。最终采用的方案是恢复 **Docker 原生 iptables 管理**，而非继续使用 `/data/bridge-net/apply.py`。覆盖默认 `bridge` 和普通自定义 bridge；跳过明确设置 `internal` 或关闭 masquerade 的网络，不为它们擅自开启出口。不负责 IPv6、公网防火墙放行、代理或上游网络故障。

在 **NAS 的真实 root 命令行**执行（不是 WSL，不是容器内，不是受限 mi-shell）：

```sh
# 下载到一个新的私有临时目录；先检查，再明确修复。
work=$(mktemp -d /tmp/minas-network.XXXXXXXX) && chmod 700 "$work"
curl -fL https://raw.githubusercontent.com/daixu0502/xiaomi-minas-tool-root/main/fix-docker-bridge.sh -o "$work/fix-docker-bridge.sh"
sh "$work/fix-docker-bridge.sh" --check
sh "$work/fix-docker-bridge.sh" --apply
```

脚本也可从仓库下载后单文件复制到 NAS，无需 Python 包或其他旁边文件。下载失败时不要继续执行；建议先审阅源码。`--check`（不传参数时也是检查）完全只读：退出码 **0** 表示基础规则正常，**2** 表示需要处理，**1** 表示检查失败。检查不拉取镜像、不创建容器、不执行用户容器内命令、不重启 Docker。结构检查通过不等于互联网、DNS 或业务连通性已通过。

`--apply` 会要求输入 `APPLY`；自动化执行须显式加 `--yes`。**会短暂停止原运行容器并重启 Docker**，应在维护窗口执行。它会：

1. 确认真正的存储池已挂载、没有其他待确认事务或未提交 UCI 改动。
2. 在 root 专用事务目录记录原 UCI、运行容器 ID、防火墙规则和旧辅助服务状态，先启用 **10 分钟自动回退**。
3. 暂停旧 `bridge-net.service`（如果存在），只把 `mi_docker.globals.iptables` 改为 `1`，由原厂启动流程重建 Docker 配置。
4. 恢复原运行容器，包括原本 `--restart no` 的容器；不改容器定义、网络、卷、代理、restart policy 或 Docker 数据目录。
5. **仅删除注释严格等于 `minas-bridge-net` 的旧辅助规则**，不清空防火墙、不删除未标记规则，不直接删除历史脚本或服务文件。
6. 校验原生 IPv4 MASQUERADE、Docker 转发链入口、IPv4 转发开关与原运行容器状态。失败时尝试恢复原配置；正常时等待用户确认。

容器出网、DNS、映射端口、容器互通、Openlist 对下载器的连接都验证正常后，**10 分钟内**用输出的事务编号确认：

```sh
sh /path/to/fix-docker-bridge.sh --confirm <事务编号>
# 或不依赖最初下载位置：
python3 -B /data/docker-bridge-repair/repair.py --confirm <事务编号>

# 尚未确认时也可以立即手动回退：
python3 -B /data/docker-bridge-repair/repair.py --rollback <事务编号>
```

确认只取消本事务的回退，不替代业务连通性验证。未确认会自动回退并再次短暂重启 Docker；已确认／已回退事务不会被过期定时器再次回退。不要在待确认期间重启 NAS、编辑 Docker/UCI 或删除状态目录；systemd 临时回退定时器**不跨 NAS 重启保存**。存储池消失时拒绝启动原容器，应挂载恢复后手动回退。事务会记录原防火墙快照供排查，但回退仅恢复原 UCI 选项、转发开关、表策略及本工具识别的旧辅助规则，不把整个防火墙历史快照覆盖回去。

原生方案通过原厂 Docker 启动流程持续生效，无需新增常驻 NAT 服务；固件重置 UCI 或改变 Docker 规则后仍需重新检查。NAS 重启后的恢复需另行实际验证。当前已正常的设备重复执行 `--apply` 不重启服务、不建立事务。

### 工具数据与日志

- `/data/docker-bridge-repair/repair.py`：定时回退所需的独立程序；不注册开机常驻服务。
- `/data/docker-bridge-repair/transactions/<事务编号>/`：原始 `mi_docker.original` 和 `state.json`，目录 0700、文件 0600。包含本机路径、网络及容器信息，不应公开上传。
- `journalctl -u 'minas-docker-bridge-rollback-*' --no-pager`：自动回退过程；脚本检查结果直接输出到当前终端。
- 备份只用于尚未确认事务的回退。已确认／回退并再次验证正常后，可由 root 删除**对应的单个事务目录**；不要删除仍 pending 的事务或与 SSH 密钥有关的其他目录。

## 构建与测试

```bash
python3 build_release.py
# 只重新生成两个 SSH Shell 入口，不改动其他修复程序：
python3 build_release.py --ssh-entrypoint-only
python3 -B -m unittest discover -s tests -p 'test_*.py'
```

Python 仅用于本地构建、回归测试，以及独立的 Docker bridge 网络工具；SSH 开启、安装开机修复及 NAS 开机恢复均为纯 Shell。测试核对两份单文件入口的 worker 与 SSH 恢复插件中的源码完全一致，覆盖假成功、多账户、中文证书目录、旧引导、Portainer TTY 执行、主目录迁移、授权保留及有限复查。

`docker_bridge_repair.py` 是独立网络修复源码，`fix-docker-bridge.sh` 是其生成入口，测试在 `tests/test_docker_bridge.py`。测试使用隔离文件和模拟命令，不修改本机或 NAS 网络。

## 获取源码

在 GitHub 仓库页面选择 **Code → Download ZIP**，解压后保留项目目录结构，按本页说明运行对应脚本。也可从 **Tags** 页面选择指定版本源码。
