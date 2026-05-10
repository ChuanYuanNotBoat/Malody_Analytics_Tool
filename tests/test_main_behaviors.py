import importlib.util
import json
import os
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import httpx
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication


def _load_main_module():
    root = Path(__file__).resolve().parents[1]
    main_path = root / "main.py"
    spec = importlib.util.spec_from_file_location("malody_analytics_main", str(main_path))
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


main = _load_main_module()


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, raise_exc=None, chunks=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self._raise_exc = raise_exc
        self._chunks = chunks or []

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc

    def json(self):
        return self._payload

    def iter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeStreamCtx:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

    def __enter__(self):
        if self._exc is not None:
            raise self._exc
        return self._response

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeClientBase:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _DummySignal:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args):
        for cb in list(self._callbacks):
            cb(*args)


class TestDiagnostics(TestCase):
    def test_classify_timeout(self):
        diag = main.classify_httpx_error(httpx.TimeoutException("boom"))
        self.assertEqual(diag["kind"], "timeout")
        self.assertTrue(diag["retryable"])

    def test_classify_http_status_error(self):
        req = httpx.Request("GET", "http://127.0.0.1:8000/health")
        resp = httpx.Response(500, request=req)
        exc = httpx.HTTPStatusError("server exploded", request=req, response=resp)
        diag = main.classify_httpx_error(exc)
        self.assertEqual(diag["kind"], "http_error")
        self.assertEqual(diag["http_status"], 500)
        self.assertTrue(diag["retryable"])

    def test_normalize_diag_and_empty_result(self):
        diag = main.MainWindow._normalize_diag("x")
        self.assertEqual(diag["kind"], "unknown_error")
        self.assertEqual(diag["summary"], "Request failed")
        self.assertTrue(main.MainWindow._is_empty_result([]))
        self.assertTrue(main.MainWindow._is_empty_result({"data": []}))
        self.assertFalse(main.MainWindow._is_empty_result({"data": [{"k": 1}]}))


class TestExportStrategies(TestCase):
    def test_schema_alignment_tolerant_downgrade(self):
        worker = main.QueryExportWorker(
            api_base="http://127.0.0.1:8000",
            export_type="profile",
            params={"mode": 0, "limit": 10},
            output_file="dummy.csv",
            output_format="csv",
            export_strategy="tolerant",
        )
        events = []
        worker.event.connect(lambda e: events.append(e))

        payload = worker._build_query_payload("profile", {"mode": 0, "limit": 10})
        schema_cols = {"player_id", "uid", "last_crawled", "needs_update"}
        aligned = worker._apply_schema_alignment(payload, schema_cols)

        self.assertEqual(aligned["columns"], ["player_id", "uid", "last_crawled", "needs_update"])
        self.assertTrue(events)
        self.assertEqual(events[0]["kind"], "warning")
        self.assertEqual(events[0]["extra"].get("reason"), "schema_alignment")

    def test_schema_alignment_strict_raises(self):
        worker = main.QueryExportWorker(
            api_base="http://127.0.0.1:8000",
            export_type="chart",
            params={"mode": 0, "limit": 10},
            output_file="dummy.csv",
            output_format="csv",
            export_strategy="strict",
        )
        payload = worker._build_query_payload("chart", {"mode": 0, "limit": 10})
        schema_cols = {"cid", "sid", "mode", "level", "status", "creator_name", "last_updated"}
        with self.assertRaises(RuntimeError):
            worker._apply_schema_alignment(payload, schema_cols)

    def test_run_tolerant_retries_on_missing_column(self):
        calls = []
        events = []
        ok = []
        err = []

        def fake_execute_query(self, _client, payload):
            calls.append(list(payload["columns"]))
            if len(calls) == 1:
                raise RuntimeError("no such column: stabled_by_name")
            return [{"cid": 1, "sid": 2}]

        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "out.csv")
            worker = main.QueryExportWorker(
                api_base="http://127.0.0.1:8000",
                export_type="chart",
                params={"mode": 0, "limit": 10},
                output_file=out,
                output_format="csv",
                export_strategy="tolerant",
            )
            worker.event.connect(lambda e: events.append(e))
            worker.finished_ok.connect(lambda p: ok.append(p))
            worker.finished_err.connect(lambda d: err.append(d))

            with patch.object(main.QueryExportWorker, "_fetch_schema_columns", return_value=None), patch.object(
                main.QueryExportWorker, "_execute_query", new=fake_execute_query
            ), patch.object(main.QueryExportWorker, "_write_rows", return_value=None):
                worker.run()

        self.assertFalse(err)
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(calls), 2)
        self.assertIn("stabled_by_name", calls[0])
        self.assertNotIn("stabled_by_name", calls[1])
        reasons = [e.get("extra", {}).get("reason") for e in events if isinstance(e, dict)]
        self.assertIn("runtime_column_retry", reasons)

    def test_run_strict_fails_on_missing_column(self):
        err = []
        ok = []

        def fake_execute_query(self, _client, _payload):
            raise RuntimeError("no such column: stabled_by_name")

        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "out.csv")
            worker = main.QueryExportWorker(
                api_base="http://127.0.0.1:8000",
                export_type="chart",
                params={"mode": 0, "limit": 10},
                output_file=out,
                output_format="csv",
                export_strategy="strict",
            )
            worker.finished_ok.connect(lambda p: ok.append(p))
            worker.finished_err.connect(lambda d: err.append(d))

            with patch.object(main.QueryExportWorker, "_fetch_schema_columns", return_value=None), patch.object(
                main.QueryExportWorker, "_execute_query", new=fake_execute_query
            ), patch.object(main.QueryExportWorker, "_write_rows", return_value=None):
                worker.run()

        self.assertFalse(ok)
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0].get("kind"), "schema_mismatch")

    def test_run_emits_empty_result_warning(self):
        events = []
        ok = []
        err = []

        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "out.csv")
            worker = main.QueryExportWorker(
                api_base="http://127.0.0.1:8000",
                export_type="history",
                params={"mode": 0, "limit": 10},
                output_file=out,
                output_format="csv",
                export_strategy="tolerant",
            )
            worker.event.connect(lambda e: events.append(e))
            worker.finished_ok.connect(lambda p: ok.append(p))
            worker.finished_err.connect(lambda d: err.append(d))

            with patch.object(main.QueryExportWorker, "_fetch_schema_columns", return_value=None), patch.object(
                main.QueryExportWorker, "_execute_query", return_value=[]
            ), patch.object(main.QueryExportWorker, "_write_rows", return_value=None):
                worker.run()

        self.assertFalse(err)
        self.assertEqual(len(ok), 1)
        reasons = [e.get("extra", {}).get("reason") for e in events if isinstance(e, dict)]
        self.assertIn("empty_result", reasons)


class TestApiAndDownloadWorkers(TestCase):
    def test_api_worker_business_error_success_false(self):
        errors = []

        class FakeClient(_FakeClientBase):
            def get(self, _url, params=None):
                return _FakeResponse(status_code=200, payload={"success": False, "error": "bad input"})

        worker = main.ApiWorker("http://127.0.0.1:8000", "/health", {}, method="GET")
        worker.finished_err.connect(lambda d: errors.append(d))
        with patch.object(main.httpx, "Client", FakeClient):
            worker.run()

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].get("kind"), "business_error")
        self.assertIn("bad input", errors[0].get("detail", ""))

    def test_api_worker_timeout(self):
        errors = []

        class FakeClient(_FakeClientBase):
            def get(self, _url, params=None):
                raise httpx.TimeoutException("timeout")

        worker = main.ApiWorker("http://127.0.0.1:8000", "/health", {}, method="GET")
        worker.finished_err.connect(lambda d: errors.append(d))
        with patch.object(main.httpx, "Client", FakeClient):
            worker.run()

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].get("kind"), "timeout")
        self.assertTrue(errors[0].get("retryable"))

    def test_api_worker_connection_failed(self):
        errors = []
        req = httpx.Request("GET", "http://127.0.0.1:8000/health")

        class FakeClient(_FakeClientBase):
            def get(self, _url, params=None):
                raise httpx.ConnectError("no route", request=req)

        worker = main.ApiWorker("http://127.0.0.1:8000", "/health", {}, method="GET")
        worker.finished_err.connect(lambda d: errors.append(d))
        with patch.object(main.httpx, "Client", FakeClient):
            worker.run()

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].get("kind"), "connection_failed")
        self.assertTrue(errors[0].get("retryable"))

    def test_api_worker_http_500(self):
        errors = []
        req = httpx.Request("GET", "http://127.0.0.1:8000/health")
        resp = httpx.Response(500, request=req)
        http_exc = httpx.HTTPStatusError("boom", request=req, response=resp)

        class FakeClient(_FakeClientBase):
            def get(self, _url, params=None):
                return _FakeResponse(status_code=500, payload={"x": 1}, raise_exc=http_exc)

        worker = main.ApiWorker("http://127.0.0.1:8000", "/health", {}, method="GET")
        worker.finished_err.connect(lambda d: errors.append(d))
        with patch.object(main.httpx, "Client", FakeClient):
            worker.run()

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].get("kind"), "http_error")
        self.assertEqual(errors[0].get("http_status"), 500)

    def test_download_worker_connection_failed(self):
        errors = []
        req = httpx.Request("GET", "http://127.0.0.1:8000/charts/export/charts")
        conn_exc = httpx.ConnectError("cannot connect", request=req)

        class FakeClient(_FakeClientBase):
            def stream(self, _method, _url, params=None):
                return _FakeStreamCtx(exc=conn_exc)

        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / "out.csv")
            worker = main.DownloadWorker("http://127.0.0.1:8000", "/charts/export/charts", {}, output)
            worker.finished_err.connect(lambda d: errors.append(d))
            with patch.object(main.httpx, "Client", FakeClient):
                worker.run()

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].get("kind"), "connection_failed")
        self.assertTrue(errors[0].get("retryable"))


class TestMainWindowTaskLogging(TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_start_worker_logs_failure_diagnostic_fields(self):
        class FakeApiWorker:
            def __init__(self, *args, **kwargs):
                self.progress = _DummySignal()
                self.finished_ok = _DummySignal()
                self.finished_err = _DummySignal()
                self.finished = _DummySignal()

            def isRunning(self):
                return False

            def cancel(self):
                return None

            def start(self):
                self.finished_err.emit(
                    {
                        "kind": "timeout",
                        "summary": "Request timed out",
                        "detail": "socket timed out",
                        "retryable": True,
                        "extra": {"probe": "unit-test"},
                    }
                )
                self.finished.emit()

        with tempfile.TemporaryDirectory() as tmp:
            win = main.MainWindow(api_base="http://127.0.0.1:8000")
            win.task_log = main.TaskLog(base_dir=tmp)
            win.api_base_edit.setText("http://127.0.0.1:8000")

            with patch.object(main, "ApiWorker", FakeApiWorker):
                win._start_worker("system", "/health", {}, method="GET")

            self.assertIsNotNone(win.current_task_id)
            log_text = win.task_log.read(win.current_task_id or "")
            lines = [json.loads(line) for line in log_text.splitlines() if line.strip()]
            failed = [e for e in lines if e.get("phase") == "failed"]
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0]["extra"].get("error_kind"), "timeout")
            self.assertTrue(failed[0]["extra"].get("retryable"))
            self.assertEqual(failed[0]["extra"].get("probe"), "unit-test")
            self.assertIn("Request timed out", win.status_label.text())
            self.assertIn("Failed", win.status_label.text())

            win.close()

    def test_export_logs_warning_reason_and_failure_fields(self):
        class FakeQueryExportWorker:
            def __init__(self, *args, **kwargs):
                self.progress = _DummySignal()
                self.event = _DummySignal()
                self.finished_ok = _DummySignal()
                self.finished_err = _DummySignal()
                self.finished = _DummySignal()

            def isRunning(self):
                return False

            def cancel(self):
                return None

            def start(self):
                self.progress.emit(20, "requesting /query/execute")
                self.event.emit(
                    {
                        "kind": "warning",
                        "message": "column fallback enabled",
                        "extra": {
                            "reason": "schema_alignment",
                            "missing_columns": ["stabled_by_name"],
                            "used_columns": ["cid", "sid"],
                        },
                    }
                )
                self.finished_err.emit(
                    {
                        "kind": "schema_mismatch",
                        "summary": "Export schema mismatch",
                        "detail": "missing column",
                        "retryable": True,
                        "extra": {"missing_column": "stabled_by_name"},
                    }
                )
                self.finished.emit()

        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "out.csv")
            win = main.MainWindow(api_base="http://127.0.0.1:8000")
            win.task_log = main.TaskLog(base_dir=tmp)
            win.api_base_edit.setText("http://127.0.0.1:8000")
            win.export_type_combo.setCurrentText("history")
            win.export_strategy_combo.setCurrentText("tolerant")
            win.export_game_mode_edit.setText("0")

            with patch.object(main, "QueryExportWorker", FakeQueryExportWorker), patch.object(
                main.QFileDialog, "getSaveFileName", return_value=(out, "CSV (*.csv)")
            ):
                win.run_export_charts()

            self.assertIsNotNone(win.current_task_id)
            log_text = win.task_log.read(win.current_task_id or "")
            lines = [json.loads(line) for line in log_text.splitlines() if line.strip()]
            warning_rows = [e for e in lines if e.get("message", "").startswith("warning:")]
            failed_rows = [e for e in lines if e.get("phase") == "failed"]
            self.assertTrue(warning_rows)
            self.assertEqual(warning_rows[-1]["extra"].get("reason"), "schema_alignment")
            self.assertEqual(warning_rows[-1]["extra"].get("event_kind"), "warning")
            self.assertEqual(len(failed_rows), 1)
            self.assertEqual(failed_rows[0]["extra"].get("error_kind"), "schema_mismatch")
            self.assertTrue(failed_rows[0]["extra"].get("retryable"))
            self.assertEqual(failed_rows[0]["extra"].get("export_strategy"), "tolerant")
            self.assertEqual(failed_rows[0]["extra"].get("missing_column"), "stabled_by_name")

            win.close()

    def test_build_hot_request_drops_unsupported_time_range(self):
        win = main.MainWindow(api_base="http://127.0.0.1:8000")
        win.hot_mode_edit.setText("1")
        win.hot_limit_edit.setText("12")
        win.hot_sort_combo.setCurrentText("play_count")
        win.chart_creators_edit.setText("alice")
        win.chart_statuses_edit.setText("2")
        win.chart_time_range_edit.setText("30d")

        params, context, diag = win._build_hot_request()
        self.assertIsNone(diag)
        self.assertEqual(params["mode"], 1)
        self.assertEqual(params["limit"], 12)
        self.assertEqual(params["sort_by"], "play_count")
        self.assertNotIn("time_range", params)
        self.assertEqual(context["dropped_params"].get("time_range"), "30d")
        reasons = [w.get("reason") for w in context.get("contract_warnings", [])]
        self.assertIn("unsupported_param", reasons)
        win.close()

    def test_recent_preflight_invalid_days_without_worker_start(self):
        class NeverWorker:
            def __init__(self, *args, **kwargs):
                raise AssertionError("ApiWorker should not be instantiated on preflight failure")

        with tempfile.TemporaryDirectory() as tmp:
            win = main.MainWindow(api_base="http://127.0.0.1:8000")
            win.task_log = main.TaskLog(base_dir=tmp)
            win.recent_days_edit.setText("0")
            win.recent_limit_edit.setText("20")
            win.recent_mode_edit.setText("0")

            with patch.object(main, "ApiWorker", NeverWorker):
                win.run_recent_charts()

            self.assertIsNotNone(win.current_task_id)
            log_text = win.task_log.read(win.current_task_id or "")
            lines = [json.loads(line) for line in log_text.splitlines() if line.strip()]
            failed = [e for e in lines if e.get("phase") == "failed"]
            self.assertEqual(len(failed), 1)
            self.assertTrue(failed[0]["extra"].get("preflight"))
            self.assertEqual(failed[0]["extra"].get("error_kind"), "input_error")
            self.assertEqual(failed[0]["extra"].get("endpoint"), "/charts/recent")
            self.assertIn("Input Validation Failed", win.result_text.toPlainText())
            win.close()

    def test_hot_logs_effective_and_dropped_and_sort_warning(self):
        captured_params = []

        class FakeApiWorker:
            def __init__(self, _api_base, _endpoint, params, method="GET", **kwargs):
                captured_params.append(dict(params))
                self.progress = _DummySignal()
                self.finished_ok = _DummySignal()
                self.finished_err = _DummySignal()
                self.finished = _DummySignal()

            def isRunning(self):
                return False

            def cancel(self):
                return None

            def start(self):
                self.finished_ok.emit([{"cid": 1, "title": "Song A", "heat": 10, "donate_count": 3}])
                self.finished.emit()

        with tempfile.TemporaryDirectory() as tmp:
            win = main.MainWindow(api_base="http://127.0.0.1:8000")
            win.task_log = main.TaskLog(base_dir=tmp)
            win.api_base_edit.setText("http://127.0.0.1:8000")
            win.hot_mode_edit.setText("0")
            win.hot_limit_edit.setText("10")
            win.hot_sort_combo.setCurrentText("play_count")
            win.chart_creators_edit.setText("alice")
            win.chart_statuses_edit.setText("2")
            win.chart_time_range_edit.setText("7d")

            with patch.object(main, "ApiWorker", FakeApiWorker):
                win.run_hot_charts()

            self.assertTrue(captured_params)
            sent = captured_params[-1]
            self.assertEqual(sent.get("sort_by"), "play_count")
            self.assertNotIn("time_range", sent)

            self.assertIsNotNone(win.current_task_id)
            log_text = win.task_log.read(win.current_task_id or "")
            lines = [json.loads(line) for line in log_text.splitlines() if line.strip()]
            warning_rows = [e for e in lines if e.get("message", "").startswith("warning:")]
            succeeded_rows = [e for e in lines if e.get("phase") == "succeeded"]

            reasons = []
            for row in warning_rows:
                reason = row.get("extra", {}).get("reason")
                if reason:
                    reasons.append(reason)
            self.assertIn("unsupported_param", reasons)
            self.assertIn("sort_field_not_returned", reasons)

            self.assertEqual(len(succeeded_rows), 1)
            s_extra = succeeded_rows[0].get("extra", {})
            self.assertEqual(s_extra.get("endpoint"), "/charts/hot")
            self.assertIsInstance(s_extra.get("effective_params"), dict)
            self.assertIsInstance(s_extra.get("dropped_params"), dict)
            self.assertIn("time_range", s_extra.get("dropped_params", {}))
            self.assertIn("/charts/hot", win.request_context_text.toPlainText())
            win.close()


class TestSettingsAndCapabilities(TestCase):
    def test_build_auth_headers(self):
        headers = main.build_auth_headers("abc123")
        self.assertEqual(headers.get("Authorization"), "Bearer abc123")
        self.assertEqual(headers.get("X-API-Key"), "abc123")
        self.assertEqual(main.build_auth_headers(""), {})

    def test_settings_roundtrip_and_sanitize(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "api_base": "http://127.0.0.1:9000/",
                "api_key": "k",
                "request_timeout": "9999",
                "default_export_strategy": "INVALID",
                "log_tail_default": "5",
                "ui_mode": "INVALID",
                "ui_language": "INVALID",
                "quick_start_default": False,
                "filter_presets": {"a": {"mode": "0"}},
            }
            saved = main.save_settings(tmp, payload, "http://127.0.0.1:8000")
            self.assertEqual(saved["api_base"], "http://127.0.0.1:9000")
            self.assertEqual(saved["request_timeout"], 600.0)
            self.assertEqual(saved["default_export_strategy"], "tolerant")
            self.assertEqual(saved["log_tail_default"], 20)
            self.assertEqual(saved["ui_mode"], "simple")
            self.assertEqual(saved["ui_language"], "zh_en")
            self.assertFalse(saved["quick_start_default"])
            loaded = main.load_settings(tmp, "http://127.0.0.1:8000")
            self.assertEqual(loaded["api_base"], "http://127.0.0.1:9000")
            self.assertEqual(loaded["api_key"], "k")
            self.assertEqual(loaded["filter_presets"]["a"]["mode"], "0")
            self.assertEqual(loaded["ui_mode"], "simple")
            self.assertEqual(loaded["ui_language"], "zh_en")
            self.assertFalse(loaded["quick_start_default"])

            payload2 = dict(payload)
            payload2["ui_language"] = "en"
            saved2 = main.save_settings(tmp, payload2, "http://127.0.0.1:8000")
            self.assertEqual(saved2["ui_language"], "en")
            loaded2 = main.load_settings(tmp, "http://127.0.0.1:8000")
            self.assertEqual(loaded2["ui_language"], "en")

    def test_capability_rows_have_supported_partial_unsupported(self):
        rows = main.MainWindow._capability_rows()
        statuses = {row.get("status") for row in rows}
        self.assertIn("supported", statuses)
        self.assertIn("partial", statuses)
        self.assertIn("unsupported", statuses)

    def test_diag_text_has_what_and_how_sections(self):
        diag = main.make_diag("connection_failed", "Cannot connect to API", "dial tcp failed", retryable=True)
        text = main.MainWindow._diag_to_text(diag, "任务失败 (Task Failed)")
        self.assertIn("发生了什么", text)
        self.assertIn("可怎么做", text)
        self.assertIn("Cannot connect to API", text)

    def test_bilingual_render_obeys_ui_language(self):
        win = main.MainWindow(api_base="http://127.0.0.1:8000")
        win.ui_language = "zh"
        self.assertEqual(win._bi("就绪", "Ready"), "就绪")
        win.ui_language = "en"
        self.assertEqual(win._bi("就绪", "Ready"), "Ready")
        win.ui_language = "zh_en"
        self.assertEqual(win._bi("就绪", "Ready"), "就绪 (Ready)")
        win.close()

    def test_calc_initial_window_size_does_not_overflow_screen(self):
        width, height = main.MainWindow._calc_initial_window_size(1280, 720)
        self.assertLessEqual(width, 1280 - 16)
        self.assertLessEqual(height, 720 - 16)
        self.assertGreaterEqual(width, 900)
        self.assertGreaterEqual(height, 620)

        tiny_width, tiny_height = main.MainWindow._calc_initial_window_size(800, 560)
        self.assertLessEqual(tiny_width, 800 - 16)
        self.assertLessEqual(tiny_height, 560 - 16)
        self.assertGreaterEqual(tiny_width, 640)
        self.assertGreaterEqual(tiny_height, 500)

    def test_extract_chart_dataset_keeps_multiple_value_types(self):
        win = main.MainWindow(api_base="http://127.0.0.1:8000")
        rows = [
            {"title": "A", "heat": 12, "play_count": 30},
            {"title": "B", "heat": 8, "play_count": 28},
            {"title": "C", "heat": 20, "play_count": 44},
        ]
        title, labels, series = win._extract_chart_dataset(rows, "/charts/hot")
        self.assertIn("/charts/hot", title)
        self.assertEqual(labels, ["A", "B", "C"])
        self.assertIn("heat", series)
        self.assertIn("play_count", series)
        self.assertEqual(series["heat"][0], 12.0)
        self.assertEqual(series["play_count"][2], 44.0)
        win.close()

    def test_display_result_updates_chart_preview(self):
        win = main.MainWindow(api_base="http://127.0.0.1:8000")
        win._display_result(
            [
                {"player_name": "alice", "mmr": 1200},
                {"player_name": "bob", "mmr": 1180},
                {"player_name": "charlie", "mmr": 1140},
            ],
            "/players/top",
        )
        self.assertTrue(win.chart_widget._labels)
        self.assertIn("/players/top", win.chart_widget._title)
        self.assertIn("mmr", win.chart_widget._series)
        win.close()

    def test_display_result_chart_not_limited_to_twelve_rows(self):
        win = main.MainWindow(api_base="http://127.0.0.1:8000")
        rows = [{"title": f"S{i}", "heat": i} for i in range(1, 31)]
        win._display_result(rows, "/charts/hot")
        self.assertEqual(len(win.chart_widget._labels), 30)
        self.assertIn("heat", win.chart_widget._series)
        win.close()


class TestQuickStartSimpleMode(TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_simple_hot_drops_hidden_params(self):
        captured = {}

        class FakeApiWorker:
            def __init__(self, _api_base, _endpoint, params, method="GET", **kwargs):
                captured["params"] = dict(params)
                captured["context"] = kwargs.get("request_context")
                self.progress = _DummySignal()
                self.finished_ok = _DummySignal()
                self.finished_err = _DummySignal()
                self.finished = _DummySignal()

            def isRunning(self):
                return False

            def cancel(self):
                return None

            def start(self):
                self.finished_ok.emit([])
                self.finished.emit()

        with tempfile.TemporaryDirectory() as tmp:
            win = main.MainWindow(api_base="http://127.0.0.1:8000")
            win.task_log = main.TaskLog(base_dir=tmp)
            win.chart_creators_edit.setText("alice")
            win.chart_statuses_edit.setText("2")
            win.chart_time_range_edit.setText("7d")
            win.qs_hot_mode_edit.setText("0")
            win.qs_hot_limit_edit.setText("10")
            with patch.object(main, "ApiWorker", FakeApiWorker):
                win.run_hot_charts_simple()
            self.assertIn("params", captured)
            self.assertNotIn("creators", captured["params"])
            self.assertNotIn("statuses", captured["params"])
            self.assertIsNotNone(win.current_task_id)
            log_text = win.task_log.read(win.current_task_id or "")
            events = [json.loads(line) for line in log_text.splitlines() if line.strip()]
            warning_events = [e for e in events if str(e.get("message", "")).startswith("warning:")]
            reasons = [e.get("extra", {}).get("reason") for e in warning_events]
            self.assertIn("simple_mode_hidden_param", reasons)
            win.close()

    def test_simple_recent_drops_hidden_params(self):
        captured = {}

        class FakeApiWorker:
            def __init__(self, _api_base, _endpoint, params, method="GET", **kwargs):
                captured["params"] = dict(params)
                self.progress = _DummySignal()
                self.finished_ok = _DummySignal()
                self.finished_err = _DummySignal()
                self.finished = _DummySignal()

            def isRunning(self):
                return False

            def cancel(self):
                return None

            def start(self):
                self.finished_ok.emit([])
                self.finished.emit()

        with tempfile.TemporaryDirectory() as tmp:
            win = main.MainWindow(api_base="http://127.0.0.1:8000")
            win.task_log = main.TaskLog(base_dir=tmp)
            win.chart_creators_edit.setText("alice")
            win.chart_statuses_edit.setText("2")
            win.chart_time_range_edit.setText("7d")
            win.qs_hot_mode_edit.setText("0")
            win.qs_recent_days_edit.setText("7")
            win.qs_recent_limit_edit.setText("10")
            with patch.object(main, "ApiWorker", FakeApiWorker):
                win.run_recent_charts_simple()
            self.assertIn("params", captured)
            self.assertNotIn("creators", captured["params"])
            self.assertNotIn("statuses", captured["params"])
            self.assertIsNotNone(win.current_task_id)
            log_text = win.task_log.read(win.current_task_id or "")
            events = [json.loads(line) for line in log_text.splitlines() if line.strip()]
            warning_events = [e for e in events if str(e.get("message", "")).startswith("warning:")]
            reasons = [e.get("extra", {}).get("reason") for e in warning_events]
            self.assertIn("simple_mode_hidden_param", reasons)
            win.close()

    def test_simple_export_drops_hidden_params(self):
        captured = {}

        class FakeQueryExportWorker:
            def __init__(self, *args, **kwargs):
                captured["params"] = dict(kwargs.get("params") or {})
                self.progress = _DummySignal()
                self.event = _DummySignal()
                self.finished_ok = _DummySignal()
                self.finished_err = _DummySignal()
                self.finished = _DummySignal()

            def isRunning(self):
                return False

            def cancel(self):
                return None

            def start(self):
                self.finished_ok.emit("out.csv")
                self.finished.emit()

        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "out.csv")
            win = main.MainWindow(api_base="http://127.0.0.1:8000")
            win.task_log = main.TaskLog(base_dir=tmp)
            win.api_base_edit.setText("http://127.0.0.1:8000")
            win.export_creators_edit.setText("alice")
            win.export_statuses_edit.setText("2")
            win.export_player_edit.setText("bob")
            win.export_time_range_edit.setText("7d")
            win.qs_export_mode_edit.setText("0")
            win.qs_export_type_combo.setCurrentText("history")
            win.qs_export_format_combo.setCurrentText("csv")
            win.qs_export_limit_edit.setText("100")
            with patch.object(main, "QueryExportWorker", FakeQueryExportWorker), patch.object(
                main.QFileDialog, "getSaveFileName", return_value=(out, "CSV (*.csv)")
            ):
                win.run_export_charts_simple()
            self.assertIn("params", captured)
            self.assertNotIn("creators", captured["params"])
            self.assertNotIn("statuses", captured["params"])
            self.assertNotIn("player_name", captured["params"])
            self.assertNotIn("time_range", captured["params"])
            self.assertIsNotNone(win.current_task_id)
            log_text = win.task_log.read(win.current_task_id or "")
            self.assertIn("simple_mode_hidden_param", log_text)
            win.close()
