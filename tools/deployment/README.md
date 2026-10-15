# NAS 部署辅助工具

这些目录保存已有的 NAS Docker 部署和网络维护脚本，与 SSH 开启流程独立；SSH 开启脚本不会自动执行它们。

- `bitcomet-docker/`：BitComet 镜像适配、部署和凭据轮换源码。凭据由运行时输入或生成，不保存到仓库。
- `bridge-net-network/`：特定自定义 `bridge-net` 网络的独立出站服务及历史部署方式。

当前通用 Docker bridge 网络修复使用仓库根目录的 `fix-docker-bridge.sh`，具体检查、回退与确认步骤见根目录 README。部署辅助脚本需要 NAS 上已有的 root 执行权限，不用于获取 SSH 权限。
