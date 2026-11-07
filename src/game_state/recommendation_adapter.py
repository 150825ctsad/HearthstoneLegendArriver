"""Map display slots in HSAng instructions to identity-bound manual actions."""

from dataclasses import dataclass, replace

from manual_controller import (
    AttackAction, DiscoverChoiceAction, EndTurnAction, HeroPowerAction,
    FRIENDLY_HAND_TARGET_CARD_IDS, LaunchStarshipAction, PlayCardAction,
    Target, TimelineAction, TradeCardAction, UseLocationAction,
    UseMinionAbilityAction,
)
from json_op import card_text, is_assembly_card
from src.recommendation_models import ActionKind
from src.game_state.choose_one import choose_one_card_ids
from src.game_state.old_gods import ability_card_id_by_name


class RecommendationStateError(ValueError):
    pass


@dataclass(frozen=True)
class BoardEntry:
    kind: str
    collection_index: int
    entity: object


@dataclass(frozen=True)
class AdaptedAction:
    manual_action: object
    source_entity_id: str | None
    target_entity_id: str | None
    postcondition: str


def ordered_board(state, side):
    minions = state.my_minions if side == "friendly" else state.oppo_minions
    locations = (getattr(state, "my_locations", []) if side == "friendly"
                 else getattr(state, "oppo_locations", []))
    entries = [BoardEntry("minion", i, entity)
               for i, entity in enumerate(minions)]
    entries.extend(BoardEntry("location", i, entity)
                   for i, entity in enumerate(locations))
    positions = [getattr(entry.entity, "zone_pos", 0) for entry in entries]
    if any(position <= 0 for position in positions):
        raise RecommendationStateError("board_position_unknown")
    if len(set(positions)) != len(positions):
        raise RecommendationStateError("duplicate_board_position")
    return tuple(sorted(entries, key=lambda entry: entry.entity.zone_pos))


def board_slot(state, side, one_based_index):
    board = ordered_board(state, side)
    if not 1 <= one_based_index <= len(board):
        raise RecommendationStateError("board_slot_out_of_range")
    return board[one_based_index - 1]


def adapt_action(proposed, state):
    if proposed.turn_number != state.game_num_turns_in_play:
        raise RecommendationStateError("turn_changed")
    if proposed.action == ActionKind.PLAY_CARD:
        adapted = _adapt_play_card(proposed, state)
        card = state.my_hand_cards[proposed.source.index - 1]
        return _with_choose_one(proposed, state, adapted, card)
    if proposed.action == ActionKind.TRADE_CARD:
        return _adapt_trade_card(proposed, state)
    if proposed.action == ActionKind.USE_HERO_POWER:
        adapted = _adapt_hero_power(proposed, state)
        return _with_choose_one(
            proposed, state, adapted, getattr(state, "my_hero_power", None))
    if proposed.action == ActionKind.USE_MINION_ABILITY:
        return _adapt_use_minion_ability(proposed, state)
    if proposed.action == ActionKind.ATTACK:
        return _adapt_attack(proposed, state)
    if proposed.action == ActionKind.USE_LOCATION:
        entry = board_slot(state, "friendly", proposed.source.index)
        if entry.kind != "location":
            raise RecommendationStateError("source_not_location")
        location = entry.entity
        target = None
        target_id = None
        if proposed.target is not None:
            side = proposed.target.owner
            if side not in {"friendly", "enemy"}:
                raise RecommendationStateError(
                    "location_target_unsupported")
            if proposed.target.kind == "hero":
                hero = (getattr(state, "my_hero", None)
                        if side == "friendly"
                        else getattr(state, "oppo_hero", None))
                if hero is None:
                    raise RecommendationStateError(
                        "location_target_missing")
                target_id = getattr(hero, "entity_id", None)
                target = Target(side, "hero", None, target_id)
            elif proposed.target.kind == "board_slot":
                target_entry = board_slot(
                    state, side, proposed.target.index)
                if target_entry.kind != "minion":
                    raise RecommendationStateError("target_not_minion")
                target_id = getattr(target_entry.entity, "entity_id", None)
                target = Target(
                    side, "minion", target_entry.collection_index,
                    target_id)
            else:
                raise RecommendationStateError(
                    "location_target_unsupported")
        manual = UseLocationAction(
            entry.collection_index, location.card_id,
            getattr(location, "entity_id", None), target=target)
        return AdaptedAction(manual, getattr(location, "entity_id", None),
                             target_id, "location_changed")
    if proposed.action == ActionKind.CHOOSE_DISCOVER:
        choice_count = getattr(state, "discover_choice_count", None)
        if choice_count not in (1, 2, 3, 4):
            raise RecommendationStateError("discover_choice_count_unavailable")
        if (proposed.source is None
                or proposed.source.kind != "discover_slot"
                or proposed.source.owner != "friendly"
                or not 1 <= proposed.source.index <= choice_count):
            raise RecommendationStateError("discover_slot_out_of_range")
        # 拼装选择 UI（来源卡文字含「拼装」）的卡牌位置与标准发现界面不同，
        # 布局随动作传给点击层。
        source_card = getattr(state, "discover_choice_source_card_id", None)
        layout = "assembly" if is_assembly_card(source_card) else "discover"
        return AdaptedAction(
            DiscoverChoiceAction(
                proposed.source.index - 1, choice_count, layout=layout),
            None, None, "choice_resolved")
    if proposed.action == ActionKind.END_TURN:
        return AdaptedAction(EndTurnAction(), None, None, "turn_changed")
    if proposed.action == ActionKind.TIMELINE_UNDO:
        # 回溯：撤销 HSAng 时间线里上一步操作，纯 HSAng 侧 UI，不动 Power.log。
        return AdaptedAction(
            TimelineAction("undo"), None, None, "timeline_clicked")
    if proposed.action == ActionKind.TIMELINE_KEEP:
        # 维持：保留当前操作、关掉 HSAng 的撤销提示，同样不改 Power.log。
        return AdaptedAction(
            TimelineAction("keep"), None, None, "timeline_clicked")
    raise RecommendationStateError("unsupported_action")


def _with_choose_one(proposed, state, adapted, source):
    name = getattr(proposed, "choice_name", None)
    if name is None or source is None:
        return adapted
    if source.card_id not in choose_one_card_ids():
        return adapted
    if (isinstance(adapted.manual_action, PlayCardAction)
            and adapted.manual_action.cardtype not in {"SPELL", "MINION"}):
        raise RecommendationStateError("choose_one_cardtype_unsupported")
    option = getattr(state, "power_options", {}).get(
        getattr(source, "entity_id", None))
    if (option is None or option.get("card_id") != source.card_id
            or option.get("player") != getattr(state, "my_player_id", None)):
        raise RecommendationStateError("choose_one_options_unavailable")
    choices = option["choices"]
    count = len(choices)
    if count not in (1, 2, 3, 4) or set(choices) != set(range(count)):
        raise RecommendationStateError("choose_one_options_incomplete")
    matches = [index for index, choice in choices.items()
               if choice.get("name") == name]
    if len(matches) != 1:
        raise RecommendationStateError("choose_one_name_not_unique")
    index = matches[0]
    if choices[index].get("error") not in {"NONE", "REQ_TARGET_TO_PLAY"}:
        raise RecommendationStateError("choose_one_option_unavailable")
    return replace(adapted, manual_action=replace(
        adapted.manual_action, choose_one=DiscoverChoiceAction(index, count)))


def _adapt_play_card(proposed, state):
    index = proposed.source.index - 1
    if not 0 <= index < len(state.my_hand_cards):
        raise RecommendationStateError("hand_slot_out_of_range")
    card = state.my_hand_cards[index]
    gap = None
    manual_target = None
    target_id = None
    if card.cardtype in {"MINION", "LOCATION"}:
        board_count = len(ordered_board(state, "friendly"))
        gap = (board_count if proposed.destination is None
               else proposed.destination.index - 1)
        if not 0 <= gap <= board_count:
            raise RecommendationStateError("minion_destination_out_of_range")
    if proposed.target is not None:
        if (card.card_id in FRIENDLY_HAND_TARGET_CARD_IDS
                and proposed.target.owner == "friendly"
                and proposed.target.kind in {"hand_slot", "board_slot"}):
            target_index = proposed.target.index - 1
            if not 0 <= target_index < len(state.my_hand_cards):
                raise RecommendationStateError("hand_target_out_of_range")
            if target_index == index:
                raise RecommendationStateError("hand_target_is_source")
            target_card = state.my_hand_cards[target_index]
            target_id = getattr(target_card, "entity_id", None)
            manual_target = Target(
                "friendly", "hand", target_index, target_id)
        elif proposed.card_type in {"SPELL", "MINION"}:
            target_error = ("spell_target" if proposed.card_type == "SPELL"
                            else "minion_target")
            if proposed.target.owner not in {"friendly", "enemy"}:
                raise RecommendationStateError(f"{target_error}_unsupported")
            if proposed.target.kind == "hero":
                hero = (getattr(state, "my_hero", None)
                        if proposed.target.owner == "friendly"
                        else getattr(state, "oppo_hero", None))
                if hero is None:
                    raise RecommendationStateError(f"{target_error}_missing")
                target_id = getattr(hero, "entity_id", None)
                manual_target = Target(
                    proposed.target.owner, "hero", None, target_id)
            elif proposed.target.kind == "location_slot":
                # 地标目标（如拆迁修理工「摧毁一个敌方地标」）：只有卡牌
                # 文字提到「地标」才允许（防误点不指向地标的牌）。
                if "地标" not in card_text(getattr(card, "card_id", "")):
                    raise RecommendationStateError(
                        f"{target_error}_not_location_card")
                entry = board_slot(
                    state, proposed.target.owner, proposed.target.index)
                if entry.kind != "location":
                    raise RecommendationStateError("target_not_location")
                target_id = getattr(entry.entity, "entity_id", None)
                row_index = int(getattr(entry.entity, "zone_pos", 0)) - 1
                if row_index < 0:
                    raise RecommendationStateError("target_position_unknown")
                manual_target = Target(
                    proposed.target.owner, "location", row_index, target_id)
            elif proposed.target.kind == "board_slot":
                target = board_slot(
                    state, proposed.target.owner, proposed.target.index)
                if target.kind != "minion":
                    raise RecommendationStateError("target_not_minion")
                target_id = getattr(target.entity, "entity_id", None)
                manual_target = Target(
                    proposed.target.owner, "minion",
                    target.collection_index, target_id)
            else:
                raise RecommendationStateError(f"{target_error}_unsupported")
        else:
            raise RecommendationStateError("targeted_action_unsupported")
    manual = PlayCardAction(
        index, card.card_id, card.cardtype, gap_index=gap,
        target=manual_target,
        hand_entity_id=getattr(card, "entity_id", None))
    return AdaptedAction(
        manual, getattr(card, "entity_id", None), target_id,
                         "hand_card_left")


def _adapt_trade_card(proposed, state):
    index = proposed.source.index - 1
    if not 0 <= index < len(state.my_hand_cards):
        raise RecommendationStateError("hand_slot_out_of_range")
    card = state.my_hand_cards[index]
    entity_id = getattr(card, "entity_id", None)
    manual = TradeCardAction(
        index, card.card_id, card.cardtype, hand_entity_id=entity_id)
    return AdaptedAction(
        manual, entity_id, None, "hand_card_left")


def _resolve_board_or_hero_target(target_ref, state, error_prefix):
    """把 目标是… 引用绑定到实体（英雄/随从/地标）；多个动作类型共用。

    location_slot（地标目标）按战场排位（zone_pos）绑定——地标在场面行里
    占一个位置，点击坐标与随从同一套排位计算。
    """
    if target_ref is None:
        return None, None
    side = target_ref.owner
    if side not in {"friendly", "enemy"}:
        raise RecommendationStateError(f"{error_prefix}_unsupported")
    if target_ref.kind == "hero":
        hero = (getattr(state, "my_hero", None)
                if side == "friendly"
                else getattr(state, "oppo_hero", None))
        if hero is None:
            raise RecommendationStateError(f"{error_prefix}_missing")
        entity_id = getattr(hero, "entity_id", None)
        return Target(side, "hero", None, entity_id), entity_id
    if target_ref.kind == "location_slot":
        entry = board_slot(state, side, target_ref.index)
        if entry.kind != "location":
            raise RecommendationStateError("target_not_location")
        entity_id = getattr(entry.entity, "entity_id", None)
        row_index = int(getattr(entry.entity, "zone_pos", 0)) - 1
        if row_index < 0:
            raise RecommendationStateError("target_position_unknown")
        return Target(side, "location", row_index, entity_id), entity_id
    if target_ref.kind == "board_slot":
        entry = board_slot(state, side, target_ref.index)
        if entry.kind != "minion":
            raise RecommendationStateError("target_not_minion")
        entity_id = getattr(entry.entity, "entity_id", None)
        return Target(side, "minion", entry.collection_index,
                      entity_id), entity_id
    raise RecommendationStateError(f"{error_prefix}_unsupported")


def _adapt_hero_power(proposed, state):
    power = getattr(state, "my_hero_power", None)
    target, target_id = _resolve_board_or_hero_target(
        proposed.target, state, "hero_power_target")
    return AdaptedAction(
        HeroPowerAction(target=target),
        getattr(power, "entity_id", None), target_id,
        "hero_power_changed")


def _adapt_use_minion_ability(proposed, state):
    """古神/类泰坦技能：点随从弹技能菜单 → 按定义序点选技能卡（可带目标）。

    技能菜单在 Power.log 里静默，技能名→按钮序号依赖：
      1. cards.json 把技能名反查成子卡 cardId（恩佐斯的召唤 → BE_017t3）；
      2. StrategyState.minion_abilities 里该古神的技能表（按 tN 升序 =
         游戏菜单按钮的排列序，与泰坦时代一致）。
    """
    if (proposed.source is None
            or proposed.source.kind != "board_slot"
            or proposed.source.owner != "friendly"):
        raise RecommendationStateError("ability_source_unsupported")
    entry = board_slot(state, "friendly", proposed.source.index)
    if entry.kind != "minion":
        raise RecommendationStateError("source_not_minion")
    minion = entry.entity
    has_ability_tags = bool(getattr(minion, "has_minion_abilities", False))
    ability_order = tuple(getattr(state, "minion_abilities", {}).get(
        getattr(minion, "card_id", ""), ()))
    if not has_ability_tags and not ability_order:
        raise RecommendationStateError("source_not_old_god")
    choice_name = getattr(proposed, "choice_name", None)
    if not choice_name:
        raise RecommendationStateError("choose_one_name_required")
    ability_card_id = ability_card_id_by_name(choice_name)
    if ability_card_id is None or ability_card_id not in ability_order:
        raise RecommendationStateError("ability_name_unresolvable")
    ability_index = ability_order.index(ability_card_id)
    target, target_id = _resolve_board_or_hero_target(
        proposed.target, state, "ability_target")
    manual = UseMinionAbilityAction(
        entry.collection_index, getattr(minion, "card_id", ""),
        ability_index, len(ability_order),
        minion_entity_id=getattr(minion, "entity_id", None),
        target=target)
    return AdaptedAction(manual, getattr(minion, "entity_id", None),
                         target_id, "minion_ability_used")


def _adapt_attack(proposed, state):
    if proposed.source.kind == "hero":
        hero = getattr(state, "my_hero", None)
        if hero is None:
            raise RecommendationStateError("friendly_hero_missing")
        source_target = Target(
            "friendly", "hero", None, getattr(hero, "entity_id", None))
        source_id = getattr(hero, "entity_id", None)
    else:
        source = board_slot(state, "friendly", proposed.source.index)
        if source.kind != "minion":
            raise RecommendationStateError("source_not_minion")
        source_id = getattr(source.entity, "entity_id", None)
        if source.entity.card_id == "SC_999t":
            manual = LaunchStarshipAction(
                source.collection_index,
                source.entity.card_id,
                source_id,
            )
            return AdaptedAction(
                manual, source_id, None, "starship_launched")
        source_target = Target(
            "friendly", "minion", source.collection_index, source_id)
    if proposed.target is None:
        raise RecommendationStateError("attack_target_required")
    if proposed.target.kind == "hero":
        hero = getattr(state, "oppo_hero", None)
        if hero is None:
            raise RecommendationStateError("enemy_hero_missing")
        target_id = getattr(hero, "entity_id", None)
        manual_target = Target("enemy", "hero", None, target_id)
    else:
        target = board_slot(state, "enemy", proposed.target.index)
        if target.kind != "minion":
            raise RecommendationStateError("target_not_minion")
        target_id = getattr(target.entity, "entity_id", None)
        manual_target = Target("enemy", "minion", target.collection_index,
                               target_id)
    postcondition = ("hero_combat_state_changed"
                     if proposed.source.kind == "hero"
                     else "combat_state_changed")
    return AdaptedAction(AttackAction(source_target, manual_target),
                         source_id, target_id, postcondition)
