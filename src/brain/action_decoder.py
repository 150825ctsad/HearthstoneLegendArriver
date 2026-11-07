# -*- coding: utf-8 -*-
"""模型输出 → ProposedAction（菜单约束解码 + 容错 JSON 提取）。"""

import json
import re
import time
import uuid

from src.recommendation_models import ActionKind, ProposedAction
from src.brain.legal_actions import build_proposed_action

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class ActionDecodeError(ValueError):
    """模型输出无法解析成菜单内的合法动作。"""


def extract_json(text):
    """从模型文本里抠出第一个 JSON 对象（容忍代码块围栏与前后废话）。"""
    match = _JSON_RE.search(str(text or ""))
    if not match:
        raise ActionDecodeError("no_json_in_response")
    try:
        return json.loads(match.group(0))
    except Exception as exc:
        raise ActionDecodeError(f"bad_json: {exc}") from exc


def decode_decision(text, menu, state, turn, revision):
    """模型文本 → ProposedAction；越菜单/非法目标抛 ActionDecodeError。"""
    payload = extract_json(text)
    action_id = str(payload.get("action") or "").strip()
    item = next((entry for entry in menu if entry["id"] == action_id), None)
    if item is None:
        raise ActionDecodeError(f"action_not_in_menu:{action_id}")
    try:
        kind, source, extra = build_proposed_action(
            item, payload, state, turn, revision)
    except ValueError as exc:
        raise ActionDecodeError(str(exc)) from exc

    kwargs = {}
    for field in ("target", "destination"):
        if field in extra:
            kwargs[field] = extra[field]
    if kind is ActionKind.PLAY_CARD:
        kwargs["card_type"] = extra.get("card_type")
    if kind is ActionKind.USE_MINION_ABILITY:
        kwargs["choice_name"] = extra["choice_name"]
    if kind is ActionKind.MULLIGAN:  # 理论不会出现，防御
        kwargs["mulligan_slots"] = ()
    return ProposedAction(
        # action_id 携带菜单 id（llm:<菜单id>），供执行失败后精准拉黑该动作
        action_id=f"llm:{item['id']}", frame_id=f"llm-{revision}",
        created_at=time.time(), turn_number=turn, log_revision=revision,
        raw_instruction=item["desc"],
        normalized_instruction=item["desc"],
        action=kind, source=source,
        ocr_confidence=1.0, semantic_confidence=1.0, **kwargs)


def decode_mulligan(text, state, turn, revision):
    """留牌输出 {"replace": [1,3]} → MULLIGAN ProposedAction。"""
    payload = extract_json(text)
    raw_slots = payload.get("replace")
    if not isinstance(raw_slots, list):
        raise ActionDecodeError("replace_not_list")
    slots = set()
    hand_count = len(state.my_hand_cards)
    for value in raw_slots:
        try:
            slot = int(value)
        except (TypeError, ValueError) as exc:
            raise ActionDecodeError(f"bad_slot:{value}") from exc
        if not 1 <= slot <= hand_count:
            raise ActionDecodeError(f"slot_out_of_range:{slot}")
        slots.add(slot)
    return ProposedAction(
        action_id=f"llm-{uuid.uuid4()}", frame_id=f"llm-{revision}",
        created_at=time.time(), turn_number=turn, log_revision=revision,
        raw_instruction="LLM 留牌决策",
        normalized_instruction="LLM 留牌决策",
        action=ActionKind.MULLIGAN,
        mulligan_slots=tuple(sorted(slots)),
        ocr_confidence=1.0, semantic_confidence=1.0)
