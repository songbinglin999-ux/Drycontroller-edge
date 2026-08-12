# Edge OS Installer USB

## 目标

把“安装 Ubuntu + Docker + edge repo + host tailnet / activation service”变成标准安装盘流程。现场人员不需要手动安装 Ubuntu、填写租户参数、执行 Docker 命令或复制 license 文件。

## 正常交付介质

| 介质 | 用途 | 现场人员是否填写参数 |
|---|---|---|
| **OS Installer USB（系统安装 U 盘）** | 把空白 IPC 安装成标准 DryController Edge Host，并预置 factory license / bootstrap tailnet 配置。 | 否 |

正常路径不再使用 Provisioning USB。设备号、节点名、production license、node enroll token 和后续权限由 Cloud Ops 在首次有效 factory activation contact 后自动签发；当前已在线的 tailnet 节点会被绑定为正式节点。历史 pending 或异常记录才需要人工兜底。旧 Provisioning USB active code 已删除，不能作为新流程验收标准。

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

当前支持版本：

- Ubuntu Server 24.04 LTS x86_64（noble）

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

在一台能访问目标 Ubuntu 包源、且发行版与安装 ISO 匹配的 Ubuntu amd64 构建机上生成离线 apt repo。当前发布介质是 Ubuntu 24.04 LTS，离线 apt repo 必须使用 noble 包集：

```bash
sudo apt-get update
sudo apt-get install -y dpkg-dev
bash scripts/build-autoinstall-offline-apt-repo.sh \
  --suite noble \
  --arch amd64 \
  --out dist/autoinstall/drycontroller-apt
```

如果默认 Ubuntu 源不稳定，显式指定可达镜像：

```bash
bash scripts/build-autoinstall-offline-apt-repo.sh \
  --suite noble \
  --arch amd64 \
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

`drycontroller-apt/` 是必备项，包含 Docker runtime、Docker Compose v2、NetworkManager 和基础 host package 的离线 apt 包集。安装阶段不依赖公网 Ubuntu mirror：`early-commands` 会从 `/cdrom` 启动 `127.0.0.1:18080` 只读 HTTP repo，target apt 通过该本地 repo 补装 `linux-generic` / grub 等 installer 包，避免现场 DNS 或外网 mirror 在 `install-kernel` / `curthooks` 阶段拖死。首次开机 post-install 会从 `/opt/drycontroller-apt` 离线安装 Docker 和现场额外依赖，IPC 运行阶段不应依赖现场能访问 `archive.ubuntu.com` 或 Docker 相关包源。

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
activation-bundle.json
```

这个文件是 factory activation 的**批次材料**，不是基础 OS bundle 的一部分。
bootstrap tailnet key 或 factory license 轮换时，只替换这个文件；不需要重新
生成 edge source bundle、离线 apt repo 或业务镜像包。如果最终交付的是只读 ISO，
仍需要 remaster 一次把新批次材料映射进 ISO 根目录；这只是重打最终介质，不是重新
设计或重建 OS 安装内容。

旧 `tailnet.env` + `factory-license.json` 不再作为安装介质或 host service 输入。
host services 会从 `activation-bundle.json` 派生运行时 `tailnet.env`。

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

## 唯一发布入口

发布级 OS Installer ISO 必须走 release wrapper：

```bash
bash scripts/build-os-installer-release.sh --channel stable
```

该脚本会强制 clean git、重建 Docker 镜像包、验证完整签名 SoftwareRelease 并把精确 digest 镜像集合重打包、重建 offline apt、重建 autoinstall overlay、remaster ISO、运行最终 ISO release checklist，并在 ISO 旁生成 `.manifest.json`。不要手工调用 `xorriso`、不要只跑 `scripts/build-autoinstall-bundle.sh` 就当成发布镜像。

制盘前要在 `dist/initial-release/` 放入三个受控输入：

- `software-release.json`：Cloud Ops 中一个 `completed` SoftwareRelease 的完整 GET 响应，其 `source_commit` 必须精确等于当前 Edge commit。
- `cosign.pub`：与 SoftwareRelease `signing_key_sha256` 匹配的发布公钥。
- `cosign-linux-amd64`：受控发布工具链中的 Linux amd64 cosign 可执行文件。

制作机还必须安装可信的 host `cosign`、登录 manifest 指定的 Harbor。wrapper 会先用 host cosign 验 manifest 和 12 个镜像签名，再把每个 registry digest 对应的 Docker image ID 一并写入签名 manifest，然后拉取、核对并打包精确镜像集合；安装时目标 IPC 使用签名 image ID 证明 12 个本地镜像，原子生成 `active` journal，并只以这些 image ID 执行 `docker compose up --pull never --no-build`。任一步不确定都停止安装，不启动业务容器或 OTA helper。

默认发布镜像不允许隐式带入 `dist/autoinstall/wifi.env`。如果某批次确实要把现场 WiFi 凭据嵌进 ISO，命令必须显式追加 `--include-wifi`，manifest 会记录 `wifi_env` 的 sha256；否则发现该文件就直接失败。

写 U 盘也必须走 manifest-aware wrapper：

```bash
diskutil list external physical
bash scripts/write-os-installer-usb.sh dist/OSYYYYMMDD-stable.iso --disk diskN
```

写盘脚本会重新校验 manifest、ISO sha256、最终 ISO checklist 和外接盘属性；没有匹配 manifest 的 ISO 不能写盘。

## 制盘发布清单

每次发布 `OSYYYYMMDD-<channel>.iso` 前，release wrapper 会自动覆盖这张清单；缺一项就不会产出可写盘 manifest。

### 已踩坑记录

- `install-kernel` / `curtin stage-curthooks` 卡住不一定是内核包坏了；本次根因是 target apt 仍指向 `mirrors.aliyun.com` / `security.ubuntu.com`，现场 DNS 失败后 `apt-get --download-only install linux-generic` 退出 100。最终策略是 installer 阶段只走 ISO-local HTTP repo：`http://127.0.0.1:18080`。
- 只刷新 `drycontroller-edge.tar.gz` 不够。clean-room 装机还会加载 `bundle-images.tar`，如果没重建 Docker 镜像，启动后的容器代码仍可能是旧版本。
- `activation-bundle.json` 是唯一产品激活材料。不要让旧的 split `tailnet.env` / `factory-license.json` 混进最终 ISO，否则后续很难判断现场到底跑的是哪条激活路径。
- 首次激活只保留宿主 systemd 路径：`drycontroller-tailnet.service` -> `drycontroller-activation.service`。`edge-api` 只读状态，不再触发 factory activation 或写入 license/device 状态。
- `preflight` 不能假设 edge-api 一定监听 `127.0.0.1`；首次激活后 `.env` 会同步 tailnet 绑定地址，检查 `/ready` 要使用当前 `EDGE_API_BIND_ADDR` 或显式 `PREFLIGHT_EDGE_API_HOST`。
- 现场没有 PLC 或输入数据时，`edge-daq` / `ai-softsensor` 可以因为依赖缺失进入 degraded；制盘验收要区分“系统/激活路径失败”和“现场外设未接入”。
- 任何 USB 写入前都重新 `diskutil list external physical`，不要沿用上一次的 `/dev/diskN`；写入完成必须 `sync` 后 eject。

### 构建输入

- `dist/drycontroller-os-installer-hybrid.iso` 是当前已知可启动 base ISO。
- `dist/autoinstall/user-data` 能被 YAML 解析，`dryops` 密码是真实 `$6$...` hash，且不含 `REPLACE_WITH_SHA512_CRYPT_HASH`。
- `dist/autoinstall/user-data` 的 installer apt 源只指向 `http://127.0.0.1:18080`，包含 `python3 -m http.server 18080` early-command，且不含 `mirrors.aliyun.com` / `security.ubuntu.com` / `archive.ubuntu.com` 等现场外网源。
- `dist/autoinstall/drycontroller-apt/Packages` 和 `Packages.gz` 存在，`pool/` 内有 Docker、Docker Compose、Tailscale、NetworkManager 及基础依赖包。
- `dist/drycontroller-edge.tar.gz` 来自当前 edge worktree；clean-room 装机要同时刷新 `dist/bundle-images.tar`。
- `dist/initial-release/software-release.json`、`cosign.pub`、`cosign-linux-amd64` 存在；SoftwareRelease 为 completed、完整 12 服务、当前 source commit，且公钥摘要和 manifest 一致。
- `dist/autoinstall/activation-bundle.json` 存在，schema 是 `drycontroller.activation-bundle.v1`，内含 `tailnet` 与 `factory_license`。
- `dist/autoinstall/` 不保留旧 split 材料作为产品路径：没有 `tailnet.env` / `factory-license.json` 参与最终 ISO。
- `dist/autoinstall/wifi.env` 必须缺省不存在；仅现场明确需要 WiFi 安装时使用 `--include-wifi`，并在 manifest 中留下 `wifi_env` 记录。

### ISO 内容

生成最终 ISO 后，至少确认：

```bash
ISO=dist/OSYYYYMMDD-<channel>.iso
shasum -a 256 "$ISO"
bsdtar -xOf "$ISO" user-data > /tmp/drycontroller-user-data.verify
ruby -ryaml -e 'YAML.load_file(ARGV[0]); puts "yaml ok"' /tmp/drycontroller-user-data.verify
rg -n 'password:|REPLACE_WITH_SHA512_CRYPT_HASH|127\\.0\\.0\\.1:18080|http.server 18080|sources_list|disable_suites|network:|dhcp4|optional:' /tmp/drycontroller-user-data.verify
! rg -n 'REPLACE_WITH_SHA512_CRYPT_HASH' /tmp/drycontroller-user-data.verify
! rg -n 'mirrors\\.aliyun\\.com|mirrors\\.tuna\\.tsinghua\\.edu\\.cn|archive\\.ubuntu\\.com|security\\.ubuntu\\.com' /tmp/drycontroller-user-data.verify
bsdtar -tf "$ISO" | rg '^(user-data|meta-data|post-install.sh|drycontroller-edge-post-install.service|drycontroller-edge.tar.gz|drycontroller-apt/Packages|drycontroller-apt/Packages.gz|activation-bundle.json|bundle-images.tar|software-release-seed.json|cosign.pub|cosign-linux-amd64)$'
bsdtar -tf "$ISO" | rg '^(tailnet.env|factory-license.json)$' && exit 1 || true
bsdtar -xOf "$ISO" dists/noble/main/binary-amd64/Packages.gz | gzip -dc | rg '^Package: (linux-generic|linux-image-generic|grub-pc|grub-efi-amd64)'
bsdtar -xOf "$ISO" boot/grub/grub.cfg | rg 'DryController Edge Autoinstall|autoinstall ds=nocloud\\;s=/cdrom/'
hdiutil imageinfo "$ISO" | sed -n '1,130p'
```

`bundle-images.tar`、`software-release-seed.json`、`cosign.pub` 和 `cosign-linux-amd64` 是一组不可拆分的首版发布材料，产品安装盘必须全部存在。`hdiutil imageinfo` 在 macOS 上偶尔会返回内部错误码但打印分区信息，仍需看到 GPT/ISO9660/ESP 结构；否则用另一种工具复核 hybrid 启动结构。

### 写盘和现场验收

- 写盘只能使用 `scripts/write-os-installer-usb.sh`；写盘前重新 `diskutil list external physical`，脚本会拒绝不在 external physical 列表里的 disk、虚拟盘和 internal disk，最终写入使用 `/dev/rdiskN`。
- `dd` 完成后必须 `sync`，再 `diskutil eject /dev/diskN`。
- 真机安装必须越过 `configure_apt`、`install kernel`、`curtin stage-curthooks` 并自动关机；只完成软件侧 ISO 验证不能关闭验收。
- 首次从内置盘启动后，必须验证 `activation-bundle.json -> auto-approve -> bind current tailnet node -> production license -> preflight` 全链路。

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
4. activation service 调 Cloud Ops；有效、未过期、签名正确且 fingerprint 匹配的 `activation-bundle.json` 会自动签发。
5. edge 在同一次 activation 响应中领取 production license、device_id、node_name、node enroll token 和 edge ops API key，并把当前 tailnet 节点绑定为正式节点。
6. 在云端 console 查看节点显示 reachable / activated。历史 pending 或异常记录才需要人工兜底处理。

成功标准：

- cloud-console 中出现节点。
- 节点 reachable。
- production license verify 成功。
- 出厂 preflight `success=true`。

## 失败排查

| 现象 | 判断 |
|---|---|
| 安装没有自动开始 | 检查启动参数是否包含 `autoinstall ds=nocloud;s=/cdrom/`，以及 U 盘根目录是否有 `user-data` / `meta-data`。 |
| `configure_apt` 阶段报 `curtin ... apt-config` 失败 | 确认 `/var/log/installer/curtin-install.log` 中 target apt source 是 `http://127.0.0.1:18080`，且 early-command 已启动 `/cdrom` HTTP repo。不要让 installer 阶段回退到公网 mirror 或直接写错成 `file://cdrom`。 |
| `install-kernel` / `stage-curthooks` 阶段补装 `linux-generic` / `grub-pc` 后失败或长时间卡住 | 先查 `/var/log/installer/curtin-install.log`；如果出现 `Temporary failure resolving mirrors.*` / `security.ubuntu.com`，说明镜像仍依赖外网源，必须重打。确认 `user-data` 只使用 `127.0.0.1:18080` ISO-local repo，同时确认 ISO 内 `dists/noble/*/Packages.gz` 和 `pool/main/g/grub2/` / `pool/main/l/linux*` 仍存在。 |
| 安装早期提示缺少 `/cdrom/drycontroller-apt/Packages` | 重新运行 `scripts/build-autoinstall-offline-apt-repo.sh`，并把生成的 `drycontroller-apt/` 目录复制到安装盘根目录。 |
| 安装中途 `install_docker.io` 失败 | 查看 `/var/log/installer/curtin-install.log`，优先确认 `drycontroller-apt/` 与 ISO 版本/架构匹配，且包含 `docker.io` 及依赖。 |
| 安装完成但没有自动关机 | 查看 `/var/log/installer/`，确认 late-commands 是否执行。 |
| 首次开机后没有继续 host install | 查看 `/var/log/drycontroller-autoinstall.log` 和 `systemctl status drycontroller-edge-post-install.service`。 |
| 首版发布初始化失败 | 检查 ISO 是否同时包含四个首版材料，查看 `/var/log/drycontroller-autoinstall.log`；不要手工补 journal。重新制盘前确认 SoftwareRelease source commit、公钥摘要、manifest 签名和 12 个镜像 digest 都通过 wrapper。 |
| WiFi 连不上 | 检查安装盘根目录是否有 `wifi.env`，SSID/密码是否正确；必要时设置 `WIFI_INTERFACE`。 |
| 激活阶段一直停在待确认 | 新装机主线不应卡在人工待确认；检查 `activation-bundle.json` 是否过期、签名/fingerprint 是否匹配、请求来源是否符合 Cloud Ops 策略。历史 pending 或异常记录才在 cloud-console Factory Activations 页面人工兜底处理。 |
| 激活失败 | 查看 `/var/log/drycontroller-host-tailnet.log`、`/var/log/drycontroller-host-activation.log` 和 `systemctl status drycontroller-tailnet.service drycontroller-activation.service`，同时在 Cloud Ops 查看 factory activation、license 和 Headscale 配置。 |

## 当前限制

- 目前是 Ubuntu Server autoinstall 方案，不覆盖 Debian、Alpine、Yocto。
- `user-data` 仍需要在制作安装盘时替换密码 hash。
- production media 必须预置真实 activation bundle、factory public trust anchor、production license public trust anchor。
- 当前 OS Maker 只接受 `dist/autoinstall/activation-bundle.json` 作为真机安装材料；缺少或格式错误时会阻塞制作流程，并会清理旧 split 材料避免误用上一个批次的 token/license。
- auto-approve -> edge bind current tailnet node 闭环跑通前，不建议把该安装盘交给现场批量使用。

## 与 Provisioning USB 的关系

Provisioning USB 已从正常路径移除。旧
`drycontroller-provisioning/provisioning.json` bootstrap 脚本、systemd unit 和
`install-edge-host.sh` 启用入口已经删除。

新路径里 OS Installer USB / factory image 预置 `activation-bundle.json`
（factory license + bootstrap tailnet credential）。Cloud Ops 在设备首次有效
factory activation contact 后自动分配设备号、节点名、production license、node
enroll token 和后续权限；同时把 Cloud Ops 调 edge-api 所需的环境级 ops key
同步到 edge `.env` 的 `ADMIN_API_KEYS` / `CONTROL_API_KEYS`。node enroll token
只用于 edge -> cloud 上报，不能用于 Cloud Ops job runner。历史 pending 或异常记录才需要人工兜底。
