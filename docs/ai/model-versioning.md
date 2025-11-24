# 📦 AI 模型版本管理规范（Model Versioning）

> 版本：v1.0  
> 适用范围：`ai-softsensor`、未来所有 AI 相关服务

---

## 1. 目录结构与命名约定

### 1.1 目录结构

```text
models/
  v1.0.0/
    model.onnx
    scaler.pkl
    params.yaml
    changelog.md
  v1.1.0/
    ...
  v1.2.0/
    ...
```

### 1.2 版本号规则

- 采用 **语义化版本号**：`MAJOR.MINOR.PATCH`
  - `MAJOR`：重大变更，不兼容旧版（如特征集合变化）  
  - `MINOR`：功能增强或效果提升，兼容旧版接口  
  - `PATCH`：小修复，如参数微调、轻微 bug 修复

---

## 2. 元数据与登记

每个版本必须包含一份 `params.yaml` 或 `metadata.json`，记录：

- 模型基本信息：
  - `version`: 模型版本号  
  - `created_at`: 训练完成时间  
  - `created_by`: 训练者  
  - `framework`: 使用的框架及版本（如 `pytorch 2.1`）
- 数据集信息：
  - `dataset_version`  
  - `time_range`：数据覆盖时间范围  
  - `lines`: 涉及产线编号
- 训练配置：
  - 特征列表  
  - 时间窗口大小  
  - 关键超参数
- 评估指标：
  - 训练/验证/测试集的 MAE/RMSE  
  - 相比上一版本的提升/退化情况

建议在 PostgreSQL 中建立模型版本表，例如 `ai_model_versions`，字段与上述元数据对应。

---

## 3. 模型生命周期

### 3.1 状态机

模型版本建议具有以下状态：

- `draft`：训练完成，未评审  
- `staging`：已通过离线评审，待现场小批量验证  
- `production`：线上正式使用  
- `deprecated`：不再推荐使用，但保留以便回溯  
- `archived`：归档备份，可冷存储

### 3.2 状态迁移规则

- `draft → staging`：通过算法组内部评审  
- `staging → production`：通过现场小批量验证，且指标不低于当前线上版本  
- `production → deprecated`：有更优版本替代或发现缺陷  
- 任一状态 → `archived`：长期不再使用，仅保留归档

---

## 4. 上线与回滚流程

### 4.1 上线新模型

1. 将新版本目录（如 `v1.2.0`）部署到 `models/` 目录  
2. 更新配置：
   - `configs/ai-config.yaml` 中的 `current_version`  
   - 或 PostgreSQL 中的模型配置表  
3. 重启或热加载 `ai-softsensor` 服务  
4. 通过 `/ai/status` 或监控面板确认：
   - 模型版本切换成功  
   - 推理状态为 `ok`

### 4.2 回滚旧模型

触发条件：

- 新模型线上效果明显变差  
- 推理异常增多或自愈模块频繁降级  
- 客户/工艺负责人反馈异常

回滚步骤：

1. 将配置中的 `current_version` 回退到上一 `production` 版本  
2. 触发服务热加载或重启  
3. 记录回滚时间、原因、操作者

---

## 5. 与自愈机制的集成

模型管理与 `self-healing` 机制配合方式：

- 当模型加载失败时：
  - 自愈模块自动尝试回滚到上一版本  
  - 若仍失败，则停止 AI 推理，标记服务为 `fail`
- 当线上推理被监控为长期偏差过大：
  - 触发告警，建议运维与算法团队评估是否回滚或重训

---

## 6. 审计与合规

- 所有模型版本的上线、回滚、废弃操作必须记录：
  - 时间  
  - 操作者  
  - 变更前后版本号  
  - 原因说明
- 建议：
  - 在 Git 仓库中单独维护 `ai-models` 仓库或分支  
  - 使用 Tag 标记与代码版本对应的模型版本

---

## 7. 最佳实践建议

- 尽量保持 **线上仅有一个 production 模型**，避免混乱  
- 模型目录中不要混杂临时文件和调试脚本  
- 严格区分 **训练代码** 与 **推理代码**，保持推理端尽可能轻量  
- 通过简单脚本或工具，一键查询：
  - 当前线上模型版本  
  - 历史上线/回滚记录

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