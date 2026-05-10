import importlib.util
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


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


class _MockApiHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args):
        del args
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/ok/data":
            self._send_json(200, {"success": True, "data": [{"id": 1, "name": "row"}]})
            return
        if parsed.path == "/ok/empty":
            self._send_json(200, {"success": True, "data": []})
            return
        if parsed.path == "/business-fail":
            self._send_json(200, {"success": False, "error": "mock business error"})
            return
        if parsed.path == "/http-error":
            self._send_json(500, {"success": False, "error": "mock server exploded"})
            return
        if parsed.path == "/protected":
            auth = self.headers.get("Authorization", "")
            api_key = self.headers.get("X-API-Key", "")
            if auth == "Bearer secret-key" or api_key == "secret-key":
                self._send_json(200, {"success": True, "data": {"status": "ok"}})
            else:
                self._send_json(401, {"detail": "Unauthorized"})
            return
        if parsed.path == "/slow":
            time.sleep(2.6)
            self._send_json(200, {"success": True, "data": {"status": "slow-ok"}})
            return
        self._send_json(404, {"detail": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/quality/check":
            query = parse_qs(parsed.query)
            content_length = int(self.headers.get("Content-Length") or "0")
            body_text = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else ""
            body_json = json.loads(body_text) if body_text else None
            self._send_json(
                200,
                {
                    "success": True,
                    "data": {
                        "stale_hours": int((query.get("stale_hours") or ["0"])[0]),
                        "async_mode": str((query.get("async_mode") or ["false"])[0]).lower() == "true",
                        "selected_rules": body_json,
                    },
                },
            )
            return
        self._send_json(404, {"detail": "not found"})


class TestIntegrationMockServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._server = ThreadingHTTPServer(("127.0.0.1", 0), _MockApiHandler)
        cls._thread = threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()
        host, port = cls._server.server_address
        cls.base_url = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls):
        cls._server.shutdown()
        cls._server.server_close()
        cls._thread.join(timeout=2)

    def test_ok_data(self):
        ok = []
        err = []
        worker = main.ApiWorker(self.base_url, "/ok/data", {}, method="GET", request_timeout=5)
        worker.finished_ok.connect(lambda d: ok.append(d))
        worker.finished_err.connect(lambda d: err.append(d))
        worker.run()
        self.assertFalse(err)
        self.assertEqual(len(ok), 1)
        self.assertIsInstance(ok[0], list)
        self.assertEqual(ok[0][0]["id"], 1)

    def test_ok_empty(self):
        ok = []
        err = []
        worker = main.ApiWorker(self.base_url, "/ok/empty", {}, method="GET", request_timeout=5)
        worker.finished_ok.connect(lambda d: ok.append(d))
        worker.finished_err.connect(lambda d: err.append(d))
        worker.run()
        self.assertFalse(err)
        self.assertEqual(ok, [[]])

    def test_business_error(self):
        ok = []
        err = []
        worker = main.ApiWorker(self.base_url, "/business-fail", {}, method="GET", request_timeout=5)
        worker.finished_ok.connect(lambda d: ok.append(d))
        worker.finished_err.connect(lambda d: err.append(d))
        worker.run()
        self.assertFalse(ok)
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["kind"], "business_error")

    def test_http_error_500(self):
        ok = []
        err = []
        worker = main.ApiWorker(self.base_url, "/http-error", {}, method="GET", request_timeout=5)
        worker.finished_ok.connect(lambda d: ok.append(d))
        worker.finished_err.connect(lambda d: err.append(d))
        worker.run()
        self.assertFalse(ok)
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["kind"], "http_error")
        self.assertEqual(err[0]["http_status"], 500)

    def test_protected_without_key_and_with_key(self):
        err = []
        worker1 = main.ApiWorker(self.base_url, "/protected", {}, method="GET", request_timeout=5)
        worker1.finished_err.connect(lambda d: err.append(d))
        worker1.run()
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["kind"], "http_error")
        self.assertEqual(err[0]["http_status"], 401)

        ok = []
        err2 = []
        worker2 = main.ApiWorker(
            self.base_url,
            "/protected",
            {},
            method="GET",
            headers=main.build_auth_headers("secret-key"),
            request_timeout=5,
        )
        worker2.finished_ok.connect(lambda d: ok.append(d))
        worker2.finished_err.connect(lambda d: err2.append(d))
        worker2.run()
        self.assertFalse(err2)
        self.assertEqual(len(ok), 1)
        self.assertEqual(ok[0]["status"], "ok")

    def test_timeout(self):
        ok = []
        err = []
        worker = main.ApiWorker(self.base_url, "/slow", {}, method="GET", request_timeout=2.0)
        worker.finished_ok.connect(lambda d: ok.append(d))
        worker.finished_err.connect(lambda d: err.append(d))
        worker.run()
        self.assertFalse(ok)
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["kind"], "timeout")

    def test_post_query_and_body(self):
        ok = []
        err = []
        worker = main.ApiWorker(
            self.base_url,
            "/quality/check",
            {"stale_hours": 72, "async_mode": True},
            method="POST",
            body=["r1", "r2"],
            request_timeout=5,
        )
        worker.finished_ok.connect(lambda d: ok.append(d))
        worker.finished_err.connect(lambda d: err.append(d))
        worker.run()
        self.assertFalse(err)
        self.assertEqual(len(ok), 1)
        data = ok[0]
        self.assertEqual(data["stale_hours"], 72)
        self.assertTrue(data["async_mode"])
        self.assertEqual(data["selected_rules"], ["r1", "r2"])


if __name__ == "__main__":
    unittest.main()
