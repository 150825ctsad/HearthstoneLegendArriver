"""古神/类泰坦技能的名称→子卡 ID 反查（cards.json zhCN 名称）。

技能菜单在 Power.log 里静默，技能名→按钮序号靠子卡 ID 的 tN 排序推定
（见 strategy._collect_minion_abilities）。本模块把盒子里 OCR 到的技能名
（如「恩佐斯的召唤」）反查成 cardId（如 BE_017t3），再由适配层在技能表里
定位按钮序号。cards.json 过期时查不到 → 适配层报 ability_name_unresolvable
重试（脚本会在启动时自动下载最新 cards.json）。
"""

import json
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def _name_to_card_id() -> dict:
    """全卡牌 zhCN 名称 → cardId（同名卡取第一个；只读本地 cards.json）。"""
    mapping: dict = {}
    try:
        with (_ROOT / "cards.json").open(encoding="utf8") as source:
            for card in json.load(source):
                name = str(card.get("name") or "").strip()
                card_id = str(card.get("id") or "").strip()
                if name and card_id and name not in mapping:
                    mapping[name] = card_id
    except Exception:
        return {}
    return mapping


def ability_card_id_by_name(name):
    """技能名 → cardId；查不到返回 None（调用方按不可解析重试）。"""
    if not name:
        return None
    return _name_to_card_id().get(str(name).strip())
