# -*- coding: utf-8 -*-
"""LLM 决策层（src/brain）的回归测试——全部用注入的 fake 客户端，不发网络。

覆盖：状态编码 / 合法动作菜单（含古神技能枚举）/ 菜单约束解码 /
决策流（执行、失败拉黑、盒子回退、发现委托、只剩结束回合的捷径）/
留牌解码 / 配置解析 / 缺密钥退化。
"""

import unittest
from types import SimpleNamespace

import config
from config import DEFAULT_DECISION, decision_settings
from src.brain.action_decoder import (
    ActionDecodeError, decode_decision, decode_mulligan, extract_json,
)
from src.brain.decision_flow import (
    LlmDecisionFlow, ShadowDecider, llm_mulligan_proposal,
)
from src.brain.legal_actions import (
    build_proposed_action, enumerate_actions, menu_for_prompt,
)
from src.brain.llm_client import LlmClient, LlmUnavailableError
from src.brain.state_encoder import encode_state
from src.flow.recommendation_flow import FlowStepStatus
from src.recommendation_models import ActionKind


def _minion(entity_id, name="淡水鳄", attack=2, health=3, zone_pos=1,
            card_id="CS2_XXX", **flags):
    base = dict(entity_id=entity_id, card_id=card_id, zone_pos=zone_pos,
                exhausted=0, can_beat_face=False, can_attack_minion=False,
                can_be_attacked=True, health=health, attack=attack)
    base.update(flags)
    return SimpleNamespace(name=name, **base)


def _hand(index_name_cost, cardtype="SPELL", cost=None):
    """index_name_cost: (index, name, card_id)"""
    index, name, card_id = index_name_cost
    return SimpleNamespace(name=name, card_id=card_id,
                           current_cost=cost if cost is not None else 2,
                           cardtype=cardtype)


def _state(hand=None, mana=5, my_minions=None, oppo_minions=None,
           hero_power=None, abilities=None, discover=None, my_turn=True):
    return SimpleNamespace(
        game_num_turns_in_play=5, is_end=False, is_my_turn=my_turn,
        my_last_mana=mana, my_total_mana=mana,
        my_deck_num=20, oppo_deck_num=20, oppo_hand_card_num=4,
        my_hero=SimpleNamespace(name="吉安娜", health=25, armor=0,
                                attack=0, exhausted=0, cant_attack=0,
                                frozen=0, entity_id="H1"),
        oppo_hero=SimpleNamespace(name="雷克萨", health=20, armor=2,
                                  entity_id="H2"),
        my_hero_power=hero_power,
        my_minions=my_minions or [], my_locations=[],
        oppo_minions=oppo_minions or [], oppo_locations=[],
        my_hand_cards=hand or [],
        discover_choice_count=discover,
        minion_abilities=abilities or {},
    )


class FakeClient:
    """按序返回预设回复；记录调用。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def decide(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        if not self.replies:
            raise LlmUnavailableError("exhausted")
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeController:
    def __init__(self, executed=True):
        self.executed = executed
        self.actions = []

    def execute(self, action, state):
        self.actions.append(action)
        return SimpleNamespace(executed=self.executed, message="ok",
                               recovery_needed=False)

    def output(self, text):
        pass


def _llm_state():
    """带一张可打法术、一个可攻击随从、一个英雄技能的标准状态。"""
    hand = [_hand((1, "火球术", "CS2_029"), cardtype="SPELL", cost=4)]
    return _state(
        hand=hand, mana=5,
        my_minions=[_minion("31", "石牙野猪", 1, 1,
                            can_beat_face=True, can_attack_minion=True)],
        oppo_minions=[_minion("77", "精灵龙", 3, 2, zone_pos=1)],
        hero_power=SimpleNamespace(name="火焰冲击", exhausted=0,
                                   current_cost=2, entity_id="HP1"),
    )


class LegalMenuTests(unittest.TestCase):
    def test_menu_contains_play_attack_power_end(self):
        menu = enumerate_actions(_llm_state())
        ids = [item["id"] for item in menu]
        self.assertIn("play_1", ids)
        self.assertIn("attack_1_face", ids)
        self.assertIn("attack_1_1", ids)
        self.assertIn("hero_power", ids)
        self.assertIn("end_turn", ids)

    def test_unaffordable_cards_are_excluded(self):
        state = _llm_state()
        state.my_hand_cards[0].current_cost = 9
        ids = [item["id"] for item in enumerate_actions(state)]
        self.assertNotIn("play_1", ids)

    def test_taunt_blocks_face_but_not_minion(self):
        state = _llm_state()
        state.oppo_minions[0].taunt = 1
        state.oppo_minions[0].can_be_attacked = True
        state.my_minions[0].can_beat_face = False   # 有嘲讽时不能打脸
        ids = [item["id"] for item in enumerate_actions(state)]
        self.assertNotIn("attack_1_face", ids)
        self.assertIn("attack_1_1", ids)

    def test_old_god_abilities_are_enumerated_with_names(self):
        state = _llm_state()
        state.my_minions.append(_minion("32", "恩佐斯，梦境之末", 1, 1,
                                        zone_pos=2, card_id="BE_017",
                                        has_minion_abilities=True,
                                        minion_abilities_used=1))
        state.minion_abilities = {
            "BE_017": ("BE_017t3", "BE_017t4", "BE_017t5", "BE_017t6")}
        with patch_json_names():
            menu = enumerate_actions(state)
        ability_items = [item for item in menu
                         if item["kind"] == "ability"]
        self.assertEqual(4, len(ability_items))
        self.assertEqual("恩佐斯的召唤",
                         ability_items[0]["choice_name"])
        self.assertEqual("ability_2_1", ability_items[0]["id"])

    def test_failed_ids_are_removed(self):
        menu = enumerate_actions(_llm_state(), failed_ids={"play_1"})
        self.assertNotIn("play_1", [item["id"] for item in menu])

    def test_prompt_lists_ids_and_targets(self):
        prompt = menu_for_prompt(enumerate_actions(_llm_state()))
        self.assertIn("play_1", prompt)
        self.assertIn("enemy_minion_1", prompt)
        self.assertIn("end_turn", prompt)


def patch_json_names():
    """把 json_op 的 id→名称 查询打桩为 BE_017t* → 恩佐斯的四技能名。"""
    from unittest.mock import patch
    names = {"BE_017t3": "恩佐斯的召唤", "BE_017t4": "恩佐斯的操纵",
             "BE_017t5": "恩佐斯的大军", "BE_017t6": "恩佐斯的领域"}
    return patch("json_op.query_json_dict",
                 side_effect=lambda cid: names.get(cid, "未知卡牌"))


class DecodeTests(unittest.TestCase):
    def _menu(self):
        return enumerate_actions(_llm_state())

    def test_plain_json_extracted_from_fenced_reply(self):
        data = extract_json('好的！```json\n{"action": "end_turn"}\n```')
        self.assertEqual({"action": "end_turn"}, data)

    def test_play_spell_with_target(self):
        proposed = decode_decision('{"action": "play_1", "target": '
                                   '"enemy_minion_1"}', self._menu(),
                                   _llm_state(), 5, 7)
        self.assertEqual(ActionKind.PLAY_CARD, proposed.action)
        self.assertEqual(1, proposed.source.index)
        self.assertEqual("enemy", proposed.target.owner)
        self.assertEqual(1, proposed.target.index)
        self.assertEqual("play_1", proposed.action_id.split(":", 1)[1])

    def test_attack_item_carries_target(self):
        proposed = decode_decision('{"action": "attack_1_face"}',
                                   self._menu(), _llm_state(), 5, 7)
        self.assertEqual(ActionKind.ATTACK, proposed.action)
        self.assertEqual("enemy", proposed.target.owner)
        self.assertIsNone(proposed.target.index)

    def test_out_of_menu_action_rejected(self):
        with self.assertRaisesRegex(ActionDecodeError, "action_not_in_menu"):
            decode_decision('{"action": "concede"}', self._menu(),
                            _llm_state(), 5, 7)

    def test_invalid_target_rejected(self):
        with self.assertRaisesRegex(ActionDecodeError, "target_not_allowed"):
            decode_decision('{"action": "play_1", "target": '
                            '"enemy_minion_9"}', self._menu(),
                            _llm_state(), 5, 7)

    def test_mulligan_decode_validates_range(self):
        state = _state(hand=[_hand((1, "A", "X1"), cost=1),
                             _hand((2, "B", "X2"), cost=2)])
        proposed = decode_mulligan('{"replace": [1]}', state, 0, 3)
        self.assertEqual(ActionKind.MULLIGAN, proposed.action)
        self.assertEqual((1,), proposed.mulligan_slots)
        with self.assertRaises(ActionDecodeError):
            decode_mulligan('{"replace": [9]}', state, 0, 3)


class DecisionFlowTests(unittest.TestCase):
    def _flow(self, client, state=None, controller=None, box=None,
              post_delay=0.0):
        state = state if state is not None else _llm_state()
        controller = controller or FakeController()
        flow = LlmDecisionFlow(
            client=client, state_supplier=lambda: (state, 7),
            adapter=_fake_adapter, controller=controller, box_flow=box,
            sleep=lambda s: None, post_action_delay=post_delay,
            draw_extra_delay_per_card=0.0)
        return flow, state, controller

    def test_happy_path_executes_llm_choice(self):
        client = FakeClient(['{"action": "play_1", "target": '
                             '"enemy_minion_1"}'])
        controller = FakeController()
        flow, state, controller = self._flow(client, controller=controller)
        # 状态 supplier 每次 revision +1，模拟执行后日志推进
        revisions = iter([7, 7, 8, 8, 9])
        flow.state_supplier = lambda: (state, next(revisions, 9))
        result = flow.run_player_turn_step()
        self.assertEqual(FlowStepStatus.EXECUTED, result.status)
        self.assertEqual(1, len(controller.actions))

    def test_api_failure_falls_back_to_box_flow(self):
        box = SimpleNamespace(
            run_player_turn_step=lambda: SimpleNamespace(
                status=FlowStepStatus.EXECUTED, diagnostics=""))
        client = FakeClient([LlmUnavailableError("timeout")])
        flow, _state, _controller = self._flow(client, box=box)
        result = flow.run_player_turn_step()
        self.assertEqual(FlowStepStatus.EXECUTED, result.status)

    def test_decode_failure_twice_falls_back_to_box(self):
        box = SimpleNamespace(
            run_player_turn_step=lambda: SimpleNamespace(
                status=FlowStepStatus.RETRY, diagnostics="box_retry"))
        client = FakeClient(["完全不是 JSON", "还不是 JSON"])
        flow, _state, _controller = self._flow(client, box=box)
        result = flow.run_player_turn_step()
        self.assertEqual(FlowStepStatus.RETRY, result.status)
        self.assertIn("box_retry", result.diagnostics)

    def test_no_box_fallback_returns_retry(self):
        client = FakeClient([LlmUnavailableError("down")])
        flow, _state, _controller = self._flow(client, box=None)
        result = flow.run_player_turn_step()
        self.assertEqual(FlowStepStatus.RETRY, result.status)
        self.assertIn("llm_unavailable", result.diagnostics)

    def test_discover_ui_delegates_to_box(self):
        state = _llm_state()
        state.discover_choice_count = 3
        box = SimpleNamespace(
            run_player_turn_step=lambda: SimpleNamespace(
                status=FlowStepStatus.OBSERVE,
                diagnostics="random_discover_choice"))
        client = FakeClient([])
        flow, _state, _controller = self._flow(client, state=state, box=box)
        result = flow.run_player_turn_step()
        self.assertEqual(FlowStepStatus.OBSERVE, result.status)
        self.assertEqual("random_discover_choice", result.diagnostics)

    def test_only_end_turn_skips_api(self):
        state = _state(hand=[], mana=0)
        client = FakeClient(replies=[])          # 不应有调用
        flow, _snap, controller = self._flow(client, state=state)
        result = flow.run_player_turn_step()
        self.assertEqual(FlowStepStatus.EXECUTED, result.status)
        self.assertEqual([], client.calls)
        self.assertEqual(1, len(controller.actions))   # EndTurnAction

    def test_executed_failure_blocks_menu_id(self):
        from src.brain.legal_actions import enumerate_actions as enum
        controller = FakeController(executed=False)
        client = FakeClient(['{"action": "play_1", "target": "none"}'] * 2)
        flow, state, _controller = self._flow(client, controller=controller)
        result = flow.run_player_turn_step()
        self.assertEqual(FlowStepStatus.RETRY, result.status)
        # 失败的 play_1 被拉黑：下一次菜单不再包含它
        self.assertNotIn("play_1",
                         [item["id"] for item in
                          enum(state, flow._failed_menu_ids)])


def _fake_adapter(proposed, state):
    """决策流测试的适配桩：不做实体绑定，只透传动作类型。"""
    return SimpleNamespace(
        manual_action=SimpleNamespace(action=proposed.action),
        source_entity_id=None, target_entity_id=None,
        postcondition="always_true")


class MulliganProposalTests(unittest.TestCase):
    def test_llm_mulligan_returns_mulligan_action(self):
        state = _state(hand=[_hand((1, "A", "X1"), cost=1),
                             _hand((2, "B", "X2"), cost=7)])
        client = FakeClient(['{"replace": [2]}'])
        proposed = llm_mulligan_proposal(state, client, 0, 3)
        self.assertEqual(ActionKind.MULLIGAN, proposed.action)
        self.assertEqual((2,), proposed.mulligan_slots)


class ShadowDeciderTests(unittest.TestCase):
    def test_shadow_logs_llm_preference(self):
        logs = []
        client = FakeClient(['{"action": "attack_1_face"}'])
        decider = ShadowDecider(client, lambda: (_llm_state(), 7),
                                output=logs.append)
        decider.observe()
        self.assertTrue(any("决策对比" in line and "攻击" in line
                            for line in logs))

    def test_shadow_silently_skips_discover(self):
        logs = []
        state = _llm_state()
        state.discover_choice_count = 2
        client = FakeClient([])
        decider = ShadowDecider(client, lambda: (state, 7),
                                output=logs.append)
        decider.observe()
        self.assertEqual([], logs)
        self.assertEqual([], client.calls)


class LlmClientTests(unittest.TestCase):
    def test_missing_key_raises_unavailable(self):
        client = LlmClient(dict(DEFAULT_DECISION), api_key="")
        with self.assertRaises(LlmUnavailableError):
            client.decide("sys", "user")

    def test_http_error_and_retry_then_success(self):
        calls = []

        def fake_post(url, payload, timeout, headers):
            calls.append(1)
            if len(calls) < 2:
                return 500, "boom"
            return 200, '{"choices":[{"message":{"content":"{\\"action\\": \\"end_turn\\"}"}}]}'

        client = LlmClient(dict(DEFAULT_DECISION, max_retries=1),
                           api_key="k", post=fake_post)
        self.assertEqual('{"action": "end_turn"}',
                         client.decide("sys", "user"))
        self.assertEqual(2, len(calls))


class DecisionConfigTests(unittest.TestCase):
    def test_default_backend_is_box(self):
        self.assertEqual("box", DEFAULT_DECISION["backend"])
        from unittest.mock import patch
        with patch.object(config, "_load_ui_config", return_value={}):
            # 用户可能已在网页把后端存成 shadow/llm：断言针对"未配置"默认值
            self.assertEqual("box", decision_settings()["backend"])

    def test_invalid_backend_falls_back_to_box(self):
        import config
        from unittest.mock import patch
        with patch.object(config, "_load_ui_config",
                          return_value={"decision": {"backend": "yolo"}}):
            self.assertEqual("box", decision_settings()["backend"])

    def test_local_fields_reserved(self):
        self.assertIn("local_base_url", DEFAULT_DECISION)
        self.assertIn("local_model", DEFAULT_DECISION)


class EncoderTests(unittest.TestCase):
    def test_state_block_contains_key_facts(self):
        text = encode_state(_llm_state())
        self.assertIn("回合 5", text)
        self.assertIn("火球术", text)
        self.assertIn("石牙野猪", text)
        self.assertIn("英雄技能", text)


if __name__ == "__main__":
    unittest.main()
