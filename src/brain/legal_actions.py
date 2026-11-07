# -*- coding: utf-8 -*-
"""从 StrategyState 确定性枚举当前全部合法动作（模型的选择菜单）。

这是整个 LLM 决策方案的安全基石：模型只能在菜单 id 里选，目标只能在
item.targets 里选，不可能幻觉出打不出的牌或点不到的目标。菜单里同时携带
解码所需的全部字段（hand_index/cardtype/choice_name...），提示词只展示
id+描述+可选目标，避免泄露内部结构。
"""

from src.recommendation_models import ActionKind

# 目标 id 前缀（菜单/解码共用）：
#   enemy_minion_<1基序号> / enemy_hero / friendly_minion_<1基序号> /
#   friendly_hero / none
_NONE_TARGET = "none"


def _target_id(kind, side, index):
    if kind == "hero":
        return "enemy_hero" if side == "enemy" else "friendly_hero"
    return f"{'enemy' if side == 'enemy' else 'friendly'}_minion_{index}"


def _spell_targets(state):
    """目标型法术/技能可选的全部目标（按显示序号 1 基）。"""
    targets = []
    for index, _minion in enumerate(state.oppo_minions, 1):
        targets.append(_target_id("minion", "enemy", index))
    if getattr(state, "oppo_hero", None) is not None:
        targets.append("enemy_hero")
    for index, _minion in enumerate(state.my_minions, 1):
        targets.append(_target_id("minion", "friendly", index))
    if getattr(state, "my_hero", None) is not None:
        targets.append("friendly_hero")
    return targets


def _parse_target_id(target_id, state):
    """目标 id → SlotRef 语义元组 (side, kind, index)；none 返回 None。"""
    if not target_id or target_id == _NONE_TARGET:
        return None
    side, _, rest = target_id.partition("_")
    if side not in ("enemy", "friendly"):
        raise ValueError(f"bad_target:{target_id}")
    if rest == "hero":
        return (side, "hero", None)
    if rest.startswith("minion_"):
        index = int(rest.split("_", 1)[1])
        collection = (state.oppo_minions if side == "enemy"
                      else state.my_minions)
        if not 1 <= index <= len(collection):
            raise ValueError(f"bad_target:{target_id}")
        return (side, "minion", index)
    raise ValueError(f"bad_target:{target_id}")


def enumerate_actions(state, failed_ids=()):
    """返回当前合法动作菜单：[{id, desc, targets, ...解码字段}, ...]。

    failed_ids 里出现过的 id 会被剔除（上一次执行失败的动作不再提供，
    迫使模型换一个选择，形成自纠错循环）。
    """
    menu = []
    mana = state.my_last_mana
    board_full = (len(state.my_minions) + len(state.my_locations) >= 7)

    for index, card in enumerate(state.my_hand_cards, 1):
        if card.current_cost > mana:
            continue
        if card.cardtype == "MINION" and board_full:
            continue
        targets = None
        if card.cardtype == "SPELL":
            targets = [_NONE_TARGET] + _spell_targets(state)
        item = {
            "id": f"play_{index}", "kind": "play",
            "desc": f"打出{index}号位{card.cardtype} {card.name}（费"
                    f"{card.current_cost}）",
            "hand_index": index, "cardtype": card.cardtype,
            "name": str(getattr(card, "name", "")),
        }
        if targets is not None:
            item["targets"] = targets
        menu.append(item)

    for index, minion in enumerate(state.my_minions, 1):
        if getattr(minion, "can_beat_face", False):
            menu.append({
                "id": f"attack_{index}_face", "kind": "attack",
                "desc": f"{index}号位 {minion.name} 攻击敌方英雄",
                "minion_index": index, "target_id": "enemy_hero",
            })
        if getattr(minion, "can_attack_minion", False):
            for j, enemy in enumerate(state.oppo_minions, 1):
                if getattr(enemy, "can_be_attacked", True):
                    menu.append({
                        "id": f"attack_{index}_{j}", "kind": "attack",
                        "desc": f"{index}号位 {minion.name} 攻击敌方"
                                f"{j}号位 {enemy.name}",
                        "minion_index": index,
                        "target_id": _target_id("minion", "enemy", j),
                    })

    hero = getattr(state, "my_hero", None)
    if (hero is not None and getattr(hero, "attack", 0) > 0
            and not getattr(hero, "cant_attack", 0)
            and not getattr(hero, "exhausted", 0)
            and not getattr(hero, "frozen", 0)):
        menu.append({
            "id": "hero_attack_face", "kind": "attack",
            "desc": f"我方英雄攻击敌方英雄（攻击力 {hero.attack}）",
            "minion_index": None, "target_id": "enemy_hero",
        })

    power = getattr(state, "my_hero_power", None)
    if (power is not None and getattr(power, "exhausted", 0) == 0
            and getattr(power, "current_cost", 0) <= mana):
        item = {
            "id": "hero_power", "kind": "hero_power",
            "desc": f"使用英雄技能 {power.name}",
            "targets": [_NONE_TARGET] + _spell_targets(state),
        }
        menu.append(item)

    abilities = getattr(state, "minion_abilities", {}) or {}
    for index, minion in enumerate(state.my_minions, 1):
        order = abilities.get(getattr(minion, "card_id", ""), ())
        if not order:
            continue
        from json_op import query_json_dict
        for k, ability_id in enumerate(order, 1):
            ability_name = str(query_json_dict(ability_id) or "")
            menu.append({
                "id": f"ability_{index}_{k}", "kind": "ability",
                "desc": f"使用{index}号位 {minion.name} 技能"
                        f"「{ability_name}」",
                "minion_index": index, "choice_name": ability_name,
                "targets": [_NONE_TARGET] + _spell_targets(state),
            })

    menu.append({"id": "end_turn", "kind": "end_turn", "desc": "结束回合"})

    failed = set(failed_ids or ())
    return [item for item in menu if item["id"] not in failed]


def menu_for_prompt(menu):
    """菜单 → 提示词文本（只含 id/描述/可选目标）。"""
    lines = ["可选动作（必须且只能从中选择一个 id）:"]
    for item in menu:
        line = f"  {item['id']}: {item['desc']}"
        if item.get("targets"):
            line += f" | 可选目标: {','.join(item['targets'])}"
        lines.append(line)
    return "\n".join(lines)


def build_proposed_action(item, payload, state, turn, revision):
    """菜单项 + 模型输出 → ProposedAction 所需字段（不含日志帧信息）。

    payload 形如 {"action": "play_3", "target": "enemy_minion_2",
    "gap": 2}；target/gap 校验失败抛 ValueError。
    返回 (action_kind, source, target, destination, extra_kwargs)。
    """
    from src.recommendation_models import SlotRef

    kind = item["kind"]
    target = None
    if kind in ("play", "hero_power", "ability"):
        target_id = str(payload.get("target") or _NONE_TARGET)
        allowed = item.get("targets")
        if allowed is not None and target_id not in allowed:
            raise ValueError(f"target_not_allowed:{target_id}")
        parsed = _parse_target_id(target_id, state)
        if parsed is not None:
            side, target_kind, index = parsed
            target = SlotRef("board_slot" if target_kind == "minion"
                             else "hero", side, index)

    if kind == "play":
        source = SlotRef("hand_slot", "friendly", item["hand_index"])
        destination = None
        gap = payload.get("gap")
        if gap is not None and item["cardtype"] in ("MINION", "LOCATION"):
            gap = int(gap)
            board_count = len(state.my_minions) + len(state.my_locations)
            if not 1 <= gap <= board_count + 1:
                raise ValueError(f"bad_gap:{gap}")
            destination = SlotRef("board_slot", "friendly", gap)
        extra = {"card_type": item["cardtype"]}
        if target is not None:
            extra["target"] = target
        if destination is not None:
            extra["destination"] = destination
        return ActionKind.PLAY_CARD, source, extra

    if kind == "attack":
        source = (SlotRef("board_slot", "friendly", item["minion_index"])
                  if item["minion_index"] is not None
                  else SlotRef("hero", "friendly"))
        parsed = _parse_target_id(item["target_id"], state)
        if parsed is None:
            raise ValueError("attack_target_required")
        side, target_kind, index = parsed
        return ActionKind.ATTACK, source, {
            "target": SlotRef(
                "board_slot" if target_kind == "minion" else "hero",
                side, index)}

    if kind == "hero_power":
        extra = {}
        if target is not None:
            extra["target"] = target
        return ActionKind.USE_HERO_POWER, SlotRef(
            "hero_power", "friendly"), extra

    if kind == "ability":
        extra = {
            "source": SlotRef("board_slot", "friendly",
                              item["minion_index"]),
            "choice_name": item["choice_name"],
        }
        if target is not None:
            extra["target"] = target
        return ActionKind.USE_MINION_ABILITY, extra["source"], extra

    if kind == "end_turn":
        return ActionKind.END_TURN, None, {}

    raise ValueError(f"unknown_kind:{kind}")
