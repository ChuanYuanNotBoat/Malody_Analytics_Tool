# API Contract (GUI-side)

本项目不新增后端 API，仅消费 `malody_api` 既有接口。

## Analysis
- `/health`
- `/analytics/*`
- `/players/*`
- `/charts/stats|summary|quality|hot|recent|search|export/charts`
- `/query/execute`
- `/query/tables/{table}/schema`

## Governance
- `/crawler/status`
- `/system/db/health`
- `/system/db/maintain`
- `/system/db/maintain/history`
- `/quality/rules`
- `/quality/check`
- `/quality/jobs/{job_id}`
- `/quality/report`

## Auth Injection
- Primary: `Authorization: Bearer <api_key>`
- Compatible: `X-API-Key: <api_key>`

## Task Log Contract
每次任务写入 `logs/tasks/<task_id>.jsonl`，字段：
- `timestamp`
- `task_id`
- `scope`
- `phase`
- `message`
- `progress`
- `extra`

`extra` 关键字段：
- `effective_params`
- `dropped_params`
- `contract_warnings`
- `endpoint`
- `error_kind`
- `retryable`

## Simple Mode Behavior Contract
- Quick Start 执行时隐藏参数若存在值：
  - 必须可见提示
  - 必须从请求剔除
  - 必须写入 `dropped_params` 与 `contract_warnings`
- warning 统一使用 `reason=simple_mode_hidden_param`。
