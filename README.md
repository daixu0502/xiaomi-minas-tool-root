# 小米 NAS 管理工具

版本 **2.1.1**。各工具的使用入口放在根目录；源码、构建文件、测试、依赖和详细说明按工具放在 `tools/`。

## 使用入口

| 根目录脚本 | 用途 | 详细说明 |
|---|---|---|
| [xiaomi-storage-open-ssh-wsl.sh](xiaomi-storage-open-ssh-wsl.sh) | WSL 开启/修复 SSH，默认安装开机恢复 | [SSH 工具](tools/ssh/README.md) |
| [install-ssh-autostart-fix.sh](install-ssh-autostart-fix.sh) | 已有正常 SSH 时安装/更新相同开机恢复 | [SSH 工具](tools/ssh/README.md) |
| [fix-docker-bridge.sh](fix-docker-bridge.sh) | NAS Docker bridge 网络检查、修复与回退 | [Docker bridge 工具](tools/docker-bridge/README.md) |

```bash
# 在 WSL 中开启/修复 SSH，自动安装开机恢复：
bash xiaomi-storage-open-ssh-wsl.sh 192.168.31.100 --user u123456789
# 只安装或更新开机恢复：
bash install-ssh-autostart-fix.sh 192.168.31.100
# 在 NAS 的真实 root 命令行中只读检查 Docker 网络：
sh fix-docker-bridge.sh --check
```

SSH 流程为纯 Shell，兼容旧 root 目录与新版 mi-shell。多账户可交互选择或用 `--user` 指定。旧证书入口若被固件封堵，仍需保留 SSH 恢复插件或 Portainer 入口。安装开机修复不主动重启设备。

SSH 开启脚本、安装脚本与 SSH 恢复插件使用同一份 worker，构建和测试核对源码一致。

## 目录

```text
nas-root/
├── xiaomi-storage-open-ssh-wsl.sh
├── install-ssh-autostart-fix.sh
├── fix-docker-bridge.sh
├── README.md
├── tools/
│   ├── ssh/                 # 两个 SSH 入口共用源码、构建、测试
│   │   ├── src/
│   │   ├── tests/
│   │   ├── build.py
│   │   ├── README.md
│   │   ├── .tools/          # 本地测试依赖，不提交
│   │   └── offline/         # 本地应急导出，不提交
│   ├── docker-bridge/       # 网络修复源码、构建、测试与说明
│   ├── bitcomet-docker/     # 独立 Docker 部署工具
│   └── bridge-net-network/  # 原自定义网络辅助工具
├── docs/firmware-reference/ # 原厂实现参考
└── dist/                   # 当前项目安装包
```

`tools/ssh/src/restore-ssh.sh` 是共享内部 worker，运行副本由安装器放到 NAS `/data/ssh-autostart-fix/restore-ssh.sh`。根目录只放三个面向使用者的 Shell 入口；`.git/`、`.gitignore` 为必要的 Git 管理文件。

## 按工具构建与测试

```bash
python3 tools/ssh/build.py
python3 -B -m unittest discover -s tools/ssh/tests -p 'test_*.py'
python3 tools/docker-bridge/build.py
python3 -B -m unittest discover -s tools/docker-bridge/tests -p 'test_*.py'
```

构建和测试在开发电脑执行；Docker bridge 工具运行需要 NAS 原有 Python，SSH 开启及开机恢复均不依赖 Python。各工具的配置、写入位置、检查和回退步骤见对应 README。

## 其他说明

- [BitComet 部署](tools/bitcomet-docker/README.md)
- [原 bridge-net 辅助服务](tools/bridge-net-network/README.md)
- [原厂插件管理实现参考](docs/firmware-reference/README.md)
- [GitHub 仓库](https://github.com/daixu0502/xiaomi-minas-tool-root)，可通过 Code → Download ZIP 下载完整源码。

`offline/` 应急材料及 `.tools/` 本地依赖保持本地保存，安装包在 `dist/`，不创建 `releases/`。
