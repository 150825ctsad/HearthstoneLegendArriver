"""Build a user-facing game snapshot from parsed Power.log state."""

import re

from constants.constants import DEBUG_PRINT
from print_info import debug_print

# 古神/类泰坦的技能子卡命名（实测 BE_017 的技能为 BE_017t3~t6）。
_ABILITY_CARD_ID_RE = re.compile(r"^(.+)t(\d+)$")


class StrategyState:
    def __init__(self, log_state):
        self.is_end = log_state.is_end
        self.is_my_turn = log_state.is_my_turn
        self.game_num_turns_in_play = log_state.game_num_turns_in_play
        self.my_total_mana = int(log_state.my_entity.query_tag("RESOURCES"))
        self.my_used_mana = int(log_state.my_entity.query_tag("RESOURCES_USED"))
        self.my_temp_mana = int(log_state.my_entity.query_tag("TEMP_RESOURCES"))

        self.oppo_minions = []
        self.oppo_graveyard = []
        self.my_minions = []
        self.my_locations = []
        self.oppo_locations = []
        self.my_hand_cards = []
        self.my_graveyard = []
        self.my_hero = None
        self.my_hero_power = None
        self.my_weapon = None
        self.oppo_hero = None
        self.oppo_hero_power = None
        self.oppo_weapon = None
        self.oppo_hand_card_num = 0
        self.my_deck_num = 0
        self.oppo_deck_num = 0
        self.discover_choice_count = log_state.discover_choice_count
        # 打开中的选择 UI 的来源卡（拼装卡/发现卡），供点击布局判定。
        self.discover_choice_source_card_id = (
            getattr(log_state, "general_choice_source_card_id", None)
            if self.discover_choice_count else None)
        self.my_player_id = log_state.my_player_id
        # 结构化两层拷贝（键与 info/choices 的值都是不可变的字符串/数字）。
        # deepcopy 会对每个小 dict 走一套 memo 机制，快照在每次日志 revision
        # 变化时都重建，这里是可观的固定开销；两层浅拷贝语义完全等价。
        self.power_options = {
            entity_id: {**option, "choices": dict(option.get("choices") or {})}
            for entity_id, option in log_state.power_options.items()
        }
        self.hand_entry_count = log_state.hand_entry_count
        self.start_of_game_card_count = getattr(
            log_state, "start_of_game_card_count", 0)

        for entity_id, entity in log_state.entity_dict.items():
            if not hasattr(entity, "generate_strategy_entity"):
                continue
            if entity.query_tag("ZONE") == "HAND":
                if log_state.is_my_entity(entity):
                    converted = entity.generate_strategy_entity(
                        log_state, entity_id)
                    if converted is not None:
                        self.my_hand_cards.append(converted)
                else:
                    self.oppo_hand_card_num += 1
            elif entity.query_tag("ZONE") == "PLAY":
                converted = entity.generate_strategy_entity(
                    log_state, entity_id)
                if converted is None:
                    continue
                mine = log_state.is_my_entity(entity)
                if entity.cardtype == "MINION":
                    (self.my_minions if mine else self.oppo_minions).append(converted)
                elif entity.cardtype == "HERO":
                    if mine:
                        self.my_hero = converted
                    else:
                        self.oppo_hero = converted
                elif entity.cardtype == "HERO_POWER":
                    if mine:
                        self.my_hero_power = converted
                    else:
                        self.oppo_hero_power = converted
                elif entity.cardtype == "WEAPON":
                    if mine:
                        self.my_weapon = converted
                    else:
                        self.oppo_weapon = converted
                elif entity.cardtype == "LOCATION":
                    (self.my_locations if mine
                     else self.oppo_locations).append(converted)
            elif entity.query_tag("ZONE") == "DECK":
                if log_state.is_my_entity(entity):
                    self.my_deck_num += 1
                else:
                    self.oppo_deck_num += 1
            elif entity.query_tag("ZONE") == "GRAVEYARD":
                (self.my_graveyard if log_state.is_my_entity(entity)
                 else self.oppo_graveyard).append(entity)

        self.my_minions.sort(key=lambda item: item.zone_pos)
        self.oppo_minions.sort(key=lambda item: item.zone_pos)
        self.my_hand_cards.sort(key=lambda item: item.zone_pos)
        self.my_locations.sort(key=lambda item: item.zone_pos)
        self.oppo_locations.sort(key=lambda item: item.zone_pos)
        self.minion_abilities = self._collect_minion_abilities(log_state)

    @staticmethod
    def _collect_minion_abilities(log_state):
        """我方随从的古神技能表：{随从card_id: 按子卡 tN 升序的技能 cardId 元组}。

        技能实体在打出古神时以 SETASIDE 注册（实测 恩佐斯 BE_017 的
        BE_017t3~t6）。技能菜单在 Power.log 里完全静默（无 Options/Choices
        事件），技能名→按钮序号只能靠子卡定义序推定：游戏 UI 的技能按钮按
        tN 升序排列（与泰坦时代 TTN_xxx t1..t3 的行为一致）。
        """
        my_minions = [
            entity for entity in log_state.entity_dict.values()
            if entity.cardtype == "MINION"
            and entity.query_tag("ZONE") == "PLAY"
            and log_state.is_my_entity(entity)]
        my_card_ids = {entity.card_id for entity in my_minions}
        grouped: dict = {}
        for entity in log_state.entity_dict.values():
            if entity.query_tag("ZONE") != "SETASIDE":
                continue
            if not log_state.is_my_entity(entity):
                continue
            match = _ABILITY_CARD_ID_RE.match(
                getattr(entity, "card_id", "") or "")
            if match is None or match.group(1) not in my_card_ids:
                continue
            grouped.setdefault(match.group(1), set()).add(int(match.group(2)))
        return {
            card_id: tuple(f"{card_id}t{number}"
                           for number in sorted(numbers))
            for card_id, numbers in grouped.items()
        }

    @property
    def my_last_mana(self):
        return self.my_total_mana - self.my_used_mana + self.my_temp_mana

    @property
    def my_minion_num(self):
        return len(self.my_minions)

    @property
    def oppo_minion_num(self):
        return len(self.oppo_minions)

    @property
    def my_board_slot_num(self):
        return len(self.my_minions) + len(self.my_locations)

    @property
    def oppo_board_slot_num(self):
        return len(self.oppo_minions) + len(self.oppo_locations)

    @property
    def my_hand_card_num(self):
        return len(self.my_hand_cards)

    @staticmethod
    def _hero_line(hero):
        if hero is None:
            return "未知"
        return f"{hero.name} 生命/护甲: {hero.health - hero.armor}+{hero.armor}"

    @staticmethod
    def _weapon_line(weapon):
        return "无" if weapon is None else str(weapon)

    def format_for_manual_control(self):
        lines = [
            "=== 当前战局 ===",
            f"回合: {self.game_num_turns_in_play}  法力: {self.my_last_mana}/{self.my_total_mana}",
            f"己方英雄: {self._hero_line(self.my_hero)}",
            f"己方武器: {self._weapon_line(self.my_weapon)}",
            "己方随从:",
        ]
        if self.my_minions:
            lines.extend(f"  [{i}] {minion}" for i, minion in enumerate(self.my_minions, 1))
        else:
            lines.append("  无")
        if self.my_locations:
            lines.append("己方地标:")
            lines.extend(
                f"  [{i}] {location}"
                for i, location in enumerate(self.my_locations, 1))
        lines.extend([
            f"敌方英雄: {self._hero_line(self.oppo_hero)}",
            f"敌方武器: {self._weapon_line(self.oppo_weapon)}",
            "敌方随从:",
        ])
        if self.oppo_minions:
            lines.extend(f"  [{i}] {minion}" for i, minion in enumerate(self.oppo_minions, 1))
        else:
            lines.append("  无")
        if self.oppo_locations:
            lines.append("敌方地标:")
            lines.extend(
                f"  [{i}] {location}"
                for i, location in enumerate(self.oppo_locations, 1))
        lines.append("手牌:")
        if self.my_hand_cards:
            lines.extend(
                f"  [{i}] {card.name} 类型: {card.cardtype} 费用: {card.current_cost}"
                for i, card in enumerate(self.my_hand_cards, 1)
            )
        else:
            lines.append("  无")
        return "\n".join(lines)

    def debug_print_out(self):
        if DEBUG_PRINT:
            for line in self.format_for_manual_control().splitlines():
                debug_print(line)
