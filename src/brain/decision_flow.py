# -*- coding: utf-8 -*-
"""LLM 决策流：菜单约束下逐动作决策，下游全部复用现有安全管线。

执行路径与盒子流程（RecommendationFlow）完全同构：
  状态 → 动作菜单 → LLM 选择 → ProposedAction → adapt_action（实体绑定）
  → 执行器点击 → 后置条件验证。区别只在「提案从哪来」。

失败链（任何一环都不会空转）：
  LLM 超时/缺密钥 → box_flow（盒子 OCR 流程）
  解码失败 ×2     → box_flow
  动作被适配层拒绝 → 该菜单 id 进入失败名单（模型下次必须换选择）
  发现/拼装界面   → 委托 box_flow（它有界面检测 + 随机兜底）
  菜单只剩结束回合 → 不调 API，直接执行

抽牌延时/手牌动画记账复用 RecommendationFlow 的既有实现（组合而非复制）。
"""

import time
import uuid

from manual_controller import EndTurnAction
from src.recommendation_models import ActionKind, ProposedAction
from src.flow.recommendation_flow import (
    ConsumedActionStore, FlowStepResult, FlowStepStatus, RecommendationFlow,
)
from src.brain.action_decoder import ActionDecodeError, decode_decision
from src.brain.legal_actions import enumerate_actions, menu_for_prompt
from src.brain.state_encoder import encode_state

SYSTEM_PROMPT = (
    "你是《炉石传说》的对局决策引擎。你会收到当前对局状态与合法动作菜单，"
    "必须且只能从菜单中选择一个 id，并按下面的 JSON 格式输出，"
    "不要输出任何其它内容：\n"
    '{"action": "<菜单id>", "target": "<目标id，仅当动作列出可选目标时>", '
    '"gap": <放置位置1基序号，仅随从/地标需要时>}\n'
    "决策要点：优先法力利用与场面交换；注意嘲讽不能打脸；古神技能全用完"
    "前该随从不能攻击；有斩杀机会时优先斩杀。")


class LlmDecisionFlow:
    """与 RecommendationFlow 同签名的决策流（duck-type 可互换）。"""

    def __init__(self, client, state_supplier, adapter, controller,
                 box_flow=None, sleep=time.sleep, clock=time.monotonic,
                 stopped=lambda: False, consumed=None,
                 post_action_delay=0.0, post_action_pause=None,
                 draw_extra_delay_per_card=0.0, history_block="",
                 recorder=None):
        self.client = client
        self.state_supplier = state_supplier
        self.adapter = adapter
        self.controller = controller
        self.box_flow = box_flow
        self.sleep = sleep
        self.clock = clock
        self.stopped = stopped
        self.consumed = consumed or ConsumedActionStore()
        self.post_action_delay = float(post_action_delay or 0.0)
        self.post_action_pause = post_action_pause
        # 近期对战经验块（battle_history 摘要，构建一次随流程复用）
        self.history_block = str(history_block or "")
        # 决策点采集器（案例库/行为克隆数据源；None=不采集）
        self.recorder = recorder
        # 抽牌额外延时/手牌动画记账：组合一个仅做记账的 RecommendationFlow，
        # 复用其已测实现（不跑它的 run_player_turn_step）。
        self._accounting = RecommendationFlow(
            capture=None, reader=None, parser=None,
            state_supplier=state_supplier, adapter=None, validator=None,
            controller=controller, sleep=sleep, clock=clock,
            draw_extra_delay_per_card=draw_extra_delay_per_card)
        self._failed_menu_ids = set()
        self._last_revision = None

    # ------------------------------------------------------------ 主入口
    def run_player_turn_step(self):
        try:
            return self._step()
        except Exception as exc:
            return self._fallback_or_retry(f"{type(exc).__name__}:{exc}")

    def _step(self):
        state, revision = self.state_supplier()
        self._accounting.hand_animation_delay.observe(
            getattr(state, "hand_entry_count", 0))
        self._accounting._account_hand_entries(state)
        if revision != self._last_revision:
            self._failed_menu_ids = set()
            self._last_revision = revision
        if getattr(state, "is_end", False):
            return FlowStepResult(FlowStepStatus.OBSERVE, "game_ended")
        if not state.is_my_turn:
            return FlowStepResult(FlowStepStatus.OBSERVE, "opponent_turn")

        # 发现/拼装界面：委托盒子流程（有界面检测与随机兜底）
        if getattr(state, "discover_choice_count", None):
            if self.box_flow is not None:
                return self.box_flow.run_player_turn_step()
            return FlowStepResult(FlowStepStatus.RETRY,
                                  "discover_delegate_unavailable")
        if self.stopped():
            return FlowStepResult(FlowStepStatus.RETRY, "stopped")

        menu = enumerate_actions(state, self._failed_menu_ids)
        if not menu:
            return FlowStepResult(FlowStepStatus.RETRY, "empty_menu")

        # 菜单只剩结束回合：不调 API
        if len(menu) == 1 and menu[0]["kind"] == "end_turn":
            return self._execute_end_turn(state, revision)

        user_prompt = (encode_state(state) + "\n\n"
                       + menu_for_prompt(menu))
        if self.history_block:
            user_prompt = self.history_block + "\n\n" + user_prompt
        try:
            answer = self.client.decide(SYSTEM_PROMPT, user_prompt)
        except Exception as exc:
            return self._fallback_or_retry(f"llm_unavailable:{exc}")

        proposed = None
        last_error = None
        for _ in range(2):                      # 一次自纠错重问
            try:
                proposed = decode_decision(
                    answer, menu, state,
                    state.game_num_turns_in_play, revision)
                break
            except ActionDecodeError as exc:
                last_error = str(exc)
                answer = self.client.decide(
                    SYSTEM_PROMPT,
                    user_prompt + f"\n\n上次输出错误：{last_error}，"
                                  "请重新只输出合法 JSON。")
        if proposed is None:
            self._failed_menu_ids.update(
                item["id"] for item in menu)    # 全部拉黑换盒子来
            return self._fallback_or_retry(f"decode_failed:{last_error}")

        return self._validate_and_execute(proposed, state, revision)

    # ------------------------------------------------------------ 执行
    def _validate_and_execute(self, proposed, state, revision):
        if (proposed.turn_number != state.game_num_turns_in_play
                or proposed.log_revision != revision
                or not state.is_my_turn or self.stopped()):
            return FlowStepResult(FlowStepStatus.RETRY, "stale_decision")
        try:
            adapted = self.adapter(proposed, state)
        except Exception as exc:
            self._failed_menu_ids.add(self._menu_id_of(proposed))
            return FlowStepResult(FlowStepStatus.RETRY,
                                  f"adapt_rejected:{exc}")

        fresh_state, fresh_revision = self._wait_hand_animation()
        if fresh_state.game_num_turns_in_play != proposed.turn_number:
            return FlowStepResult(FlowStepStatus.RETRY, "turn_changed")

        print(f"[推荐(LLM)] {proposed.normalized_instruction}")
        print("[执行] 开始点击。")
        result = self.controller.execute(adapted.manual_action, fresh_state)
        if not result.executed or result.recovery_needed:
            self._failed_menu_ids.add(self._menu_id_of(proposed))
            return FlowStepResult(FlowStepStatus.RETRY,
                                  f"execution_failed:{result.message}")

        self._mark_consumed(proposed, adapted)
        self._post_action_pause_and_delay()

        # 结果验证：日志推进 + 后置条件（与盒子流程同一套 postcondition）
        after_state, after_revision = self.state_supplier()
        self._accounting._account_hand_entries(after_state)
        verified = RecommendationFlow._postcondition(
            adapted.postcondition, fresh_state, after_state,
            adapted.source_entity_id, adapted.target_entity_id)
        if after_revision <= fresh_revision and not verified:
            self._failed_menu_ids.add(self._menu_id_of(proposed))
            return FlowStepResult(FlowStepStatus.RETRY,
                                  "result_not_confirmed")
        self._failed_menu_ids = set()
        if self.recorder is not None:
            try:
                from src.brain.state_encoder import encode_state_features
                self.recorder.record(
                    source="llm",
                    game_generation=getattr(
                        fresh_state, "game_generation", None),
                    turn=getattr(fresh_state,
                                 "game_num_turns_in_play", None),
                    state=encode_state_features(fresh_state),
                    instruction=proposed.normalized_instruction,
                    action=getattr(proposed.action, "value",
                                   str(proposed.action)),
                    verified=True,
                    chosen_id=self._menu_id_of(proposed),
                )
            except Exception:
                pass
        return FlowStepResult(FlowStepStatus.EXECUTED)

    def _execute_end_turn(self, state, revision):
        proposed = ProposedAction(
            action_id="llm:end_turn", frame_id=f"llm-{revision}",
            created_at=time.time(), turn_number=state.game_num_turns_in_play,
            log_revision=revision, raw_instruction="结束回合",
            normalized_instruction="结束回合",
            action=ActionKind.END_TURN,
            ocr_confidence=1.0, semantic_confidence=1.0)
        print("[推荐(LLM)] 结束回合（菜单无其它合法动作）")
        result = self.controller.execute(EndTurnAction(), state)
        if not result.executed or result.recovery_needed:
            return FlowStepResult(FlowStepStatus.RETRY,
                                  f"execution_failed:{result.message}")
        return FlowStepResult(FlowStepStatus.EXECUTED)

    # ------------------------------------------------------------ 工具
    def _fallback_or_retry(self, diagnostics):
        if self.box_flow is not None:
            try:
                return self.box_flow.run_player_turn_step()
            except Exception as exc:
                return FlowStepResult(
                    FlowStepStatus.RETRY,
                    f"{diagnostics};box_fallback_failed:{exc}")
        return FlowStepResult(FlowStepStatus.RETRY, diagnostics)

    def _menu_id_of(self, proposed):
        """从 action_id（llm:<菜单id>）取菜单 id；非 LLM 提案返回其指令文本。"""
        action_id = getattr(proposed, "action_id", "") or ""
        if action_id.startswith("llm:"):
            return action_id.split(":", 1)[1]
        return proposed.normalized_instruction

    def _wait_hand_animation(self):
        while True:
            state, revision = self.state_supplier()
            self._accounting.hand_animation_delay.observe(
                getattr(state, "hand_entry_count", 0))
            remaining = self._accounting.hand_animation_delay.remaining()
            if remaining <= 0:
                return state, revision
            self.sleep(remaining)

    def _mark_consumed(self, proposed, adapted):
        try:
            self.consumed.mark_consumed(
                ConsumedActionStore.key_for(proposed, adapted))
        except Exception:
            pass

    def _post_action_pause_and_delay(self):
        paused = False
        if self.post_action_pause is not None:
            try:
                paused = bool(self.post_action_pause())
            except Exception:
                paused = False
        if not paused and self.post_action_delay:
            output = getattr(self.controller, "output", None)
            if output is not None:
                try:
                    output(f"[SYS] 操作结束，延时 "
                           f"{self.post_action_delay:.1f}s 后继续")
                except Exception:
                    pass
            self.sleep(self.post_action_delay)


# ---------------------------------------------------------------- 留牌（LLM）
MULLIGAN_SYSTEM_PROMPT = (
    "你是《炉石传说》的留牌决策引擎。你会看到起手手牌（卡名/费用/效果文字），"
    "只输出一个 JSON 对象：{\"replace\": [要替换的手牌编号数组]}，"
    "编号为 1 基；不留任何牌输出 {\"replace\": []}。不要输出其它内容。"
    "留牌要点：优先保留低费曲线与起手能展开的牌，换掉高费卡手牌。")


def llm_mulligan_proposal(state, client, turn, revision,
                          history_block=""):
    """LLM 留牌决策 → MULLIGAN ProposedAction；失败抛异常（调用方回退 OCR）。"""
    from src.brain.action_decoder import decode_mulligan
    hand_lines = []
    for index, card in enumerate(state.my_hand_cards, 1):
        from src.brain.state_encoder import card_text
        text = card_text(getattr(card, "card_id", ""))
        hand_lines.append(f"[{index}] {card.name} 费{card.current_cost}"
                          f" {card.cardtype}"
                          + (f" —— {text}" if text else ""))
    prompt = (history_block + "\n\n" if history_block else "") + (
        "起手手牌如下：\n" + "\n".join(hand_lines)
        + "\n\n请给出留牌决策（replace 数组为要替换的编号）。")
    answer = client.decide(MULLIGAN_SYSTEM_PROMPT, prompt)
    return decode_mulligan(answer, state, turn, revision)


# ---------------------------------------------------------------- 影子模式
class ShadowDecider:
    """影子模式：盒子照常决策执行，LLM 同步给出倾向写入日志做对比。"""

    def __init__(self, client, state_supplier, output=None,
                 history_block=""):
        self.client = client
        self.state_supplier = state_supplier
        self.output = output or print
        self.history_block = str(history_block or "")

    def observe(self):
        state, revision = self.state_supplier()
        if getattr(state, "discover_choice_count", None):
            return                      # 发现/拼装界面交给盒子通道
        if not getattr(state, "is_my_turn", False):
            return
        menu = enumerate_actions(state)
        if len(menu) <= 1:
            return
        user_prompt = encode_state(state) + "\n\n" + menu_for_prompt(menu)
        if self.history_block:
            user_prompt = self.history_block + "\n\n" + user_prompt
        try:
            answer = self.client.decide(SYSTEM_PROMPT, user_prompt)
            proposed = decode_decision(answer, menu, state,
                                       state.game_num_turns_in_play,
                                       revision)
            self.output(f"[决策对比] LLM 倾向: "
                        f"{proposed.normalized_instruction}")
        except Exception as exc:
            self.output(f"[决策对比] LLM 不可用: {exc}")
