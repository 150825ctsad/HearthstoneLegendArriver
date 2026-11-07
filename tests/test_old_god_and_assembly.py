# -*- coding: utf-8 -*-
"""37.0 适配：古神技能链路 + 拼装/发现随机兜底的回归测试。

实测依据（2026-11-06/07 对局日志）：
  * 盒子用「操作N号位随从攻击」+「选择卡牌/技能名」表达古神技能；
  * 技能菜单在 Power.log 里静默（无 Options/Choices）；
  * 技能实体以 SETASIDE 注册（BE_017t3~t6），实体 tag
    ACTIVATABLE_MINION_ABILITY_USED_N 标记已用技能；
  * 拼装/发现界面打开时盒子可长时间 recommendation_not_stable。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from log_state import CardEntity, GameEntity, LogState, PlayerEntity
from manual_controller import (
    ClickExecutor, ManualController, Target, UseMinionAbilityAction,
)
from src.flow.recommendation_flow import (
    FlowStepStatus, RecommendationFlow,
)
from src.game_state import recommendation_adapter as adapter_module
from src.game_state import old_gods
from src.game_state.recommendation_adapter import adapt_action
from src.parser.recommendation_parser import (
    RecommendationParseError, RecommendationParser,
)
from src.recommendation_models import ActionKind, SlotRef
from strategy import StrategyState


def _ocr(text):
    return SimpleNamespace(frame_id="frame", normalized_text=text,
                           confidence=0.99)


def _god_state(ability_name_resolves=True):
    """最小可用的 StrategyState：1 个带 4 技能的古神 + 1 个敌方随从。"""
    def minion(entity_id, card_id, zone_pos, has_tags=False, used=0):
        return SimpleNamespace(
            entity_id=entity_id, card_id=card_id, zone_pos=zone_pos,
            has_minion_abilities=has_tags, minion_abilities_used=used,
            exhausted=0)
    return SimpleNamespace(
        is_end=False, is_my_turn=True, game_num_turns_in_play=3,
        my_player_id="1",
        my_minions=[minion("31", "BE_017", 1, True, 1)],
        my_locations=[], oppo_minions=[minion("77", "CS2_182", 1)],
        oppo_locations=[], my_hand_cards=[], oppo_hand_card_num=0,
        my_hero=None, oppo_hero=None, my_hero_power=None,
        discover_choice_count=None,
        minion_abilities={"BE_017": ("BE_017t3", "BE_017t4",
                                     "BE_017t5", "BE_017t6")},
        power_options={},
    )


class OldGodParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = RecommendationParser()

    def test_attack_primary_with_choose_marker_parses_as_ability(self):
        proposed = self.parser.parse(_ocr(
            "操作2号位随从攻击\n选择卡牌\n恩佐斯的召唤"), 3, 7)
        self.assertEqual(ActionKind.USE_MINION_ABILITY, proposed.action)
        self.assertEqual(SlotRef("board_slot", "friendly", 2), proposed.source)
        self.assertEqual("恩佐斯的召唤", proposed.choice_name)
        self.assertIsNone(proposed.target)

    def test_ability_with_target_line_binds_target(self):
        proposed = self.parser.parse(_ocr(
            "操作1号位随从攻击\n选择卡牌\n恩佐斯的领域\n目标是敌方3号位随从"),
            3, 7)
        self.assertEqual(ActionKind.USE_MINION_ABILITY, proposed.action)
        self.assertEqual(SlotRef("board_slot", "enemy", 3), proposed.target)

    def test_plain_attack_without_marker_stays_attack(self):
        proposed = self.parser.parse(_ocr(
            "操作2号位随从攻击\n目标是对方英雄"), 3, 7)
        self.assertEqual(ActionKind.ATTACK, proposed.action)
        self.assertEqual(SlotRef("hero", "enemy"), proposed.target)

    def test_minion_attack_without_target_or_marker_stays_attack(self):
        """普通随从攻击可以不带目标（打脸由点击层处理），保持原语义。"""
        proposed = self.parser.parse(_ocr("操作2号位随从攻击"), 3, 7)
        self.assertEqual(ActionKind.ATTACK, proposed.action)


class OldGodStrategyTests(unittest.TestCase):
    def _log_state(self):
        state = LogState()
        state.my_player_id = "1"
        # StrategyState 读取 is_end/is_my_turn 需要 Game/Player 实体在场
        state.add_entity("1", GameEntity())
        state.game_entity_id = "1"
        state.add_player_entity("2", "1", PlayerEntity())
        state.add_player_entity("3", "2", PlayerEntity())
        god = CardEntity("BE_017")
        god.set_tag("CARDTYPE", "MINION")
        god.set_tag("ZONE", "PLAY")
        god.set_tag("ZONE_POSITION", "1")
        god.set_tag("CONTROLLER", "1")
        god.set_tag("ACTIVATABLE_MINION_ABILITY_USED_3", "1")
        state.add_entity("31", god)
        for number, entity_id in ((3, "41"), (4, "42"), (5, "43"), (6, "44")):
            ability = CardEntity(f"BE_017t{number}")
            ability.set_tag("ZONE", "SETASIDE")
            ability.set_tag("CONTROLLER", "1")
            state.add_entity(entity_id, ability)
        return state

    def test_minion_abilities_collected_sorted_by_sub_card_number(self):
        snapshot = StrategyState(self._log_state())
        self.assertEqual(
            {"BE_017": ("BE_017t3", "BE_017t4", "BE_017t5", "BE_017t6")},
            snapshot.minion_abilities)
        minion = snapshot.my_minions[0]
        self.assertTrue(minion.has_minion_abilities)
        self.assertEqual(1, minion.minion_abilities_used)

    def test_enemy_abilities_are_not_collected(self):
        state = self._log_state()
        for entity in list(state.entity_dict.values()):
            entity.set_tag("CONTROLLER", "2")
        state.my_player_id = "1"
        snapshot = StrategyState(state)
        self.assertEqual({}, snapshot.minion_abilities)


class OldGodAdapterTests(unittest.TestCase):
    def _proposed(self, name="恩佐斯的召唤", slot=1, target=None):
        return SimpleNamespace(
            action=ActionKind.USE_MINION_ABILITY, turn_number=3,
            log_revision=7, normalized_instruction="操作1号位随从攻击",
            source=SlotRef("board_slot", "friendly", slot),
            target=target, choice_name=name)

    def test_ability_binds_menu_index_by_sorted_sub_card(self):
        state = _god_state()
        with patch.object(adapter_module, "ability_card_id_by_name",
                          side_effect={"恩佐斯的召唤": "BE_017t3"}.get):
            adapted = adapt_action(self._proposed(), state)
        manual = adapted.manual_action
        self.assertIsInstance(manual, UseMinionAbilityAction)
        self.assertEqual(0, manual.minion_index)          # 场上第 1 个
        self.assertEqual(0, manual.ability_index)         # t3 = 第 1 个按钮
        self.assertEqual(4, manual.ability_count)
        self.assertEqual("BE_017", manual.card_id)
        self.assertEqual("31", manual.minion_entity_id)
        self.assertEqual("minion_ability_used", adapted.postcondition)

    def test_second_ability_maps_to_second_button(self):
        state = _god_state()
        with patch.object(adapter_module, "ability_card_id_by_name",
                          side_effect={"恩佐斯的领域": "BE_017t6"}.get):
            adapted = adapt_action(self._proposed("恩佐斯的领域"), state)
        self.assertEqual(3, adapted.manual_action.ability_index)

    def test_normal_minion_is_rejected(self):
        state = _god_state()
        state.my_minions[0].has_minion_abilities = False
        del state.minion_abilities["BE_017"]
        with patch.object(adapter_module, "ability_card_id_by_name",
                          side_effect={"恩佐斯的召唤": "BE_017t3"}.get):
            with self.assertRaisesRegex(adapter_module.RecommendationStateError,
                                        "source_not_old_god"):
                adapt_action(self._proposed(), state)

    def test_unknown_ability_name_is_rejected(self):
        state = _god_state()
        with patch.object(adapter_module, "ability_card_id_by_name",
                          return_value=None):
            with self.assertRaisesRegex(adapter_module.RecommendationStateError,
                                        "ability_name_unresolvable"):
                adapt_action(self._proposed("乱码技能名"), state)


class OldGodExecutorTests(unittest.TestCase):
    def test_click_sequence_is_minion_then_ability_then_target(self):
        calls = []

        class FakeClick:
            def choose_my_board_entity(self, index, count):
                calls.append(("board", index, count))

            def choose_discover_card(self, index, count):
                calls.append(("ability", index, count))

            def choose_opponent_minion(self, index, count):
                calls.append(("target", index, count))

            def cancel_click(self):
                calls.append(("cancel",))

        executor = ClickExecutor(click_module=FakeClick(),
                                 sleep_func=lambda s: None)
        controller = ManualController(
            input_func=lambda prompt: "0",
            output_func=lambda text: None,
            executor=executor)
        state = SimpleNamespace(
            is_my_turn=True, game_num_turns_in_play=3,
            my_minions=[SimpleNamespace(card_id="BE_017", entity_id="31",
                                        zone_pos=1, name="恩佐斯，梦境之末")],
            my_locations=[],
            oppo_minions=[SimpleNamespace(entity_id="77", zone_pos=1)],
            oppo_locations=[])
        action = UseMinionAbilityAction(
            0, "BE_017", 3, 4, minion_entity_id="31",
            target=Target("enemy", "minion", 0, "77"))
        result = controller.execute(action, state)
        self.assertTrue(result.executed)
        self.assertEqual(
            [("board", 0, 1), ("ability", 3, 4), ("target", 0, 1),
             ("cancel",)], calls)


class DiscoverRandomFallbackTests(unittest.TestCase):
    """拼装/发现界面打开但盒子推荐不可读 → 连续 N 次后随机点一个选项。"""

    def _flow(self, attempts=2, provider=lambda count: count - 1,
              choice_count=3, fallback=True):
        recorded = []

        class Controller:
            def execute(self, action, state):
                recorded.append(action)
                return SimpleNamespace(executed=True, message="ok",
                                       recovery_needed=False)

            def output(self, text):
                pass

        class Reader:
            def read(self, *args, **kwargs):
                raise RuntimeError("OcrRejectedError:recommendation_not_stable")

            def read_frame(self, *args, **kwargs):
                raise RuntimeError("OcrRejectedError:recommendation_not_stable")

        state = SimpleNamespace(
            is_my_turn=True, is_end=False, game_num_turns_in_play=3,
            discover_choice_count=choice_count, hand_entry_count=0,
            start_of_game_card_count=0)
        flow = RecommendationFlow(
            capture=None, reader=Reader(), parser=None,
            state_supplier=lambda: (state, 7),
            adapter=None, validator=None, controller=Controller(),
            sleep=lambda s: None,
            discover_random_fallback=fallback,
            discover_random_fallback_attempts=attempts,
            random_index_provider=provider)
        return flow, recorded

    def test_random_choice_fires_after_streak_threshold(self):
        flow, recorded = self._flow(attempts=2)
        first = flow.run_player_turn_step()
        self.assertEqual(FlowStepStatus.RETRY, first.status)
        self.assertEqual([], recorded)
        second = flow.run_player_turn_step()
        self.assertEqual(FlowStepStatus.OBSERVE, second.status)
        self.assertEqual("random_discover_choice", second.diagnostics)
        self.assertEqual(1, len(recorded))
        self.assertEqual(2, recorded[0].choice_index)   # provider 取 count-1
        self.assertEqual(3, recorded[0].choice_count)

    def test_no_fallback_when_disabled(self):
        flow, recorded = self._flow(fallback=False)
        for _ in range(5):
            result = flow.run_player_turn_step()
            self.assertEqual(FlowStepStatus.RETRY, result.status)
        self.assertEqual([], recorded)

    def test_no_fallback_when_choice_ui_closed(self):
        flow, recorded = self._flow(choice_count=None, attempts=1)
        result = flow.run_player_turn_step()
        self.assertEqual(FlowStepStatus.RETRY, result.status)
        self.assertEqual([], recorded)

    def test_streak_resets_after_random_click(self):
        flow, recorded = self._flow(attempts=1)
        flow.run_player_turn_step()
        flow.run_player_turn_step()
        self.assertEqual(2, len(recorded))   # 每次失败立即再随机（阈值=1）


class MinionAbilityPostconditionTests(unittest.TestCase):
    def test_used_count_change_confirms_ability(self):
        before = SimpleNamespace(my_minions=[SimpleNamespace(
            entity_id="31", minion_abilities_used=1)])
        after = SimpleNamespace(my_minions=[SimpleNamespace(
            entity_id="31", minion_abilities_used=2)])
        self.assertTrue(RecommendationFlow._postcondition(
            "minion_ability_used", before, after, "31", None))
        self.assertFalse(RecommendationFlow._postcondition(
            "minion_ability_used", before, before, "31", None))


class RealCardsJsonTests(unittest.TestCase):
    """本机 cards.json 已刷新到 37.0：技能名必须能反查（文件缺失则跳过）。"""

    def test_nzoth_summon_resolves(self):
        from pathlib import Path
        cards = Path(__file__).resolve().parents[1] / "cards.json"
        if not cards.exists():
            self.skipTest("cards.json 不存在")
        self.assertEqual("BE_017t3",
                         old_gods.ability_card_id_by_name("恩佐斯的召唤"))


if __name__ == "__main__":
    unittest.main()
