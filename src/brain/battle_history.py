# -*- coding: utf-8 -*-
"""对战历史摘要：把最近的 Power.log 会话压成给大模型看的经验块。

API 大模型无法直接"训练"（对话接口不接受训练数据），但把近期对局的
「双方职业 / 胜负 / 回合数」压缩成经验摘要注入提示词，等价于在推理时
做上下文学习——模型能据此调整对不同职业的打法倾向。

输出（logs/battle_history.json）：
    {"built_at": "...", "games": [{"time", "my_name", "my_class",
     "oppo_name", "oppo_class", "result", "turns"}...]}

解析规则（全部来自 Power.log 原始行，容错优先——摘要错了不影响执行）：
  * CREATE_GAME 开新局；GameEntity tag=STATE value=COMPLETE 结束；
  * PlayerID↔玩家名 来自 "PlayerID=N, PlayerName=xxx" 行；
  * 英雄：FULL_ENTITY CardID=HERO_xxx + 其 CONTROLLER → 各自职业
    （cardClass 经 cards.json 映射成中文）；
  * 胜负：TAG_CHANGE Entity=<昵称> tag=PLAYSTATE value=WON/LOST/TIED；
  * 回合数：GameEntity tag=NUM_TURNS_IN_PLAY 最后一次的值。
"""

import json
import re
import time
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
HISTORY_PATH = _ROOT / "logs" / "battle_history.json"
# 经验块最多带最近多少局（控制提示词体积）
MAX_GAMES_IN_PROMPT = 12

_CREATE_RE = re.compile(r"GameState\.DebugPrintPower\(\) - CREATE_GAME")
_PLAYER_ID_RE = re.compile(r"PlayerID=(\d+), PlayerName=(.+)")
_FULL_ENTITY_RE = re.compile(r"FULL_ENTITY - (?:Creating|Updating)")
# 顶层实体 ID（Creating ID=72 / Updating [.. id=72 ..]）；
# 负向环视排除 CardID=/cardId= 里的 "ID=" 子串。
_ENTITY_TOP_ID_RE = re.compile(r"(?<![A-Za-z])ID=(\d+)")
_HERO_CARD_RE = re.compile(r"CardID=(HERO_\S+)")
_CURRENT_CONTROLLER_RE = re.compile(r"tag=CONTROLLER value=(\d+)")
_CARDTYPE_RE = re.compile(r"tag=CARDTYPE value=([A-Z]+)")
_CARDTYPE_RE = re.compile(r"tag=CARDTYPE value=([A-Z]+)")
# PLAYSTATE 行两种形式：裸名（无空格昵称）与括号实体（"UNKNOWN HUMAN
# PLAYER" 等带空格名字走括号形式）：
#   TAG_CHANGE Entity=GGboom#51837 tag=PLAYSTATE value=WON
#   TAG_CHANGE Entity=[entityName=UNKNOWN HUMAN PLAYER id=2 ...] tag=PLAYSTATE value=LOST
_PLAYSTATE_RE = re.compile(
    r"TAG_CHANGE Entity=(?:\[entityName=([^\]]+?) id=\d+[^\]]*\]|([^\[\s]+))"
    r" tag=PLAYSTATE value=(WON|LOST|TIED)")
_STATE_COMPLETE_RE = re.compile(
    r"TAG_CHANGE Entity=GameEntity tag=STATE value=COMPLETE")
_TURNS_RE = re.compile(
    r"TAG_CHANGE Entity=GameEntity tag=NUM_TURNS_IN_PLAY value=(\d+)")
_TS_RE = re.compile(r"^D (\d\d:\d\d:\d\d)")

_CLASS_CN = {
    "MAGE": "法师", "HUNTER": "猎人", "WARRIOR": "战士", "PRIEST": "牧师",
    "PALADIN": "圣骑士", "ROGUE": "潜行者", "SHAMAN": "萨满",
    "WARLOCK": "术士", "DRUID": "德鲁伊", "DEMONHUNTER": "恶魔猎手",
    "DEATHKNIGHT": "死亡骑士", "NEUTRAL": "中立",
}


@lru_cache(maxsize=1)
def _class_map() -> dict:
    """HERO 卡 ID → 中文职业名（读 cards.json；失败返回空表）。"""
    try:
        with (_ROOT / "cards.json").open(encoding="utf8") as source:
            data = json.load(source)
        return {
            str(card.get("id") or ""):
                _CLASS_CN.get(str(card.get("cardClass") or ""),
                              str(card.get("id") or ""))
            for card in data if card.get("id")
        }
    except Exception:
        return {}


def _class_of(card_id: str) -> str:
    return _class_map().get(str(card_id or ""), card_id or "?")


def build_game_digests(power_log_path) -> list:
    """解析单个 Power.log 会话里的全部对局，返回摘要列表。"""
    try:
        lines = Path(power_log_path).read_text(
            encoding="utf8", errors="replace").splitlines()
    except Exception:
        return []
    games = []
    game = None
    current_entity_id = None
    lines_since_entity = 999
    games = []
    game = None
    current_entity_id = None
    lines_since_entity = 999
    for line in lines:
        if game is None:
            if _CREATE_RE.search(line):
                game = {"hero_candidates": {}, "block_cardtype": {},
                        "controller": {}, "playstate": {}, "turns": 0,
                        "names": {}}
                current_entity_id = None
                m = _TS_RE.match(line)
                if m:
                    game["time"] = m.group(1)
            continue
        m = _PLAYER_ID_RE.search(line)
        if m:
            game["names"][m.group(1)] = m.group(2).strip()
            continue
        if _FULL_ENTITY_RE.search(line):
            eid = _ENTITY_TOP_ID_RE.search(line)
            current_entity_id = eid.group(1) if eid else None
            lines_since_entity = 0
            hero = _HERO_CARD_RE.search(line)
            # HERO_* 候选（含英雄技能 HERO_xxhp），卡型由块内 tag=CARDTYPE 定
            if hero and current_entity_id:
                game["hero_candidates"][current_entity_id] = hero.group(1)
            continue
        if current_entity_id and lines_since_entity <= 10:
            m = _CARDTYPE_RE.search(line)
            if m:
                game["block_cardtype"][current_entity_id] = m.group(1)
                continue
            m = _CURRENT_CONTROLLER_RE.search(line)
            if m:
                game["controller"][current_entity_id] = m.group(1)
                continue
        lines_since_entity += 1
        m = _PLAYSTATE_RE.search(line)
        if m:
            name = (m.group(1) or m.group(2) or "").strip()
            if name:
                game["playstate"][name] = m.group(3)
            continue
        m = _TURNS_RE.search(line)
        if m:
            game["turns"] = int(m.group(1))
            continue
        if _STATE_COMPLETE_RE.search(line):
            digest = _finalize(game)
            if digest:
                games.append(digest)
            game = None
    return games


def _finalize(game) -> dict | None:
    """把一局的中间状态压成摘要；信息不足返回 None。"""
    names = game.get("names") or {}
    cardtypes = game.get("block_cardtype") or {}
    # 英雄 = HERO_* 候选中卡型确为 HERO 的（排除英雄技能 HERO_xxhp）
    heroes = {
        entity_id: card_id
        for entity_id, card_id in (game.get("hero_candidates") or {}).items()
        if cardtypes.get(entity_id) == "HERO"
    }
    hero_class = {}
    for entity_id, card_id in heroes.items():
        player_id = (game.get("controller") or {}).get(entity_id)
        if player_id:
            hero_class[player_id] = _class_of(card_id)
    if len(names) < 2 or len(hero_class) < 2:
        return None
    players = sorted(names.items())          # [(player_id, name), ...]
    first_id, second_id = players[0][0], players[1][0]
    my_playstate = game.get("playstate", {}).get(names[first_id], "?")
    oppo_playstate = game.get("playstate", {}).get(names[second_id], "?")
    if my_playstate == "?":
        # 中途退出/投降的一方不会写 PLAYSTATE：从对方状态反推
        my_playstate = {"WON": "LOST", "LOST": "WON", "TIED": "TIED"}.get(
            oppo_playstate, "中断")
    return {
        "time": game.get("time", ""),
        "my_name": names[first_id],
        "my_class": hero_class.get(first_id, "?"),
        "oppo_name": names[second_id],
        "oppo_class": hero_class.get(second_id, "?"),
        "result": my_playstate,
        "turns": game.get("turns", 0),
    }


def build_all_digests(log_root) -> list:
    """扫描日志目录下全部会话的 Power.log，按文件序返回全部对局摘要。"""
    root = Path(log_root)
    all_games = []
    if not root.is_dir():
        return all_games
    for log_path in sorted(root.glob("*/Power.log")):
        all_games.extend(build_game_digests(log_path))
    return all_games


def render_history_block(games: list,
                         max_games: int = MAX_GAMES_IN_PROMPT) -> str:
    """对局摘要 → 提示词经验块（每局一行，result 为 my_name 视角的胜负）。"""
    if not games:
        return ""
    lines = ["近期对战记录（result 是「我方」视角的胜负，供参考打法倾向）:"]
    for g in games[-max_games:]:
        lines.append(
            f"  {g.get('time', '')} {g.get('my_name', '?')}"
            f"({g.get('my_class', '?')}) vs {g.get('oppo_name', '?')}"
            f"({g.get('oppo_class', '?')}) —— {g.get('result', '?')}"
            f"，{g.get('turns', '?')} 回合")
    return "\n".join(lines)


def save_history(games: list, path=HISTORY_PATH) -> None:
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "games": games}, ensure_ascii=False, indent=1),
            encoding="utf8")
    except Exception:
        pass


def load_history(path=HISTORY_PATH) -> list:
    try:
        data = json.loads(Path(path).read_text(encoding="utf8"))
        return list(data.get("games") or [])
    except Exception:
        return []
