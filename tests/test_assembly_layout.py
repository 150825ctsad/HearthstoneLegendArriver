# -*- coding: utf-8 -*-
"""拼装选择 UI 点击布局的回归测试。

背景：拼装（37.0）选择界面的卡牌位置与标准发现界面不同，旧随机兜底用
DISCOVER_CARD_X 坐标点击落空（2026-11-07 08:31 对局实测）。修复链路：
Power.log GENERAL_CHOICE 的 Source 卡牌 ID → cards.json 文字判定拼装 →
DiscoverChoiceAction.layout → click.choose_discover_card 分表取坐标。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from click import ASSEMBLY_POSITIONS, choose_discover_card
from json_op import card_text, is_assembly_card
from log_op import LOG_LINE_GENERAL_CHOICE_SOURCE, parse_line
from log_state import GameEntity, LogState, PlayerEntity, update_state
from manual_controller import DiscoverChoiceAction
from src.game_state.recommendation_adapter import (
    RecommendationStateError, adapt_action,
)
from src.recommendation_models import ActionKind, SlotRef


def _source_line(card_id):
    # 真实日志行（2026-11-07 09:01 实测）："- " 后是三个空格缩进
    return (
        "D 09:01:45.3411625 GameState.DebugPrintEntityChoices() -   "
        f"Source=[entityName=修补 id=55 zone=PLAY zonePos=0 "
        f"cardId={card_id} player=2]")


def _choice_open_state(card_id="BE_553", count=4):
    """构造一个「拼装选择界面打开中」的 LogState（我方玩家 = 2 号位）。"""
    state = LogState()
    state.my_player_id = "2"
    state.add_entity("1", GameEntity())
    state.game_entity_id = "1"
    state.entity_dict["1"].set_tag("NUM_TURNS_IN_PLAY", "3")
    state.add_player_entity("2", "2", PlayerEntity())
    state.add_player_entity("3", "1", PlayerEntity())
    start = parse_line(
        "D 09:01:45.0000000 GameState.DebugPrintEntityChoices() - "
        "id=5 Player=GGboom#51837 TaskList=396 ChoiceType=GENERAL "
        "CountMin=1 CountMax=1")
    update_state(state, start)
    # 来源行用真实日志行（-   Source= 三空格缩进）
    update_state(state, SimpleNamespace(
        line_type=LOG_LINE_GENERAL_CHOICE_SOURCE,
        info_dict={"card_id": card_id, "player": "2"}))
    entity = parse_line(
        "D 08:31:39.0000000 GameState.DebugPrintEntityChoices() - "
        "Entities[0]=[entityName=苦痛侍僧 id=103 zone=SETASIDE zonePos=0 "
        "cardId=CORE_EX1_007 player=2]")
    update_state(state, entity)
    # 非法途径同步选项数量（绕过逐行喂日志的繁琐）
    state.general_choice_indexes = set(range(count))
    state.general_choice_ready = True
    return state


def _strategy(state):
    from strategy import StrategyState
    return StrategyState(state)


class SourceCardTrackingTests(unittest.TestCase):
    def test_real_source_line_parses_with_multispace_indent(self):
        # 回归：真实日志 "-   Source=" 是三空格缩进，单空格正则会漏匹配
        # （2026-09-01 09:01:45 修补事件实测原文）
        container = parse_line(_source_line("BE_553"))
        self.assertIsNotNone(container)
        self.assertEqual("BE_553", container.info_dict["card_id"])

    def test_source_card_id_parsed_while_choice_open(self):
        state = _choice_open_state()
        self.assertEqual("BE_553",
                         state.general_choice_source_card_id)

    def test_source_ignored_without_open_choice(self):
        state = LogState()
        update_state(state, SimpleNamespace(
            line_type=LOG_LINE_GENERAL_CHOICE_SOURCE,
            info_dict={"card_id": "BE_553", "player": "2"}))
        self.assertIsNone(state.general_choice_source_card_id)

    def test_clear_resets_source_card(self):
        state = _choice_open_state()
        state.clear_general_choice()
        self.assertIsNone(state.general_choice_source_card_id)

    def test_strategy_exposes_source_only_when_choice_open(self):
        state = _choice_open_state()
        state.general_choice_indexes = set(range(4))
        state.general_choice_ready = True
        snapshot = _strategy(state)
        self.assertEqual("BE_553",
                         snapshot.discover_choice_source_card_id)


class AssemblyDetectionTests(unittest.TestCase):
    def test_real_assembly_card_detected_by_text(self):
        # BE_553 修补 的规则文字含「拼装」（cards.json 实测）
        self.assertTrue(is_assembly_card("BE_553"))

    def test_plain_discover_card_not_detected(self):
        # 追踪术 CORE_DS1_184 是普通发现，文字不含「拼装」
        self.assertFalse(is_assembly_card("CORE_DS1_184"))
        self.assertFalse(is_assembly_card(""))
        self.assertFalse(is_assembly_card(None))

    def test_card_text_strips_markup(self):
        text = card_text("BE_553")
        self.assertIn("拼装", text)
        self.assertNotIn("<b>", text)


class ChooseDiscoverLayoutTests(unittest.TestCase):
    def _clicks(self, layout, count, index, assembly_table,
                confirm=None):
        clicks = []

        class FakeRand:
            @staticmethod
            def sleep(_s):
                pass

        with patch("click.rand_sleep", FakeRand.sleep), \
                patch("click.left_click",
                      side_effect=lambda x, y: clicks.append((x, y))), \
                patch("click.ASSEMBLY_POSITIONS",
                      {count: [[x, 495] for x in assembly_table]}), \
                patch("click.ASSEMBLY_CONFIRM_POS",
                      confirm or (940, 760)), \
                patch("click._assembly_layout_from_config",
                      return_value=({}, None)):
            choose_discover_card(index, count, layout=layout)
        return clicks

    def test_default_layout_unchanged(self):
        clicks = self._clicks("discover", 4, 1, [100, 200, 300, 400])
        # 走标准发现表（X 数组 + 固定 y=500）
        self.assertEqual([(775, 500)], clicks)

    def test_assembly_clicks_slot_then_confirm(self):
        clicks = self._clicks("assembly", 4, 2, [100, 200, 300, 400])
        # 拼装 = 点替换槽位 + 补点「拼装」确认按钮
        self.assertEqual([(300, 495), (940, 760)], clicks)

    def test_assembly_falls_back_to_discover_when_empty(self):
        # 布局表未量取前回退标准表（保持旧行为，不点确认）
        clicks = self._clicks("assembly", 4, 0, [], confirm=None)
        self.assertEqual([(400, 500)], clicks)

    def test_assembly_positions_cover_both_slots(self):
        # 4 实体映射：0/1 → 左替换槽位，2/3 → 右替换槽位（实测帧量取）
        self.assertEqual(
            [(505, 495), (505, 495), (1330, 495), (1330, 495)],
            ASSEMBLY_POSITIONS[4])


class AdapterLayoutTests(unittest.TestCase):
    def _proposed(self, index=1):
        return SimpleNamespace(
            action=ActionKind.CHOOSE_DISCOVER, turn_number=3,
            log_revision=7, normalized_instruction="选择第1个选项",
            source=SlotRef("discover_slot", "friendly", index))

    def _adapted_state(self, source_card_id):
        from strategy import StrategyState
        log_state = _choice_open_state(card_id=source_card_id or "",
                                       count=3)
        log_state.general_choice_indexes = set(range(3))
        return StrategyState(log_state)

    def test_assembly_source_sets_assembly_layout(self):
        with patch("src.game_state.recommendation_adapter.is_assembly_card",
                   return_value=True):
            adapted = adapt_action(self._proposed(),
                                   self._adapted_state("BE_553"))
        self.assertEqual("assembly",
                         adapted.manual_action.layout)

    def test_discover_source_keeps_discover_layout(self):
        with patch("src.game_state.recommendation_adapter.is_assembly_card",
                   return_value=False):
            adapted = adapt_action(self._proposed(),
                                   self._adapted_state("CORE_DS1_184"))
        self.assertEqual("discover", adapted.manual_action.layout)


if __name__ == "__main__":
    unittest.main()
