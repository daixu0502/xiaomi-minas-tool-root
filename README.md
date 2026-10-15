# 小米智能存储 SSH 开启与开机恢复

版本 **2.0.0**。[仓库](https://github.com/daixu0502/xiaomi-minas-tool-root)。SSH shell 入口可单文件运行，直接内嵌可阅读的 Python/Shell 源码，不使用 Base64 压缩包隐藏静态源码；内置修复程序与 SSH 恢复插件 1.1.0 保持一致。另附独立的 Docker bridge 网络出口修复工具。

## 开启或修复 SSH

```bash
bash xiaomi-storage-open-ssh-wsl.sh 192.168.31.100 --user u123456789
```

WSL 脚本先检查公钥认证及真实 root 命令输出；正常时通过 SSH 修复，未开启时尝试原有的小米客户端证书引导。客户端需已登录，证书目录默认查找 Windows LocalAppData/minasCert，也可用 `--cert-dir` 指定。多个 NAS 账户时，已有 SSH 路径需用 `--user` 选择。

通过 `--identity` 选择现有密钥。保留私钥，以实际私钥导出的公钥为准；只有私钥和公钥都不存在时才生成 Ed25519 密钥。不输出或上传登录私钥，不修改 root 密码或 RPMB。HTTP 成功或 mi-shell 的退出码 0 都不会被当成 root 命令成功。

## 当前固件的 Portainer 路径

当前测试固件会强制进入 mi-shell，且未通过旧证书引导探测。因此新脚本同时提供已有 SSH、旧证书引导、Portainer 三条路径；不会把修复 mi-shell 的代码误当成获得 root 执行权限的入口。

```bash
bash xiaomi-storage-open-ssh-wsl.sh 192.168.31.100 --portainer \
  --portainer-url http://192.168.31.100:9001 --portainer-user admin \
  --endpoint 3 --container test --user u123456789
```

Portainer 密码交互输入，不作为命令行参数或写入文件。使用现有管理员权限，不新增服务或凭据；HTTP 仅用于可信局域网，已有 HTTPS 时使用对应 URL，不跳过证书验证。

目标容器须运行，并把宿主机 `/` 以可写 bind 方式映射到某个容器目录。脚本通过 Docker Inspect 确认映射并使用其实际位置，不假定 `/nas` 或 `/nas/home`。它先 chroot 准备文件，由宿主机一次性 Cron 激活 SSH；取得真实 SSH root 命令后安装开机恢复。不会创建特权容器或新镜像。缺少 Cron 时，排队状态不能当成成功。

未来固件若移除所有管理入口或必需组件，无法保证恢复，应提前保留至少一个管理入口。

## 更新开机修复

```bash
bash install-ssh-autostart-fix.sh 192.168.31.100
```

已有正常 root 命令行后运行。单文件内置 `recover-ssh.py` 和 `restore-ssh.sh`，保留原有等待真实开机检查与有限复查：最多等待 300 秒，每隔 5 秒复查 12 次，结束后退出。还会保存现有登录授权副本，仅在文件缺失时补回，并处理 mi-shell 固件。现有授权文件不会被旧副本覆盖。

支持旧 `/root` 与新 `/home/rootx`。迁移时保留原有公钥、密码和其他账户；Bash 仅在 Dropbear 服务专用挂载空间中映射到 mi-shell，不替换原厂二进制、不重启设备。

```bash
ssh root@192.168.31.100 id
ssh root@192.168.31.100 '/bin/sh /data/ssh-autostart-fix/restore-ssh.sh --check'
ssh root@192.168.31.100 'journalctl -b -t ssh-autostart-fix --no-pager'
```

修改或撤销公钥后，重新安装开机修复可同步副本。删除 `/data/ssh-autostart-fix/login-authorized-keys` 可停止缺失授权文件恢复。固件可能删除 systemd/Cron 配置，有限复查不意味着系统配置永久保留。

## 离线工具和只读检查

提前从 [SSH 恢复插件](https://github.com/daixu0502/xiaomi-minas-plugin-ssh_recovery) 下载应急包，或使用本仓库的 root 工具：

```sh
python3 recover-ssh.py --diagnose
python3 recover-ssh.py --local-recover --user u123456789 --public-key ./my-key.pub --install-boot
```

在已映射宿主机根目录的容器中 chroot 执行同一程序，增加 `--offline`，可以通过宿主机一次性 Cron 激活。最终结果位于 `/data/ssh-autostart-fix/emergency-result.json`；`pending` 尚未完成，`ready` 表示服务已启动，最终仍须验证客户端真实 SSH 命令。

```bash
bash xiaomi-storage-open-ssh-wsl.sh 192.168.31.100 --check
bash xiaomi-storage-open-ssh-wsl.sh 192.168.31.100 --probe-bootstrap
```

证书引导探测只创建并清理临时 UID 标记，不修改 SSH、账户或启动设置。旧认证引导参考 Scruel 的[原项目](https://github.com/scruel/xiaomi-storage-nas-enable-ssh)；新版保留此兼容路径，但不承诺已修复该入口的新固件仍能使用它。

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
# 仅重新生成明文 SSH 开启入口，不同步或改动其他修复程序：
python3 build_release.py --ssh-entrypoint-only
python3 -B -m unittest discover -s tests -p 'test_*.py'
```

单文件脚本由旁边的已审阅源码生成。测试核对内嵌程序逐字节一致，并覆盖假成功、超时、Portainer 挂载与 Docker 流协议。可通过仓库页面的 Code → Download ZIP 获取完整源码。

`docker_bridge_repair.py` 是网络修复源码，`fix-docker-bridge.sh` 是生成的单文件入口，回归测试在 `tests/test_docker_bridge.py`。测试使用隔离文件和模拟命令，不修改本机或 NAS 网络。

## 获取源码

在 GitHub 仓库页面选择 **Code → Download ZIP**，解压后保留项目目录结构，按本页说明运行对应脚本。也可从 **Tags** 页面选择指定版本源码。
