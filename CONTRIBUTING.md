# 贡献指南

感谢您考虑为本项目做出贡献！

## 如何贡献

### 报告 Bug

如果您发现了 Bug，请创建一个 Issue，并包含以下信息：

- Bug 的详细描述
- 复现步骤
- 预期行为
- 实际行为
- 环境信息（操作系统、Docker 版本等）
- 相关日志

### 提出新功能

如果您有新功能的想法：

1. 先创建一个 Issue 讨论该功能
2. 说明功能的用途和价值
3. 提供使用场景示例

### 提交代码

1. Fork 本仓库
2. 创建您的特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交您的更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 创建 Pull Request

## 代码规范

### Python 代码

- 遵循 PEP 8 规范
- 使用 Black 格式化代码
- 使用 Ruff 进行静态检查
- 添加类型提示
- 编写 Docstring

示例：

```python
def calculate_moisture(
    temperature: float,
    humidity: float,
    pressure: float,
) -> float:
    """
    计算含水率
    
    Args:
        temperature: 温度 (°C)
        humidity: 湿度 (%)
        pressure: 压力 (Pa)
        
    Returns:
        含水率 (%)
    """
    # 实现代码
    pass
```

### 提交信息

使用约定式提交（Conventional Commits）：

```
<类型>(<范围>): <描述>

[可选的正文]

[可选的脚注]
```

类型：
- `feat`: 新功能
- `fix`: Bug 修复
- `docs`: 文档更新
- `style`: 代码格式（不影响代码运行）
- `refactor`: 重构
- `perf`: 性能优化
- `test`: 测试
- `chore`: 构建过程或辅助工具的变动

示例：

```
feat(api): 添加历史数据查询接口

- 支持时间范围查询
- 支持数据聚合
- 添加缓存机制

Closes #123
```

## 测试

- 所有新功能必须包含测试
- 保持测试覆盖率 > 80%
- 运行 `pytest` 确保所有测试通过

## 文档

- 更新相关文档
- 添加 API 文档
- 更新 CHANGELOG.md

## 代码审查

所有 Pull Request 都需要经过代码审查：

- 至少 1 人审查通过
- 通过所有 CI 检查
- 没有冲突

## 问题咨询

如有任何问题，欢迎通过以下方式联系：

- 创建 Issue
- 发送邮件到 support@example.com

再次感谢您的贡献！

