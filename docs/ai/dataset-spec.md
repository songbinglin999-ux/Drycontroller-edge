# 📂 含水率软测量数据集规范（Dataset Specification）

> 版本：v1.0  
> 适用对象：用于训练、验证、测试 `ai-softsensor` 模型的数据集

---

## 1. 目的与适用范围

- 统一数据格式与字段含义，确保不同批次数据可复用  
- 便于算法工程师与工艺工程师对齐数据含义  
- 为后续自动化数据管线（ETL / 特征构建）提供标准

---

## 2. 数据集结构

### 2.1 目录规范

```text
dataset/
  raw/                 # 原始导出数据（InfluxDB/PLC/人工记录）
  interim/             # 经过清洗但未构建特征
  processed/           # 已构建好训练特征的数据
  metadata/            # 元数据（字段说明、工艺信息）
```

### 2.2 文件格式

- 推荐使用：
  - `CSV`（便于人工查看与 Excel 打开）  
  - 或 `Parquet`（高效读写，适合大规模数据）

---

## 3. 字段定义（示例）

### 3.1 通用字段

- `timestamp`：UTC 时间戳或本地时间（需在元数据中说明时区）  
- `line_id`：产线编号（如 `L1`、`L2`）  
- `batch_id`：批次编号（如原料批次或生产批次）  
- `shift`：班次（如 `A/B/C`）  
- `operator_id`：操作员编号（可选）

### 3.2 工艺过程量字段

| 字段名              | 含义                     | 单位 | 备注             |
|---------------------|--------------------------|------|------------------|
| `T_ZONE1`           | 一段烘干区温度          | ℃    | 关键特征         |
| `T_ZONE2`           | 二段烘干区温度          | ℃    | 关键特征         |
| `T_EXHAUST`         | 尾气温度                | ℃    | 影响预烘干       |
| `T_PRE_DRY_OUT`     | 预烘干出口料温          | ℃    | 关键特征         |
| `CHAIN_SPEED`       | 链速                     | m/min| 关键特征         |
| `FAN_FREQ`          | 抽风机频率              | Hz   | 关键特征         |
| `AUGER_TORQUE`      | 料斗绞龙扭矩            | Nm   | 负载变化信息     |
| `FURNACE_TEMP`      | 炉温                     | ℃    | 热源相关         |

（实际字段与命名请以 `tags.yaml` / PLC 标签定义为准）

### 3.3 标签字段

- `MOISTURE_LAB`：人工测得含水率（%）  
- `MOISTURE_LAB_SOURCE`：测量方式/设备编码（可选）  
- `SAMPLE_TIME`：取样时间（如与过程时间不同需单独记录）

---

## 4. 采样与对齐规则

### 4.1 过程数据采样

- 源数据采集频率：1 Hz  
- 训练数据可根据需要做：
  - 下采样（如每 10s / 30s 一个点）  
  - 时间窗口聚合（见 `model-training-guidelines.md`）

### 4.2 含水率标签对齐

- 由于烘干有物料传输延迟：
  - 需根据工艺流程估计“过程条件影响到出口含水率”的时间延迟  
  - 将对应窗口的过程特征对齐到标签时间点附近

---

## 5. 数据质量要求

### 5.1 缺失数据

- 对关键字段（温度、链速、风机频率等）：
  - 缺失占比超过某阈值（如 10%）的批次不建议用于训练  
  - 单个样本若关键字段缺失，可以整行丢弃

### 5.2 异常样本

- 工艺停机、清洗、试机等特殊工况应打标：`is_valid=0`  
- 算法训练默认仅使用 `is_valid=1` 的正常生产数据

---

## 6. 元数据与说明文件

在 `dataset/metadata/` 中建议维护：

- `fields.yaml`：字段中英文说明、单位、是否必填  
- `batches.yaml`：各批次的工艺备注、原料信息  
- `issues.md`：采集过程中已知问题记录（如某段时间传感器故障）

---

## 7. 数据安全与脱敏

- 生产数据出厂/出厂前需根据合同及隐私要求进行脱敏：
  - 移除与客户/厂名直接相关的字段  
  - 操作员信息可匿名化（hash）
- 训练数据需存放在受控环境，禁止通过非授权渠道传播。

---

## 8. InfluxDB Measurement：`softsensor_training_data`

### 8.1 设计目标

- **实时回采训练数据**：每一次 `ai-softsensor` 推理都写入该 measurement，包含 *输入特征 + 推理结果*。
- **延迟补写标签**：实验室含水率（LAB）可在拿到结果后写回同一 measurement，实现“数据-标签”一体化。
- **一键导出**：未来训练新模型时，只需 `SELECT * FROM softsensor_training_data WHERE time >= ...` 即可拿到结构化数据集。

### 8.2 Tags（维度字段）

| Tag 名称          | 说明                                 | 示例               | 是否必选 |
|-------------------|--------------------------------------|--------------------|----------|
| `line_id`         | 产线编号                             | `L1`               | 是       |
| `model_version`   | 推理模型版本                         | `v1.0.0`           | 是       |
| `inference_source`| 推理来源（默认 `ai-softsensor`）     | `ai-softsensor`    | 是       |
| `batch_id`        | 批次编号 / 原料批次                  | `BATCH-20241125`   | 否（无则留空） |
| `lab_sample_id`   | 实验室样本编号                       | `LAB-231126-01`    | 否（LAB 到齐后写入） |
| `lab_operator`    | 实验室检测员编码（脱敏后）           | `OP-03`            | 否       |

> 若需要增加新的 tag（如 `product_grade`），请在 `metadata/fields.yaml` 中同步维护。

### 8.3 Fields（数值/属性字段）

| Field 名称               | 类型   | 单位 | 说明                                        | 是否必填 |
|--------------------------|--------|------|---------------------------------------------|----------|
| `moisture_predicted`     | float  | %    | 当前推理得到的含水率                        | 是 |
| `moisture_confidence`    | float  | -    | 推理置信度（0-1）                            | 是 |
| `inference_time_ms`      | float  | ms   | 单次推理耗时                                 | 是 |
| `moisture_lab`           | float  | %    | 实验室测得含水率                             | 否（LAB 有结果时写入） |
| `lab_measured_at`        | int    | epoch(ms) | 实验室实际测量时间戳                      | 否 |
| `lab_sample_time_offset` | float  | min  | 采样→测量的延迟，便于对齐                   | 否 |
| `is_lab_confirmed`       | bool   | -    | 是否已对齐实验室结果                        | 否 |
| `feature_window_start`   | int    | epoch(ms) | 特征窗口起始时间                         | 否（只在有窗口聚合时写入） |
| `feature_window_end`     | int    | epoch(ms) | 特征窗口结束时间                         | 否 |
| `DRYER1.IN.TEMP` ~ `DRYER2.TORQUE` | float | 各自单位 | 工艺输入特征（参考 `softsensor.input_features` 列表） | 是 |
| 其他新增输入特征         | float  | -    | 若模型增加新特征，需在此处补充说明           | 视情况 |

> 模型输入字段以 `configs/ai-config.yaml` → `softsensor.input_features` 为准；如字段名称变更，需同步更新本规范。

### 8.4 写入规则

1. **推理写入（系统自动）**  
   - 时间戳：推理触发时刻（纳秒精度）。  
   - Tag：至少包含 `line_id`、`model_version`、`inference_source`，可选 `batch_id`。  
   - Field：所有输入特征 + `moisture_predicted` + `moisture_confidence` + `inference_time_ms`。
2. **实验室补写（人工/脚本）**  
   - 拿到 LAB 数据后，复用相同时间戳或附加 `lab_sample_id` 写入。  
   - 仅更新 `moisture_lab` 及相关字段，不改动历史推理字段。  
   - 建议使用 `upsert` 策略，确保多次补写保持幂等。
3. **缺失与空值**  
   - 推理阶段要求特征齐全；缺失则不写入该 measurement。  
   - LAB 字段在无数据时保持缺省，不写 `null`。

### 8.5 导出示例

```sql
from(bucket: "industrial-data")
  |> range(start: 2024-11-01T00:00:00Z, stop: 2024-11-30T23:59:59Z)
  |> filter(fn: (r) => r._measurement == "softsensor_training_data" and r.line_id == "L1")
  |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> filter(fn: (r) => exists r.moisture_lab) // 仅保留已有 LAB 标签的样本
```

导出的 `CSV/Parquet` 建议放于 `dataset/raw/softsensor_training_data/`，并在 `metadata/fields.yaml` 记录导出时间、字段版本与过滤条件。

---

## 9. 导出与采集工具

- **API 查询**：`/api/v1/ai/lab/samples` 支持通过 HTTP 过滤时间窗、产线、LAB 标识，便于大屏或 Notebook 直接拉取样本。
- **脚本导出**：`scripts/export_training_dataset.py` 可批量导出 JSON/CSV，例如：

  ```bash
  python scripts/export_training_dataset.py --start -7d --line L1 --format csv --output L1_week.csv
  ```

  - `--require-lab`：仅导出已录入 LAB 的样本  
  - `--limit`：控制最大记录数，避免一次导出过大  

- **LAB 回写**：
  - 使用 `scripts/import_lab_results.py lab_results.csv --line L1` 将实验室 CSV 写入 `softsensor_training_data`（仅追加，不会覆盖历史字段）。
  - 或调用 `/api/v1/ai/lab/samples` 确认数据是否生效。

{
  "cells": [],
  "metadata": {
    "language_info": {
      "name": "python"
    }
  },
  "nbformat": 4,
  "nbformat_minor": 2
}