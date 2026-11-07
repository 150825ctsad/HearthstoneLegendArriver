# -*- coding: utf-8 -*-
"""Web 控制台 HTTP API 的端到端测试（进程内起真实 HTTPServer + urllib 调用）。

只测安全端点：不碰 /api/start、/api/prepare、/api/calibrate、/api/overlay、
/api/schedule（会启动自动化线程/弹窗/定时线程）。
会写 ui_config.json 的接口在用例里先备份、teardown 还原，保证不覆盖用户配置。
"""

import json
import pathlib
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import web_ui

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = ROOT / "ui_config.json"
UI_LOG = ROOT / "ui_log_last.txt"


class ApiHttpTests(unittest.TestCase):
    """起真实服务，走完整 HTTP 边界（路由/JSON/状态码/保存后立即生效）。"""

    @classmethod
    def setUpClass(cls):
        # 备份用户真实配置与 UI 日志（可能不存在）
        cls._backup = {
            path: path.read_bytes() if path.exists() else None
            for path in (CONFIG, UI_LOG)}
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web_ui.Handler)
        cls.server.daemon_threads = True
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever,
                         daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        # 还原/清理：原本存在就还原内容，原本不存在就删掉测试产物
        for path, data in cls._backup.items():
            if data is not None:
                path.write_bytes(data)
            elif path.exists():
                path.unlink()

    def _call(self, method, path, body=None, raw=None, timeout=90):
        data = raw if raw is not None else (
            json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data,
                                     method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8")
                try:
                    return resp.status, json.loads(text)
                except ValueError:
                    return resp.status, text
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(text)
            except ValueError:
                return exc.code, text

    # ------------------------------------------------------------ 只读接口
    def test_index_serves_html(self):
        status, body = self._call("GET", "/")
        self.assertEqual(200, status)
        self.assertIsInstance(body, str)
        self.assertIn("HSLegendArriver", body)

    def test_status_has_all_sections(self):
        status, body = self._call("GET", "/api/status")
        self.assertEqual(200, status)
        for key in ("running", "phase", "concede", "concede_detect",
                    "human_like", "liveness", "delays", "selfcheck",
                    "config", "games", "wins"):
            self.assertIn(key, body)

    def test_logs_incremental_cursor(self):
        status, body = self._call("GET", "/api/logs?seq=0")
        self.assertEqual(200, status)
        self.assertIn("seq", body)
        self.assertIn("lines", body)
        status, body = self._call("GET", f"/api/logs?seq={body['seq']}")
        self.assertEqual([], body["lines"])

    def test_check_log_exists_flag(self):
        status, body = self._call(
            "GET", "/api/check_log?path=" + quote("C:\\非常确定不存在的目录"))
        self.assertEqual(200, status)
        self.assertFalse(body["exists"])
        self.assertTrue(body["checked"])

        status, body = self._call(
            "GET", "/api/check_log?path=" + quote(str(ROOT)))
        self.assertEqual(200, status)
        self.assertTrue(body["exists"])

    def test_selfcheck_runs_then_caches(self):
        status, body = self._call("GET", "/api/selfcheck", timeout=120)
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertGreaterEqual(len(body["result"].get("items") or []), 5)

    def test_regions_returns_preview_or_capture_error(self):
        status, body = self._call("GET", "/api/regions")
        self.assertEqual(200, status)
        self.assertTrue(body.get("ok")
                        or str(body.get("error", "")).startswith("截图失败"))

    def test_unknown_routes_return_404(self):
        for method, path in (("GET", "/api/unknown"),
                             ("POST", "/api/unknown"),
                             ("POST", "/api/status")):
            status, body = self._call(method, path, {})
            self.assertEqual(404, status, path)
            self.assertFalse(body["ok"])

    def test_invalid_json_body_returns_400(self):
        status, body = self._call("POST", "/api/config", raw=b"{ not json")
        self.assertEqual(400, status)

    # ------------------------------------------------------------ 保存接口
    def test_save_config_is_visible_in_status_immediately(self):
        status, body = self._call(
            "POST", "/api/config", {"name": "APITest#7788", "log_root": ""})
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])

        # 保存后立即查询必须看到新值（stat 缓存必须已失效）
        status, body = self._call("GET", "/api/status")
        self.assertEqual("APITest#7788", body["config"]["name"])

        # 无 # 的昵称可保存，但要给出警告
        status, body = self._call(
            "POST", "/api/config", {"name": "没有井号的昵称"})
        self.assertTrue(body["ok"])
        self.assertTrue(body.get("warnings"))

    def test_save_concede_roundtrip_and_validation(self):
        status, body = self._call(
            "POST", "/api/concede",
            {"enabled": True, "threshold": 25, "rounds": 4})
        self.assertTrue(body["ok"])
        self.assertEqual(25, body["concede"]["threshold"])

        status, body = self._call("GET", "/api/status")
        self.assertEqual(25, body["concede"]["threshold"])
        self.assertEqual(4, body["concede"]["rounds"])

        status, body = self._call("POST", "/api/concede",
                                  {"threshold": "abc"})
        self.assertFalse(body["ok"])
        self.assertIn("数字", body["error"])

        status, body = self._call("POST", "/api/concede", {"threshold": 999})
        self.assertTrue(body["ok"])
        self.assertEqual(100.0, body["concede"]["threshold"])

    def test_save_human_like_roundtrip(self):
        status, body = self._call(
            "POST", "/api/human_like",
            {"enabled": True, "post_delay_min": 1.0, "post_delay_max": 2.0,
             "hover_min": 0.3, "hover_max": 0.8})
        self.assertTrue(body["ok"])

        status, body = self._call("GET", "/api/status")
        self.assertTrue(body["human_like"]["enabled"])
        self.assertEqual(1.0, body["human_like"]["post_delay_min"])

    def test_save_liveness_roundtrip_and_validation(self):
        status, body = self._call(
            "POST", "/api/liveness",
            {"enabled": True, "log_stale_warn_seconds": 90,
             "log_stale_stop_seconds": 240})
        self.assertTrue(body["ok"])

        status, body = self._call("GET", "/api/status")
        self.assertEqual(90, body["liveness"]["log_stale_warn_seconds"])

        status, body = self._call(
            "POST", "/api/liveness", {"log_stale_warn_seconds": "x"})
        self.assertFalse(body["ok"])

    def test_save_delays_roundtrip_and_bounds(self):
        status, body = self._call(
            "POST", "/api/delays",
            {"pre_action_delay_seconds": 3.5,
             "draw_extra_delay_per_card_seconds": 0})
        self.assertTrue(body["ok"])

        status, body = self._call("GET", "/api/status")
        self.assertEqual(3.5, body["delays"]["pre_action_delay_seconds"])
        self.assertEqual(0.0,
                         body["delays"]["draw_extra_delay_per_card_seconds"])

        status, body = self._call(
            "POST", "/api/delays", {"pre_action_delay_seconds": 999})
        self.assertFalse(body["ok"])
        self.assertIn("介于", body["error"])

        status, body = self._call(
            "POST", "/api/delays", {"ocr_preprocess_scale": "bad"})
        self.assertFalse(body["ok"])

    def test_saved_file_matches_api_values(self):
        self._call("POST", "/api/delays", {"post_action_delay_seconds": 0.7})
        saved = json.loads(CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(0.7, saved["delays"]["post_action_delay_seconds"])

    # ------------------------------------------------------------ 安全负向
    def test_stop_without_automation_reports_error(self):
        status, body = self._call("POST", "/api/stop",
                                  {"mode": "immediate"})
        self.assertEqual(200, status)
        self.assertFalse(body["ok"])
        self.assertIn("没有", body["error"])

    def test_cancel_schedule_is_idempotent(self):
        status, _body = self._call("POST", "/api/schedule/cancel")
        self.assertEqual(200, status)

    def test_status_polling_does_not_reparse_unchanged_config(self):
        """稳态轮询不应反复读盘解析（stat 缓存生效）。"""
        import time as _time
        orig_read = pathlib.Path.read_text
        reads = []

        def counting_read(self, *args, **kwargs):
            if str(self).endswith("ui_config.json"):
                reads.append(1)
            return orig_read(self, *args, **kwargs)

        self._call("GET", "/api/status")  # 预热
        pathlib.Path.read_text = counting_read
        try:
            for _ in range(5):
                self._call("GET", "/api/status")
        finally:
            pathlib.Path.read_text = orig_read
        self.assertEqual([], reads)
        _time.sleep(0)  # 保持 import 使用，避免某些环境告警


if __name__ == "__main__":
    unittest.main()
