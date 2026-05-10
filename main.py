import argparse
import csv
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import httpx
from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from utils.i18n import SUPPORTED_UI_LANGUAGES, bi_text, normalize_ui_language


def app_base_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def default_app_settings(fallback_api_base: str = "http://127.0.0.1:8000") -> Dict[str, Any]:
    return {
        "api_base": (fallback_api_base or "http://127.0.0.1:8000").rstrip("/"),
        "api_key": "",
        "request_timeout": 20.0,
        "default_export_strategy": "tolerant",
        "log_tail_default": 200,
        "ui_mode": "simple",
        "ui_language": "zh_en",
        "quick_start_default": True,
        "filter_presets": {},
    }


def settings_path(base_dir: Optional[str] = None) -> str:
    root = base_dir or app_base_dir()
    return os.path.join(root, "config", "settings.json")


def _normalize_int(value: Any, default_value: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default_value
    return max(min_value, min(max_value, parsed))


def normalize_settings(raw: Optional[Dict[str, Any]], fallback_api_base: str) -> Dict[str, Any]:
    defaults = default_app_settings(fallback_api_base)
    payload = dict(defaults)
    source = raw if isinstance(raw, dict) else {}
    api_base = str(source.get("api_base") or defaults["api_base"]).strip().rstrip("/")
    if api_base:
        payload["api_base"] = api_base
    payload["api_key"] = str(source.get("api_key") or "").strip()

    timeout_value = source.get("request_timeout", defaults["request_timeout"])
    try:
        timeout_f = float(timeout_value)
    except Exception:
        timeout_f = float(defaults["request_timeout"])
    if timeout_f < 2.0:
        timeout_f = 2.0
    if timeout_f > 600.0:
        timeout_f = 600.0
    payload["request_timeout"] = timeout_f

    strategy = str(source.get("default_export_strategy") or defaults["default_export_strategy"]).strip().lower()
    if strategy not in {"tolerant", "strict"}:
        strategy = "tolerant"
    payload["default_export_strategy"] = strategy

    payload["log_tail_default"] = _normalize_int(source.get("log_tail_default"), defaults["log_tail_default"], 20, 5000)
    ui_mode = str(source.get("ui_mode") or defaults["ui_mode"]).strip().lower()
    if ui_mode not in {"simple", "advanced"}:
        ui_mode = defaults["ui_mode"]
    payload["ui_mode"] = ui_mode

    payload["ui_language"] = normalize_ui_language(source.get("ui_language"), defaults["ui_language"])

    payload["quick_start_default"] = bool(source.get("quick_start_default", defaults["quick_start_default"]))
    presets = source.get("filter_presets")
    payload["filter_presets"] = presets if isinstance(presets, dict) else {}
    return payload


def load_settings(base_dir: Optional[str], fallback_api_base: str) -> Dict[str, Any]:
    path = settings_path(base_dir)
    if not os.path.exists(path):
        return normalize_settings(None, fallback_api_base)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return normalize_settings(None, fallback_api_base)
    return normalize_settings(raw, fallback_api_base)


def save_settings(base_dir: Optional[str], settings: Dict[str, Any], fallback_api_base: str) -> Dict[str, Any]:
    normalized = normalize_settings(settings, fallback_api_base)
    path = settings_path(base_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
    return normalized


def build_auth_headers(api_key: str) -> Dict[str, str]:
    key = (api_key or "").strip()
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}", "X-API-Key": key}


def build_http_timeout(seconds: float) -> httpx.Timeout:
    value = max(2.0, min(600.0, float(seconds)))
    connect_timeout = max(1.0, min(12.0, value / 2.0))
    return httpx.Timeout(value, connect=connect_timeout)


def now_iso() -> str:
    return datetime.now().isoformat()


def make_diag(
    kind: str,
    summary: str,
    detail: str = "",
    *,
    http_status: Optional[int] = None,
    retryable: bool = False,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "kind": kind,
        "summary": summary,
        "detail": detail,
        "http_status": http_status,
        "retryable": retryable,
        "extra": extra or {},
    }


def classify_httpx_error(exc: Exception) -> Dict[str, Any]:
    if isinstance(exc, httpx.TimeoutException):
        return make_diag("timeout", "Request timed out", str(exc), retryable=True)
    if isinstance(exc, httpx.ConnectError):
        return make_diag("connection_failed", "Cannot connect to API", str(exc), retryable=True)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else None
        return make_diag(
            "http_error",
            f"HTTP request failed ({status})" if status is not None else "HTTP request failed",
            str(exc),
            http_status=status,
            retryable=status is not None and status >= 500,
        )
    if isinstance(exc, httpx.RequestError):
        return make_diag("request_error", "Network request failed", str(exc), retryable=True)
    return make_diag("unknown_error", "Unexpected request failure", str(exc))


def detect_api_base(preferred: Optional[str] = None) -> str:
    candidates = []
    if preferred:
        candidates.append(preferred.strip())
    env_base = os.getenv("MALODY_API_BASE", "").strip()
    if env_base:
        candidates.append(env_base)
    candidates.extend(["http://127.0.0.1:8000", "http://127.0.0.1:18765"])

    seen = set()
    uniq = []
    for item in candidates:
        if not item or item in seen:
            continue
        seen.add(item)
        uniq.append(item)

    timeout = httpx.Timeout(1.5, connect=1.0)
    for base in uniq:
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.get(f"{base.rstrip('/')}/health")
            if resp.status_code == 200:
                return base.rstrip("/")
        except Exception:
            continue
    return (preferred or "http://127.0.0.1:8000").rstrip("/")


class TaskLog:
    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = base_dir or os.getcwd()
        self.logs_dir = os.path.join(self.base_dir, "logs", "tasks")
        os.makedirs(self.logs_dir, exist_ok=True)
        self._task_files: Dict[str, str] = {}

    def create_task(self, scope: str, message: str) -> str:
        task_id = uuid.uuid4().hex[:12]
        filepath = os.path.join(self.logs_dir, f"{task_id}.jsonl")
        self._task_files[task_id] = filepath
        self.append(task_id, scope=scope, phase="queued", message=message, progress=0, extra={})
        return task_id

    def append(
        self,
        task_id: str,
        *,
        scope: str,
        phase: str,
        message: str,
        progress: Optional[int],
        extra: Dict[str, Any],
    ) -> None:
        filepath = self._task_files.get(task_id) or os.path.join(self.logs_dir, f"{task_id}.jsonl")
        self._task_files[task_id] = filepath
        event = {
            "timestamp": now_iso(),
            "task_id": task_id,
            "scope": scope,
            "phase": phase,
            "message": message,
            "progress": progress,
            "extra": extra,
        }
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read(self, task_id: str, tail: int = 200) -> str:
        filepath = self._task_files.get(task_id) or os.path.join(self.logs_dir, f"{task_id}.jsonl")
        if not os.path.exists(filepath):
            return ""
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-tail:]
        return "".join(lines)


class ApiWorker(QThread):
    progress = Signal(int, str)
    finished_ok = Signal(object)
    finished_err = Signal(object)

    def __init__(
        self,
        api_base: str,
        endpoint: str,
        params: Dict[str, Any],
        method: str = "GET",
        body: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        request_timeout: float = 20.0,
    ):
        super().__init__()
        self.api_base = api_base.rstrip("/")
        self.endpoint = endpoint
        self.params = params
        self.method = method
        self.body = body
        self.headers = headers or {}
        self.request_timeout = request_timeout
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        self.progress.emit(10, "request started")
        try:
            timeout = build_http_timeout(self.request_timeout)
            with httpx.Client(timeout=timeout, headers=self.headers) as client:
                if self._cancelled:
                    self.finished_err.emit(make_diag("cancelled", "Task cancelled", "Cancelled by user", retryable=True))
                    return
                if self.method == "POST":
                    request_kwargs: Dict[str, Any] = {"params": self.params or None}
                    if self.body is not None:
                        request_kwargs["json"] = self.body
                    resp = client.post(f"{self.api_base}{self.endpoint}", **request_kwargs)
                else:
                    resp = client.get(f"{self.api_base}{self.endpoint}", params=self.params)
                self.progress.emit(80, f"http {resp.status_code}")
                resp.raise_for_status()
                payload = resp.json()
                if isinstance(payload, dict) and "success" in payload:
                    if not payload.get("success", False):
                        detail = str(payload.get("error") or payload.get("message") or "request failed")
                        self.finished_err.emit(make_diag("business_error", "API returned success=false", detail))
                        return
                    data = payload.get("data")
                    if data is None:
                        data = {}
                else:
                    # Accept plain JSON responses such as /health {"status":"healthy"}
                    data = payload
                self.progress.emit(100, "completed")
                self.finished_ok.emit(data)
        except Exception as exc:
            self.finished_err.emit(classify_httpx_error(exc))


class DownloadWorker(QThread):
    progress = Signal(int, str)
    finished_ok = Signal(str)
    finished_err = Signal(object)

    def __init__(
        self,
        api_base: str,
        endpoint: str,
        params: Dict[str, Any],
        output_file: str,
        headers: Optional[Dict[str, str]] = None,
        request_timeout: float = 60.0,
    ):
        super().__init__()
        self.api_base = api_base.rstrip("/")
        self.endpoint = endpoint
        self.params = params
        self.output_file = output_file
        self.headers = headers or {}
        self.request_timeout = request_timeout
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        self.progress.emit(10, "download started")
        try:
            timeout = build_http_timeout(self.request_timeout)
            with httpx.Client(timeout=timeout, headers=self.headers) as client:
                if self._cancelled:
                    self.finished_err.emit(make_diag("cancelled", "Export cancelled", "Cancelled by user", retryable=True))
                    return
                with client.stream("GET", f"{self.api_base}{self.endpoint}", params=self.params) as resp:
                    resp.raise_for_status()
                    self.progress.emit(40, f"http {resp.status_code}")
                    total = int(resp.headers.get("content-length") or 0)
                    written = 0
                    with open(self.output_file, "wb") as f:
                        for chunk in resp.iter_bytes():
                            if self._cancelled:
                                self.finished_err.emit(make_diag("cancelled", "Export cancelled", "Cancelled by user", retryable=True))
                                return
                            if not chunk:
                                continue
                            f.write(chunk)
                            written += len(chunk)
                            if total > 0:
                                pct = min(99, 40 + int((written / total) * 55))
                                self.progress.emit(pct, f"downloading {written}/{total} bytes")
                self.progress.emit(100, "download completed")
                self.finished_ok.emit(self.output_file)
        except Exception as exc:
            self.finished_err.emit(classify_httpx_error(exc))


class QueryExportWorker(QThread):
    progress = Signal(int, str)
    event = Signal(object)
    finished_ok = Signal(str)
    finished_err = Signal(object)

    def __init__(
        self,
        api_base: str,
        export_type: str,
        params: Dict[str, Any],
        output_file: str,
        output_format: str,
        export_strategy: str = "tolerant",
        headers: Optional[Dict[str, str]] = None,
        request_timeout: float = 60.0,
    ):
        super().__init__()
        self.api_base = api_base.rstrip("/")
        self.export_type = export_type
        self.params = params
        self.output_file = output_file
        self.output_format = output_format
        self.export_strategy = (export_strategy or "tolerant").strip().lower()
        if self.export_strategy not in {"tolerant", "strict"}:
            self.export_strategy = "tolerant"
        self.headers = headers or {}
        self.request_timeout = request_timeout
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @staticmethod
    def _split_csv_values(raw: str) -> list[str]:
        return [x.strip() for x in (raw or "").split(",") if x and x.strip()]

    @staticmethod
    def _parse_time_range_filter(raw: str) -> Optional[list[str]]:
        value = (raw or "").strip()
        if not value:
            return None
        for sep in ("..", ",", "~"):
            if sep in value:
                parts = [x.strip() for x in value.split(sep, 1)]
                if len(parts) == 2 and parts[0] and parts[1]:
                    return parts
        # Support relative format like 30d/8w/6m/12h
        try:
            unit = value[-1].lower()
            count = int(value[:-1])
            now = datetime.now()
            if unit == "d":
                start = now - timedelta(days=count)
            elif unit == "w":
                start = now - timedelta(weeks=count)
            elif unit == "m":
                start = now - timedelta(days=count * 30)
            elif unit == "h":
                start = now - timedelta(hours=count)
            else:
                return None
            return [start.isoformat(), now.isoformat()]
        except Exception:
            return None

    @staticmethod
    def _build_query_payload(export_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
        mode = params.get("mode")
        limit = max(1, int(params.get("limit") or 100))
        players = QueryExportWorker._split_csv_values(str(params.get("player_name") or ""))
        statuses = QueryExportWorker._split_csv_values(str(params.get("statuses") or params.get("status") or ""))
        creators = QueryExportWorker._split_csv_values(str(params.get("creators") or params.get("creator") or ""))
        time_range = QueryExportWorker._parse_time_range_filter(str(params.get("time_range") or ""))

        if export_type == "top":
            filters = []
            if mode is not None:
                filters.append({"field": "mode", "operator": "=", "value": mode})
            if players:
                filters.append({"field": "name", "operator": "IN", "value": players})
            return {
                "table": "player_rankings",
                "columns": ["rank", "name", "lv", "exp", "acc", "combo", "pc", "crawl_time", "mode", "player_id", "uid"],
                "filters": filters,
                "order_by": ["crawl_time DESC", "rank"],
                "group_by": [],
                "having": [],
                "limit": limit,
                "offset": 0,
                "distinct": False,
            }

        if export_type == "history":
            filters = []
            if mode is not None:
                filters.append({"field": "mode", "operator": "=", "value": mode})
            if players:
                if len(players) == 1:
                    filters.append({"field": "name", "operator": "LIKE", "value": f"%{players[0]}%"})
                else:
                    filters.append({"field": "name", "operator": "IN", "value": players})
            if time_range:
                filters.append({"field": "crawl_time", "operator": "BETWEEN", "value": time_range})
            return {
                "table": "player_rankings",
                "columns": ["player_id", "name", "mode", "rank", "lv", "exp", "acc", "combo", "pc", "crawl_time", "uid"],
                "filters": filters,
                "order_by": ["crawl_time DESC"],
                "group_by": [],
                "having": [],
                "limit": limit,
                "offset": 0,
                "distinct": False,
            }

        if export_type == "song":
            return {
                "table": "songs",
                "columns": ["sid", "title", "artist"],
                "filters": [],
                "order_by": ["sid DESC"],
                "group_by": [],
                "having": [],
                "limit": limit,
                "offset": 0,
                "distinct": False,
            }

        if export_type == "profile":
            filters = []
            numeric_players = [p for p in players if p.isdigit()]
            if numeric_players:
                filters.append({"field": "uid", "operator": "IN", "value": [int(p) for p in numeric_players]})
            return {
                "table": "player_profiles",
                "columns": [
                    "player_id",
                    "uid",
                    "avatar_url",
                    "country",
                    "bio",
                    "join_date",
                    "last_crawled",
                    "needs_update",
                ],
                "filters": filters,
                "order_by": ["player_id DESC"],
                "group_by": [],
                "having": [],
                "limit": limit,
                "offset": 0,
                "distinct": False,
            }

        # chart
        filters = []
        if mode is not None:
            filters.append({"field": "mode", "operator": "=", "value": mode})
        if statuses:
            status_values: list[int] = []
            for raw in statuses:
                try:
                    status_values.append(int(raw))
                except Exception:
                    continue
            if status_values:
                if len(status_values) == 1:
                    filters.append({"field": "status", "operator": "=", "value": status_values[0]})
                else:
                    filters.append({"field": "status", "operator": "IN", "value": status_values})
        if creators:
            if len(creators) == 1:
                filters.append({"field": "creator_name", "operator": "LIKE", "value": f"%{creators[0]}%"})
            else:
                filters.append({"field": "creator_name", "operator": "IN", "value": creators})
        if time_range:
            try:
                filters.append({"field": "last_updated", "operator": "BETWEEN", "value": time_range})
            except Exception:
                pass
        return {
            "table": "charts",
            "columns": [
                "cid",
                "sid",
                "mode",
                "version",
                "level",
                "status",
                "creator_name",
                "stabled_by_name",
                "heat",
                "donate_count",
                "play_count",
                "last_updated",
            ],
            "filters": filters,
            "order_by": ["last_updated DESC"],
            "group_by": [],
            "having": [],
            "limit": limit,
            "offset": 0,
            "distinct": False,
        }

    @staticmethod
    def _column_specs(export_type: str) -> Dict[str, list[str]]:
        specs = {
            "chart": {
                "primary": [
                    "cid",
                    "sid",
                    "mode",
                    "version",
                    "level",
                    "status",
                    "creator_name",
                    "stabled_by_name",
                    "heat",
                    "donate_count",
                    "play_count",
                    "last_updated",
                ],
                "fallback": ["cid", "sid", "mode", "level", "status", "creator_name", "last_updated"],
            },
            "top": {
                "primary": ["rank", "name", "lv", "exp", "acc", "combo", "pc", "crawl_time", "mode", "player_id", "uid"],
                "fallback": ["rank", "name", "exp", "crawl_time", "mode"],
            },
            "history": {
                "primary": ["player_id", "name", "mode", "rank", "lv", "exp", "acc", "combo", "pc", "crawl_time", "uid"],
                "fallback": ["name", "mode", "rank", "exp", "crawl_time"],
            },
            "song": {
                "primary": ["sid", "title", "artist"],
                "fallback": ["sid", "title"],
            },
            "profile": {
                "primary": ["player_id", "uid", "avatar_url", "country", "bio", "join_date", "last_crawled", "needs_update"],
                "fallback": ["player_id", "uid", "last_crawled", "needs_update"],
            },
        }
        return specs.get(export_type, {"primary": [], "fallback": []})

    @staticmethod
    def _extract_missing_column(message: str) -> Optional[str]:
        text = message or ""
        m = re.search(r"no such column:\s*([a-zA-Z_][a-zA-Z0-9_]*)", text, flags=re.IGNORECASE)
        if not m:
            return None
        return m.group(1)

    def _emit_warning(self, message: str, **extra: Any) -> None:
        self.event.emit({"kind": "warning", "message": message, "extra": extra})

    def _fetch_schema_columns(self, client: httpx.Client, table: str) -> Optional[set[str]]:
        resp = client.get(f"{self.api_base}/query/tables/{table}/schema")
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(payload, dict) and "success" in payload and not payload.get("success", False):
            raise RuntimeError(str(payload.get("error") or payload.get("message") or "schema query failed"))
        if not isinstance(data, dict):
            return None
        columns = data.get("columns")
        if not isinstance(columns, list):
            return None
        names: set[str] = set()
        for item in columns:
            if isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name.strip():
                    names.add(name.strip())
        return names or None

    def _apply_schema_alignment(self, payload: Dict[str, Any], schema_cols: set[str]) -> Dict[str, Any]:
        current_cols = list(payload.get("columns", []))
        if not current_cols:
            return payload

        missing = [c for c in current_cols if c not in schema_cols]
        if not missing:
            return payload

        if self.export_strategy == "strict":
            raise RuntimeError(f"missing columns in table schema: {', '.join(missing)}")

        keep = [c for c in current_cols if c in schema_cols]
        specs = self._column_specs(self.export_type)
        for c in specs.get("fallback", []):
            if c in schema_cols and c not in keep:
                keep.append(c)
        if not keep:
            raise RuntimeError(f"no exportable columns remain after schema alignment: missing {', '.join(missing)}")
        payload = dict(payload)
        payload["columns"] = keep
        self._emit_warning(
            "column fallback enabled",
            strategy=self.export_strategy,
            missing_columns=missing,
            used_columns=keep,
            reason="schema_alignment",
        )
        return payload

    def _execute_query(self, client: httpx.Client, payload: Dict[str, Any]) -> list[dict[str, Any]]:
        query = httpx.QueryParams(
            [
                ("table", payload["table"]),
                *[("columns", c) for c in payload.get("columns", [])],
                *[("order_by", c) for c in payload.get("order_by", [])],
                *[("group_by", c) for c in payload.get("group_by", [])],
                ("limit", str(payload.get("limit", 100))),
                ("offset", str(payload.get("offset", 0))),
                ("distinct", str(bool(payload.get("distinct", False))).lower()),
            ]
        )
        body = {"filters": payload.get("filters") or None, "having": payload.get("having") or None}
        resp = client.post(f"{self.api_base}/query/execute", params=query, json=body)
        self.progress.emit(60, f"http {resp.status_code}")
        resp.raise_for_status()
        result = resp.json()
        if isinstance(result, dict) and "success" in result and not result.get("success", False):
            raise RuntimeError(str(result.get("error") or result.get("message") or "query failed"))
        rows = result.get("data") if isinstance(result, dict) else []
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise RuntimeError("unexpected query result shape")
        return rows

    def _classify_failure(self, exc: Exception) -> Dict[str, Any]:
        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError, httpx.RequestError)):
            return classify_httpx_error(exc)
        detail = str(exc)
        if "missing columns in table schema" in detail or "no such column" in detail.lower():
            return make_diag("schema_mismatch", "Export schema mismatch", detail, retryable=True)
        if "cancelled" in detail.lower():
            return make_diag("cancelled", "Export cancelled", detail, retryable=True)
        return make_diag("business_error", "Export query failed", detail)

    def _write_rows(self, rows: list[dict[str, Any]]) -> None:
        if self.output_format == "csv":
            headers: list[str] = []
            for row in rows:
                for key in row.keys():
                    if key not in headers:
                        headers.append(key)
            with open(self.output_file, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)
            return

        # xlsx
        try:
            from openpyxl import Workbook
        except Exception as exc:
            raise RuntimeError(f"openpyxl unavailable: {exc}")

        wb = Workbook()
        ws = wb.active
        ws.title = "export"
        headers: list[str] = []
        for row in rows:
            for key in row.keys():
                if key not in headers:
                    headers.append(key)
        ws.append(headers)
        for row in rows:
            ws.append([row.get(h, "") for h in headers])
        wb.save(self.output_file)

    def run(self) -> None:
        self.progress.emit(10, "building export query")
        try:
            payload = self._build_query_payload(self.export_type, self.params)
            if self._cancelled:
                self.finished_err.emit(make_diag("cancelled", "Export cancelled", "Cancelled by user", retryable=True))
                return

            self.progress.emit(25, "requesting /query/execute")
            timeout = build_http_timeout(self.request_timeout)
            with httpx.Client(timeout=timeout, headers=self.headers) as client:
                try:
                    schema_cols = self._fetch_schema_columns(client, payload["table"])
                except Exception as exc:
                    schema_cols = None
                    self._emit_warning(
                        "schema probe failed; executing with requested columns",
                        strategy=self.export_strategy,
                        table=payload["table"],
                        reason="schema_probe_failed",
                        detail=str(exc),
                    )

                if schema_cols:
                    payload = self._apply_schema_alignment(payload, schema_cols)

                max_tolerant_retries = 8
                retry_count = 0
                while True:
                    try:
                        rows = self._execute_query(client, payload)
                        break
                    except Exception as exc:
                        if self.export_strategy != "tolerant":
                            raise
                        missing_col = self._extract_missing_column(str(exc))
                        current_cols = list(payload.get("columns", []))
                        if not missing_col or missing_col not in current_cols:
                            raise
                        retry_count += 1
                        if retry_count > max_tolerant_retries:
                            raise RuntimeError("tolerant retry limit exceeded while removing missing columns")
                        next_cols = [c for c in current_cols if c != missing_col]
                        if not next_cols:
                            raise RuntimeError(f"no exportable columns remain after removing '{missing_col}'")
                        payload = dict(payload)
                        payload["columns"] = next_cols
                        self._emit_warning(
                            "removed missing column and retried query",
                            strategy=self.export_strategy,
                            removed_column=missing_col,
                            remaining_columns=next_cols,
                            retry_count=retry_count,
                            reason="runtime_column_retry",
                        )

                if self.export_type == "profile":
                    plain_players = self._split_csv_values(str(self.params.get("player_name") or ""))
                    if plain_players and not any(x.isdigit() for x in plain_players):
                        self._emit_warning(
                            "profile name filter requires uid for precise match",
                            strategy=self.export_strategy,
                            provided_players=plain_players,
                            reason="profile_filter_limit",
                        )
                if len(rows) == 0:
                    self._emit_warning(
                        "query returned empty dataset",
                        strategy=self.export_strategy,
                        export_type=self.export_type,
                        reason="empty_result",
                    )

            if self._cancelled:
                self.finished_err.emit(make_diag("cancelled", "Export cancelled", "Cancelled by user", retryable=True))
                return

            self.progress.emit(80, f"writing {len(rows)} rows")
            self._write_rows(rows)
            self.progress.emit(100, "export completed")
            self.finished_ok.emit(self.output_file)
        except Exception as exc:
            detail = str(exc)
            missing_col = self._extract_missing_column(detail)
            if self.export_strategy == "tolerant" and missing_col:
                self.finished_err.emit(
                    make_diag(
                        "schema_mismatch",
                        "Export schema mismatch after tolerance",
                        f"missing column '{missing_col}' still blocks query; adjust DB/API schema",
                        retryable=True,
                        extra={"missing_column": missing_col, "strategy": self.export_strategy},
                    )
                )
                return
            self.finished_err.emit(self._classify_failure(exc))


class InlineBarChart(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._title = "图表预览 (Chart Preview)"
        self._labels: list[str] = []
        self._series: Dict[str, list[Optional[float]]] = {}
        self._message = "等待查询结果 (Waiting for data)"
        self._colors = [
            QColor("#2F80ED"),
            QColor("#27AE60"),
            QColor("#F2994A"),
            QColor("#9B51E0"),
            QColor("#EB5757"),
            QColor("#56CCF2"),
            QColor("#6FCF97"),
        ]
        self.setMinimumHeight(230)
        self.setMinimumWidth(640)

    def clear_chart(self, message: str = "等待查询结果 (Waiting for data)") -> None:
        self._labels = []
        self._series = {}
        self._message = message
        self.setMinimumWidth(640)
        self.update()

    @staticmethod
    def _coerce_numeric(value: Any) -> Optional[float]:
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            text = value.strip().replace(",", "")
            if not text:
                return None
            try:
                return float(text)
            except Exception:
                return None
        return None

    def set_chart_data(self, title: str, labels: list[str], series: Dict[str, list[Any]]) -> None:
        cleaned_labels = [str(x or "-") for x in labels]
        if not cleaned_labels:
            self.clear_chart("暂无可视化数据 (No chartable numeric data)")
            return

        cleaned_series: Dict[str, list[Optional[float]]] = {}
        for name, values in (series or {}).items():
            if not isinstance(values, list):
                continue
            normalized: list[Optional[float]] = []
            for idx in range(len(cleaned_labels)):
                raw = values[idx] if idx < len(values) else None
                normalized.append(self._coerce_numeric(raw))
            if any(v is not None for v in normalized):
                cleaned_series[str(name)] = normalized

        self._title = title or "图表预览 (Chart Preview)"
        self._labels = cleaned_labels
        self._series = cleaned_series
        self._message = "" if self._series else "暂无可视化数据 (No chartable numeric data)"
        series_count = max(1, len(self._series))
        group_w = 12 * series_count + max(0, series_count - 1) * 3 + 10
        requested_width = 96 + len(self._labels) * group_w
        self.setMinimumWidth(max(640, requested_width))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        outer = self.rect().adjusted(8, 8, -8, -8)
        if outer.width() <= 0 or outer.height() <= 0:
            return

        painter.setPen(QPen(QColor("#D4DCE8")))
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawRoundedRect(outer, 10, 10)

        painter.setPen(QColor("#1F2937"))
        painter.drawText(outer.adjusted(12, 8, -12, -8), Qt.AlignLeft | Qt.AlignTop, self._title)

        if not self._series or not self._labels:
            painter.setPen(QColor("#6B7280"))
            painter.drawText(outer.adjusted(12, 30, -12, -12), Qt.AlignCenter, self._message)
            return

        chart_rect = outer.adjusted(48, 42, -14, -62)
        if chart_rect.width() <= 10 or chart_rect.height() <= 10:
            return

        max_value = 0.0
        for series_values in self._series.values():
            for value in series_values:
                if value is None:
                    continue
                max_value = max(max_value, float(value))
        if max_value <= 0:
            max_value = 1.0

        painter.setPen(QPen(QColor("#C8D3E3")))
        painter.drawLine(chart_rect.bottomLeft(), chart_rect.bottomRight())
        painter.drawLine(chart_rect.bottomLeft(), chart_rect.topLeft())

        series_items = list(self._series.items())
        point_count = len(self._labels)
        series_count = max(1, len(series_items))
        bar_width = 12
        inner_gap = 3
        group_gap = 10
        group_width = series_count * bar_width + max(0, series_count - 1) * inner_gap + group_gap
        label_color = QColor("#4B5563")
        value_color = QColor("#1E3A8A")
        max_label = 14
        for idx, label in enumerate(self._labels):
            group_x = chart_rect.left() + group_gap + idx * group_width
            for s_idx, (_series_name, values) in enumerate(series_items):
                value = values[idx] if idx < len(values) else None
                if value is None:
                    continue
                x = group_x + s_idx * (bar_width + inner_gap)
                ratio = max(0.0, float(value) / max_value)
                bar_h = int(chart_rect.height() * ratio)
                top = chart_rect.bottom() - bar_h
                painter.setPen(Qt.NoPen)
                painter.setBrush(self._colors[s_idx % len(self._colors)])
                painter.drawRoundedRect(x, top, bar_width, bar_h, 3, 3)

            if idx % 2 == 0 or point_count <= 18:
                painter.setPen(label_color)
                trimmed = label if len(label) <= max_label else f"{label[: max_label - 1]}…"
                painter.drawText(
                    group_x - 8,
                    chart_rect.bottom() + 6,
                    series_count * bar_width + max(0, series_count - 1) * inner_gap + 16,
                    24,
                    Qt.AlignHCenter | Qt.AlignTop,
                    trimmed,
                )

        painter.setPen(value_color)
        painter.drawText(chart_rect.left() - 42, chart_rect.top() - 4, 38, 16, Qt.AlignRight | Qt.AlignVCenter, f"{max_value:.1f}")
        painter.drawText(chart_rect.left() - 42, chart_rect.bottom() - 8, 38, 16, Qt.AlignRight | Qt.AlignVCenter, "0")

        legend_x = outer.left() + 12
        legend_y = outer.top() + 26
        for s_idx, (name, _values) in enumerate(series_items):
            row = s_idx // 3
            col = s_idx % 3
            x = legend_x + col * 180
            y = legend_y + row * 16
            painter.setPen(Qt.NoPen)
            painter.setBrush(self._colors[s_idx % len(self._colors)])
            painter.drawRoundedRect(x, y + 2, 10, 10, 2, 2)
            painter.setPen(QColor("#475569"))
            painter.drawText(x + 16, y, 160, 14, Qt.AlignLeft | Qt.AlignVCenter, name)

        painter.setPen(QColor("#6B7280"))
        painter.drawText(
            outer.adjusted(12, 8, -12, -8),
            Qt.AlignRight | Qt.AlignTop,
            f"series={len(series_items)} · rows={len(self._labels)}",
        )


class MainWindow(QMainWindow):
    def __init__(self, api_base: str, open_task_id: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("Malody Analytics Desktop")
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            window_w, window_h = self._calc_initial_window_size(available.width(), available.height())
            self.resize(window_w, window_h)
        else:
            self.resize(1200, 780)

        self.app_dir = app_base_dir()
        self.settings = load_settings(self.app_dir, api_base)
        self.api_base = detect_api_base(self.settings.get("api_base") or api_base)
        self.settings["api_base"] = self.api_base
        self.api_key = str(self.settings.get("api_key") or "").strip()
        self.request_timeout = float(self.settings.get("request_timeout", 20.0))
        self.log_tail_default = int(self.settings.get("log_tail_default", 200))
        self.ui_mode = str(self.settings.get("ui_mode", "simple")).strip().lower()
        self.ui_language = normalize_ui_language(self.settings.get("ui_language", "zh_en"))
        self.quick_start_default = bool(self.settings.get("quick_start_default", True))

        self.task_log = TaskLog(base_dir=self.app_dir)
        self.current_task_id: Optional[str] = open_task_id
        self.worker: Optional[ApiWorker] = None
        self.download_worker: Optional[DownloadWorker] = None
        self.query_export_worker: Optional[QueryExportWorker] = None
        self.last_request: Dict[str, Any] = {}
        self._active_task_started_at: Optional[float] = None
        self._pending_export_context: Optional[Dict[str, Any]] = None

        self.status_label = QLabel("就绪 (Ready)")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        self.cancel_btn = QPushButton("取消任务 (Cancel Task)")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_task)

        self.api_base_edit = QLineEdit(self.api_base)

        self.mode_compare_edit = QLineEdit("0,1,2,3")
        self.player_compare_edit = QLineEdit("alice,bob")
        self.player_mode_edit = QLineEdit("0")
        self.player_days_edit = QLineEdit("30")
        self.trend_mode_edit = QLineEdit("0")
        self.trend_period_combo = QComboBox()
        self.trend_period_combo.addItems(["days", "months"])
        self.top_limit_edit = QLineEdit("20")
        self.top_mode_edit = QLineEdit("0")
        self.top_rank_type_combo = QComboBox()
        self.top_rank_type_combo.addItems(["exp", "mm"])
        self.history_player_edit = QLineEdit("alice")
        self.history_days_edit = QLineEdit("30")
        self.history_mode_edit = QLineEdit("0")
        self.history_metric_combo = QComboBox()
        self.history_metric_combo.addItems(["exp_rank", "mm_rank", "mmr"])
        self.chart_mode_edit = QLineEdit("0")
        self.chart_creators_edit = QLineEdit("")
        self.chart_statuses_edit = QLineEdit("2")
        self.chart_time_range_edit = QLineEdit("")
        self.summary_detail_combo = QComboBox()
        self.summary_detail_combo.addItems(["basic", "detailed"])
        self.hot_mode_edit = QLineEdit("0")
        self.hot_limit_edit = QLineEdit("20")
        self.hot_sort_combo = QComboBox()
        self.hot_sort_combo.addItems(["heat", "donate_count", "play_count", "love_count"])
        self.recent_mode_edit = QLineEdit("0")
        self.recent_days_edit = QLineEdit("7")
        self.recent_limit_edit = QLineEdit("20")
        self.search_keyword_edit = QLineEdit("alice")
        self.search_limit_edit = QLineEdit("20")
        self.search_mode_edit = QLineEdit("0")
        self.export_game_mode_edit = QLineEdit("0")
        self.export_strategy_combo = QComboBox()
        self.export_strategy_combo.addItems(["tolerant", "strict"])
        self.export_creators_edit = QLineEdit("")
        self.export_statuses_edit = QLineEdit("2")
        self.export_player_edit = QLineEdit("")
        self.export_time_range_edit = QLineEdit("")
        self.export_limit_edit = QLineEdit("200")
        self.export_type_combo = QComboBox()
        self.export_type_combo.addItems(["chart", "top", "history", "song", "profile"])
        self.export_format_combo = QComboBox()
        self.export_format_combo.addItems(["csv", "xlsx"])
        self.export_strategy_combo.setCurrentText(str(self.settings.get("default_export_strategy", "tolerant")))

        self.preset_name_edit = QLineEdit("")
        self.preset_combo = QComboBox()

        self.quality_stale_hours_edit = QLineEdit("72")
        self.quality_async_check = QCheckBox("异步模式 (Async mode)")
        self.quality_rules_edit = QLineEdit("")
        self.quality_job_id_edit = QLineEdit("")
        self.db_action_combo = QComboBox()
        self.db_action_combo.addItems(["analyze", "vacuum"])
        self.db_confirm_check = QCheckBox("确认执行 (Confirm action)")
        self.db_dry_run_check = QCheckBox("仅演练 (Dry run)")
        self.db_history_limit_edit = QLineEdit("20")

        self.qs_summary_mode_edit = QLineEdit("0")
        self.qs_export_mode_edit = QLineEdit("0")
        self.qs_hot_limit_edit = QLineEdit("20")
        self.qs_recent_days_edit = QLineEdit("7")
        self.qs_recent_limit_edit = QLineEdit("20")
        self.qs_summary_detail_combo = QComboBox()
        self.qs_summary_detail_combo.addItems(["basic", "detailed"])
        self.qs_export_limit_edit = QLineEdit("200")
        self.qs_export_type_combo = QComboBox()
        self.qs_export_type_combo.addItems(["chart", "top", "history", "song", "profile"])
        self.qs_export_format_combo = QComboBox()
        self.qs_export_format_combo.addItems(["csv", "xlsx"])
        self.qs_mode_toggle_btn = QPushButton()

        self.settings_api_base_edit = QLineEdit(self.api_base)
        self.settings_api_key_edit = QLineEdit(self.api_key)
        self.settings_api_key_edit.setEchoMode(QLineEdit.Password)
        self.settings_timeout_edit = QLineEdit(str(self.request_timeout))
        self.settings_export_strategy_combo = QComboBox()
        self.settings_export_strategy_combo.addItems(["tolerant", "strict"])
        self.settings_export_strategy_combo.setCurrentText(str(self.settings.get("default_export_strategy", "tolerant")))
        self.settings_log_tail_edit = QLineEdit(str(self.log_tail_default))
        self.settings_ui_mode_combo = QComboBox()
        self.settings_ui_mode_combo.addItems(["simple", "advanced"])
        self.settings_ui_mode_combo.setCurrentText(self.ui_mode)
        self.settings_ui_language_combo = QComboBox()
        self.settings_ui_language_combo.addItems(list(SUPPORTED_UI_LANGUAGES))
        self.settings_ui_language_combo.setCurrentText(self.ui_language)
        self.settings_quick_start_check = QCheckBox("默认打开快速开始 (Use Quick Start as default)")
        self.settings_quick_start_check.setChecked(self.quick_start_default)
        self.log_tail_edit = QLineEdit(str(self.log_tail_default))
        self.capability_text = QTextEdit()
        self.capability_text.setReadOnly(True)

        self.result_tabs = QTabWidget()
        self.result_text = QTextEdit()
        self.result_text.setReadOnly(True)
        self.chart_widget = InlineBarChart()
        self.chart_scroll = QScrollArea()
        self.chart_scroll.setWidgetResizable(False)
        self.chart_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.chart_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.chart_scroll.setWidget(self.chart_widget)
        self.chart_scroll.setMinimumHeight(260)
        self.result_table = QTableWidget()
        self.result_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.result_table.setAlternatingRowColors(True)
        self.result_table.setSortingEnabled(True)
        self.result_table.horizontalHeader().setStretchLastSection(True)
        self.request_context_text = QTextEdit()
        self.request_context_text.setReadOnly(True)
        self.warning_text = QTextEdit()
        self.warning_text.setReadOnly(True)
        self.diagnostic_text = QTextEdit()
        self.diagnostic_text.setReadOnly(True)
        self.result_tabs.addTab(self.result_text, "数据 JSON (Data)")
        self.result_tabs.addTab(self.result_table, "表格 (Table)")
        self.result_tabs.addTab(self.request_context_text, "请求上下文 (Request)")
        self.result_tabs.addTab(self.warning_text, "告警 (Warnings)")
        self.result_tabs.addTab(self.diagnostic_text, "诊断 (Diagnostic)")
        self.table_filter_edit = QLineEdit("")
        self.table_filter_edit.setPlaceholderText("筛选表格行（包含关键词）(Filter table rows contains keyword)")
        self.table_filter_edit.textChanged.connect(self._apply_table_filter)
        clear_filter_btn = QPushButton("清除筛选 (Clear Filter)")
        clear_filter_btn.clicked.connect(lambda: self.table_filter_edit.setText(""))
        copy_rows_btn = QPushButton("复制选中行 (Copy Selected Rows)")
        copy_rows_btn.clicked.connect(self.copy_selected_rows)
        copy_diag_btn = QPushButton("复制诊断 (Copy Diagnostic)")
        copy_diag_btn.clicked.connect(self.copy_diagnostic)
        self.result_panel = QWidget()
        result_layout = QVBoxLayout(self.result_panel)
        result_layout.setContentsMargins(6, 6, 6, 6)
        result_layout.setSpacing(8)
        result_toolbar = QHBoxLayout()
        result_toolbar.setSpacing(8)
        result_toolbar.addWidget(QLabel("表格筛选 (Table Filter)"))
        result_toolbar.addWidget(self.table_filter_edit, 1)
        result_toolbar.addWidget(clear_filter_btn)
        result_toolbar.addWidget(copy_rows_btn)
        result_toolbar.addWidget(copy_diag_btn)
        result_layout.addLayout(result_toolbar)
        result_layout.addWidget(self.chart_scroll, 0)
        result_layout.addWidget(self.result_tabs, 1)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        top = QHBoxLayout()
        top.addWidget(QLabel("接口地址 (API Base)"))
        top.addWidget(self.api_base_edit, 1)
        copy_req_btn = QPushButton("复制最近请求 (Copy Last Request)")
        copy_req_btn.clicked.connect(self.copy_last_request)
        top.addWidget(copy_req_btn)
        ping_btn = QPushButton("健康检查 (Ping /health)")
        ping_btn.clicked.connect(self.ping_health)
        top.addWidget(ping_btn)
        self.top_mode_toggle_btn = QPushButton()
        self.top_mode_toggle_btn.clicked.connect(self.toggle_ui_mode)
        top.addWidget(self.top_mode_toggle_btn)
        layout.addLayout(top)

        self.main_tabs = QTabWidget()
        self.main_tabs.addTab(self._wrap_scroll_page(self._build_quick_start_tab()), "快速开始 (Quick Start)")
        self.advanced_tab = self._build_advanced_tab()
        self.main_tabs.addTab(self._wrap_scroll_page(self.advanced_tab), "高级功能 (Advanced)")
        self.main_tabs.addTab(self._build_logs_tab(), "任务日志 (Task Logs)")
        self.main_tabs.addTab(self._wrap_scroll_page(self._build_settings_tab()), "设置 (Settings)")
        self.main_tabs.addTab(self._build_capabilities_tab(), "能力矩阵 (Capabilities)")
        self.main_splitter = QSplitter(Qt.Vertical)
        self.main_splitter.addWidget(self.main_tabs)
        self.main_splitter.addWidget(self.result_panel)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 2)
        self.main_splitter.setSizes([620, 320])
        layout.addWidget(self.main_splitter, 1)

        footer = QHBoxLayout()
        footer.addWidget(self.status_label, 1)
        footer.addWidget(self.progress_bar, 2)
        footer.addWidget(self.cancel_btn)
        layout.addLayout(footer)

        self._apply_visual_theme()
        self._refresh_filter_preset_combo()
        self._refresh_capability_matrix()
        self._apply_ui_mode_widgets()
        if self.quick_start_default:
            self.main_tabs.setCurrentIndex(0)

        if open_task_id:
            self.log_text.setPlainText(self.task_log.read(open_task_id, tail=self.log_tail_default))

    def _build_quick_start_tab(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)

        title = QLabel("快速开始 (Quick Start)")
        subtitle = QLabel("默认仅展示高频必填参数；可一键展开高级功能。")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        self.qs_mode_toggle_btn.clicked.connect(self.toggle_ui_mode)
        layout.addWidget(self.qs_mode_toggle_btn)

        health_box = QGroupBox("1) 健康检查 (Health)")
        health_layout = QHBoxLayout(health_box)
        ping_btn = QPushButton("运行健康检查 (Ping /health)")
        ping_btn.clicked.connect(self.ping_health)
        health_layout.addWidget(ping_btn)
        layout.addWidget(health_box)

        hot_recent_box = QGroupBox("2) 热门/最近 (Hot/Recent)")
        hr_form = QFormLayout(hot_recent_box)
        hot_recent_mode = QLineEdit()
        hot_recent_mode.setText("0")
        self.qs_hot_mode_edit = hot_recent_mode
        hr_form.addRow("模式 (Mode)", self.qs_hot_mode_edit)
        hr_form.addRow("热门上限 (Hot Limit)", self.qs_hot_limit_edit)
        hr_form.addRow("最近天数 (Recent Days)", self.qs_recent_days_edit)
        hr_form.addRow("最近上限 (Recent Limit)", self.qs_recent_limit_edit)
        hot_btn = QPushButton("运行热门图谱 (Run Hot)")
        hot_btn.clicked.connect(self.run_hot_charts_simple)
        recent_btn = QPushButton("运行最近图谱 (Run Recent)")
        recent_btn.clicked.connect(self.run_recent_charts_simple)
        hr_form.addRow(hot_btn)
        hr_form.addRow(recent_btn)
        layout.addWidget(hot_recent_box)

        summary_box = QGroupBox("3) 统计摘要 (Summary)")
        summary_form = QFormLayout(summary_box)
        summary_form.addRow("模式 (Mode)", self.qs_summary_mode_edit)
        summary_form.addRow("摘要粒度 (Detail)", self.qs_summary_detail_combo)
        stats_btn = QPushButton("运行统计 (Run Stats)")
        stats_btn.clicked.connect(self.run_chart_stats_simple)
        summary_btn = QPushButton("运行摘要 (Run Summary)")
        summary_btn.clicked.connect(self.run_chart_summary_simple)
        summary_form.addRow(stats_btn)
        summary_form.addRow(summary_btn)
        layout.addWidget(summary_box)

        export_box = QGroupBox("4) 导出 (Export)")
        export_form = QFormLayout(export_box)
        export_form.addRow("模式 (Mode)", self.qs_export_mode_edit)
        export_form.addRow("类型 (Type)", self.qs_export_type_combo)
        export_form.addRow("格式 (Format)", self.qs_export_format_combo)
        export_form.addRow("上限 (Limit)", self.qs_export_limit_edit)
        export_btn = QPushButton("运行导出 (Run Export)")
        export_btn.clicked.connect(self.run_export_charts_simple)
        export_form.addRow(export_btn)
        layout.addWidget(export_box)

        layout.addStretch(1)
        return pane

    def _build_advanced_tab(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        self.advanced_hint_label = QLabel("高级功能区：完整参数、治理工具与完整任务入口。")
        layout.addWidget(self.advanced_hint_label)
        self.advanced_unlock_btn = QPushButton("展开高级功能 (Open Advanced)")
        self.advanced_unlock_btn.clicked.connect(self.enable_advanced_mode)
        layout.addWidget(self.advanced_unlock_btn)
        self.advanced_subtabs = QTabWidget()
        self.advanced_subtabs.addTab(self._wrap_scroll_page(self._build_analytics_tab()), "分析主线 (Analytics)")
        self.advanced_subtabs.addTab(self._wrap_scroll_page(self._build_search_export_tab()), "搜索导出 (Search & Export)")
        self.advanced_subtabs.addTab(self._wrap_scroll_page(self._build_governance_tab()), "治理工具 (Governance)")
        layout.addWidget(self.advanced_subtabs, 1)
        return pane

    def _build_analytics_tab(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        form = QFormLayout()
        run_mode_btn = QPushButton("运行模式对比 (Run Mode Comparison)")
        run_mode_btn.clicked.connect(self.run_mode_compare)
        run_player_btn = QPushButton("运行玩家对比 (Run Player Compare)")
        run_player_btn.clicked.connect(self.run_player_compare)
        run_trend_btn = QPushButton("运行图谱趋势 (Run Chart Trends)")
        run_trend_btn.clicked.connect(self.run_chart_trends)
        run_top_btn = QPushButton("运行玩家榜单 (Run Top Players)")
        run_top_btn.clicked.connect(self.run_top_players)
        run_history_btn = QPushButton("运行历史排行 (Run Player History)")
        run_history_btn.clicked.connect(self.run_player_history)
        run_chart_stats_btn = QPushButton("运行图谱统计 (Run Chart Stats)")
        run_chart_stats_btn.clicked.connect(self.run_chart_stats)
        run_chart_summary_btn = QPushButton("运行图谱摘要 (Run Chart Summary)")
        run_chart_summary_btn.clicked.connect(self.run_chart_summary)
        run_chart_quality_btn = QPushButton("运行质量检查 (Run Chart Quality)")
        run_chart_quality_btn.clicked.connect(self.run_chart_quality)
        run_hot_btn = QPushButton("运行热门图谱 (Run Hot)")
        run_hot_btn.clicked.connect(self.run_hot_charts)
        run_recent_btn = QPushButton("运行最近图谱 (Run Recent)")
        run_recent_btn.clicked.connect(self.run_recent_charts)

        form.addRow("模式列表 (Modes)", self.mode_compare_edit)
        form.addRow(run_mode_btn)
        form.addRow("玩家列表 (Players)", self.player_compare_edit)
        form.addRow("模式 (Mode)", self.player_mode_edit)
        form.addRow("天数 (Days)", self.player_days_edit)
        form.addRow(run_player_btn)
        form.addRow("趋势模式 (Trend Mode)", self.trend_mode_edit)
        form.addRow("趋势周期 (Trend Period)", self.trend_period_combo)
        form.addRow(run_trend_btn)
        form.addRow("榜单上限 (Top Limit)", self.top_limit_edit)
        form.addRow("榜单模式 (Top Mode)", self.top_mode_edit)
        form.addRow("榜单类型 (Rank Type)", self.top_rank_type_combo)
        form.addRow(run_top_btn)
        form.addRow("历史玩家 (History Player)", self.history_player_edit)
        form.addRow("历史天数 (History Days)", self.history_days_edit)
        form.addRow("历史模式 (History Mode)", self.history_mode_edit)
        form.addRow("历史指标 (History Metric)", self.history_metric_combo)
        form.addRow(run_history_btn)
        form.addRow("图谱模式 (Chart Mode)", self.chart_mode_edit)
        form.addRow("创作者 (Creators)", self.chart_creators_edit)
        form.addRow("状态 (Statuses)", self.chart_statuses_edit)
        form.addRow("时间范围 (TimeRange)", self.chart_time_range_edit)
        form.addRow("摘要粒度 (Detail)", self.summary_detail_combo)
        form.addRow(run_chart_stats_btn)
        form.addRow(run_chart_summary_btn)
        form.addRow(run_chart_quality_btn)
        form.addRow("热门模式 (Hot Mode)", self.hot_mode_edit)
        form.addRow("热门上限 (Hot Limit)", self.hot_limit_edit)
        form.addRow("热门排序 (Hot Sort)", self.hot_sort_combo)
        form.addRow(run_hot_btn)
        form.addRow("最近模式 (Recent Mode)", self.recent_mode_edit)
        form.addRow("最近天数 (Recent Days)", self.recent_days_edit)
        form.addRow("最近上限 (Recent Limit)", self.recent_limit_edit)
        form.addRow(run_recent_btn)
        layout.addLayout(form)
        layout.addStretch(1)
        return pane

    def _build_search_export_tab(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        form = QFormLayout()
        run_search_player_btn = QPushButton("搜索玩家 (Search Players)")
        run_search_player_btn.clicked.connect(self.run_search_players)
        run_search_chart_btn = QPushButton("搜索图谱 (Search Charts)")
        run_search_chart_btn.clicked.connect(self.run_search_charts)
        run_search_creator_btn = QPushButton("搜索作者 (Search Creators)")
        run_search_creator_btn.clicked.connect(self.run_search_creators)
        run_export_btn = QPushButton("导出数据 (Export)")
        run_export_btn.clicked.connect(self.run_export_charts)
        preset_save_btn = QPushButton("保存预设 (Save Preset)")
        preset_save_btn.clicked.connect(self.save_filter_preset)
        preset_apply_btn = QPushButton("应用预设 (Apply)")
        preset_apply_btn.clicked.connect(self.apply_filter_preset)
        preset_delete_btn = QPushButton("删除预设 (Delete)")
        preset_delete_btn.clicked.connect(self.delete_filter_preset)
        preset_bar = QWidget()
        preset_layout = QHBoxLayout(preset_bar)
        preset_layout.setContentsMargins(0, 0, 0, 0)
        preset_layout.addWidget(self.preset_combo, 1)
        preset_layout.addWidget(preset_apply_btn)
        preset_layout.addWidget(preset_delete_btn)
        preset_save_bar = QWidget()
        preset_save_layout = QHBoxLayout(preset_save_bar)
        preset_save_layout.setContentsMargins(0, 0, 0, 0)
        preset_save_layout.addWidget(self.preset_name_edit, 1)
        preset_save_layout.addWidget(preset_save_btn)

        form.addRow("搜索关键词 (Keyword)", self.search_keyword_edit)
        form.addRow("搜索上限 (Limit)", self.search_limit_edit)
        form.addRow("搜索模式 (Mode)", self.search_mode_edit)
        form.addRow(run_search_player_btn)
        form.addRow(run_search_chart_btn)
        form.addRow(run_search_creator_btn)
        form.addRow("预设名称 (Preset Name)", preset_save_bar)
        form.addRow("已存预设 (Saved Presets)", preset_bar)
        form.addRow("导出模式 (Export Mode)", self.export_game_mode_edit)
        form.addRow("导出策略 (Export Strategy)", self.export_strategy_combo)
        form.addRow("导出创作者 (Export Creators)", self.export_creators_edit)
        form.addRow("导出状态 (Export Statuses)", self.export_statuses_edit)
        form.addRow("导出玩家 (Export Player)", self.export_player_edit)
        form.addRow("导出时间范围 (Export TimeRange)", self.export_time_range_edit)
        form.addRow("导出上限 (Export Limit)", self.export_limit_edit)
        form.addRow("导出类型 (Export Type)", self.export_type_combo)
        form.addRow("导出格式 (Export Format)", self.export_format_combo)
        form.addRow(run_export_btn)
        layout.addLayout(form)
        layout.addStretch(1)
        return pane

    def _build_governance_tab(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        form = QFormLayout()

        crawler_status_btn = QPushButton("爬虫状态 (Crawler Status)")
        crawler_status_btn.clicked.connect(self.run_crawler_status)
        db_health_btn = QPushButton("数据库健康 (DB Health)")
        db_health_btn.clicked.connect(self.run_db_health)
        db_maintain_btn = QPushButton("数据库维护 (DB Maintain)")
        db_maintain_btn.clicked.connect(self.run_db_maintain)
        db_history_btn = QPushButton("维护历史 (Maintain History)")
        db_history_btn.clicked.connect(self.run_db_maintain_history)

        quality_rules_btn = QPushButton("质量规则 (Quality Rules)")
        quality_rules_btn.clicked.connect(self.run_quality_rules)
        quality_check_btn = QPushButton("质量检查 (Quality Check)")
        quality_check_btn.clicked.connect(self.run_quality_check)
        quality_report_btn = QPushButton("最新质量报告 (Latest Report)")
        quality_report_btn.clicked.connect(self.run_quality_report)
        quality_job_btn = QPushButton("查询任务 (Quality Job)")
        quality_job_btn.clicked.connect(self.run_quality_job)

        form.addRow(crawler_status_btn)
        form.addRow(db_health_btn)
        form.addRow("维护动作 (Action)", self.db_action_combo)
        form.addRow(self.db_confirm_check)
        form.addRow(self.db_dry_run_check)
        form.addRow(db_maintain_btn)
        form.addRow("历史上限 (History Limit)", self.db_history_limit_edit)
        form.addRow(db_history_btn)
        form.addRow(quality_rules_btn)
        form.addRow("陈旧阈值小时 (Stale Hours)", self.quality_stale_hours_edit)
        form.addRow(self.quality_async_check)
        form.addRow("选择规则 (CSV)", self.quality_rules_edit)
        form.addRow(quality_check_btn)
        form.addRow(quality_report_btn)
        form.addRow("任务ID (Job ID)", self.quality_job_id_edit)
        form.addRow(quality_job_btn)

        desc = QTextEdit()
        desc.setReadOnly(True)
        desc.setPlainText(
            "未支持或部分支持的动作会在能力矩阵中显式标注。\n"
            "Unsupported/partial actions are explicitly marked in Capabilities.\n"
            "- 深度修复动作目前不在 GUI 中提供。\n"
            "- For deep repair, use backend CLI/scripts."
        )
        layout.addLayout(form)
        layout.addWidget(desc, 1)
        return pane

    def _build_capabilities_tab(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.addWidget(self.capability_text, 1)
        return pane

    def _build_logs_tab(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("日志行数 (Tail Lines)"))
        toolbar.addWidget(self.log_tail_edit)
        apply_tail_btn = QPushButton("应用行数 (Apply Tail)")
        apply_tail_btn.clicked.connect(self.apply_log_tail_default)
        toolbar.addWidget(apply_tail_btn)
        refresh_btn = QPushButton("刷新当前任务日志 (Refresh Current Task Log)")
        refresh_btn.clicked.connect(self.refresh_task_log)
        layout.addLayout(toolbar)
        layout.addWidget(refresh_btn)
        layout.addWidget(self.log_text, 1)
        return pane

    def _build_settings_tab(self) -> QWidget:
        pane = QWidget()
        layout = QVBoxLayout(pane)
        form = QFormLayout()
        form.addRow("接口地址 (API Base)", self.settings_api_base_edit)
        form.addRow("接口密钥 (API Key)", self.settings_api_key_edit)
        form.addRow("请求超时秒数 (Request Timeout)", self.settings_timeout_edit)
        form.addRow("默认导出策略 (Export Strategy)", self.settings_export_strategy_combo)
        form.addRow("默认日志行数 (Log Tail)", self.settings_log_tail_edit)
        form.addRow("界面模式 (UI Mode)", self.settings_ui_mode_combo)
        form.addRow("界面语言 (UI Language)", self.settings_ui_language_combo)
        form.addRow("默认打开快速开始 (Quick Start Default)", self.settings_quick_start_check)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        save_btn = QPushButton("保存设置 (Save Settings)")
        save_btn.clicked.connect(self.save_app_settings)
        restore_btn = QPushButton("恢复已保存 (Restore Saved)")
        restore_btn.clicked.connect(self.restore_saved_settings)
        defaults_btn = QPushButton("恢复默认 (Load Defaults)")
        defaults_btn.clicked.connect(self.load_default_settings)
        buttons.addWidget(save_btn)
        buttons.addWidget(restore_btn)
        buttons.addWidget(defaults_btn)
        layout.addLayout(buttons)

        return pane

    @staticmethod
    def _capability_rows() -> list[Dict[str, str]]:
        return [
            {
                "family": "stats.top",
                "status": "supported",
                "gui": "Top Players",
                "endpoint": "/players/top",
                "fallback": "-",
            },
            {
                "family": "stats.history",
                "status": "supported",
                "gui": "Player History",
                "endpoint": "/players/{player}/history",
                "fallback": "-",
            },
            {
                "family": "stats.search",
                "status": "supported",
                "gui": "Search Players/Charts/Creators",
                "endpoint": "/players/search | /charts/search | /charts/creators/search",
                "fallback": "-",
            },
            {
                "family": "stats.summary",
                "status": "supported",
                "gui": "Chart Stats/Summary/Hot/Recent/Quality",
                "endpoint": "/charts/stats | /charts/summary | /charts/hot | /charts/recent | /charts/quality",
                "fallback": "-",
            },
            {
                "family": "stats.export",
                "status": "supported",
                "gui": "Export (chart/top/history/song/profile)",
                "endpoint": "/charts/export/charts | /query/execute",
                "fallback": "-",
            },
            {
                "family": "governance.crawl_status",
                "status": "supported",
                "gui": "Crawler Status",
                "endpoint": "/crawler/status",
                "fallback": "requires API key",
            },
            {
                "family": "governance.optimize",
                "status": "supported",
                "gui": "DB Health/Maintain/History",
                "endpoint": "/system/db/health | /system/db/maintain | /system/db/maintain/history",
                "fallback": "maintain/history require API key",
            },
            {
                "family": "governance.quality",
                "status": "partial",
                "gui": "Quality Rules/Check/Report/Job by ID",
                "endpoint": "/quality/rules | /quality/check | /quality/report | /quality/jobs/{job_id}",
                "fallback": "job list unsupported: query by job_id only",
            },
            {
                "family": "governance.select",
                "status": "supported",
                "gui": "Local filter presets save/load",
                "endpoint": "local settings.json",
                "fallback": "-",
            },
            {
                "family": "governance.repair.deep",
                "status": "unsupported",
                "gui": "Deep repair actions",
                "endpoint": "-",
                "fallback": "use CLI/backend scripts with task logs",
            },
        ]

    def _refresh_capability_matrix(self) -> None:
        rows = self._capability_rows()
        lines = ["能力映射矩阵 (CLI/Capability -> GUI Matrix)", ""]
        for row in rows:
            lines.append(
                f"[{row['status']}] {row['family']}\n"
                f"  GUI: {row['gui']}\n"
                f"  Endpoint: {row['endpoint']}\n"
                f"  Fallback: {row['fallback']}"
            )
        self.capability_text.setPlainText("\n\n".join(lines))

    def _bi(self, zh: str, en: str) -> str:
        return bi_text(zh, en, self.ui_language)

    @staticmethod
    def _calc_initial_window_size(available_width: int, available_height: int) -> tuple[int, int]:
        if available_width <= 0 or available_height <= 0:
            return 1200, 780
        width = int(available_width * 0.9)
        height = int(available_height * 0.88)
        width = max(900, min(1200, width))
        height = max(620, min(820, height))
        width = min(width, max(640, available_width - 16))
        height = min(height, max(500, available_height - 16))
        return width, height

    @staticmethod
    def _wrap_scroll_page(page: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        area.setWidget(page)
        return area

    def _apply_visual_theme(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #f3f6fb; }
            QTabWidget::pane { border: 1px solid #d5deeb; border-radius: 10px; background: #ffffff; top: -1px; }
            QTabBar::tab {
                background: #e7edf7; color: #334155; border: 1px solid #d5deeb;
                border-top-left-radius: 8px; border-top-right-radius: 8px; padding: 7px 12px; margin-right: 4px;
            }
            QTabBar::tab:selected { background: #ffffff; color: #1d4ed8; }
            QGroupBox {
                border: 1px solid #d6deea; border-radius: 10px; margin-top: 12px;
                padding: 10px; background: #ffffff; font-weight: 600;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #334155; }
            QLineEdit, QComboBox, QTextEdit, QTableWidget {
                border: 1px solid #cad5e5; border-radius: 8px; background: #ffffff; padding: 4px 6px;
            }
            QPushButton {
                border: none; border-radius: 8px; padding: 6px 12px; color: #ffffff; background: #2f6fd8;
            }
            QPushButton:hover { background: #3d7ee6; }
            QPushButton:disabled { background: #b4c7e8; color: #f5f9ff; }
            QLabel { color: #1f2937; }
            QProgressBar {
                border: 1px solid #cad5e5; border-radius: 7px; background: #ffffff; text-align: center;
            }
            QProgressBar::chunk { background: #2f80ed; border-radius: 6px; }
            """
        )

    def _task_state_label(self, state: str) -> str:
        mapping = {
            "ready": self._bi("就绪", "Ready"),
            "running": self._bi("运行中", "Running"),
            "succeeded": self._bi("成功", "Succeeded"),
            "failed": self._bi("失败", "Failed"),
            "no_data": self._bi("无数据", "No Data"),
            "warning": self._bi("告警", "Warning"),
            "cancelled": self._bi("已取消", "Cancelled"),
        }
        return mapping.get(state, self._bi("状态未知", "Unknown"))

    def _set_task_status(self, state: str, recent_event: str) -> None:
        elapsed = 0.0
        if self._active_task_started_at is not None:
            elapsed = max(0.0, time.monotonic() - self._active_task_started_at)
        label = self._task_state_label(state)
        event_text = (recent_event or "").strip() or "-"
        self.status_label.setText(f"{label} | {elapsed:.1f}s | {event_text}")

    def _set_request_context_text(self, request_context: Optional[Dict[str, Any]]) -> None:
        if isinstance(request_context, dict) and request_context:
            self.request_context_text.setPlainText(json.dumps(request_context, ensure_ascii=False, indent=2))
            return
        self.request_context_text.setPlainText("")

    def _set_warning_text(self, warnings: Optional[list[Dict[str, Any]]]) -> None:
        if not warnings:
            self.warning_text.setPlainText("")
            return
        self.warning_text.setPlainText(json.dumps(warnings, ensure_ascii=False, indent=2))

    def _set_diagnostic_text(self, diag_text: str) -> None:
        self.diagnostic_text.setPlainText((diag_text or "").strip())

    def copy_diagnostic(self) -> None:
        text = self.diagnostic_text.toPlainText()
        if not text.strip():
            QMessageBox.information(self, "提示", "当前没有可复制的诊断信息。")
            return
        QApplication.clipboard().setText(text)
        self._set_task_status("ready", self._bi("诊断已复制", "Diagnostic copied"))

    def _apply_ui_mode_widgets(self) -> None:
        is_advanced = self.ui_mode == "advanced"
        self.advanced_subtabs.setVisible(is_advanced)
        self.advanced_unlock_btn.setVisible(not is_advanced)
        self.advanced_hint_label.setText(
            "已展开：可使用全部参数与治理入口。"
            if is_advanced
            else "当前为简洁模式：点击下方按钮展开高级功能。"
        )
        self.top_mode_toggle_btn.setText(
            "切换简洁模式 (Switch to Simple)" if is_advanced else "展开高级模式 (Open Advanced)"
        )
        self.qs_mode_toggle_btn.setText(
            "切换简洁模式 (Switch to Simple)" if is_advanced else "展开高级模式 (Open Advanced)"
        )
        self.settings_ui_mode_combo.setCurrentText("advanced" if is_advanced else "simple")

    def enable_advanced_mode(self) -> None:
        self.ui_mode = "advanced"
        self.settings["ui_mode"] = self.ui_mode
        self._apply_ui_mode_widgets()
        self.main_tabs.setCurrentIndex(1)

    def toggle_ui_mode(self) -> None:
        switching_to_advanced = self.ui_mode != "advanced"
        self.ui_mode = "simple" if self.ui_mode == "advanced" else "advanced"
        self.settings["ui_mode"] = self.ui_mode
        self._apply_ui_mode_widgets()
        if switching_to_advanced:
            self.main_tabs.setCurrentIndex(1)
        else:
            self.main_tabs.setCurrentIndex(0)

    def _collect_simple_dropped(
        self,
        *,
        include_creators: bool = True,
        include_statuses: bool = True,
        include_time_range: bool = True,
        include_player: bool = False,
        filter_source: str = "chart",
    ) -> tuple[Dict[str, Any], list[Dict[str, Any]]]:
        dropped: Dict[str, Any] = {}
        warnings: list[Dict[str, Any]] = []
        source = (filter_source or "chart").strip().lower()
        if source not in {"chart", "export"}:
            source = "chart"
        creators_widget = self.export_creators_edit if source == "export" else self.chart_creators_edit
        statuses_widget = self.export_statuses_edit if source == "export" else self.chart_statuses_edit
        time_range_widget = self.export_time_range_edit if source == "export" else self.chart_time_range_edit
        if include_creators:
            creators = (creators_widget.text() or "").strip()
            if creators:
                dropped["creators"] = creators
                warnings.append(
                    {
                        "reason": "simple_mode_hidden_param",
                        "field": "creators",
                        "message": "creators hidden in Simple mode and ignored for this quick action",
                    }
                )
        if include_statuses:
            statuses = (statuses_widget.text() or "").strip()
            if statuses:
                dropped["statuses"] = statuses
                warnings.append(
                    {
                        "reason": "simple_mode_hidden_param",
                        "field": "statuses",
                        "message": "statuses hidden in Simple mode and ignored for this quick action",
                    }
                )
        if include_time_range:
            time_range = (time_range_widget.text() or "").strip()
            if time_range:
                dropped["time_range"] = time_range
                warnings.append(
                    {
                        "reason": "simple_mode_hidden_param",
                        "field": "time_range",
                        "message": "time_range hidden in Simple mode and ignored for this quick action",
                    }
                )
        if include_player:
            player = (self.export_player_edit.text() or "").strip()
            if player:
                dropped["player_name"] = player
                warnings.append(
                    {
                        "reason": "simple_mode_hidden_param",
                        "field": "player_name",
                        "message": "player_name hidden in Simple mode and ignored for this quick action",
                    }
                )
        return dropped, warnings

    def run_hot_charts_simple(self) -> None:
        self.hot_mode_edit.setText((self.qs_hot_mode_edit.text() or "").strip() or "0")
        self.hot_limit_edit.setText((self.qs_hot_limit_edit.text() or "").strip() or "20")
        params, context, diag = self._build_hot_request()
        if diag:
            self._fail_preflight("stats", "/charts/hot", {}, diag)
            return
        dropped, warnings = self._collect_simple_dropped(include_creators=True, include_statuses=True, include_time_range=True)
        if isinstance(params, dict):
            params.pop("creators", None)
            params.pop("statuses", None)
        context["dropped_params"] = {**context.get("dropped_params", {}), **dropped}
        context["contract_warnings"] = [*(context.get("contract_warnings") or []), *warnings]
        self._start_worker("stats", "/charts/hot", params or {}, method="GET", request_context=context)

    def run_recent_charts_simple(self) -> None:
        self.recent_mode_edit.setText((self.qs_hot_mode_edit.text() or "").strip() or "0")
        self.recent_days_edit.setText((self.qs_recent_days_edit.text() or "").strip() or "7")
        self.recent_limit_edit.setText((self.qs_recent_limit_edit.text() or "").strip() or "20")
        params, context, diag = self._build_recent_request()
        if diag:
            self._fail_preflight("stats", "/charts/recent", {}, diag)
            return
        dropped, warnings = self._collect_simple_dropped(include_creators=True, include_statuses=True, include_time_range=True)
        if isinstance(params, dict):
            params.pop("creators", None)
            params.pop("statuses", None)
        context["dropped_params"] = {**context.get("dropped_params", {}), **dropped}
        context["contract_warnings"] = [*(context.get("contract_warnings") or []), *warnings]
        self._start_worker("stats", "/charts/recent", params or {}, method="GET", request_context=context)

    def run_chart_stats_simple(self) -> None:
        self.chart_mode_edit.setText((self.qs_summary_mode_edit.text() or "").strip() or "0")
        params, diag = self._build_chart_common_params()
        if diag:
            self._fail_preflight("stats", "/charts/stats", {}, diag)
            return
        dropped, warnings = self._collect_simple_dropped(include_creators=True, include_statuses=True, include_time_range=True)
        params.pop("creators", None)
        params.pop("statuses", None)
        params.pop("time_range", None)
        self._run_with_context(
            "stats",
            "/charts/stats",
            params,
            method="GET",
            dropped_params=dropped,
            contract_warnings=warnings,
        )

    def run_chart_summary_simple(self) -> None:
        self.chart_mode_edit.setText((self.qs_summary_mode_edit.text() or "").strip() or "0")
        self.summary_detail_combo.setCurrentText((self.qs_summary_detail_combo.currentText() or "basic").strip())
        params, diag = self._build_chart_common_params()
        if diag:
            self._fail_preflight("stats", "/charts/summary", {}, diag)
            return
        dropped, warnings = self._collect_simple_dropped(include_creators=True, include_statuses=True, include_time_range=True)
        params.pop("creators", None)
        params.pop("statuses", None)
        params.pop("time_range", None)
        params["detail_level"] = self.summary_detail_combo.currentText()
        self._run_with_context(
            "stats",
            "/charts/summary",
            params,
            method="GET",
            dropped_params=dropped,
            contract_warnings=warnings,
        )

    def run_export_charts_simple(self) -> None:
        self.export_game_mode_edit.setText((self.qs_export_mode_edit.text() or "").strip() or "0")
        self.export_type_combo.setCurrentText((self.qs_export_type_combo.currentText() or "chart").strip())
        self.export_format_combo.setCurrentText((self.qs_export_format_combo.currentText() or "csv").strip())
        self.export_limit_edit.setText((self.qs_export_limit_edit.text() or "").strip() or "200")
        dropped, warnings = self._collect_simple_dropped(
            include_creators=True,
            include_statuses=True,
            include_time_range=True,
            include_player=True,
            filter_source="export",
        )
        self._pending_export_context = {"dropped_params": dropped, "contract_warnings": warnings}
        self.run_export_charts()

    def _current_filter_preset(self) -> Dict[str, str]:
        return {
            "mode": (self.chart_mode_edit.text() or "").strip(),
            "creators": (self.chart_creators_edit.text() or "").strip(),
            "statuses": (self.chart_statuses_edit.text() or "").strip(),
            "player": (self.export_player_edit.text() or "").strip(),
            "time_range": (self.chart_time_range_edit.text() or "").strip(),
        }

    def _apply_filter_preset(self, preset: Dict[str, Any]) -> None:
        mode = str(preset.get("mode") or "").strip()
        creators = str(preset.get("creators") or "").strip()
        statuses = str(preset.get("statuses") or "").strip()
        player = str(preset.get("player") or "").strip()
        time_range = str(preset.get("time_range") or "").strip()

        if mode:
            for widget in [
                self.chart_mode_edit,
                self.hot_mode_edit,
                self.recent_mode_edit,
                self.export_game_mode_edit,
                self.search_mode_edit,
                self.top_mode_edit,
                self.trend_mode_edit,
                self.player_mode_edit,
                self.history_mode_edit,
            ]:
                widget.setText(mode)
        self.chart_creators_edit.setText(creators)
        self.export_creators_edit.setText(creators)
        self.chart_statuses_edit.setText(statuses)
        self.export_statuses_edit.setText(statuses)
        self.export_player_edit.setText(player)
        self.chart_time_range_edit.setText(time_range)
        self.export_time_range_edit.setText(time_range)

    def _refresh_filter_preset_combo(self) -> None:
        current = self.preset_combo.currentText().strip()
        self.preset_combo.clear()
        presets = self.settings.get("filter_presets")
        if not isinstance(presets, dict):
            presets = {}
        names = sorted([name for name in presets.keys() if str(name).strip()])
        self.preset_combo.addItems(names)
        if current and current in names:
            self.preset_combo.setCurrentText(current)

    def save_filter_preset(self) -> None:
        name = (self.preset_name_edit.text() or self.preset_combo.currentText() or "").strip()
        if not name:
            QMessageBox.warning(self, "输入错误", "预设名称不能为空。")
            return
        presets = self.settings.get("filter_presets")
        if not isinstance(presets, dict):
            presets = {}
        presets[name] = self._current_filter_preset()
        self.settings["filter_presets"] = presets
        self._refresh_filter_preset_combo()
        self.preset_combo.setCurrentText(name)
        self._set_task_status("ready", f"预设已保存 (Preset saved): {name}")

    def apply_filter_preset(self) -> None:
        name = (self.preset_combo.currentText() or "").strip()
        if not name:
            QMessageBox.information(self, "提示", "未选择预设。")
            return
        presets = self.settings.get("filter_presets")
        if not isinstance(presets, dict) or name not in presets:
            QMessageBox.warning(self, "未找到", f"未找到预设: {name}")
            return
        preset = presets.get(name)
        if not isinstance(preset, dict):
            QMessageBox.warning(self, "预设无效", f"预设数据格式无效: {name}")
            return
        self._apply_filter_preset(preset)
        self._set_task_status("ready", f"预设已应用 (Preset applied): {name}")

    def delete_filter_preset(self) -> None:
        name = (self.preset_combo.currentText() or "").strip()
        if not name:
            QMessageBox.information(self, "提示", "未选择预设。")
            return
        presets = self.settings.get("filter_presets")
        if not isinstance(presets, dict) or name not in presets:
            QMessageBox.warning(self, "未找到", f"未找到预设: {name}")
            return
        presets.pop(name, None)
        self.settings["filter_presets"] = presets
        self._refresh_filter_preset_combo()
        self._set_task_status("ready", f"预设已删除 (Preset deleted): {name}")

    def _apply_settings_to_widgets(self) -> None:
        self.api_base = str(self.settings.get("api_base") or self.api_base).rstrip("/")
        self.api_key = str(self.settings.get("api_key") or "").strip()
        self.request_timeout = float(self.settings.get("request_timeout", 20.0))
        self.log_tail_default = int(self.settings.get("log_tail_default", 200))
        self.ui_mode = str(self.settings.get("ui_mode", "simple")).strip().lower()
        self.ui_language = normalize_ui_language(self.settings.get("ui_language", "zh_en"))
        self.quick_start_default = bool(self.settings.get("quick_start_default", True))

        self.api_base_edit.setText(self.api_base)
        self.export_strategy_combo.setCurrentText(str(self.settings.get("default_export_strategy", "tolerant")))
        self.log_tail_edit.setText(str(self.log_tail_default))

        self.settings_api_base_edit.setText(self.api_base)
        self.settings_api_key_edit.setText(self.api_key)
        self.settings_timeout_edit.setText(str(self.request_timeout))
        self.settings_export_strategy_combo.setCurrentText(str(self.settings.get("default_export_strategy", "tolerant")))
        self.settings_log_tail_edit.setText(str(self.log_tail_default))
        self.settings_ui_mode_combo.setCurrentText(self.ui_mode if self.ui_mode in {"simple", "advanced"} else "simple")
        self.settings_ui_language_combo.setCurrentText(normalize_ui_language(self.ui_language))
        self.settings_quick_start_check.setChecked(self.quick_start_default)
        self._apply_ui_mode_widgets()

    def _read_settings_from_ui(self) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        api_base_value = (self.settings_api_base_edit.text() or "").strip().rstrip("/")
        timeout_raw = (self.settings_timeout_edit.text() or "").strip()
        log_tail_raw = (self.settings_log_tail_edit.text() or "").strip()
        ui_mode = (self.settings_ui_mode_combo.currentText() or "").strip().lower()
        ui_language = normalize_ui_language((self.settings_ui_language_combo.currentText() or "").strip().lower())

        if not api_base_value:
            return None, make_diag("input_error", "Invalid API base", "API Base cannot be empty", extra={"field": "api_base"})

        try:
            timeout_value = float(timeout_raw)
        except Exception:
            return None, make_diag(
                "input_error",
                "Invalid request timeout",
                f"request_timeout must be a number, got '{timeout_raw}'",
                extra={"field": "request_timeout", "value": timeout_raw},
            )
        if timeout_value < 2 or timeout_value > 600:
            return None, make_diag(
                "input_error",
                "Invalid request timeout",
                f"request_timeout must be in [2, 600], got {timeout_value}",
                extra={"field": "request_timeout", "value": timeout_value},
            )

        try:
            log_tail_value = int(log_tail_raw)
        except Exception:
            return None, make_diag(
                "input_error",
                "Invalid log tail",
                f"log_tail_default must be integer, got '{log_tail_raw}'",
                extra={"field": "log_tail_default", "value": log_tail_raw},
            )
        if log_tail_value < 20 or log_tail_value > 5000:
            return None, make_diag(
                "input_error",
                "Invalid log tail",
                f"log_tail_default must be in [20, 5000], got {log_tail_value}",
                extra={"field": "log_tail_default", "value": log_tail_value},
            )

        payload = dict(self.settings)
        payload["api_base"] = api_base_value
        payload["api_key"] = (self.settings_api_key_edit.text() or "").strip()
        payload["request_timeout"] = timeout_value
        payload["default_export_strategy"] = self.settings_export_strategy_combo.currentText().strip().lower() or "tolerant"
        payload["log_tail_default"] = log_tail_value
        payload["ui_mode"] = ui_mode if ui_mode in {"simple", "advanced"} else "simple"
        payload["ui_language"] = normalize_ui_language(ui_language)
        payload["quick_start_default"] = bool(self.settings_quick_start_check.isChecked())
        return payload, None

    def save_app_settings(self) -> None:
        payload, diag = self._read_settings_from_ui()
        if diag:
            QMessageBox.warning(self, "校验失败", self._diag_to_text(diag, "设置校验失败 (Settings Validation Failed)"))
            return
        self.settings = save_settings(self.app_dir, payload or {}, self.api_base)
        self._apply_settings_to_widgets()
        self.main_tabs.setCurrentIndex(0 if self.quick_start_default else 1)
        self._set_task_status("ready", "设置已保存 (Settings saved)")

    def restore_saved_settings(self) -> None:
        self.settings = load_settings(self.app_dir, self.api_base)
        self._apply_settings_to_widgets()
        self.main_tabs.setCurrentIndex(0 if self.quick_start_default else 1)
        self._set_task_status("ready", "设置已恢复 (Settings restored)")

    def load_default_settings(self) -> None:
        self.settings = default_app_settings(self.api_base)
        self._apply_settings_to_widgets()
        self.main_tabs.setCurrentIndex(0 if self.quick_start_default else 1)
        self._set_task_status("ready", "默认设置已加载 (Defaults loaded)")

    def apply_log_tail_default(self) -> None:
        raw = (self.log_tail_edit.text() or "").strip()
        try:
            tail = int(raw)
        except Exception:
            QMessageBox.warning(self, "输入错误", "日志行数必须是整数。")
            return
        if tail < 20 or tail > 5000:
            QMessageBox.warning(self, "输入错误", "日志行数必须在 [20, 5000]。")
            return
        self.log_tail_default = tail
        self.settings["log_tail_default"] = tail
        self.settings_log_tail_edit.setText(str(tail))
        self.refresh_task_log()

    def cancel_task(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self._set_task_status("running", "正在取消任务 (Cancelling)")
        if self.download_worker and self.download_worker.isRunning():
            self.download_worker.cancel()
            self._set_task_status("running", "正在取消下载 (Cancelling download)")
        if self.query_export_worker and self.query_export_worker.isRunning():
            self.query_export_worker.cancel()
            self._set_task_status("running", "正在取消导出 (Cancelling export)")

    def _resolve_api_base(self) -> str:
        entered = (self.api_base_edit.text() or "").strip()
        if entered:
            resolved = entered.rstrip("/")
            self.settings["api_base"] = resolved
            self.settings_api_base_edit.setText(resolved)
            return resolved
        detected = detect_api_base(None)
        self.api_base_edit.setText(detected)
        self.settings["api_base"] = detected
        self.settings_api_base_edit.setText(detected)
        return detected

    @staticmethod
    def _parse_csv(raw: str) -> list[str]:
        return [item.strip() for item in (raw or "").split(",") if item and item.strip()]

    def _api_headers(self) -> Dict[str, str]:
        key = (self.settings_api_key_edit.text() or self.api_key or "").strip()
        self.api_key = key
        self.settings["api_key"] = key
        return build_auth_headers(key)

    def _timeout_seconds(self) -> float:
        raw = (self.settings_timeout_edit.text() or "").strip()
        try:
            timeout = float(raw)
        except Exception:
            timeout = self.request_timeout
        timeout = max(2.0, min(600.0, timeout))
        self.request_timeout = timeout
        self.settings["request_timeout"] = timeout
        return timeout

    @staticmethod
    def _make_request_context(
        endpoint: str,
        effective_params: Dict[str, Any],
        dropped_params: Optional[Dict[str, Any]] = None,
        contract_warnings: Optional[list[Dict[str, Any]]] = None,
        expected_sort_field: Optional[str] = None,
    ) -> Dict[str, Any]:
        context: Dict[str, Any] = {
            "endpoint": endpoint,
            "effective_params": dict(effective_params),
            "dropped_params": dropped_params or {},
            "contract_warnings": contract_warnings or [],
        }
        if expected_sort_field:
            context["expected_sort_field"] = expected_sort_field
        return context

    @staticmethod
    def _parse_int_strict(
        text: str,
        field: str,
        *,
        default: Optional[int] = None,
        min_value: Optional[int] = None,
        max_value: Optional[int] = None,
        required: bool = False,
    ) -> tuple[Optional[int], Optional[Dict[str, Any]]]:
        raw = (text or "").strip()
        if not raw:
            if required and default is None:
                return None, make_diag("input_error", f"Missing {field}", f"{field} is required", extra={"field": field})
            value = default
        else:
            try:
                value = int(raw)
            except Exception:
                return None, make_diag(
                    "input_error",
                    f"Invalid {field}",
                    f"{field} must be integer, got '{raw}'",
                    extra={"field": field, "value": raw},
                )

        if value is None:
            return None, None
        if min_value is not None and value < min_value:
            return None, make_diag(
                "input_error",
                f"Invalid {field}",
                f"{field} must be >= {min_value}, got {value}",
                extra={"field": field, "value": value},
            )
        if max_value is not None and value > max_value:
            return None, make_diag(
                "input_error",
                f"Invalid {field}",
                f"{field} must be <= {max_value}, got {value}",
                extra={"field": field, "value": value},
            )
        return value, None

    def _start_worker(
        self,
        scope: str,
        endpoint: str,
        params: Dict[str, Any],
        method: str = "GET",
        body: Optional[Any] = None,
        request_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "Busy", "已有任务正在运行，请先取消后再发起新任务。")
            return

        request_context = request_context or {}
        self.api_base = self._resolve_api_base()
        headers = self._api_headers()
        timeout_seconds = self._timeout_seconds()
        self.last_request = {
            "api_base": self.api_base,
            "api_key_configured": bool(headers),
            "request_timeout": timeout_seconds,
            "scope": scope,
            "method": method,
            "endpoint": endpoint,
            "params": params,
            "body": body,
            "request_context": request_context,
        }
        task_id = self.task_log.create_task(scope, f"call {endpoint}")
        self.current_task_id = task_id
        self.task_log.append(
            task_id,
            scope=scope,
            phase="running",
            message="task started",
            progress=5,
            extra={
                "endpoint": endpoint,
                "method": method,
                "api_base": self.api_base,
                "api_key_configured": bool(headers),
                "request_timeout": timeout_seconds,
                "params": params,
                "body": body,
                "request_context": request_context,
            },
        )
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(5)
        self.cancel_btn.setEnabled(True)
        self._active_task_started_at = time.monotonic()
        self._set_task_status("running", f"{scope}: {endpoint}")
        self._set_request_context_text(request_context)
        self._set_warning_text([])
        self._set_diagnostic_text("")

        if request_context:
            self.task_log.append(
                task_id,
                scope=scope,
                phase="running",
                message="request contract prepared",
                progress=6,
                extra=request_context,
            )
            for warning in request_context.get("contract_warnings", []) if isinstance(request_context, dict) else []:
                if not isinstance(warning, dict):
                    continue
                message = str(warning.get("message") or "contract warning")
                self.task_log.append(
                    task_id,
                    scope=scope,
                    phase="running",
                    message=f"warning: {message}",
                    progress=7,
                    extra={"event_kind": "warning", **warning},
                )

        worker = ApiWorker(
            self.api_base,
            endpoint,
            params,
            method=method,
            body=body,
            headers=headers,
            request_timeout=timeout_seconds,
        )
        self.worker = worker

        def _on_progress(value: int, text: str) -> None:
            self.progress_bar.setValue(max(0, min(100, value)))
            self._set_task_status("running", text)
            self.task_log.append(task_id, scope=scope, phase="running", message=text, progress=value, extra={})

        def _on_ok(data: Any) -> None:
            self.cancel_btn.setEnabled(False)
            self.progress_bar.setValue(100)
            runtime_warnings: list[dict[str, Any]] = []
            expected_sort_field = request_context.get("expected_sort_field") if isinstance(request_context, dict) else None
            if expected_sort_field and isinstance(data, list) and data and all(isinstance(row, dict) for row in data):
                if not all(expected_sort_field in row for row in data):
                    runtime_warnings.append(
                        {
                            "reason": "sort_field_not_returned",
                            "field": expected_sort_field,
                            "message": f"sort field '{expected_sort_field}' is used by backend but not returned in result rows",
                        }
                    )

            has_contract_warnings = bool(request_context.get("contract_warnings")) if isinstance(request_context, dict) else False
            has_runtime_warnings = len(runtime_warnings) > 0
            has_warnings = has_contract_warnings or has_runtime_warnings
            empty_result = self._is_empty_result(data)
            if empty_result and has_warnings:
                status_state = "no_data"
                status_event = "No Data with warnings"
            elif empty_result:
                status_state = "no_data"
                status_event = "No Data"
            elif has_warnings:
                status_state = "warning"
                status_event = "Succeeded with warnings"
            else:
                status_state = "succeeded"
                status_event = "Succeeded"
            self._set_task_status(status_state, status_event)
            rows = len(data) if isinstance(data, list) else (
                len(data.get("players", []))
                if isinstance(data, dict) and isinstance(data.get("players"), list)
                else (len(data.get("data", [])) if isinstance(data, dict) and isinstance(data.get("data"), list) else None)
            )
            self.task_log.append(
                task_id,
                scope=scope,
                phase="succeeded",
                message="task completed with empty result" if empty_result else "task completed",
                progress=100,
                extra={
                    "rows": rows,
                    "empty_result": empty_result,
                    "effective_params": request_context.get("effective_params") if isinstance(request_context, dict) else None,
                    "dropped_params": request_context.get("dropped_params") if isinstance(request_context, dict) else None,
                    "contract_warnings": request_context.get("contract_warnings") if isinstance(request_context, dict) else None,
                    "runtime_warnings": runtime_warnings or None,
                    "endpoint": request_context.get("endpoint") if isinstance(request_context, dict) else endpoint,
                },
            )
            for warning in runtime_warnings:
                self.task_log.append(
                    task_id,
                    scope=scope,
                    phase="running",
                    message=f"warning: {warning.get('message')}",
                    progress=99,
                    extra={"event_kind": "warning", **warning},
                )
            resolved_endpoint = request_context.get("endpoint") if isinstance(request_context, dict) else endpoint
            self._display_result(data, resolved_endpoint)
            self._set_request_context_text(request_context)
            all_warnings = []
            if isinstance(request_context.get("contract_warnings"), list):
                all_warnings.extend(request_context.get("contract_warnings") or [])
            all_warnings.extend(runtime_warnings)
            self._set_warning_text(all_warnings)
            self._set_diagnostic_text("")

            if empty_result:
                diag = make_diag("empty_result", "Request completed with no data", "The API call succeeded but returned no rows.")
                self.result_text.setPlainText(
                    self._diag_to_text(diag, "无数据 (No Data)") + "\n\n" + json.dumps(data, ensure_ascii=False, indent=2)
                )
                self._set_diagnostic_text(self._diag_to_text(diag, "无数据 (No Data)"))
            self.refresh_task_log()
            self._active_task_started_at = None

        def _on_err(error: Any) -> None:
            diag = self._normalize_diag(error)
            self.cancel_btn.setEnabled(False)
            self._set_task_status("failed", str(diag.get("summary") or "failed"))
            self.task_log.append(
                task_id,
                scope=scope,
                phase="failed",
                message=str(diag.get("summary") or "request failed"),
                progress=None,
                extra={
                    "error_kind": diag.get("kind"),
                    "detail": diag.get("detail"),
                    "http_status": diag.get("http_status"),
                    "retryable": diag.get("retryable"),
                    "effective_params": request_context.get("effective_params") if isinstance(request_context, dict) else None,
                    "dropped_params": request_context.get("dropped_params") if isinstance(request_context, dict) else None,
                    "contract_warnings": request_context.get("contract_warnings") if isinstance(request_context, dict) else None,
                    "endpoint": request_context.get("endpoint") if isinstance(request_context, dict) else endpoint,
                    **(diag.get("extra") if isinstance(diag.get("extra"), dict) else {}),
                },
            )
            result_text = self._diag_to_text(diag, "任务失败 (Task Failed)")
            if isinstance(request_context, dict) and request_context:
                result_text = result_text + "\n\n请求上下文 (Request Context)\n" + json.dumps(
                    request_context, ensure_ascii=False, indent=2
                )
            self.result_text.setPlainText(result_text)
            self._set_request_context_text(request_context)
            self._set_warning_text(request_context.get("contract_warnings") if isinstance(request_context, dict) else [])
            self._set_diagnostic_text(result_text)
            self._clear_result_table()
            self.refresh_task_log()
            self._active_task_started_at = None

        worker.progress.connect(_on_progress)
        worker.finished_ok.connect(_on_ok)
        worker.finished_err.connect(_on_err)
        worker.finished.connect(lambda: self.progress_bar.setVisible(False))
        worker.start()

    def _fail_preflight(self, scope: str, endpoint: str, params: Dict[str, Any], diag: Dict[str, Any]) -> None:
        self.api_base = self._resolve_api_base()
        task_id = self.task_log.create_task(scope, f"call {endpoint}")
        self.current_task_id = task_id
        self.cancel_btn.setEnabled(False)
        self.progress_bar.setVisible(False)
        self._set_task_status("failed", str(diag.get("summary") or "input_error"))
        self.task_log.append(
            task_id,
            scope=scope,
            phase="failed",
            message=str(diag.get("summary") or "input validation failed"),
            progress=None,
            extra={
                "preflight": True,
                "endpoint": endpoint,
                "params": params,
                "error_kind": diag.get("kind"),
                "detail": diag.get("detail"),
                "retryable": diag.get("retryable"),
                **(diag.get("extra") if isinstance(diag.get("extra"), dict) else {}),
            },
        )
        diag_text = self._diag_to_text(diag, "输入校验失败 (Input Validation Failed)")
        self.result_text.setPlainText(diag_text)
        self._set_request_context_text({"endpoint": endpoint, "effective_params": params})
        self._set_warning_text([])
        self._set_diagnostic_text(diag_text)
        self._clear_result_table()
        self.refresh_task_log()
        self._active_task_started_at = None

    def refresh_task_log(self) -> None:
        if not self.current_task_id:
            self.log_text.setPlainText("")
            return
        tail_text = (self.log_tail_edit.text() or "").strip()
        try:
            tail = int(tail_text)
        except Exception:
            tail = self.log_tail_default
        tail = max(20, min(5000, tail))
        self.log_text.setPlainText(self.task_log.read(self.current_task_id, tail=tail))

    @staticmethod
    def _normalize_diag(error: Any) -> Dict[str, Any]:
        if isinstance(error, dict):
            return {
                "kind": str(error.get("kind") or "unknown_error"),
                "summary": str(error.get("summary") or "Request failed"),
                "detail": str(error.get("detail") or ""),
                "http_status": error.get("http_status"),
                "retryable": bool(error.get("retryable", False)),
                "extra": error.get("extra") if isinstance(error.get("extra"), dict) else {},
            }
        if isinstance(error, str):
            return make_diag("unknown_error", "Request failed", error)
        return make_diag("unknown_error", "Request failed", str(error))

    @staticmethod
    def _diag_action_hint(diag: Dict[str, Any]) -> str:
        kind = str(diag.get("kind") or "")
        if kind == "connection_failed":
            return "检查 API 地址与端口是否正确，并先运行健康检查 (Ping /health)。"
        if kind == "timeout":
            return "请求超时，可提高 Settings 中 timeout，或缩小查询范围后重试。"
        if kind == "http_error":
            status = diag.get("http_status")
            if status == 401:
                return "鉴权失败，请检查 Settings 中 API Key。"
            if status and int(status) >= 500:
                return "服务端异常，请查看后端日志后重试。"
            return "请求被服务端拒绝，请核对参数与接口可用性。"
        if kind == "business_error":
            return "业务校验未通过，请根据 detail 调整参数。"
        if kind == "input_error":
            return "输入参数本地校验失败，请修正后重试。"
        if kind == "empty_result":
            return "请求成功但无数据，可放宽筛选条件。"
        if kind == "schema_mismatch":
            return "导出字段与后端/DB不匹配，可尝试 tolerant 或调整字段。"
        if kind == "cancelled":
            return "任务已取消，可按需重新发起。"
        return "请复制诊断信息并结合任务日志定位问题。"

    @staticmethod
    def _diag_to_text(diag: Dict[str, Any], title: str) -> str:
        lines = [title, "", "发生了什么 (What happened)", f"- kind: {diag.get('kind')}", f"- summary: {diag.get('summary')}"]
        if diag.get("http_status") is not None:
            lines.append(f"- http_status: {diag.get('http_status')}")
        if diag.get("retryable"):
            lines.append("- retryable: true")
        detail = str(diag.get("detail") or "").strip()
        if detail:
            lines.append("- detail:")
            lines.append(detail)
        extra = diag.get("extra")
        if isinstance(extra, dict) and extra:
            lines.append("- extra:")
            lines.append(json.dumps(extra, ensure_ascii=False, indent=2))
        lines.append("")
        lines.append("可怎么做 (What you can do)")
        lines.append(f"- {MainWindow._diag_action_hint(diag)}")
        return "\n".join(lines)

    @staticmethod
    def _is_empty_result(data: Any) -> bool:
        if data is None:
            return True
        if isinstance(data, list):
            return len(data) == 0
        if isinstance(data, dict):
            if not data:
                return True
            candidate_keys = ("players", "tasks", "history", "data", "rows")
            matched = False
            for key in candidate_keys:
                if key in data:
                    matched = True
                    value = data.get(key)
                    if isinstance(value, list):
                        if len(value) > 0:
                            return False
                        continue
                    if value not in (None, "", [], {}):
                        return False
            if matched:
                return True
            return all(v in (None, "", [], {}) for v in data.values())
        return False

    def _clear_result_table(self) -> None:
        self.result_table.setSortingEnabled(False)
        self.result_table.clear()
        self.result_table.setRowCount(0)
        self.result_table.setColumnCount(0)
        self.result_table.setSortingEnabled(True)
        self.chart_widget.clear_chart("暂无图表数据 (No chart data)")

    @staticmethod
    def _coerce_float(value: Any) -> Optional[float]:
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            text = value.strip().replace(",", "")
            if not text:
                return None
            try:
                return float(text)
            except Exception:
                return None
        return None

    @staticmethod
    def _extract_rows_for_display(data: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            for key in ("players", "tasks", "history", "data"):
                maybe = data.get(key)
                if isinstance(maybe, list) and maybe and all(isinstance(item, dict) for item in maybe):
                    rows = maybe
                    break
            if not rows and data and all(not isinstance(v, (dict, list)) for v in data.values()):
                rows = [data]
        return rows

    def _extract_chart_dataset(
        self, rows: list[dict[str, Any]], endpoint: Optional[str]
    ) -> tuple[str, list[str], Dict[str, list[Optional[float]]]]:
        if not rows:
            return "图表预览 (Chart Preview)", [], {}

        all_columns: list[str] = []
        for row in rows:
            for key in row.keys():
                if key not in all_columns:
                    all_columns.append(key)

        label_keys = ["title", "name", "player_name", "username", "creator", "song", "date", "day", "month", "cid", "sid", "uid"]
        label_key: Optional[str] = None
        for key in label_keys:
            if key in all_columns:
                label_key = key
                break

        # Single-row scalar result: use fields as labels to avoid swallowing value types.
        if len(rows) == 1:
            scalar_labels: list[str] = []
            scalar_values: list[Optional[float]] = []
            for key in all_columns:
                numeric = self._coerce_float(rows[0].get(key))
                if numeric is None:
                    continue
                scalar_labels.append(key)
                scalar_values.append(numeric)
            if scalar_labels:
                title = f"字段分布 (Field Distribution) · {endpoint or 'result'}"
                return title, scalar_labels, {"value": scalar_values}

        labels: list[str] = []
        for idx, row in enumerate(rows):
            if label_key is None:
                labels.append(f"#{idx + 1}")
                continue
            raw = row.get(label_key)
            text = str(raw).strip() if raw is not None else ""
            labels.append(text or f"#{idx + 1}")

        numeric_series: Dict[str, list[Optional[float]]] = {}
        for key in all_columns:
            if key == label_key:
                continue
            values = [self._coerce_float(row.get(key)) for row in rows]
            if any(v is not None for v in values):
                numeric_series[key] = values

        title = f"图表预览 (Chart Preview) · {endpoint or 'result'}"
        return title, labels, numeric_series

    def _display_result(self, data: Any, endpoint: Optional[str] = None) -> None:
        self.result_text.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))
        rows = self._extract_rows_for_display(data)
        title, labels, series = self._extract_chart_dataset(rows, endpoint)
        self.chart_widget.set_chart_data(title, labels, series)

        if not rows:
            self._clear_result_table()
            return

        columns: list[str] = []
        for row in rows:
            for key in row.keys():
                if key not in columns:
                    columns.append(key)

        self.result_table.setSortingEnabled(False)
        self.result_table.setColumnCount(len(columns))
        self.result_table.setHorizontalHeaderLabels(columns)
        self.result_table.setRowCount(len(rows))
        for r_idx, row in enumerate(rows):
            for c_idx, col in enumerate(columns):
                value = row.get(col, "")
                if isinstance(value, (dict, list)):
                    text = json.dumps(value, ensure_ascii=False)
                else:
                    text = str(value if value is not None else "")
                self.result_table.setItem(r_idx, c_idx, QTableWidgetItem(text))
        self.result_table.resizeColumnsToContents()
        self.result_table.setSortingEnabled(True)
        self._apply_table_filter(self.table_filter_edit.text())

    def _apply_table_filter(self, keyword: str) -> None:
        key = (keyword or "").strip().lower()
        row_count = self.result_table.rowCount()
        col_count = self.result_table.columnCount()
        for row in range(row_count):
            if not key:
                self.result_table.setRowHidden(row, False)
                continue
            matched = False
            for col in range(col_count):
                item = self.result_table.item(row, col)
                if item and key in item.text().lower():
                    matched = True
                    break
            self.result_table.setRowHidden(row, not matched)

    def copy_selected_rows(self) -> None:
        selected = self.result_table.selectionModel().selectedRows()
        if not selected:
            QMessageBox.information(self, "提示", "请先选择至少一行。")
            return

        headers = []
        for col in range(self.result_table.columnCount()):
            header_item = self.result_table.horizontalHeaderItem(col)
            headers.append(header_item.text() if header_item else f"col_{col}")

        lines = ["\t".join(headers)]
        row_indexes = sorted({index.row() for index in selected})
        for row in row_indexes:
            if self.result_table.isRowHidden(row):
                continue
            cells = []
            for col in range(self.result_table.columnCount()):
                item = self.result_table.item(row, col)
                cells.append(item.text() if item else "")
            lines.append("\t".join(cells))

        QApplication.clipboard().setText("\n".join(lines))
        self._set_task_status("ready", f"已复制 {max(0, len(lines) - 1)} 行 (Rows copied)")

    def ping_health(self) -> None:
        self._start_worker("system", "/health", {}, method="GET")

    def copy_last_request(self) -> None:
        if not self.last_request:
            QMessageBox.information(self, "提示", "当前没有可复制的请求。")
            return
        QApplication.clipboard().setText(json.dumps(self.last_request, ensure_ascii=False, indent=2))
        self._set_task_status("ready", "已复制最近请求 (Last request copied)")

    def _run_with_context(
        self,
        scope: str,
        endpoint: str,
        params: Dict[str, Any],
        *,
        method: str = "GET",
        body: Optional[Any] = None,
        expected_sort_field: Optional[str] = None,
        dropped_params: Optional[Dict[str, Any]] = None,
        contract_warnings: Optional[list[Dict[str, Any]]] = None,
    ) -> None:
        context = self._make_request_context(
            endpoint=endpoint,
            effective_params=params,
            dropped_params=dropped_params,
            contract_warnings=contract_warnings,
            expected_sort_field=expected_sort_field,
        )
        self._start_worker(scope, endpoint, params, method=method, body=body, request_context=context)

    def run_mode_compare(self) -> None:
        params = {"modes": self.mode_compare_edit.text().strip() or "0,1,2,3"}
        self._run_with_context("analytics", "/analytics/mode-comparison", params, method="GET")

    def run_player_compare(self) -> None:
        mode, diag = self._parse_int_strict(self.player_mode_edit.text(), "mode", default=0, min_value=0, max_value=9)
        if diag:
            self._fail_preflight("analytics", "/analytics/player-compare", {}, diag)
            return
        days, diag = self._parse_int_strict(self.player_days_edit.text(), "days", default=30, min_value=1, max_value=3650)
        if diag:
            self._fail_preflight("analytics", "/analytics/player-compare", {}, diag)
            return
        params = {
            "players": self.player_compare_edit.text().strip() or "alice,bob",
            "mode": mode,
            "days": days,
        }
        self._run_with_context("analytics", "/analytics/player-compare", params, method="GET")

    def run_chart_trends(self) -> None:
        mode, diag = self._parse_int_strict(self.trend_mode_edit.text(), "mode", default=0, min_value=0, max_value=9)
        if diag:
            self._fail_preflight("analytics", "/analytics/chart-trends", {}, diag)
            return
        params = {"mode": mode, "period": self.trend_period_combo.currentText()}
        self._run_with_context("analytics", "/analytics/chart-trends", params, method="GET")

    @staticmethod
    def _parse_int(text: str, default: int) -> int:
        try:
            return int((text or "").strip())
        except Exception:
            return default

    def run_top_players(self) -> None:
        limit, diag = self._parse_int_strict(self.top_limit_edit.text(), "limit", default=20, min_value=1, max_value=100)
        if diag:
            self._fail_preflight("stats", "/players/top", {}, diag)
            return
        mode, diag = self._parse_int_strict(self.top_mode_edit.text(), "mode", default=0, min_value=0, max_value=9)
        if diag:
            self._fail_preflight("stats", "/players/top", {}, diag)
            return
        params = {"limit": limit, "mode": mode, "rank_type": self.top_rank_type_combo.currentText()}
        self._run_with_context("stats", "/players/top", params, method="GET")

    def run_player_history(self) -> None:
        player = (self.history_player_edit.text() or "").strip()
        if not player:
            self._fail_preflight(
                "stats",
                f"/players/{player}/history",
                {},
                make_diag("input_error", "Missing history player", "History player is required.", extra={"field": "player"}),
            )
            return
        days, diag = self._parse_int_strict(self.history_days_edit.text(), "days", default=30, min_value=1, max_value=365)
        if diag:
            self._fail_preflight("stats", f"/players/{player}/history", {}, diag)
            return
        mode, diag = self._parse_int_strict(self.history_mode_edit.text(), "mode", default=0, min_value=0, max_value=9)
        if diag:
            self._fail_preflight("stats", f"/players/{player}/history", {}, diag)
            return
        params = {"days": days, "mode": mode, "metric": self.history_metric_combo.currentText()}
        self._run_with_context("stats", f"/players/{player}/history", params, method="GET")

    def _build_chart_common_params(self) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        mode, diag = self._parse_int_strict(self.chart_mode_edit.text(), "mode", default=0, min_value=0, max_value=9)
        if diag:
            return None, diag
        params: Dict[str, Any] = {"mode": mode}
        creators = (self.chart_creators_edit.text() or "").strip()
        if creators:
            params["creators"] = creators
        statuses = (self.chart_statuses_edit.text() or "").strip()
        if statuses:
            params["statuses"] = statuses
        time_range = (self.chart_time_range_edit.text() or "").strip()
        if time_range:
            params["time_range"] = time_range
        return params, None

    def run_chart_stats(self) -> None:
        params, diag = self._build_chart_common_params()
        if diag:
            self._fail_preflight("stats", "/charts/stats", {}, diag)
            return
        self._run_with_context("stats", "/charts/stats", params, method="GET")

    def run_chart_summary(self) -> None:
        params, diag = self._build_chart_common_params()
        if diag:
            self._fail_preflight("stats", "/charts/summary", {}, diag)
            return
        params["detail_level"] = self.summary_detail_combo.currentText()
        self._run_with_context("stats", "/charts/summary", params, method="GET")

    def run_chart_quality(self) -> None:
        params, diag = self._build_chart_common_params()
        if diag:
            self._fail_preflight("stats", "/charts/quality", {}, diag)
            return
        self._run_with_context("stats", "/charts/quality", params, method="GET")

    def _build_hot_request(self) -> tuple[Optional[Dict[str, Any]], Dict[str, Any], Optional[Dict[str, Any]]]:
        mode, diag = self._parse_int_strict(self.hot_mode_edit.text(), "mode", default=0, min_value=0, max_value=9)
        if diag:
            return None, {}, diag
        limit, diag = self._parse_int_strict(self.hot_limit_edit.text(), "limit", default=20, min_value=1, max_value=50)
        if diag:
            return None, {}, diag
        sort_by = (self.hot_sort_combo.currentText() or "").strip()
        creators = (self.chart_creators_edit.text() or "").strip()
        statuses = (self.chart_statuses_edit.text() or "").strip()
        time_range = (self.chart_time_range_edit.text() or "").strip()

        valid_sort = {"heat", "donate_count", "play_count", "love_count"}
        if sort_by not in valid_sort:
            diag = make_diag(
                "input_error",
                "Invalid hot sort field",
                f"sort_by must be one of {sorted(valid_sort)}, got '{sort_by}'",
                extra={"field": "sort_by", "value": sort_by},
            )
            return None, {}, diag

        params: Dict[str, Any] = {"mode": mode, "limit": limit, "sort_by": sort_by}
        dropped_params: Dict[str, Any] = {}
        contract_warnings: list[dict[str, Any]] = []
        if creators:
            params["creators"] = creators
        if statuses:
            params["statuses"] = statuses
        if time_range:
            dropped_params["time_range"] = time_range
            contract_warnings.append(
                {
                    "reason": "unsupported_param",
                    "field": "time_range",
                    "message": "time_range is not supported by /charts/hot and was ignored",
                }
            )

        context = self._make_request_context(
            endpoint="/charts/hot",
            effective_params=params,
            dropped_params=dropped_params,
            contract_warnings=contract_warnings,
            expected_sort_field=sort_by,
        )
        return params, context, None

    def _build_recent_request(self) -> tuple[Optional[Dict[str, Any]], Dict[str, Any], Optional[Dict[str, Any]]]:
        mode, diag = self._parse_int_strict(self.recent_mode_edit.text(), "mode", default=0, min_value=0, max_value=9)
        if diag:
            return None, {}, diag
        days, diag = self._parse_int_strict(self.recent_days_edit.text(), "days", default=7, min_value=1, max_value=365)
        if diag:
            return None, {}, diag
        limit, diag = self._parse_int_strict(self.recent_limit_edit.text(), "limit", default=20, min_value=1, max_value=50)
        if diag:
            return None, {}, diag
        creators = (self.chart_creators_edit.text() or "").strip()
        statuses = (self.chart_statuses_edit.text() or "").strip()
        time_range = (self.chart_time_range_edit.text() or "").strip()

        params: Dict[str, Any] = {"mode": mode, "days": days, "limit": limit}
        dropped_params: Dict[str, Any] = {}
        contract_warnings: list[dict[str, Any]] = []
        if creators:
            params["creators"] = creators
        if statuses:
            params["statuses"] = statuses
        if time_range:
            dropped_params["time_range"] = time_range
            contract_warnings.append(
                {
                    "reason": "unsupported_param",
                    "field": "time_range",
                    "message": "time_range is not supported by /charts/recent and was ignored",
                }
            )

        context = self._make_request_context(
            endpoint="/charts/recent",
            effective_params=params,
            dropped_params=dropped_params,
            contract_warnings=contract_warnings,
            expected_sort_field="last_updated",
        )
        return params, context, None

    def run_hot_charts(self) -> None:
        params, context, diag = self._build_hot_request()
        if diag:
            self._fail_preflight("stats", "/charts/hot", {}, diag)
            return
        self._start_worker("stats", "/charts/hot", params or {}, method="GET", request_context=context)

    def run_recent_charts(self) -> None:
        params, context, diag = self._build_recent_request()
        if diag:
            self._fail_preflight("stats", "/charts/recent", {}, diag)
            return
        self._start_worker("stats", "/charts/recent", params or {}, method="GET", request_context=context)

    def run_search_players(self) -> None:
        keyword = (self.search_keyword_edit.text() or "").strip()
        if not keyword:
            self._fail_preflight(
                "search",
                "/players/search/{keyword}",
                {},
                make_diag("input_error", "Missing keyword", "Search keyword is required.", extra={"field": "keyword"}),
            )
            return
        limit, diag = self._parse_int_strict(self.search_limit_edit.text(), "limit", default=20, min_value=1, max_value=50)
        if diag:
            self._fail_preflight("search", f"/players/search/{keyword}", {}, diag)
            return
        mode, diag = self._parse_int_strict(self.search_mode_edit.text(), "mode", default=0, min_value=0, max_value=9)
        if diag:
            self._fail_preflight("search", f"/players/search/{keyword}", {}, diag)
            return
        params = {"limit": limit, "mode": mode}
        self._run_with_context("search", f"/players/search/{keyword}", params, method="GET")

    def run_search_charts(self) -> None:
        keyword = (self.search_keyword_edit.text() or "").strip()
        if not keyword:
            self._fail_preflight(
                "search",
                "/charts/search/{keyword}",
                {},
                make_diag("input_error", "Missing keyword", "Search keyword is required.", extra={"field": "keyword"}),
            )
            return
        limit, diag = self._parse_int_strict(self.search_limit_edit.text(), "limit", default=20, min_value=1, max_value=50)
        if diag:
            self._fail_preflight("search", f"/charts/search/{keyword}", {}, diag)
            return
        mode, diag = self._parse_int_strict(self.search_mode_edit.text(), "mode", default=0, min_value=0, max_value=9)
        if diag:
            self._fail_preflight("search", f"/charts/search/{keyword}", {}, diag)
            return
        params = {"limit": limit, "mode": mode}
        self._run_with_context("search", f"/charts/search/{keyword}", params, method="GET")

    def run_search_creators(self) -> None:
        keyword = (self.search_keyword_edit.text() or "").strip()
        if not keyword:
            self._fail_preflight(
                "search",
                "/charts/creators/search/{keyword}",
                {},
                make_diag("input_error", "Missing keyword", "Search keyword is required.", extra={"field": "keyword"}),
            )
            return
        limit, diag = self._parse_int_strict(self.search_limit_edit.text(), "limit", default=20, min_value=1, max_value=50)
        if diag:
            self._fail_preflight("search", f"/charts/creators/search/{keyword}", {}, diag)
            return
        mode, diag = self._parse_int_strict(self.search_mode_edit.text(), "mode", default=0, min_value=0, max_value=9)
        if diag:
            self._fail_preflight("search", f"/charts/creators/search/{keyword}", {}, diag)
            return
        params = {"limit": limit, "mode": mode}
        self._run_with_context("search", f"/charts/creators/search/{keyword}", params, method="GET")

    def run_crawler_status(self) -> None:
        self._run_with_context("governance", "/crawler/status", {}, method="GET")

    def run_db_health(self) -> None:
        self._run_with_context("governance", "/system/db/health", {}, method="GET")

    def run_db_maintain(self) -> None:
        action = (self.db_action_combo.currentText() or "").strip().lower()
        if action not in {"analyze", "vacuum"}:
            self._fail_preflight(
                "governance",
                "/system/db/maintain",
                {},
                make_diag("input_error", "Invalid maintenance action", f"Unsupported action '{action}'", extra={"field": "action"}),
            )
            return
        if not self.db_confirm_check.isChecked():
            self._fail_preflight(
                "governance",
                "/system/db/maintain",
                {},
                make_diag("input_error", "Missing confirmation", "Enable confirm action before DB maintain", extra={"field": "confirm"}),
            )
            return
        params = {"action": action, "confirm": True, "dry_run": bool(self.db_dry_run_check.isChecked())}
        self._run_with_context("governance", "/system/db/maintain", params, method="POST")

    def run_db_maintain_history(self) -> None:
        limit, diag = self._parse_int_strict(self.db_history_limit_edit.text(), "limit", default=20, min_value=1, max_value=200)
        if diag:
            self._fail_preflight("governance", "/system/db/maintain/history", {}, diag)
            return
        params = {"limit": limit}
        self._run_with_context("governance", "/system/db/maintain/history", params, method="GET")

    def run_quality_rules(self) -> None:
        self._run_with_context("governance", "/quality/rules", {}, method="GET")

    def run_quality_check(self) -> None:
        stale_hours, diag = self._parse_int_strict(
            self.quality_stale_hours_edit.text(),
            "stale_hours",
            default=72,
            min_value=1,
            max_value=24 * 30,
        )
        if diag:
            self._fail_preflight("governance", "/quality/check", {}, diag)
            return
        selected_rules = self._parse_csv(self.quality_rules_edit.text())
        params: Dict[str, Any] = {"stale_hours": stale_hours, "async_mode": bool(self.quality_async_check.isChecked())}
        body = selected_rules if selected_rules else None
        self._run_with_context("governance", "/quality/check", params, method="POST", body=body)

    def run_quality_report(self) -> None:
        self._run_with_context("governance", "/quality/report", {}, method="GET")

    def run_quality_job(self) -> None:
        job_id = (self.quality_job_id_edit.text() or "").strip()
        if not job_id:
            self._fail_preflight(
                "governance",
                "/quality/jobs/{job_id}",
                {},
                make_diag("input_error", "Missing job id", "Quality job id is required", extra={"field": "job_id"}),
            )
            return
        self._run_with_context("governance", f"/quality/jobs/{job_id}", {}, method="GET")

    def run_export_charts(self) -> None:
        if (self.download_worker and self.download_worker.isRunning()) or (
            self.query_export_worker and self.query_export_worker.isRunning()
        ):
            QMessageBox.warning(self, "繁忙", "已有导出任务正在运行。")
            return

        mode, diag = self._parse_int_strict(self.export_game_mode_edit.text(), "mode", default=0, min_value=0, max_value=9)
        if diag:
            self._fail_preflight("export", "/charts/export/charts", {}, diag)
            return
        export_strategy = self.export_strategy_combo.currentText().strip().lower() or "tolerant"
        if export_strategy not in {"tolerant", "strict"}:
            self._fail_preflight(
                "export",
                "/charts/export/charts",
                {},
                make_diag("input_error", "Invalid export strategy", f"unsupported strategy '{export_strategy}'"),
            )
            return
        creators = (self.export_creators_edit.text() or "").strip()
        statuses = (self.export_statuses_edit.text() or "").strip()
        player_name = (self.export_player_edit.text() or "").strip()
        time_range = (self.export_time_range_edit.text() or "").strip()
        export_limit, diag = self._parse_int_strict(self.export_limit_edit.text(), "limit", default=200, min_value=1, max_value=50000)
        if diag:
            self._fail_preflight("export", "/charts/export/charts", {}, diag)
            return
        export_type = self.export_type_combo.currentText()
        file_format = self.export_format_combo.currentText()

        suffix = "csv" if file_format == "csv" else "xlsx"
        default_name = f"{export_type}_export_{now_iso().replace(':', '-').replace('.', '-')}.{suffix}"
        output_file, _ = QFileDialog.getSaveFileName(
            self,
            "Save Export File",
            os.path.join(os.getcwd(), default_name),
            "CSV (*.csv);;Excel (*.xlsx)" if file_format == "csv" else "Excel (*.xlsx);;CSV (*.csv)",
        )
        if not output_file:
            return

        params: Dict[str, Any] = {"mode": mode, "format": file_format, "limit": export_limit, "export_strategy": export_strategy}
        if creators:
            params["creators"] = creators
        if statuses:
            params["statuses"] = statuses
        if player_name:
            params["player_name"] = player_name
        if time_range:
            params["time_range"] = time_range

        pending = self._pending_export_context or {}
        self._pending_export_context = None
        pending_dropped = pending.get("dropped_params") if isinstance(pending, dict) else None
        pending_warnings = pending.get("contract_warnings") if isinstance(pending, dict) else None
        if isinstance(pending_dropped, dict):
            for dropped_key in pending_dropped.keys():
                params.pop(str(dropped_key), None)
        self.api_base = self._resolve_api_base()
        headers = self._api_headers()
        timeout_seconds = self._timeout_seconds()
        self.api_base_edit.setText(self.api_base)
        export_context = self._make_request_context(
            "/charts/export/charts",
            params,
            dropped_params=pending_dropped if isinstance(pending_dropped, dict) else {},
            contract_warnings=pending_warnings if isinstance(pending_warnings, list) else [],
        )
        self.last_request = {
            "api_base": self.api_base,
            "api_key_configured": bool(headers),
            "request_timeout": timeout_seconds,
            "scope": "export",
            "method": "GET/POST",
            "endpoint": "/charts/export/charts or /query/execute",
            "params": {**params, "type": export_type},
            "request_context": export_context,
        }
        task_id = self.task_log.create_task("export", "/charts/export/charts")
        self.current_task_id = task_id
        self.task_log.append(
            task_id,
            scope="export",
            phase="running",
            message="export started",
            progress=5,
            extra={
                "params": params,
                "output_file": output_file,
                "export_strategy": export_strategy,
                "request_context": export_context,
                "api_key_configured": bool(headers),
                "request_timeout": timeout_seconds,
            },
        )
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(5)
        self.cancel_btn.setEnabled(True)
        self._active_task_started_at = time.monotonic()
        self._set_task_status("running", "导出任务已启动 (Export started)")
        self._set_request_context_text(export_context)
        self._set_warning_text(export_context.get("contract_warnings") if isinstance(export_context, dict) else [])
        self._set_diagnostic_text("")

        if export_type == "chart":
            self.task_log.append(
                task_id,
                scope="export",
                phase="running",
                message=f"strategy '{export_strategy}' uses direct chart export endpoint",
                progress=8,
                extra={"export_type": export_type, "export_strategy": export_strategy},
            )
            worker = DownloadWorker(
                self.api_base,
                "/charts/export/charts",
                params,
                output_file,
                headers=headers,
                request_timeout=timeout_seconds,
            )
            self.download_worker = worker
            run_worker = worker
        else:
            worker2 = QueryExportWorker(
                api_base=self.api_base,
                export_type=export_type,
                params=params,
                output_file=output_file,
                output_format=file_format,
                export_strategy=export_strategy,
                headers=headers,
                request_timeout=timeout_seconds,
            )
            self.query_export_worker = worker2
            run_worker = worker2

        def _on_progress(value: int, text: str) -> None:
            self.progress_bar.setValue(max(0, min(100, value)))
            self._set_task_status("running", text)
            self.task_log.append(task_id, scope="export", phase="running", message=text, progress=value, extra={})

        def _on_event(event: Any) -> None:
            if not isinstance(event, dict):
                return
            kind = str(event.get("kind") or "info")
            message = str(event.get("message") or "").strip() or "event"
            extra = event.get("extra") if isinstance(event.get("extra"), dict) else {}
            self.task_log.append(
                task_id,
                scope="export",
                phase="running",
                message=f"{kind}: {message}",
                progress=self.progress_bar.value(),
                extra={"event_kind": kind, **extra},
            )
            if kind == "warning":
                self._set_task_status("warning", message)
            existing_warnings = self.warning_text.toPlainText().strip()
            line = f"[{kind}] {message}"
            self.warning_text.setPlainText(f"{existing_warnings}\n{line}".strip() if existing_warnings else line)
            self.refresh_task_log()

        def _on_ok(filepath: str) -> None:
            self.cancel_btn.setEnabled(False)
            self.progress_bar.setValue(100)
            self._set_task_status("succeeded", "导出完成 (Export completed)")
            self.task_log.append(
                task_id,
                scope="export",
                phase="succeeded",
                message="export completed",
                progress=100,
                extra={
                    "file": filepath,
                    "effective_params": export_context.get("effective_params"),
                    "dropped_params": export_context.get("dropped_params"),
                    "contract_warnings": export_context.get("contract_warnings"),
                    "endpoint": export_context.get("endpoint"),
                },
            )
            self.result_text.setPlainText(f"导出完成 (Export completed):\n{filepath}")
            self._set_request_context_text(export_context)
            self._set_diagnostic_text("")
            self._clear_result_table()
            self.refresh_task_log()
            self._active_task_started_at = None

        def _on_err(error: Any) -> None:
            diag = self._normalize_diag(error)
            self.cancel_btn.setEnabled(False)
            self._set_task_status("failed", str(diag.get("summary") or "export failed"))
            self.task_log.append(
                task_id,
                scope="export",
                phase="failed",
                message=str(diag.get("summary") or "export failed"),
                progress=None,
                extra={
                    "error_kind": diag.get("kind"),
                    "detail": diag.get("detail"),
                    "http_status": diag.get("http_status"),
                    "retryable": diag.get("retryable"),
                    "export_strategy": export_strategy,
                    "effective_params": export_context.get("effective_params"),
                    "dropped_params": export_context.get("dropped_params"),
                    "contract_warnings": export_context.get("contract_warnings"),
                    "endpoint": export_context.get("endpoint"),
                    **(diag.get("extra") if isinstance(diag.get("extra"), dict) else {}),
                },
            )
            diag_text = self._diag_to_text(diag, "导出失败 (Export Failed)")
            self.result_text.setPlainText(diag_text)
            self._set_request_context_text(export_context)
            self._set_diagnostic_text(diag_text)
            self.refresh_task_log()
            self._active_task_started_at = None

        run_worker.progress.connect(_on_progress)
        if hasattr(run_worker, "event"):
            run_worker.event.connect(_on_event)
        run_worker.finished_ok.connect(_on_ok)
        run_worker.finished_err.connect(_on_err)
        run_worker.finished.connect(lambda: self.progress_bar.setVisible(False))
        run_worker.start()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Malody analytics desktop (PySide6)")
    parser.add_argument("--api-base", default="", help="Malody API base URL (optional, auto-detect by default)")
    parser.add_argument("--open-task-id", default=None, help="Open log panel with specific task id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_base = detect_api_base(args.api_base or None)
    app = QApplication(sys.argv)
    win = MainWindow(api_base=api_base, open_task_id=args.open_task_id)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
