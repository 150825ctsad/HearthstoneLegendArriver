# -*- coding: utf-8 -*-
"""决策点采集器（案例库/行为克隆数据源）的回归测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from src.brain.decision_recorder import DecisionRecorder
from src.brain.state_encoder import encode_state_features


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "training_data.jsonl"

    def test_record_appends_jsonl(self):
        recorder = DecisionRecorder(path=self.path)
        self.assertTrue(recorder.record(source="box", turn=3,
                                        instruction="打出1号位随从"))
        self.assertTrue(recorder.record(source="llm", turn=4,
                                        instruction="结束回合"))
        lines = self.path.read_text(encoding="utf8").splitlines()
        self.assertEqual(2, len(lines))
        first = json.loads(lines[0])
        self.assertEqual("box", first["source"])
        self.assertEqual("打出1号位随从", first["instruction"])
        self.assertIn("time", first)

    def test_disabled_records_nothing(self):
        recorder = DecisionRecorder(path=self.path, enabled=False)
        recorder.record(source="box", turn=1)
        self.assertFalse(self.path.exists())

    def test_errors_never_escape(self):
        recorder = DecisionRecorder(path=self.path)
        recorder.path.parent  # 正常
        # 把 path 换成目录使写入失败
        broken = DecisionRecorder(path=self.path / "是目录" / "x.jsonl"
                                  if False else self.path)
        broken.path.parent.mkdir(parents=True, exist_ok=True)
        broken.path.parent.chmod(0o000) if False else None
        # 直接让 open 失败：把 path 指向一个不存在的深层且父级为文件
        import tempfile
        blocking = Path(self._tmp.name) / "blockfile"
        blocking.write_text("x", encoding="utf8")
        bad = DecisionRecorder(path=blocking / "sub" / "data.jsonl")
        self.assertFalse(bad.record(source="box", turn=1))


class StateFeaturesTests(unittest.TestCase):
    def test_features_are_json_friendly_and_complete(self):
        from types import SimpleNamespace
        state = SimpleNamespace(
            game_num_turns_in_play=5, my_last_mana=3, my_total_mana=7,
            my_deck_num=15, oppo_deck_num=20, oppo_hand_card_num=4,
            my_minions=[SimpleNamespace(
                name="淡水鳄", attack=2, health=3, taunt=0,
                divine_shield=0, exhausted=0, frozen=0)],
            oppo_minions=[],
            my_hand_cards=[SimpleNamespace(
                name="火球术", current_cost=4, cardtype="SPELL")],
            my_hero_power=SimpleNamespace(exhausted=0),
            discover_choice_count=None,
        )
        features = encode_state_features(state)
        # JSON 序列化不抛错（采集写入的前提）
        text = json.dumps(features, ensure_ascii=False)
        self.assertIn("淡水鳄", text)
        self.assertEqual(5, features["turn"])
        self.assertEqual(1, len(features["my_minions"]))
        self.assertTrue(features["hero_power_ready"])


if __name__ == "__main__":
    unittest.main()
