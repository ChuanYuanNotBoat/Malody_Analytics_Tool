# Exclusions / Non-goals (Current Stable Phase)

以下内容本阶段明确不做，以保证稳定交付：

## Backend Scope
- 不改 `malody_api` 后端接口实现。
- 不新增后端任务中心联动依赖（本轮以本地 JSONL 为主）。

## GUI Scope
- 不做首启向导（先交付 Settings 配置中心）。
- 不做复杂图表组件增强（本轮不扩展 QtCharts/pyqtgraph 面板）。
- 不实现深度 repair 动作（仅显式降级 + 替代路径说明）。
- 不做全量文案外置翻译系统（仅支持当前 `zh_en/zh/en` 模式与基础翻译基座）。

## Engineering Scope
- 不做跨平台承诺（仅 Windows 优先）。
- 不做自动更新系统与安装器生态（本轮只交付 PyInstaller 产物构建脚本）。
- 不在本阶段重构早期 `PyQt5` 原型目录（`ui/`, `widgets/`, `core/`）为主入口。
