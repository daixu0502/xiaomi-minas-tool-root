# 小米智能存储 SSH 开启与开机恢复

版本 **2.0.0**。[仓库](https://github.com/daixu0502/xiaomi-minas-tool-root)。两个 shell 入口仍可单文件运行，内置修复程序与 SSH 恢复插件 1.1.0 保持一致。

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

## 构建与测试

```bash
python3 build_release.py
python3 -B -m unittest discover -s tests -p 'test_*.py'
```

单文件脚本由旁边的已审阅源码生成。测试核对内嵌程序逐字节一致，并覆盖假成功、超时、Portainer 挂载与 Docker 流协议。发布包及 SHA256SUMS 位于 `releases/`。
