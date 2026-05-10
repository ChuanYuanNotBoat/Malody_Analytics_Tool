# Stats Alignment Matrix

口径：以“关键统计一致”为准（行数 / 主键列 / 核心统计列一致），展示样式可不同。

## 主线能力
- `top`: supported
- `history`: supported
- `search`: supported
- `summary/stats`: supported
- `hot/recent`: supported（专用契约、有效参数/丢弃参数可观测）
- `quality`: supported
- `export`: supported（chart/top/history/song/profile）

## 治理能力
- `crawler status`: supported
- `db health/maintain/history`: supported
- `quality rules/check/report/job-by-id`: partial（job list 未提供后端接口）
- `local selector presets`: supported
- `deep repair`: unsupported（显式降级，建议 CLI）

## UX 对齐约束
- Quick Start 默认满足高频场景；Advanced 保留全量入口。
- 任一任务必须可判断：运行中 / 成功 / 失败 / 无数据。
- 结果区必须可复盘：请求上下文、warnings、诊断文本、任务 JSONL 日志。
