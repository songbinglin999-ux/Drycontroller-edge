-- PostgreSQL 初始化脚本
-- 用于创建边缘计算系统所需的数据库表结构

-- ==================== 创建扩展 ====================
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- ==================== 用户和授权表 ====================

-- 用户表
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    email VARCHAR(100),
    role VARCHAR(20) NOT NULL DEFAULT 'operator',
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 授权记录表
CREATE TABLE IF NOT EXISTS licenses (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    license_key VARCHAR(255) UNIQUE NOT NULL,
    hardware_id VARCHAR(255),
    expiry_date TIMESTAMP NOT NULL,
    features JSONB,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 授权验证日志
CREATE TABLE IF NOT EXISTS license_verifications (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    license_id UUID REFERENCES licenses(id),
    verification_result BOOLEAN NOT NULL,
    error_message TEXT,
    verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ==================== 设备和配置表 ====================

-- 设备信息表
CREATE TABLE IF NOT EXISTS devices (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    device_name VARCHAR(100) NOT NULL,
    device_type VARCHAR(50) NOT NULL,
    ip_address INET,
    port INTEGER,
    status VARCHAR(20) DEFAULT 'offline',
    last_seen TIMESTAMP,
    metadata JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 系统配置表
CREATE TABLE IF NOT EXISTS system_configs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    config_key VARCHAR(100) UNIQUE NOT NULL,
    config_value JSONB NOT NULL,
    description TEXT,
    updated_by UUID REFERENCES users(id),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ==================== 报警和事件表 ====================

-- 报警记录表
CREATE TABLE IF NOT EXISTS alarms (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    alarm_type VARCHAR(50) NOT NULL,
    priority VARCHAR(20) NOT NULL,
    tag_name VARCHAR(100),
    message TEXT NOT NULL,
    value NUMERIC,
    threshold NUMERIC,
    is_acknowledged BOOLEAN DEFAULT false,
    acknowledged_by UUID REFERENCES users(id),
    acknowledged_at TIMESTAMP,
    resolved_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创建报警索引
CREATE INDEX idx_alarms_created_at ON alarms(created_at DESC);
CREATE INDEX idx_alarms_priority ON alarms(priority);
CREATE INDEX idx_alarms_is_acknowledged ON alarms(is_acknowledged);

-- 系统事件日志
CREATE TABLE IF NOT EXISTS system_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    event_type VARCHAR(50) NOT NULL,
    event_category VARCHAR(50) NOT NULL,
    description TEXT NOT NULL,
    severity VARCHAR(20) NOT NULL,
    user_id UUID REFERENCES users(id),
    metadata JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创建事件索引
CREATE INDEX idx_events_created_at ON system_events(created_at DESC);
CREATE INDEX idx_events_type ON system_events(event_type);

-- ==================== AI 模型管理表 ====================

-- AI 模型版本表
CREATE TABLE IF NOT EXISTS ai_models (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    model_name VARCHAR(100) NOT NULL,
    model_version VARCHAR(50) NOT NULL,
    model_type VARCHAR(50) NOT NULL,
    model_path VARCHAR(255) NOT NULL,
    description TEXT,
    metrics JSONB,
    is_active BOOLEAN DEFAULT false,
    deployed_at TIMESTAMP,
    deployed_by UUID REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(model_name, model_version)
);

-- AI 推理记录表
CREATE TABLE IF NOT EXISTS ai_inferences (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    model_id UUID REFERENCES ai_models(id),
    input_data JSONB NOT NULL,
    output_data JSONB NOT NULL,
    inference_time_ms NUMERIC,
    confidence NUMERIC,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创建推理记录索引
CREATE INDEX idx_inferences_model_id ON ai_inferences(model_id);
CREATE INDEX idx_inferences_created_at ON ai_inferences(created_at DESC);

-- ==================== 工艺数据表 ====================

-- 生产批次表
CREATE TABLE IF NOT EXISTS production_batches (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    batch_number VARCHAR(100) UNIQUE NOT NULL,
    material_type VARCHAR(100),
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP,
    target_moisture NUMERIC,
    actual_moisture NUMERIC,
    status VARCHAR(20) DEFAULT 'running',
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 工艺参数记录表
CREATE TABLE IF NOT EXISTS process_parameters (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    batch_id UUID REFERENCES production_batches(id),
    parameter_name VARCHAR(100) NOT NULL,
    parameter_value NUMERIC NOT NULL,
    unit VARCHAR(20),
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创建工艺参数索引
CREATE INDEX idx_process_params_batch_id ON process_parameters(batch_id);
CREATE INDEX idx_process_params_recorded_at ON process_parameters(recorded_at DESC);

-- ==================== 操作审计表 ====================

-- 操作日志表
CREATE TABLE IF NOT EXISTS operation_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES users(id),
    operation_type VARCHAR(50) NOT NULL,
    operation_target VARCHAR(100),
    operation_details JSONB,
    ip_address INET,
    user_agent TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创建操作日志索引
CREATE INDEX idx_operation_logs_user_id ON operation_logs(user_id);
CREATE INDEX idx_operation_logs_created_at ON operation_logs(created_at DESC);
CREATE INDEX idx_operation_logs_operation_type ON operation_logs(operation_type);

-- ==================== OTA 升级管理表 ====================

-- OTA 升级记录表
CREATE TABLE IF NOT EXISTS ota_updates (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    version VARCHAR(50) NOT NULL,
    description TEXT,
    update_type VARCHAR(50) NOT NULL,
    file_path VARCHAR(255),
    checksum VARCHAR(255),
    status VARCHAR(20) DEFAULT 'pending',
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    error_message TEXT,
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ==================== 初始化数据 ====================

-- 插入默认管理员用户 (密码: admin123, 需要在应用层进行哈希)
INSERT INTO users (username, password_hash, email, role) 
VALUES ('admin', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5GyYVBjLxunxm', 'admin@edge.local', 'admin')
ON CONFLICT (username) DO NOTHING;

-- 插入默认系统配置
INSERT INTO system_configs (config_key, config_value, description) 
VALUES 
    ('system_name', '"Industrial Dryer Control System"', '系统名称'),
    ('system_version', '"1.0.0"', '系统版本'),
    ('auto_control_enabled', 'false', '自动控制是否启用'),
    ('data_retention_days', '90', '数据保留天数')
ON CONFLICT (config_key) DO NOTHING;

-- ==================== 创建视图 ====================

-- 活动报警视图
CREATE OR REPLACE VIEW active_alarms AS
SELECT 
    id, 
    alarm_type, 
    priority, 
    tag_name, 
    message, 
    value, 
    threshold, 
    created_at
FROM alarms
WHERE is_acknowledged = false AND resolved_at IS NULL
ORDER BY 
    CASE priority 
        WHEN 'critical' THEN 1
        WHEN 'error' THEN 2
        WHEN 'warning' THEN 3
        ELSE 4
    END,
    created_at DESC;

-- 当前活动模型视图
CREATE OR REPLACE VIEW active_ai_models AS
SELECT 
    model_name,
    model_version,
    model_type,
    model_path,
    deployed_at,
    metrics
FROM ai_models
WHERE is_active = true;

-- ==================== 触发器函数 ====================

-- 更新 updated_at 时间戳的触发器函数
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 为相关表添加触发器
CREATE TRIGGER update_users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_licenses_updated_at BEFORE UPDATE ON licenses
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_devices_updated_at BEFORE UPDATE ON devices
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_batches_updated_at BEFORE UPDATE ON production_batches
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ==================== 完成 ====================
COMMENT ON DATABASE edge_db IS 'Edge Computing System Database for Industrial Dryer Control';

