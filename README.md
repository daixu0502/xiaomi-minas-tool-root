# 小米智能存储 SSH 开机恢复

先使用开启 SSH 的脚本，确认可以通过 root 密钥登录设备，再在 WSL 中运行：

```bash
bash install-ssh-autostart-fix.sh <NAS的IPv4地址>
```

安装脚本是单文件，不需要额外携带 `keep-ssh`。它不会重启设备，不修改 RPMB，也不更改 root 密码。

## 开机恢复方式

- Cron 在开机时运行 `/data/ssh-autostart-fix/restore-ssh.sh`。
- 读取 `minas.boot_check.service` 的状态和本次启动的执行时间戳，确认小米开机检查已经结束、没有待执行任务后，再启动 `dropbear.socket`；不再依赖固定 30 秒延迟。
- 等待窗口为 300 秒；每次状态查询另有 8 秒超时限制。超时未完成检查会记录错误并退出，不强行提前启动 SSH。
- 首次恢复后，每 5 秒复查一次，共 12 次。如果检查再次运行，会继续等待其结束；如果 SSH 被关闭，会尝试恢复。单次启动请求最多等待 10 秒。
- 复查结束后退出，不是永久守护程序。并发执行会通过锁避免重复运行。
- 保留 `/etc/systemd/system/minas.boot_check.service.d/90-keep-ssh.conf` 作为辅助恢复。部分固件启动早期尚未挂载 `/etc`，因此不单独依赖该配置。

安装时会启动一次同样的有限复查，但这不等于完成了真实重启验证。

## 查看结果

在 NAS 的 SSH 终端运行：

```bash
systemctl is-active dropbear.socket
/bin/sh /data/ssh-autostart-fix/restore-ssh.sh --check
journalctl -b -t ssh-autostart-fix --no-pager
```

日志中的 `SSH recovery verified; bounded rechecks finished` 表示此次复查完成且 SSH socket 正常。

如果下次启动后依然无法连接，请先用原有开启 SSH 的脚本恢复访问，再查看上述日志。固件更新可能改变服务名称、覆盖配置或改变启动流程，届时需要重新核对兼容性。
