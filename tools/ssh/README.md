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

插件源文件 `ssh-recovery-plugin/payload/system/restore-ssh.sh` 与 `tools/ssh/src/restore-ssh.sh` 保持完全一致；两个单文件脚本直接嵌入它。构建与测试校验逐字节一致。插件网页后台仍使用原有 Python API；其安装的开机 worker 与电脑端脚本均为 Shell。

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

## 构建与测试

从仓库根目录执行：

```bash
python3 tools/ssh/build.py
python3 -B -m unittest discover -s tools/ssh/tests -p 'test_*.py'
```

`src/open-ssh-client.sh` 为客户端源码，`src/restore-ssh.sh` 为与插件共用的 worker。构建生成根目录的两个单文件 SSH 入口，不生成其他根目录文件。Python 仅用于本地构建和测试，SSH 运行流程为纯 Shell。

本地测试依赖保存在本工具 `.tools/`；旧版离线应急材料保存在 `offline/`，均不提交 GitHub、不收入安装包。
