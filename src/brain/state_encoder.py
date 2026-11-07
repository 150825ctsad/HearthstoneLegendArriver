# -*- coding: utf-8 -*-
"""StrategyState → LLM 可读的紧凑对局状态（含 cards.json 卡牌文字注入）。

卡牌文字是关键：模型（尤其小模型/不认识新版本的模型）不可能背下全部卡牌，
把手牌与场面关键牌的规则文字直接喂进提示词，模型才有判断依据。文字来自
cards.json（脚本启动时已刷新到最新版本，新卡自动覆盖）。
"""

from json_op import card_text

# 场面随从编码时会带上这些关键词旗标（StrategyMinion 字段 → 中文标签）。
_MINION_FLAGS = (
    ("taunt", "嘲讽"), ("divine_shield", "圣盾"), ("stealth", "潜行"),
    ("poisonous", "剧毒"), ("windfury", "风怒"), ("charge", "冲锋"),
    ("rush", "突袭"), ("frozen", "冻结"), ("dormant", "休眠"),
    ("immune", "免疫"),
)
# 卡牌文字查询下沉在数据层 json_op.card_text，此处直接复用，不再本地实现。


def _flags(minion) -> str:
    labels = [label for field, label in _MINION_FLAGS
              if getattr(minion, field, 0)]
    return ("(" + "/".join(labels) + ")") if labels else ""


def _hero_line(hero) -> str:
    if hero is None:
        return "未知"
    health = getattr(hero, "health", None)
    armor = getattr(hero, "armor", 0)
    attack = getattr(hero, "attack", 0)
    parts = [f"{hero.name} 生命 {health}+{armor}" if health is not None
             else str(hero.name)]
    if attack:
        parts.append(f"攻击力 {attack}")
    return " ".join(parts)


def encode_state(state) -> str:
    """把对局快照压成给模型看的中文状态块（不含动作菜单）。"""
    lines = [
        f"回合 {state.game_num_turns_in_play} | "
        f"我方法力 {state.my_last_mana}/{state.my_total_mana} | "
        f"牌库 我{getattr(state, 'my_deck_num', '?')}"
        f"/对手{getattr(state, 'oppo_deck_num', '?')} | "
        f"对手手牌 {getattr(state, 'oppo_hand_card_num', '?')}",
        f"我方英雄: {_hero_line(getattr(state, 'my_hero', None))}",
        f"敌方英雄: {_hero_line(getattr(state, 'oppo_hero', None))}",
    ]

    my_minions = list(getattr(state, "my_minions", []))
    lines.append("我方场面(%d): %s" % (
        len(my_minions),
        " ".join(
            f"[{i}]{m.name} {m.attack}/{getattr(m, 'health', '?')}"
            f"{'(已行动)' if getattr(m, 'exhausted', 0) else ''}{_flags(m)}"
            for i, m in enumerate(my_minions, 1)) or "空"))
    oppo_minions = list(getattr(state, "oppo_minions", []))
    lines.append("敌方场面(%d): %s" % (
        len(oppo_minions),
        " ".join(
            f"[{i}]{m.name} {m.attack}/{getattr(m, 'health', '?')}{_flags(m)}"
            for i, m in enumerate(oppo_minions, 1)) or "空"))

    power = getattr(state, "my_hero_power", None)
    if power is not None:
        used = getattr(power, "exhausted", 0)
        lines.append(f"英雄技能: {power.name}"
                     f"（{'已使用' if used else '可用'}）")

    hand = list(getattr(state, "my_hand_cards", []))
    lines.append(f"我方手牌({len(hand)}):")
    for index, card in enumerate(hand, 1):
        text = card_text(getattr(card, "card_id", ""))
        suffix = f" —— {text}" if text else ""
        lines.append(f"  [{index}] {card.name} 费{card.current_cost}"
                     f" {card.cardtype}{suffix}")

    abilities = getattr(state, "minion_abilities", {}) or {}
    if abilities:
        lines.append("古神技能（每场各限一次，技能全用完该随从才能攻击）:")
        from json_op import query_json_dict
        for i, minion in enumerate(my_minions, 1):
            order = abilities.get(getattr(minion, "card_id", ""), ())
            if order:
                names = "、".join(
                    f"{k}.{query_json_dict(ability_id)}"
                    for k, ability_id in enumerate(order, 1))
                used = getattr(minion, "minion_abilities_used", 0)
                lines.append(f"  [{i}]{minion.name}: {names}"
                             f"（已用 {used}/{len(order)}）")

    choice_count = getattr(state, "discover_choice_count", None)
    if choice_count:
        lines.append(f"拼装/发现选择界面: {choice_count} 个选项（另行处理）")
    return "\n".join(lines)


def encode_state_features(state) -> dict:
    """对局状态 → 紧凑结构化特征（决策点采集/案例检索用，非提示词）。

    与 encode_state 的区别：这是给机器看的 JSON 友好 dict（字段稳定、
    数值化），供案例库按特征匹配与行为克隆训练使用。
    """

    def _minion(m):
        return {
            "name": str(getattr(m, "name", "")),
            "atk": getattr(m, "attack", 0),
            "hp": getattr(m, "health", None),
            "taunt": bool(getattr(m, "taunt", 0)),
            "shield": bool(getattr(m, "divine_shield", 0)),
            "exhausted": bool(getattr(m, "exhausted", 0)),
            "frozen": bool(getattr(m, "frozen", 0)),
        }

    def _card(c):
        return {"name": str(getattr(c, "name", "")),
                "cost": getattr(c, "current_cost", 0),
                "type": str(getattr(c, "cardtype", ""))}

    power = getattr(state, "my_hero_power", None)
    return {
        "turn": getattr(state, "game_num_turns_in_play", 0),
        "mana": getattr(state, "my_last_mana", 0),
        "mana_total": getattr(state, "my_total_mana", 0),
        "my_deck": getattr(state, "my_deck_num", None),
        "oppo_deck": getattr(state, "oppo_deck_num", None),
        "oppo_hand": getattr(state, "oppo_hand_card_num", None),
        "my_minions": [_minion(m) for m in getattr(state, "my_minions", [])],
        "oppo_minions": [_minion(m)
                         for m in getattr(state, "oppo_minions", [])],
        "hand": [_card(c) for c in getattr(state, "my_hand_cards", [])],
        "hero_power_ready": bool(
            power is not None and getattr(power, "exhausted", 0) == 0),
        "discover_choices": getattr(state, "discover_choice_count", None),
    }
