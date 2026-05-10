# Stats Alignment Progress (2026-05-10)

## 本轮目标
完成“用户友好收口”：`Quick Start + Advanced`、双语文案、可诊断反馈、Simple 参数降级可见化。

## 已完成

### 1) 导航与信息架构
- 顶层导航调整为：
  - `Quick Start`
  - `Advanced`
  - `Task Logs`
  - `Settings`
  - `Capabilities`
- 旧功能未删除，全部下沉到 `Advanced` 子标签（Analytics / Search & Export / Governance）。

### 2) Simple + Advanced 模型
- 默认 `Simple`。
- 一键展开 `Advanced`（可切回 `Simple`）。
- `Simple` 快捷流程：
  - Health
  - Hot/Recent
  - Stats/Summary
  - Export

### 3) 反馈与诊断
- 顶部任务状态条统一显示：`状态 + 耗时 + 最近事件`。
- 结果区统一为 5 标签：
  - 数据 JSON
  - 表格
  - 请求上下文
  - 告警
  - 诊断
- 诊断文案模板统一为：
  - `发生了什么 (What happened)`
  - `可怎么做 (What you can do)`

### 4) Simple 模式参数降级可见化
- 快捷 Hot/Recent/Stats/Summary/Export 执行时：
  - 隐藏参数自动剔除
  - 生成 warning（`reason=simple_mode_hidden_param`）
  - JSONL 任务日志保留 `dropped_params/contract_warnings`

### 5) 配置中心扩展
- 新增并持久化：
  - `ui_mode=simple|advanced`
  - `ui_language=zh_en|zh|en`
  - `quick_start_default=true|false`
- 保持既有配置兼容：
  - `api_base/api_key/request_timeout/default_export_strategy/log_tail_default`

## 测试结果
- 命令：`python -m unittest discover -s tests -p "test_*.py" -v`
- 结果：`Ran 30 tests ... OK`

新增/扩展覆盖：
- Simple 模式隐藏参数剔除与日志 warning
- 双语诊断模板结构
- 新配置键读写与回退
- 回归 hot/recent/export/worker 既有行为
