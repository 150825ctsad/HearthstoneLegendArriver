# -*- coding: utf-8 -*-
"""ui_config 读取的 stat 缓存与相关热路径优化的回归测试。

背景：网页每秒轮询 /api/status，一次快照会经 load_config / _current_delays /
_load_concede_config / liveness_settings 等多条路径反复读 ui_config.json
（优化前约 30 次/秒）。改为 config._load_ui_config 的 (mtime_ns, size) 缓存后，
必须保证「保存配置后立即生效」这一原有语义不变。
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
import web_ui
from src.capture import desktop_capture


class UiConfigCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "ui_config.json"
        self._write({"name": "初始#1111"})

    def _write(self, data):
        self.path.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _cached(self):
        with patch.object(config, "CONFIG_PATH", self.path):
            return config._load_ui_config()

    def test_same_content_returns_cached_dict_without_reparse(self):
        first = self._cached()
        second = self._cached()
        self.assertIs(first, second)

    def test_file_change_invalidates_cache_immediately(self):
        self.assertEqual("初始#1111", self._cached().get("name"))
        # 内容与长度都不同，避免依赖文件系统时间戳精度
        self._write({"name": "换号后的新昵称#2222"})
        self.assertEqual("换号后的新昵称#2222", self._cached().get("name"))

    def test_invalid_json_is_not_cached_and_falls_back_to_empty(self):
        self.path.write_text("{ 不是合法 JSON", encoding="utf-8")
        self.assertEqual({}, self._cached())
        # 恢复合法内容后能立刻读到（失败结果不落缓存）
        self._write({"name": "恢复#3333"})
        self.assertEqual("恢复#3333", self._cached().get("name"))

    def test_missing_file_returns_empty(self):
        self.path.unlink()
        self.assertEqual({}, self._cached())

    def test_invalidate_ui_config_cache_forces_next_read(self):
        self._cached()
        config.invalidate_ui_config_cache()
        self._write({"name": "失效后重读的另一个昵称#4444"})
        self.assertEqual("失效后重读的另一个昵称#4444", self._cached().get("name"))


class WebConfigTests(unittest.TestCase):
    """web_ui.load_config/save_config 必须在保存后立刻读到新值。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "ui_config.json"
        self.path.write_text(json.dumps({"name": "旧#0000"}), encoding="utf-8")

    def test_save_then_load_roundtrip_uses_fresh_file(self):
        with patch.object(config, "CONFIG_PATH", self.path), \
                patch.object(web_ui, "CONFIG_PATH", self.path):
            web_ui.invalidate_ui_config_cache()
            cfg = web_ui.load_config()
            self.assertEqual("旧#0000", cfg["name"])
            cfg["name"] = "新#9999"
            web_ui.save_config(cfg)
            self.assertEqual("新#9999", web_ui.load_config()["name"])

    def test_current_delays_instantiates_config_once(self):
        calls = []

        class CountingConfig(SimpleNamespace):
            def __init__(self):
                calls.append(1)
                super().__init__(**{key: 1.0
                                    for key in config._USER_DELAY_KEYS})

        with patch.object(web_ui, "RecommendationConfig", CountingConfig):
            delays = web_ui._current_delays()

        self.assertEqual(len(config._USER_DELAY_KEYS), len(delays))
        self.assertEqual(1, len(calls))


class StableReaderCacheBoundTests(unittest.TestCase):
    def test_cache_is_bounded_and_evicts_oldest(self):
        from src.ocr.stable_reader import StableRecommendationReader

        reader = StableRecommendationReader(
            SimpleNamespace(min_ocr_confidence=0.7), None, cache_max_entries=4)

        class Frame:
            def __init__(self, key):
                self.frame_id = f"frame-{key}"
                self.exact_hash = f"hash-{key}"

        for i in range(6):
            reader._cache_store(f"hash-{i}", SimpleNamespace(index=i))

        self.assertEqual(4, len(reader._cache))
        # 最旧的 0/1 被逐出，最近的仍在
        self.assertNotIn("hash-0", reader._cache)
        self.assertNotIn("hash-1", reader._cache)
        self.assertIn("hash-5", reader._cache)

    def test_injected_plain_dict_keeps_original_semantics(self):
        from src.ocr.stable_reader import StableRecommendationReader

        plain = {}
        reader = StableRecommendationReader(
            SimpleNamespace(min_ocr_confidence=0.7), None, cache=plain,
            cache_max_entries=2)

        for i in range(5):
            reader._cache_store(f"hash-{i}", SimpleNamespace(index=i))

        # 注入的普通 dict 不做淘汰（保持旧语义），只写入
        self.assertEqual(5, len(plain))


class PowerOptionsSnapshotIsolationTests(unittest.TestCase):
    """快照里的 power_options 与 log_state 的后续变更必须隔离。"""

    def _state_with_options(self):
        state = SimpleNamespace(
            is_end=False,
            is_my_turn=True,
            game_num_turns_in_play=3,
            my_entity=SimpleNamespace(query_tag=lambda tag,
                                       default="0": "0"),
            discover_choice_count=None,
            my_player_id="1",
            power_options={
                "64": {"name": "冰雪绽放", "entity_id": "64",
                       "card_id": "CS2_017", "player": "1", "error": "NONE",
                       "choices": {}},
            },
            hand_entry_count=0,
            start_of_game_card_count=0,
            entity_dict={},
        )
        return state

    def test_later_suboption_log_does_not_mutate_taken_snapshot(self):
        from strategy import StrategyState

        log_state = self._state_with_options()
        snapshot = StrategyState(log_state)

        # 模拟 Power.log 随后写入 subOption（原地改 live state 的 choices）
        log_state.power_options["64"]["choices"][2] = {
            "index": 2, "name": "寒冰箭", "entity_id": "65",
            "card_id": "CS2_029", "player": "1", "error": "NONE"}

        self.assertEqual({}, snapshot.power_options["64"]["choices"])
        # 快照的顶层结构完整保留原有字段
        self.assertEqual("CS2_017", snapshot.power_options["64"]["card_id"])


class ProcessCheckerTtlTests(unittest.TestCase):
    def setUp(self):
        # 保存模块级缓存状态，测试后还原，不影响其他用例
        self._saved = (desktop_capture._PROCESS_CACHE_AT,
                       desktop_capture._PROCESS_CACHE_VALUE)
        desktop_capture._PROCESS_CACHE_AT = -1e9

    def tearDown(self):
        (desktop_capture._PROCESS_CACHE_AT,
         desktop_capture._PROCESS_CACHE_VALUE) = self._saved

    def test_second_call_within_ttl_uses_cache(self):
        clock = iter([100.0, 100.5, 200.0])
        enumerations = []

        def fake_lister():
            enumerations.append(1)
            return {"hearthstone.exe", "hsang.exe"}

        with patch.object(desktop_capture.time, "monotonic",
                          side_effect=lambda: next(clock)), \
                patch("src.safety.hearthstone_liveness.list_process_names",
                      side_effect=fake_lister):
            self.assertTrue(desktop_capture._default_process_checker())
            self.assertTrue(desktop_capture._default_process_checker())
            # TTL 内只枚举了一次
            self.assertEqual(1, len(enumerations))
            # 超过 TTL 后重新枚举
            self.assertTrue(desktop_capture._default_process_checker())
            self.assertEqual(2, len(enumerations))


if __name__ == "__main__":
    unittest.main()
