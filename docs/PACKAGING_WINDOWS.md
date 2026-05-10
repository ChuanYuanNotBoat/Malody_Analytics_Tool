# Windows Packaging (PyInstaller)

## One-click Build
- `powershell -ExecutionPolicy Bypass -File scripts/build_windows.ps1`

可选参数：
- `-PythonExe py`（使用 `py` 启动器）
- `-SkipTests`（跳过测试）

## Output
- 可执行文件：
  - `dist/MalodyAnalyticsDesktop/MalodyAnalyticsDesktop.exe`
- 临时构建目录：
  - `build/`

## Runtime Notes
- 首次运行会在程序目录生成：
  - `config/settings.json`
  - `logs/tasks/*.jsonl`
- 需要受保护接口时，在 Settings 面板填写 `API Key`。

## Troubleshooting
- 打包时提示缺少 PyInstaller：
  - `python -m pip install -U pyinstaller`
- 运行后 API 连接失败：
  - 检查 Settings 的 `api_base`。
  - 先点击 `Ping /health`。
- `401 Unauthorized`：
  - 检查 Settings 的 `api_key` 是否正确。
