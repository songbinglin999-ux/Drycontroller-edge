# Edge OS Installer USB

## 目标

把“安装 Ubuntu + Docker + edge repo + systemd bootstrap service”变成标准安装盘流程。现场人员不需要手动安装 Ubuntu、填写租户参数、执行 Docker 命令或复制 license 文件。

## 两种 U 盘

| U 盘 | 用途 | 现场人员是否填写参数 |
|---|---|---|
| **OS Installer USB（系统安装 U 盘）** | 把空白 IPC 安装成标准 DryController Edge Host。 | 否 |
| **Provisioning USB（部署/激活 U 盘）** | 首次开机激活设备身份、Headscale、Harbor、license。 | 否 |

OS Installer USB 只负责基础 OS。设备号、节点名、API key、license、tailnet 和 registry 凭据都由 Provisioning USB 和 cloud-ops 分配。

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

输出目录：

```text
dist/autoinstall/
  user-data
  meta-data
  post-install.sh
  drycontroller-edge-post-install.service
  wifi.env.example
  drycontroller-edge.tar.gz
```

### 3. 设置安装用户密码 hash

`dist/autoinstall/user-data` 里有占位：

```text
$6$REPLACE_WITH_SHA512_CRYPT_HASH
```

生成 hash：

```bash
openssl passwd -6
```

把生成值替换到 `identity.password`。测试阶段可以使用统一临时密码；生产阶段必须走密码管理器或后续改成无密码 SSH key 模式。

### 4. 放入 Ubuntu 安装盘根目录

把这些文件放到 Ubuntu Server 安装介质根目录：

```text
user-data
meta-data
post-install.sh
drycontroller-edge-post-install.service
drycontroller-edge.tar.gz
```

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
- 安装或校验 Docker / Docker Compose / Python / curl
- 配置 Docker 日志轮转
- 复制 edge repo 到 `/opt/Drycontroller-edge`
- 创建 `/opt/Drycontroller-edge/.env`
- 安装并启用 `drycontroller-bootstrap.service`
- 写入 `/var/lib/drycontroller/host-install.json`
- 如果 Provisioning USB 已经插入，会立即启动 `drycontroller-bootstrap.service`
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

1. 插入 Provisioning USB。
2. 开机。
3. 首次进入系统后，机器会先自动连接 WiFi，再完成 Docker / edge host 安装，然后自动启动 bootstrap。
4. 等待 edge 自动 bootstrap。
5. 在云端 console 查看节点出现，并显示 reachable。

成功标准：

- cloud-console 中出现节点。
- 节点 reachable。
- license verify 成功。
- 出厂 preflight `success=true`。

## 失败排查

| 现象 | 判断 |
|---|---|
| 安装没有自动开始 | 检查启动参数是否包含 `autoinstall ds=nocloud;s=/cdrom/`，以及 U 盘根目录是否有 `user-data` / `meta-data`。 |
| 安装中途失败 | 查看安装器日志，优先确认 ISO 是否能访问 apt 源，或是否需要离线 apt mirror。 |
| 安装完成但没有自动关机 | 查看 `/var/log/installer/`，确认 late-commands 是否执行。 |
| 首次开机后没有继续 host install | 查看 `/var/log/drycontroller-autoinstall.log` 和 `systemctl status drycontroller-edge-post-install.service`。 |
| WiFi 连不上 | 检查安装盘根目录是否有 `wifi.env`，SSID/密码是否正确；必要时设置 `WIFI_INTERFACE`。 |
| 激活阶段找不到 U 盘 | 确认 U 盘里存在 `drycontroller-provisioning/provisioning.json`。 |
| 激活失败 | 查看 `/var/log/drycontroller-bootstrap.log`，同时在 cloud-ops 查看 provisioning batch、license、Headscale 和 Harbor 配置。 |

## 当前限制

- 目前是 Ubuntu Server autoinstall 方案，不覆盖 Debian、Alpine、Yocto。
- `user-data` 仍需要在制作安装盘时替换密码 hash。
- apt 包安装依赖网络或可用 Ubuntu mirror；全离线 apt mirror 尚未打包。
- 第一次真实 Linux edge e2e 前，不建议把该安装盘交给现场批量使用。

## 与 Provisioning USB 的关系

OS Installer USB 不包含业务身份和长期凭据。Provisioning USB 仍按云端分配模式制作，只保留：

```json
{
  "cloud_ops_base_url": "https://ops.example.com",
  "batch_id": "factory-2026-05-a",
  "installer_token": "xxxxx"
}
```

云端批次负责保存 Headscale / Harbor 参数，并为每台 edge 分配设备号、节点名、per-node API key 和签名 license。
