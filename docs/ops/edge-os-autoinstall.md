# Edge OS Installer USB

## 目标

把“安装 Ubuntu + Docker + edge repo + host tailnet / activation service”变成标准安装盘流程。现场人员不需要手动安装 Ubuntu、填写租户参数、执行 Docker 命令或复制 license 文件。

## 正常交付介质

| 介质 | 用途 | 现场人员是否填写参数 |
|---|---|---|
| **OS Installer USB（系统安装 U 盘）** | 把空白 IPC 安装成标准 DryController Edge Host，并预置 factory license / bootstrap tailnet 配置。 | 否 |

正常路径不再使用 Provisioning USB。设备号、节点名、production license、formal tailnet key 和后续权限由 Cloud Ops 在首次上线并经运维确认后签发。旧 Provisioning USB 仅保留为 legacy/emergency 路径，不能作为新流程验收标准。

> 注意：OS Installer USB 是整机重装介质。安装器会选择最大的非安装介质磁盘，清除旧 Windows / Linux 分区和文件系统元数据，并重建 DryController Edge Host 分区。

## 产物位置

```text
infra/autoinstall/
  user-data
  meta-data
  post-install.sh
  drycontroller-edge-post-install.service
  wifi.env.example

scripts/build-autoinstall-bundle.sh
scripts/build-autoinstall-offline-apt-repo.sh
```

## 内部制作流程

### 1. 准备 Ubuntu Server ISO

建议版本：

- Ubuntu Server 22.04 LTS x86_64
- 后续真实 Linux e2e 稳定后，再评估 24.04 LTS

### macOS 写入 U 盘注意

macOS 用 `dd` 写安装 U 盘时，必须写 raw device，例如 `/dev/rdisk2`，不要写 buffered device `/dev/disk2`。实测 `/dev/disk2` 只有约 148 KB/s，16 GB 级别镜像可能耗时约 5 小时；`/dev/rdisk2` 通常快 10-20 倍。

先确认盘号：

```bash
diskutil list
```

卸载目标 U 盘后写入：

```bash
diskutil unmountDisk /dev/disk2
sudo dd if=ubuntu-server.iso of=/dev/rdisk2 bs=4m status=progress
sync
diskutil eject /dev/disk2
```

注意确认 `/dev/disk2` 是目标 U 盘；写错盘号会直接覆盖本机磁盘数据。

### 2. 生成安装介质附加文件

在 edge 仓库执行：

```bash
bash scripts/build-autoinstall-bundle.sh
```

在一台能访问目标 Ubuntu 包源、且发行版与安装 ISO 匹配的 Ubuntu amd64 构建机上生成离线 apt repo。Ubuntu 22.04 ISO 用 jammy 构建机，Ubuntu 24.04 ISO 用 noble 构建机：

```bash
sudo apt-get update
sudo apt-get install -y dpkg-dev
bash scripts/build-autoinstall-offline-apt-repo.sh --out dist/autoinstall/drycontroller-apt
```

如果默认 Ubuntu 源不稳定，显式指定可达镜像：

```bash
bash scripts/build-autoinstall-offline-apt-repo.sh \
  --mirror https://mirrors.aliyun.com/ubuntu/ \
  --out dist/autoinstall/drycontroller-apt
```

输出目录：

```text
dist/autoinstall/
  user-data
  meta-data
  post-install.sh
  drycontroller-edge-post-install.service
  wifi.env.example
  drycontroller-edge.tar.gz
  drycontroller-apt/
```

`drycontroller-apt/` 是必备项，包含 Docker runtime、Docker Compose v2、NetworkManager 和基础 host package 的离线 apt 包集。安装阶段不覆盖 Subiquity 的 apt 配置，只校验并复制 `drycontroller-apt/`；首次开机 post-install 会从 `/opt/drycontroller-apt` 离线安装 Docker 和现场额外依赖，IPC 运行阶段不应依赖现场能访问 `archive.ubuntu.com` 或 Docker 相关包源。

### 3. 设置 dryops 登录和 SSH 密码

`dist/autoinstall/user-data` 里有占位：

```text
$6$REPLACE_WITH_SHA512_CRYPT_HASH
```

`dryops` 是安装后本机登录用户；因为 installer 配置了 `ssh.allow-pw: true`，同一个密码也用于 SSH 登录：

```bash
ssh dryops@<IPC-IP>
```

推荐使用本地不入库的 `password.env` 输入明文密码。这个文件只在制作介质的机器上存在，不复制到安装盘，也不会进入 `drycontroller-edge.tar.gz`：

```bash
cp infra/autoinstall/password.env.example infra/autoinstall/password.env
chmod 0600 infra/autoinstall/password.env
$EDITOR infra/autoinstall/password.env
bash scripts/build-autoinstall-bundle.sh \
  --password-env-file infra/autoinstall/password.env
```

`password.env` 内容：

```text
DRYOPS_PASSWORD=replace-with-strong-password
```

脚本会在本机把明文转成 sha512-crypt hash，再注入 `dist/autoinstall/user-data`；最终安装介质里只有 hash，没有明文密码。不要把 `password.env` 提交到 git，也不要把它放到安装盘根目录。

如果已经有 hash，也可以继续用 hash 文件方式：

```bash
openssl passwd -6 > /tmp/drycontroller-autoinstall.passhash
bash scripts/build-autoinstall-bundle.sh \
  --password-hash-file /tmp/drycontroller-autoinstall.passhash
```

写 U 盘或重打 ISO 前必须确认 artifact 里已经不是占位符：

```bash
rg -n 'REPLACE_WITH_SHA512_CRYPT_HASH' dist/autoinstall/user-data && exit 1
```

测试阶段可以使用统一临时密码；生产阶段必须走密码管理器或后续改成无密码 SSH key 模式。

### 4. 放入 Ubuntu 安装盘根目录

把这些文件放到 Ubuntu Server 安装介质根目录：

```text
user-data
meta-data
post-install.sh
drycontroller-edge-post-install.service
drycontroller-edge.tar.gz
drycontroller-apt/
bundle-images.tar   # optional, first-boot Docker images for clean-room install
```

真机 / 生产安装还需要放入同一介质根目录：

```text
tailnet.env
factory-license.json
```

这两个文件是 factory activation 的**批次材料**，不是基础 OS bundle 的一部分。
bootstrap tailnet key 或 factory license 轮换时，只替换这两个文件；不需要重新
生成 edge source bundle、离线 apt repo 或业务镜像包。如果最终交付的是只读 ISO，
仍需要 remaster 一次把新批次材料映射进 ISO 根目录；这只是重打最终介质，不是重新
设计或重建 OS 安装内容。

如果安装车间只有 WiFi，再额外放：

```text
wifi.env
```

`wifi.env` 从 `wifi.env.example` 复制并填写：

```bash
WIFI_SSID=Factory-WiFi
WIFI_PASSWORD=replace-with-password
```

如自动选择无线网卡失败，可追加：

```bash
WIFI_INTERFACE=wlan0
```

`bundle-images.tar` 来自 `dist/bundle-images.tar`，用于首版 clean-room 装机。包含它时，安装器会复制到 `/opt/drycontroller-images/bundle-images.tar`，首次开机在 Docker 安装完成后自动 `docker load`，避免人工 `scp` 镜像包。

### 5. 配置 autoinstall 启动参数

Ubuntu Server installer 的 kernel command line 需要包含：

```text
autoinstall ds=nocloud;s=/cdrom/
```

第一次验证时，可以在启动 GRUB 菜单临时编辑一次。流程确认后，再把这个参数固化到最终安装盘。

## 安装盘会做什么

Ubuntu autoinstall 完成系统安装后，会在目标系统里安装 first-boot service：

```text
drycontroller-edge-post-install.service
```

第一次真实启动进入已安装系统后，该 service 会运行：

```text
/usr/local/sbin/drycontroller-edge-post-install.sh
```

该脚本会：

- 如果 `/opt/drycontroller-wifi.env` 存在，安装/启用 NetworkManager 并自动连接 WiFi
- 解压 `drycontroller-edge.tar.gz`
- 调用 `infra/bootstrap/install-edge-host.sh`
- 从 `/opt/drycontroller-apt` 离线安装或校验 Docker / Docker Compose / Python / curl
- 如果存在 `/opt/drycontroller-images/bundle-images.tar`，自动导入首版业务 Docker 镜像并删除 tar
- 配置 Docker 日志轮转
- 复制 edge repo 到 `/opt/Drycontroller-edge`
- 创建 `/opt/Drycontroller-edge/.env`
- 安装并启用 `drycontroller-tailnet.service`
- 安装并启用 `drycontroller-activation.service`
- 默认不启用旧 `drycontroller-bootstrap.service`
- 写入 `/var/lib/drycontroller/host-install.json`
- host install 成功后禁用自身，避免后续重复执行

## 磁盘分区策略

安装盘使用显式 destructive storage 配置，不依赖 Ubuntu 安装器的默认 direct layout：

- 选择最大的非安装介质磁盘作为目标系统盘。
- 对目标磁盘执行 `wipe: superblock-recursive`，清除旧 Windows / Linux 分区表、LVM、RAID、bcache 等残留元数据。
- 使用 GPT 分区表。
- 创建 1 MiB `bios_grub` 分区，兼容 legacy BIOS 启动。
- 创建 1 GiB FAT32 EFI System Partition，挂载到 `/boot/efi`。
- 剩余空间格式化为 ext4，挂载到 `/`。
- 禁用 swap 分区。

如果 IPC 有多块内部磁盘，量产前必须确认“最大磁盘”就是系统盘；否则需要把 `infra/autoinstall/user-data` 里的 `match` 改成具体 `serial` / `model` / `path` 匹配。

## 现场人员流程

### 阶段 1：安装系统

1. 插入 OS Installer USB。
2. 开机，选择从 U 盘启动。
3. 选择 DryController Edge 自动安装项。
4. 等机器自动安装并关机。
5. 拔出 OS Installer USB。

成功标准：机器自动关机。现场人员不需要登录系统。

### 阶段 2：激活设备

1. 开机，确保有到 Headscale login endpoint 的出站网络。
2. 首次进入系统后，机器会先自动连接 WiFi（如配置），再完成 Docker / edge host 安装。
3. host Tailscale 使用 factory bootstrap credential 加入受限 bootstrap tailnet。
4. activation service 调 Cloud Ops，云端记录为 `pending_ops_approval`。
5. 运维在 cloud-console 确认真实设备后点击「签发正式授权」。
6. edge 下一次 activation 重试领取 production license 和 formal tailnet key，完成 formal re-auth。
7. 在云端 console 查看节点显示 reachable / activated。

成功标准：

- cloud-console 中出现节点。
- 节点 reachable。
- production license verify 成功。
- 出厂 preflight `success=true`。

## 失败排查

| 现象 | 判断 |
|---|---|
| 安装没有自动开始 | 检查启动参数是否包含 `autoinstall ds=nocloud;s=/cdrom/`，以及 U 盘根目录是否有 `user-data` / `meta-data`。 |
| `configure_apt` 阶段报 `curtin ... apt-config` 失败 | 不要在 `user-data` 里自定义 `apt.primary` / `apt.sources` 指向 `file:/cdrom`；安装阶段让 Subiquity 使用默认 apt 配置，Docker/runtime 包放到首次开机从 `/opt/drycontroller-apt` 离线安装。 |
| 安装早期提示缺少 `/cdrom/drycontroller-apt/Packages` | 重新运行 `scripts/build-autoinstall-offline-apt-repo.sh`，并把生成的 `drycontroller-apt/` 目录复制到安装盘根目录。 |
| 安装中途 `install_docker.io` 失败 | 查看 `/var/log/installer/curtin-install.log`，优先确认 `drycontroller-apt/` 与 ISO 版本/架构匹配，且包含 `docker.io` 及依赖。 |
| 安装完成但没有自动关机 | 查看 `/var/log/installer/`，确认 late-commands 是否执行。 |
| 首次开机后没有继续 host install | 查看 `/var/log/drycontroller-autoinstall.log` 和 `systemctl status drycontroller-edge-post-install.service`。 |
| WiFi 连不上 | 检查安装盘根目录是否有 `wifi.env`，SSID/密码是否正确；必要时设置 `WIFI_INTERFACE`。 |
| 激活阶段一直停在待确认 | 在 cloud-console Factory Activations 页面确认真实设备后点击「签发正式授权」。 |
| 激活失败 | 查看 `/var/log/drycontroller-host-tailnet.log`、`/var/log/drycontroller-host-activation.log` 和 `systemctl status drycontroller-tailnet.service drycontroller-activation.service`，同时在 Cloud Ops 查看 factory activation、license 和 Headscale 配置。 |

## 当前限制

- 目前是 Ubuntu Server autoinstall 方案，不覆盖 Debian、Alpine、Yocto。
- `user-data` 仍需要在制作安装盘时替换密码 hash。
- production media 必须预置真实 factory license、factory public trust anchor、production license public trust anchor 和 `/etc/drycontroller/tailnet.env`。
- 当前 OS Maker 会把 `dist/autoinstall/tailnet.env` 与 `dist/autoinstall/factory-license.json` 映射到 ISO 根目录；如果取消包含 factory activation 材料，它会移除旧材料，避免误用上一个批次的 token/license。
- staging UI approve -> edge retry -> formal tailnet confirm 闭环跑通前，不建议把该安装盘交给现场批量使用。

## 与 Provisioning USB 的关系

Provisioning USB 已从正常路径移除。旧 `drycontroller-provisioning/provisioning.json`
流程只作为 legacy/emergency bootstrap 保留，需要显式启用
`--enable-legacy-bootstrap` 或手动启动 `drycontroller-bootstrap.service`。

新路径里 OS Installer USB / factory image 预置 factory license 和 bootstrap
tailnet credential。Cloud Ops 在设备首次上线并经运维确认后分配设备号、
节点名、production license、formal tailnet key 和后续权限。
