# -*- coding: utf-8 -*-
"""API Key 网页填写链路的回归测试。

覆盖：/api/decision 保存回路（留空=保留、-=清除）、密钥在 /api/status
只回显尾号绝不回传原文、make_client 的密钥优先级（环境变量 > 配置）、
网页卡片元素存在。
"""

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import web_ui
from src.brain.llm_client import LlmClient, make_client

INDEX_HTML = Path(__file__).resolve().parent.parent / "web" / "index.html"


class DecisionSaveTests(unittest.TestCase):
    def setUp(self):
        self.saved = {}

        def _save(cfg):
            self.saved = cfg

        def _fake_load():
            # 模拟"写盘后 stat 缓存失效"：decision_settings 读到已保存内容
            return dict(self.saved)

        self._patches = [
            patch.object(web_ui, "load_config",
                         side_effect=lambda: dict(self.saved)),
            patch.object(web_ui, "save_config", side_effect=_save),
            patch.object(config, "_load_ui_config", side_effect=_fake_load),
            patch.object(web_ui, "_log"),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self):
        for item in self._patches:
            item.stop()

    def test_save_decision_persists_fields(self):
        result = web_ui.api_save_decision({
            "backend": "llm",
            "api_base": "https://api.example.com/v4",
            "model": "test-model",
            "api_key": "sk-secret-1234",
        })
        self.assertTrue(result["ok"])
        decision = self.saved["decision"]
        self.assertEqual("llm", decision["backend"])
        self.assertEqual("test-model", decision["model"])
        self.assertEqual("sk-secret-1234", decision["api_key"])
        # 响应只带掩码
        self.assertEqual("1234", result["decision"]["api_key_tail"])
        self.assertNotIn("sk-secret-1234", json.dumps(result))

    def test_empty_key_keeps_old_value(self):
        web_ui.api_save_decision({"api_key": "sk-keep-me"})
        web_ui.api_save_decision({"backend": "shadow", "api_key": ""})
        self.assertEqual("shadow", self.saved["decision"]["backend"])
        self.assertEqual("sk-keep-me", self.saved["decision"]["api_key"])

    def test_dash_clears_key(self):
        web_ui.api_save_decision({"api_key": "sk-keep-me"})
        web_ui.api_save_decision({"api_key": "-"})
        self.assertEqual("", self.saved["decision"]["api_key"])

    def test_invalid_backend_rejected(self):
        result = web_ui.api_save_decision({"backend": "yolo"})
        self.assertFalse(result["ok"])

    def test_status_never_exposes_raw_key(self):
        web_ui.api_save_decision({
            "backend": "llm", "api_key": "sk-super-secret-value"})
        snapshot = web_ui.status_snapshot()
        self.assertTrue(snapshot["decision"]["api_key_set"])
        self.assertEqual("alue", snapshot["decision"]["api_key_tail"])
        self.assertNotIn("sk-super-secret-value", json.dumps(snapshot))


class MakeClientKeyPriorityTests(unittest.TestCase):
    def test_config_key_used_when_env_absent(self):
        settings = dict(config.DEFAULT_DECISION, api_key="sk-from-config")
        with patch.dict(os.environ, {}, clear=True):
            client = make_client(settings)
        self.assertEqual("sk-from-config", client.api_key)

    def test_env_key_wins_over_config(self):
        settings = dict(config.DEFAULT_DECISION, api_key="sk-from-config")
        env = {"HS_LLM_API_KEY": "sk-from-env"}
        with patch.dict(os.environ, env, clear=True):
            client = make_client(settings)
        self.assertEqual("sk-from-env", client.api_key)

    def test_missing_everything_reports_no_key(self):
        settings = dict(config.DEFAULT_DECISION)
        with patch.dict(os.environ, {}, clear=True):
            client = make_client(settings)
        self.assertEqual("", client.api_key)
        with self.assertRaises(Exception):
            client.decide("sys", "user")


class LlmClientDirectKeyTests(unittest.TestCase):
    def test_direct_api_key_used(self):
        client = LlmClient(dict(config.DEFAULT_DECISION), api_key="sk-x")
        self.assertEqual("sk-x", client.api_key)


class DecisionPageTests(unittest.TestCase):
    def setUp(self):
        self.html = INDEX_HTML.read_text(encoding="utf8")

    def test_decision_card_exists(self):
        self.assertIn("决策模型", self.html)
        self.assertIn('id="selDecision"', self.html)
        self.assertIn('id="inpApiKey"', self.html)
        self.assertIn('id="inpApiBase"', self.html)
        self.assertIn('id="inpModel"', self.html)
        self.assertIn('id="btnDecision"', self.html)

    def test_page_posts_decision_api(self):
        self.assertIn('"/api/decision"', self.html)

    def test_key_input_is_password_type(self):
        self.assertIn('id="inpApiKey" type="password"', self.html)

    def test_shadow_option_present(self):
        self.assertIn('value="shadow"', self.html)
        self.assertIn('value="llm"', self.html)


if __name__ == "__main__":
    unittest.main()
