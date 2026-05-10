# First Commit Prep Checklist

目标：在第一次提交前，确保仓库内容可复现、可测试、边界清晰。

## 1) 基础卫生
- 确认 `.gitignore` 已覆盖：
  - 本地配置与日志（`config/settings.json`, `logs/tasks/*.jsonl`）
  - 构建产物（`build/`, `dist/`, `*.spec`, `*.exe`）
  - 缓存目录（`__pycache__/`, `.pytest_cache/`, `.mypy_cache/`）
- 确认不提交本地密钥文件（`.env*`）。

## 2) 预检命令
- 推荐一键执行：
  - `powershell -ExecutionPolicy Bypass -File scripts/precommit_check.ps1`
- 等价手动执行：
  - `python -m compileall -q main.py utils tests scripts`
  - `python -m unittest discover -s tests -p "test_*.py" -v`
  - `python scripts/verify_repo_docs.py`

## 3) 国际化检查
- `ui_language` 允许值：`zh_en | zh | en`。
- 新增交互文案应优先采用 `MainWindow._bi(zh, en)`。
- `docs/I18N.md` 与 `README.md` 保持一致。

## 4) 文档一致性
- `README.md`、`docs/EXCLUSIONS.md`、`docs/API_CONTRACT.md`、`docs/I18N.md` 内容互不冲突。
- 如更新范围或非目标，请同步 `docs/EXCLUSIONS.md`。

## 5) 提交前核对
- `git status --short` 输出符合预期。
- 不包含临时调试文件、运行时日志、导出结果文件。
