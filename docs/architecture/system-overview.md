# 系统架构概述

## 整体架构

本系统采用**边缘计算架构**，分为三层：

### 1. 设备层
- PLC 控制器
- 变频器
- 温度传感器
- 湿度传感器
- 流量计
- 执行器

### 2. 边缘计算层
- 数据采集服务
- AI 推理服务
- 控制优化服务
- 数据存储
- 授权服务
- 监控服务

### 3. 展示层
- Web 大屏
- 移动端 APP
- 运维管理工具

## 服务间通信

### MQTT 消息总线

所有实时数据通过 MQTT 进行发布和订阅：

```
industrial/dryer/realtime/<TAG>   # 实时数据
industrial/dryer/ai/moisture      # AI 预测结果
industrial/dryer/ai/control       # 控制优化建议
industrial/dryer/alarm/<TAG>      # 报警消息
industrial/dryer/status/<SERVICE> # 服务状态
```

### REST API

对外提供统一的 REST API 接口用于查询和控制。

### WebSocket

用于实时数据推送到大屏。

## 数据流

```
PLC → edge-daq → MQTT → ai-softsensor → MQTT → edge-api → 大屏
              ↓                                    ↑
          InfluxDB ←------------------------------- 
              ↓
        ai-ctrl-optimizer → MQTT → PLC
```

## 关键设计

### 1. 离线运行
系统完全自包含，无需云端支持即可运行。

### 2. 高可用
- 服务自动重启
- 数据缓存
- 异常容错

### 3. 可扩展
- 模块化设计
- 配置驱动
- 插件式架构

### 4. 安全
- 加密狗授权
- 操作审计
- 数据加密（可选）

