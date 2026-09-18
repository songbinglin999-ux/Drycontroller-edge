# PLC 通讯协议与现场接入记录

> **2026-09-19当前控制设计：** 以[I/O控制契约v2.1](io-and-control-contract.md)和根活动项PLC-EDGE-SINGLE-CONTROL-20260918为准。Edge在线时为主控制仲裁入口，HMI2在Edge不可用时需要直连PLC读写的现场应急能力。本文原D5100/D5550、PLC多来源owner/epoch及HMI直连接管内容只作历史/候选证据，不自动恢复为已签收现场实现；本轮不解除运行写门禁。新现场点表为人类确认的AI.csv，旧点数/用途不能覆盖新表与订正。

本文记录 PLC 接口要求、现场实测和 Edge 通讯配置。接口设计不等于现场已部署；当前原程序只读接入以第 9 节为准。

> **设备身份补充（2026-09-19人类确认）：** `192.0.2.100`是现场PLC专用HMI；PLC是`192.0.2.200`。旧文对`.100`的连接失败不是PLC服务失败。专用屏向Edge提交启停/调频是新设计目标，屏型号/协议和转发链尚未验收，不能根据其IP推断可访问的服务端口。本次没有网络探测或现场配置变更。

> **当前配置入口（2026-09-14 验收）：** [dry-edge-tmp 通讯固化、验收与回退](#field-comm-current)。PLC `192.0.2.200:2000`，标准 Modbus TCP / 站号 `1`；Edge PLC 侧 `192.0.2.250/24`；仅以 `0.3` 秒间隔读取 M1001（FC01 / 31001），全部控制写关闭。主机重启、断网恢复、卡死告警及工艺数据仍未验收。

> 2026-06-26 历史边界：旧SamSoar CSV只证明PLC原工程把`D30-D35`输出到`AO140-AO151`，没有证明PLC与变频器通讯或读取实际频率。旧设计中的edge在线六频率目标`D5100-D5299`事务和DryHMI edge-down direct `D5550-D5599`事务可作为HMI2离线兜底工程候选，但不能直接覆盖2026-09-18 AI.csv新点表和人类订正。是否复用这些地址，必须重新给出请求/ACK/NAK/读回、PLC有效目标、PLC输出命令/MV、PV/status invalid/not-wired标记、状态/安全覆盖和报警码；真实VFD实际频率/状态要等反馈线或通讯补齐并签收后再作为后续验收项。

## 1. 通讯参数
- **本次已实测参数（2026-09-14）：标准 Modbus TCP（MBAP帧、不带RTU CRC），IP `192.0.2.200`，端口 `2000`，站号 `1`。** 用户工程截图显示上述网口参数、Modbus TCP/IP从站勾选，子网掩码255.255.255.0、网关192.0.2.1。
- **首次读通证据（历史阶段）：** 从 `dry-edge-tmp` 经临时同网段地址192.0.2.250访问，ping 2/2成功（平均0.909ms），TCP2000连接成功；同一连接间隔1秒连续3次FC03读取协议地址40000起2个寄存器，均返回有效MBAP与PDU、原始值 `[32769, 32769]`。首帧请求 `00010000000601039c400002`，响应 `00010000000701030480018001`。仅证明基本只读通讯，未验证原始值工艺含义、全量点表、心跳、控制或屏与Edge并发。目标MAC `02:00:00:00:00:01`，交换机端口7；实际硬件型号尚未读取。
- **历史订正：** 用户先前提供的192.0.2.100对应另一MAC `02:00:00:00:00:02` / 端口8，其TCP2000拒绝连接证据不能归给上述PLC。原“2026-06-20现场实测RTU-over-TCP/2022/站号1”陈述已撤回；2026-09-14才取得实际Modbus读响应；运行配置随后经用户批准完成固化，见第9节。
- **网络状态：** 首次诊断使用的临时192.0.2.250/32和目标主机路由已清理；随后经批准持久化192.0.2.250/24，保留管理网DHCP（当次198.51.100.151/24）及默认网关198.51.100.1。配置路径与恢复验收见第9节；未写PLC、未开启交换机镜像。
- 端口区别：厂商SamSoar2022帮助文档区分固定下载/监视2022（监视站号1、单连接）与Modbus TCP/IP从站默认2000（可配置）；见 [项目留存的厂商帮助文件](source-materials/vendor-help-not-included) 中 `Inst/Communication/EthernetConfig.html`。
- 并发连接：上述厂商帮助文档说明 Modbus TCP/IP 从站最多支持6个客户端，启用自由口后从站支持5个；不能把专用屏使用2000端口直接判为端口独占或拒绝连接的根因。现场型号、固件及连接占用仍需核对。
- 功能码：读 3（保持寄存器，D 字读点）、读 1（线圈，料位/限位等位元件）、读 4（输入寄存器）；写 6/16（控制写，本期按只读决策锁停、未启用）
- 字节序：默认 Big Endian；32 位值字序需按具体点位签收；当前 SamSoar CSV 只证明湿度 `D45` 为 WORD

## 2. 点表字段（请按表格提供）
- 必填：`name`（唯一）、`address`、`function_code`（3/4）、`type`（float/int/int32/uint32/bool）
- 可选：`byte_order`(big/little)、`scale`（工程值 = 原值 * scale）、`unit`、`range[min,max]`、`alarm_low`、`alarm_high`
- 只读 I/O 点：对应当前 DryController 28 个 D 字读点 + 6 个料位/限位线圈，edge 只读不写；`D30-D35` 不进入 DryController 点表
- 控制写入点：Edge在线主路径和HMI2离线直连兜底均需提供请求/ACK/读回事务；旧`D5100-D5299`、`D5550-D5599`只作可复用候选，若采用请标明`point_role=control_softpoint`
- 同步与仲裁点：参数同步、报警同步、状态同步、心跳、HMI/edge 控制权锁、HMI direct 授权状态、无扰切换状态，请标明 `point_role=sync|alarm|status|heartbeat|authority`
- 唯一性：同一功能码下 `(address, function_code)` 不可重复

表头示例（含中文说明）：

| 字段            | 含义              | 示例        |
|-----------------|-------------------|------------|
| name            | 标签名（唯一）    | TEMP.PV    |
| address         | 寄存器地址        | 40010      |
| function_code   | 功能码 3/4        | 3          |
| type            | 数据类型          | float      |
| byte_order      | 字节序 big/little | big        |
| scale           | 缩放（工程=原值*scale） | 1.0  |
| unit            | 工程单位          | °C         |
| range_min       | 最小值            |            |
| range_max       | 最大值            |            |
| alarm_low       | 低报              |            |
| alarm_high      | 高报              |            |
| point_role      | 点角色            | readonly_io/control_softpoint/sync/alarm/status/heartbeat/authority |

## 3. 读取规则（PLC 侧无需改逻辑，只需点表正确）
- `float`：占 2 寄存器，按 `byte_order` 组字
- `int32`/`uint32`：占 2 寄存器
- `int`/`bool`：占 1 寄存器（bool 非 0 即 True）
- 我们会自动合并连续地址批量读取（同功能码、同字节序、同寄存器长度）。

## 4. 写入规则（控制软点）
- 仅 `point_role=control_softpoint` 的 PLC 软点会被写；52 个 I/O 位号全部只读
- 寄存器占用：`float`/`int32`/`uint32` 用 2 个寄存器；`int`/`bool` 用 1 个寄存器（bool 写 0/1）
- 如配置 `scale`：寄存器值 = 工程值 / scale
- 如配置 `range`：超出范围将被拒绝下发
- D5100 单路频率事务和 DryHMI D5550 direct 事务至少要求写入后可读 PLC 有效目标、PLC 输出命令/MV、PV/status invalid/not-wired 标记、PLC 安全覆盖/报警状态；反馈线或通讯补齐后再要求真实 VFD 实际频率/状态，并要求 PLC 对每条写入返回 ACK/NAK 与原因码，不满足控制权、模式、范围、联锁或同步条件时必须 NAK，不能静默忽略

## 5. 通讯心跳

> **2026-09-14 现场只读核验：** `192.0.2.200:2000` / 站号1，按当前地址表同时读取 `M1001`（FC01，协议地址31001）和 `D5290`（FC03，协议地址45290）。16组采样起点跨度5.176秒，约0.345秒一组；M1001为 `[0,0,1,0,1,1,0,1,1,0,1,1,0,1,1,0]`，观察到10次变化，证明该映射的只读心跳在本窗口有效。D5290的16次返回均为0，未验证其具备心跳功能，不能将其当成现场有效心跳或据此判PLC离线。首帧M1001请求/响应 `000100000006010179190001` / `00010000000401010100`；D5290请求/响应 `0003000000060103b0ea0001` / `0003000000050103020000`。未写D5291或任何PLC点，临时IP/目标路由已清理并读回原配置；运行采集配置未修改。下述为接口要求，不代表D5290/D5291已经在现场实现并验收。

- 必须提供读心跳寄存器（如 50000）和/或写心跳寄存器（如 50001），最终形式联调时确认
- 读：PLC 侧可 0/1 翻转或更新任意变化值；edge 用短窗口确认读值是否变化，不能依赖单次 5s 采样
- 写：edge 周期写递增计数；当前默认 5s
- 请在点表注明心跳地址、功能码、超时窗口和心跳丢失后切 PLC 本地控制的状态位

心跳推荐：
- 读功能码：3/4（按 PLC 寄存器类型确认）；写功能码：6（单保持寄存器）
- 写周期：5s（可调）；读 PLC 0/1 翻转时，edge 在同一检查周期内做短窗口确认采样
- 判定：连续 2~3 次读值不变或读/写失败，判定通讯异常
- edge 写计数回绕：计数达上限回到 1，不写 0；0 保留给未初始化或禁用状态

## 6. 同步 / 无扰切换 / 控制权锁（最终接口）
- 参数同步：PLC 与 edge 对关键设定值、当前生效参数、参数版本/更新时间有一致字段。
- 报警同步：PLC 告警码、告警状态、确认/复位状态需可读；需给出报警码表。
- 状态同步：PLC 模式、本地/远程控制权、软点执行状态、最近 ACK/NAK 原因码需可读。
- 无扰切换：edge 控制权切入/切出时，PLC 需保持当前安全输出或按约定斜坡，不允许突变。
- 控制权锁：PLC 是最终执行仲裁点；edge 无心跳、PLC 本地模式、维护锁、联锁触发、HMI 接管时，PLC 拒绝 edge 软点并返回 NAK 原因。
- HMI2 direct：HMI2 edge-down direct 是最终必做能力；若复用旧`D5550-D5599`，必须经PLC授权/代次/direct-enable后写DryHMI目标缓冲区，不能绕过PLC输出提交形成并行输出链路；若不用旧D5550，则需定义等价点集。最终e2e前生产disabled。
- epoch/代次：D5100 写控制软点时需携带当前 `AUTHORITY_EPOCH`；PLC 拒绝 stale epoch，避免 edge 恢复后用旧状态抢写。本项目现场内网不引入 token/签名租约。

建议至少提供这些 authority 字段（名称可按 PLC 习惯调整，但语义需保留）：

| name | 方向 | type | point_role | 说明 |
|---|---|---|---|---|
| CTRL.MODE | PLC -> edge/HMI | int | authority | 当前模式：EDGE_MASTER/HMI_STANDBY/HMI_LOCAL/PLC_LOCAL/LOCAL_MANUAL/MAINTENANCE_LOCK/FAULT_STOP |
| CTRL.AUTHORITY_OWNER | PLC -> edge/HMI | int | authority | 当前控制权 owner：PLC_LOCAL/LOCAL_SCREEN/EDGE/DRY_HMI_DIRECT/FAULT；MAINTENANCE 为保留/不支持码，edge fail-closed |
| CTRL.AUTHORITY_REQUEST | edge/HMI -> PLC | int | authority | D5100 接管/释放请求；DryHMI direct 接管/释放请求 |
| CTRL.AUTHORITY_GRANT | PLC -> edge/HMI | int | authority | PLC 对请求的授予/拒绝结果 |
| CTRL.AUTHORITY_EPOCH | PLC -> edge/HMI | int | authority | 控制权代次，写软点时需匹配 |
| HMI.DIRECT_ENABLE | PLC -> HMI/edge | bool | authority | DryHMI direct 显式授权；最终 e2e 前生产 disabled |
| CTRL.TAKEOVER_REASON | PLC -> edge/HMI | int | authority | 最近一次接管原因 |
| CTRL.REJECT_CODE | PLC -> edge/HMI | int | authority | 最近一次拒绝原因 |

点表示例（含中文备注）：

| name       | address | function_code | type  | byte_order | scale | unit | range_min | range_max | alarm_low | alarm_high | point_role | 备注                  |
|------------|---------|---------------|-------|------------|-------|------|-----------|-----------|-----------|------------|------------|-----------------------|
| HB.READ    | 50000   | 3             | int   | big        | 1     |      |           |           |           |            | heartbeat  | 心跳读，PLC 可 0/1 翻转；edge 只判断变化 |
| HB.WRITE   | 50001   | 6             | int   | big        | 1     |      |           |           |           |            | heartbeat  | 心跳写，edge 写递增计数 |
| FTT1       | 40010   | 3             | float | big        | 1.0   | °C   |           |           |           |            | readonly_io | 主机1风温采集         |
| VFM1_FREQ_SP | D5110 payload / D5550-D5599 direct | D5100-D5299 / D5550-D5599 | int | big | 1.0 | Hz | 0 | 50 | | | control_softpoint | VFM1 目标频率；edge 走 D5100，DryHMI direct 走 D5550-D5599 |
| CTRL.AUTHORITY_OWNER | 45201 | 3       | int   | big        | 1     |      |           |           |           |            | authority  | 当前控制权 owner；PLC D5201      |
| CTRL.AUTHORITY_EPOCH | 45414 | 3       | int   | big        | 1     |      |           |           |           |            | authority  | 控制权代次；PLC D5414            |
| HMI.DIRECT_ENABLE | 45571 | 3          | bool  | big        | 1     |      |           |           |           |            | authority  | DryHMI direct 授权状态；PLC D5571      |
| ALARM.CODE | 42010   | 3             | int   | big        | 1     |      |           |           |           |            | alarm      | 当前 PLC 告警码       |

## 7. 联调检查清单
D5100/DryHMI direct 最小联调：
1) 确认站号、端口、功能码、字节序。
2) 提供当前 28D + 6 线圈只读点表，标明地址、类型、scale、byte_order。
3) 提供 D5100 请求序号、D5104 通道、D5110 单个频率值、D5550-D5565 DryHMI direct 请求块、ACK/NAK、有效目标和输出 MV 的地址、类型、scale、byte_order、range。
4) 提供 PLC 有效目标、PLC 输出命令/MV、PV/status invalid/not-wired 标记、PLC 安全覆盖状态和报警码。
5) 验证 HMI/edge-api -> edge-daq -> PLC -> 变频器命令/MV 变化，以及 edge down + PLC grant -> DryHMI direct -> PLC -> MV 变化；当前不验收 VFD 实际反馈回读，越界/未授权/edge 不健康时拒绝。

后续监督控制联调：
1) 确认站号、端口、功能码、字节序。
2) 提供完整点表，标明可写点。
3) 提供 D5291 EDGE_HOST_ALIVE freshness 寄存器与 stale 后 hold-last / 拒绝新 edge 请求状态。
4) 提供参数同步、报警同步、状态同步、无扰切换、HMI/edge 控制权锁和 HMI 直连启用字段。
5) 联调前用测试 PLC/模拟器自查：无重复地址、功能码正确、字节序与类型匹配，ACK/NAK/原因码可复现。


## 8. 2026-09-14 原程序只读采集检查

用户明确现场未下载我方PLC程序，并确认设备停机或仅控制柜通电。不能将D5290/D5291、控制仲裁或命令确认等新增接口视作已部署。

从dry-edge-tmp临时同网段接入192.0.2.200:2000/站号1，使用标准Modbus TCP，16轮读取D0-D21、D40-D46、X0-X6及M1001（X004只作为连续读取中的未使用位，不映射为产品点）。10.425秒完成64/64有效响应，平均21.88ms、范围21.43–23.20ms，无Modbus异常。

| 点表类别 | 本次原始值 | 验证边界 |
|---|---|---|
| 22路电流D0-D21 | 全部32769（0x8001），16轮不变 | 不可按倍率直接发布3276.9A为有效值；须核对模块、信号及原工程赋值 |
| 5路温度D40-D44 | 全部0，16轮不变 | 尚未对照专用屏/实测温度，不能确认有效温度 |
| 湿度D45-D46 | 两字均0 | 工程值倍率仍未确认；当前tags/address-map的WORD与int32材料差异未关闭 |
| LL1-LL6对应X000/001/002/005/003/006 | 全部0 | 停机状态下未做现场触发验证，不能据此完成接线验收 |
| M1001 | 0/1跳变，16轮间观察到10次变化 | 只读心跳本窗口有效 |

0x8001按signed16为-32767。项目留存SamSoar帮助`Utility/All-In-One/Local.html`及部分F-4TC/F-4PT模块说明将-32767用作超量程、空载或断线标记；本机模块/信号来源尚未读取，该资料只支持排障假设，不能直接认定现场22路都断线。

同时只读检查发现常驻edge-daq为unhealthy，健康检查HTTP503；实际容器参数为PLC_HOST=192.0.2.100、PLC_PORT=2022、MODBUS_FRAMER=rtu、PLC_SLAVE_ID=1，日志持续modbus_connection_failed/plc_heartbeat_read_failed。与已验证的目标及帧格式不一致，且仍读取未验证的45290心跳和新增接口。独立只读采样成功不等于常驻采集恢复。此次未修改或重启服务；正式接入前须处理PLC网段、协议、已部署点集、有效心跳及无效原始值识别，并确保不会向现场未部署接口写入D5291或控制点。

全部临时IP/目标主机路由已移除并核验恢复原198.51.100.151/24及经198.51.100.1路由；未写PLC、未启动设备、未改交换机配置。


### 专用屏照片对照

用户随后提供6张专用屏照片（画面时钟均17:07，拍摄时间与远程采样未证明同步）：进料、预发酵、一级烘干、发酵、二级烘干、出料页面上可见的电流均标32769A；预发酵/一级/二级的蒸汽温度均0℃；出料湿度0%HR。与本次Modbus电流原始字32769及相应温湿度零值相符，支持异常值并非仅由独立诊断客户端产生；屏能显示页面不等于传感器数据有效。照片未覆盖两锅炉温度，也未提供控件绑定地址，且大量相同值不能证明逐点映射正确或长期刷新正常。

屏将32769直接标为A，而当前地址表电流scale=0.1；故障样本不足以判断哪方倍率正确，不能因此修改倍率或将0x8001转成有效电流。后续应核对电流采集模块型号、供电/信号及原程序赋值，再用一个有效非零样本对照屏和原始字。照片中的“报警值”属于设置项，不与电流测量值混为一谈；当前证据不能区分传感器未供电/未接、通道模式不符、模块异常或程序映射问题。


<a id="field-comm-current"></a>

## 9. dry-edge-tmp 通讯固化（2026-09-14 已应用）

本节为当前运行状态，前述临时测试/旧DAQ配置为历史证据。用户批准“先把通讯固化”，仅固化已验证M1001点，不接入未验收工艺量，也不恢复控制。

- 主机：dry-edge-tmp / 100.100.0.11；管理口enp3s0f1保留DHCP（本次198.51.100.151/24）和默认网关198.51.100.1。冲突探测无回应后增加192.0.2.250/24，持久文件为`/etc/netplan/60-drycontroller-plc.yaml`，`netplan generate/get`验证成功。当前以只增地址生效，未down管理口、未执行主机重启。
- 目标：192.0.2.200:2000、站号1、MODBUS_FRAMER=socket。仅采集canonical `PLC_LEGACY_HEARTBEAT_TOGGLE`，FC01/31001，间隔0.3秒；作为普通只读点发布到现有MQTT/Influx通路。
- 可复现配置：本仓 [tags.yaml](../../configs/field-comm/tags.yaml) 和 [modbus-address-map.yaml](../../configs/field-comm/modbus-address-map.yaml)仅从canonical tags/address-map选择这一点，不定义新位号。[docker-compose.field-comm.yml](../../docker-compose.field-comm.yml) 是本次现场配置，在该主机安装为`/home/dryops/Drycontroller-edge/docker-compose.override.yml`，标准Compose启动时自动合并；两份点表位于同目录configs/field-comm，由既有只读configs挂载提供。
- 写入边界：配置不含PLC_HEARTBEAT/EDGE_HOST_ALIVE，因此不派生D5291周期写；无writable软点令控制索引为空，CONTROL_ENABLE_D5100_WRITER=false令频率事务提前拒绝。未改通用点表、程序逻辑、签名、鉴权、License或控制许可。M1001暂不进入旧寄存器心跳超时算法，`/ready=200`及control.ready仅证明通路/订阅可用，不能解释为控制授权或已验证卡死心跳告警。
- 本次不更换镜像：DAQ仍为`sha256:cf8ab59f0cdd4fa3836bbb3ea1b7abc1bbb73fbea2f547046e5337d7af745815`，API仍为`sha256:85596bb1ca3021a7f27479ecc9920fe738d675b87bdabe3dbbb8ddfb244fb81d`。这是未注册临时Edge上旧Compose栈的窄配置固化，不代表完成正式edge-app-v1发布/License激活，也不授权更新旧整栈镜像。

### 验证与回退

独立审查GO；审查实际导入现有代码，Mock验证仅FC01/31001读取并发布、普通控制索引为空、D5291无写配置及D5100写关闭。现场main/plc_reader/modbus_client/control/frequency_transaction与审查源码哈希一致。应用前、每次重建前后均核对DAQ镜像ID。

应用后DAQ由unhealthy变healthy，`/health`与`/ready`均HTTP200，MQTT和Influx连接正常、缓冲为0。两次8秒MQTT订阅各收到27条fresh/good心跳且各16次变化。首段完整45秒抓包116请求/116响应，第二次强制重建后完整45秒抓包123请求/123响应；全为源192.0.2.250到PLC2000的FC01/31001读，0写请求，响应包含0/1。第二次容器于04:57:33Z创建，镜像不变，连接failure_count=0。04:58:43Z验收后停止10分钟自动回滚timer，确认未触发回退。

备份/证据保存在目标机root-only目录`/var/lib/drycontroller/comm-backup-20260914`：原compose/env/netplan备份、`rollback.py`、两段PCAP/packet-proof JSON与`accepted.json`。回退脚本只移除本次新增配置/辅助地址并重建原DAQ，执行前验证原镜像ID；回退会恢复旧的不通PLC参数，不能作为正常采集方案。首次验收订阅使用DAQ发布账号未收到数据，改用已有API消费账号后验证通过，未修改MQTT ACL或泄露凭据。

持久文件SHA256：netplan `98527391de0537dede9bda838409cd3b594ac47a33d40bd38bcca898fb8e1714`；compose override `3ec8e3668704766189d343d2114464b012dd2dbba60e01dc4267563bca56577b`；tags `8cb6cfe58594546a92355701efbd24fbfb1672e0f92682a193e7bd374207a9a3`；address-map `c982b76f09d6377e046463ae850814ef9f33f69caf5f7a74a2104cb392dc8c2f`。

验收边界：服务强制重建恢复已实测，主机断电/重启恢复仅完成持久配置校验，未实测；M1001卡死告警、真实网络中断恢复、全量工艺数据有效性、正式注册/发布及控制仍未验收。工艺量不在此次profile中发布。

<a id="edge9-real-plc"></a>

## 10. 原Edge接入真实PLC（2026-09-18）

人类要求原Edge停用模拟器并连接真实PLC。经临时Edge实时FC01读取M1001确认，真实目标仍为`192.0.2.200:2000`、站号1、标准Modbus TCP/socket。

- 原Edge：`100.100.0.9`，管理口`enp2s0` DHCP（本次`198.51.100.211/24`），新增辅助地址`192.0.2.249/24`。3次ARP冲突探测无响应，PLC响应MAC`02:00:00:00:00:01`。`/etc/netplan/60-drycontroller-plc.yaml`将原all-en匹配收窄至enp1s0保留DHCP，另以field-plc精确匹配enp2s0并保留DHCP/增加辅助地址；生成的两份networkd配置逐项检查通过。仅即时增加地址，未重置管理口或网关，未重启主机。
- 在宿主`/opt/drycontroller-host/current/configs/`的默认`tags.yaml`和`modbus-address-map.yaml`安装本仓既有field-comm两文件原字节，仅有canonical `PLC_LEGACY_HEARTBEAT_TOGGLE`（FC01/31001，M1001）。项目`.env`只改PLC_HOST/PLC_PORT/MODBUS_FRAMER/PLC_SCAN_INTERVAL为`192.0.2.200/2000/socket/0.3`；runtime plc.yaml不存在。使用默认文件路径使API和DAQ一致，且不修改签名Compose或OTA镜像override。
- 仅重建edge-api/edge-daq，精确镜像保持API`bec97bea…6c560`、DAQ`70244ebf…93f91b`；active sequence12/source9a10fbe、previous、journal及signed Compose/override前后摘要一致。`drycontroller-ipc-plc-simulator`已停止并设置restart=no；保留历史文件和镜像用于追溯，不得运行旧整栈Compose重新启用它。
- 实测12:06:01–12:06:42北京时间抓包：单连接、双方SYN及TCP2000验证、139请求/139响应，全部FC01/31001/unit1，值含0/1，窗口内写请求0。8秒MQTT收到26条good心跳、15次变化，末点龄0.89秒；API `/data/tags`和默认`/data/latest`仅含该点、missing为空。12:10复核API/DAQ healthy，MQTT/Influx连接正常、缓冲0。临时Edge原通讯仍healthy，未改其配置。
- 实际容器导入配置验证：自动heartbeat写配置为空、可写索引为空、D5100 writer=false。API为startup_fail_closed/MANUAL、auto_control_enabled=false；静态六个频率标签名称不代表可写授权。未发任何PLC控制写请求，未复用历史M/Y操作授权。

独立审查通过的现场脚本SHA256为`2138c989590769b549bc33426365a892b431c9c226ad44ba6c5f93366010fd32`。原配置（包含敏感.env）仅备份在原Edge root-only `/var/lib/drycontroller/edge9-plc-cutover-20260918`；非秘密验收数据和抓包归档于根仓`docs/workspace/archive/edge9-plc-20260918/`。失败处理为停止采集/API/模拟器并撤辅助地址，不自动恢复模拟数据发布；人工恢复必须先复核真实PLC零写边界。

本次仅验收真实PLC只读通讯心跳，不代表完整工艺点、M1001卡死告警、控制、HMI界面或物理重启验收。旧模拟历史未删除，显式查询旧位号仍可能读到其历史quality=good，必须结合时间戳与freshness判断，不能解释为新鲜现场数据。
