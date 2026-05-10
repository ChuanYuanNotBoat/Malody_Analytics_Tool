# Malody Analytics Desktop

独立分析 GUI（Windows 优先），面向 `malody_api` 现有接口。  
定位：新手可快速上手（Quick Start），高级用户可完整控制（Advanced）。

## 当前体验结构
- `Quick Start`：4 条主流程卡片
  - 健康检查 (Ping /health)
  - 热门/最近查询 (Hot/Recent)
  - 统计摘要 (Stats/Summary)
  - 导出 (Export)
- `Advanced`：原有分析/搜索导出/治理入口完整保留（下沉为子标签）
- `Task Logs`：本地 JSONL 任务日志
- `Settings`：配置中心
- `Capabilities`：能力矩阵（supported/partial/unsupported）

## UX 收口点（本轮）
- `Simple + Advanced` 模式切换（默认 `Simple`）
- 双语文案（中文主文案 + 英文术语括注）
- 顶部任务状态条：状态 + 耗时 + 最近事件
- 结果区新增同步图表预览：请求成功后自动与表格同步刷新（无需手动切换）
- 图表支持多数值类型并列展示（多 series），避免只显示单一指标
- 图表不再限制固定条数，数据量大时可横向滚动查看全部
- 统一视觉主题与间距优化：卡片、按钮、标签页与输入控件风格一致
- 结果区统一为 5 标签：
  - 数据 JSON
  - 表格
  - 请求上下文
  - 告警
  - 诊断
- 失败诊断标准化：
  - 发生了什么 (What happened)
  - 可怎么做 (What you can do)
- `Simple` 模式下隐藏参数会“可见提示 + 自动剔除 + JSONL 落日志”。

## 启动
1. 安装依赖
   - `pip install -r requirements.txt`
2. 运行
   - `python main.py`

可选参数：
- `python main.py --api-base http://127.0.0.1:8000`
- `python main.py --open-task-id <task_id>`

## 配置
保存于 `config/settings.json`：
- `api_base`
- `api_key`
- `request_timeout`
- `default_export_strategy`
- `log_tail_default`
- `ui_mode` (`simple|advanced`)
- `ui_language` (`zh_en|zh|en`)
- `quick_start_default` (`true|false`)

## 测试
- `python -m unittest discover -s tests -p "test_*.py" -v`

## 首次提交前预检
- `powershell -ExecutionPolicy Bypass -File scripts/precommit_check.ps1`
- 如只做快速检查可跳过测试：
  - `powershell -ExecutionPolicy Bypass -File scripts/precommit_check.ps1 -SkipTests`

## 打包
- `powershell -ExecutionPolicy Bypass -File scripts/build_windows.ps1`

## 文档
- [docs/API_CONTRACT.md](docs/API_CONTRACT.md)
- [docs/STATS_ALIGNMENT.md](docs/STATS_ALIGNMENT.md)
- [docs/STATS_ALIGNMENT_PROGRESS_2026-05-10.md](docs/STATS_ALIGNMENT_PROGRESS_2026-05-10.md)
- [docs/PACKAGING_WINDOWS.md](docs/PACKAGING_WINDOWS.md)
- [docs/EXCLUSIONS.md](docs/EXCLUSIONS.md)
- [docs/I18N.md](docs/I18N.md)
- [docs/FIRST_COMMIT_PREP.md](docs/FIRST_COMMIT_PREP.md)
