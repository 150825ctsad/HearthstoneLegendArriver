# -*- coding: utf-8 -*-
"""「摧毁地标」目标链路的回归测试。

背景：拆迁修理工（CORE_REV_023，战吼：摧毁一个敌方地标）的盒子推荐
「打出N号位随从 + 目标是敌方N号位地标」——旧目标语法只认随从/英雄，
地标目标触发 unsupported_minion_target 死循环（2026-11-07 用户实测）。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.game_state import recommendation_adapter as adapter_module
from src.game_state.recommendation_adapter import adapt_action
from src.parser.recommendation_parser import (
    RecommendationParseError, RecommendationParser,
)
from src.recommendation_models import ActionKind


def _ocr(text):
    return SimpleNamespace(frame_id="frame", normalized_text=text,
                           confidence=0.99)


def _state():
    """我方 1 随从 + 1 地标（2 号位）；敌方 1 随从 + 1 地标（1 号位）。"""
    return SimpleNamespace(
        game_num_turns_in_play=4, is_end=False, is_my_turn=True,
        my_player_id="1",
        my_hand_cards=[SimpleNamespace(
            name="拆迁修理工", card_id="CORE_REV_023", cardtype="MINION",
            current_cost=3, entity_id="21")],
        my_minions=[SimpleNamespace(entity_id="31", card_id="CS2_XXX",
                                    zone_pos=1)],
        my_locations=[SimpleNamespace(entity_id="41", card_id="BE_251",
                                      zone_pos=2)],
        oppo_minions=[SimpleNamespace(entity_id="77", card_id="CS2_182",
                                      zone_pos=2)],
        oppo_locations=[SimpleNamespace(entity_id="88", card_id="REV_990",
                                        zone_pos=1)],
        oppo_locations_note=None,
        oppo_hand_card_num=3,
    )


class DestroyLocationParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = RecommendationParser()

    def test_minion_play_with_enemy_location_target(self):
        proposed = self.parser.parse(_ocr(
            "打出1号位随从\n目标是敌方1号位地标"), 4, 9)
        self.assertEqual(ActionKind.PLAY_CARD, proposed.action)
        self.assertEqual("location_slot", proposed.target.kind)
        self.assertEqual("enemy", proposed.target.owner)
        self.assertEqual(1, proposed.target.index)

    def test_destroy_style_target_line(self):
        """盒子也可能直接写「摧毁敌方1号位地标」（无「目标」前缀）。"""
        proposed = self.parser.parse(_ocr(
            "打出1号位随从\n摧毁敌方1号位地标"), 4, 9)
        self.assertEqual("location_slot", proposed.target.kind)
        self.assertEqual(1, proposed.target.index)

    def test_friendly_location_target(self):
        proposed = self.parser.parse(_ocr(
            "打出1号位随从\n目标是我方2号位地标"), 4, 9)
        self.assertEqual("location_slot", proposed.target.kind)
        self.assertEqual("friendly", proposed.target.owner)

    def test_plain_minion_target_still_works(self):
        proposed = self.parser.parse(_ocr(
            "打出1号位随从\n目标是敌方1号位随从"), 4, 9)
        self.assertEqual("board_slot", proposed.target.kind)

    def test_spell_location_target(self):
        proposed = self.parser.parse(_ocr(
            "打出1号位法术\n目标是敌方1号位地标"), 4, 9)
        self.assertEqual(ActionKind.PLAY_CARD, proposed.action)
        self.assertEqual("location_slot", proposed.target.kind)

    def test_attack_ignores_location_lines(self):
        """随从不能攻击地标：攻击主句遇地标目标行不应误绑。"""
        proposed = self.parser.parse(_ocr(
            "操作1号位随从攻击\n目标是敌方1号位地标"), 4, 9)
        self.assertEqual(ActionKind.ATTACK, proposed.action)
        self.assertIsNone(proposed.target)


class DestroyLocationAdapterTests(unittest.TestCase):
    def test_battlecry_binds_enemy_location(self):
        proposed = RecommendationParser().parse(_ocr(
            "打出1号位随从\n目标是敌方1号位地标"), 4, 9)
        adapted = adapt_action(proposed, _state())
        manual = adapted.manual_action
        self.assertEqual("location", manual.target.kind)
        self.assertEqual("enemy", manual.target.side)
        self.assertEqual(0, manual.target.index)      # zone_pos 1 → 排位 0
        self.assertEqual("88", manual.target.entity_id)
        self.assertEqual("88", adapted.target_entity_id)

    def test_non_location_card_cannot_target_location(self):
        """卡牌文字不含「地标」时禁止地标目标（防误点）。"""
        state = _state()
        state.my_hand_cards[0].card_id = "CS2_182"   # 文字与地标无关
        proposed = RecommendationParser().parse(_ocr(
            "打出1号位随从\n目标是敌方1号位地标"), 4, 9)
        with self.assertRaisesRegex(
                adapter_module.RecommendationStateError,
                "minion_target_not_location_card"):
            adapt_action(proposed, state)

    def test_location_slot_rejects_minion_entry(self):
        """目标行写地标但该排位实际是随从 → 拒绝。"""
        state = _state()
        state.oppo_locations = []                    # 1 号位不是地标
        proposed = RecommendationParser().parse(_ocr(
            "打出1号位随从\n目标是敌方1号位地标"), 4, 9)
        with self.assertRaisesRegex(
                adapter_module.RecommendationStateError,
                "target_not_location"):
            adapt_action(proposed, state)


class DestroyLocationExecutorTests(unittest.TestCase):
    def test_click_sequence_hits_location_row_position(self):
        from manual_controller import (
            ClickExecutor, ManualController, PlayCardAction, Target,
        )
        calls = []

        class FakeClick:
            def cancel_click(self):
                calls.append(("cancel",))

            def choose_card(self, index, count):
                calls.append(("hand", index, count))

            def put_minion(self, gap, count):
                calls.append(("put", gap, count))

            def choose_opponent_minion(self, index, count):
                calls.append(("target", index, count))

        controller = ManualController(
            input_func=lambda p: "0", output_func=lambda t: None,
            executor=ClickExecutor(click_module=FakeClick(),
                                   sleep_func=lambda s: None))
        state = SimpleNamespace(
            is_my_turn=True, game_num_turns_in_play=4,
            my_board_slot_num=1, oppo_board_slot_num=2,
            my_hand_cards=[SimpleNamespace(
                name="拆迁修理工", card_id="CORE_REV_023",
                cardtype="MINION", entity_id="21")],
            my_minions=[SimpleNamespace(entity_id="31", zone_pos=1)],
            my_locations=[],
            oppo_minions=[SimpleNamespace(entity_id="77", zone_pos=2)],
            oppo_locations=[SimpleNamespace(entity_id="88", zone_pos=1)],
        )
        action = PlayCardAction(
            0, "CORE_REV_023", "MINION", gap_index=0,
            target=Target("enemy", "location", 0, "88"),
            hand_entity_id="21")
        result = controller.execute(action, state)
        self.assertTrue(result.executed)
        # 点手牌 → 放置（排位数含地标）→ 点敌方 1 号排位（地标）→ 取消
        self.assertEqual(
            [("hand", 0, 1), ("put", 0, 1), ("target", 0, 2),
             ("cancel",)], calls)


if __name__ == "__main__":
    unittest.main()
