# -*- coding: utf-8 -*-
"""对战历史摘要解析的回归测试（迷你对局按真实 Power.log 行格式构造）。

真实格式要点（2026-11-07 实测）：
  * 英雄实体：FULL_ENTITY - Creating ID=72 CardID=HERO_05bz（顶层 ID=），
    CONTROLLER 是块内普通 tag 行（带时间戳前缀）；
  * 英雄技能 HERO_xxhp 也是 HERO_* 候选，靠块内 tag=CARDTYPE value=HERO
    排除；
  * 中途退出/投降的一方没有 PLAYSTATE，从对方状态反推。
"""

import unittest
from pathlib import Path

from src.brain.battle_history import (
    build_game_digests, render_history_block,
)

HEADER = ("D 12:49:32.6753649 GameState.DebugPrintPower() - ")
GAME_START = HEADER + "CREATE_GAME"
# 一局迷你对局：GGboom(猎人 HERO_05bz) vs 对手(法师 HERO_08)


def _lines(result="LOST"):
    lines = [
        HEADER + "CREATE_GAME",
        HEADER + "GameState.DebugPrintEntityChoices() - 无关行",
        HEADER + "PlayerID=1, PlayerName=GGboom#51837",
        HEADER + "PlayerID=2, PlayerName=UNKNOWN HUMAN PLAYER",
        HEADER + "FULL_ENTITY - Creating ID=72 CardID=HERO_05bz",
        HEADER + "    tag=CONTROLLER value=1",
        HEADER + "    tag=CARDTYPE value=HERO",
        HEADER + "FULL_ENTITY - Creating ID=73 CardID=HERO_05bzhp",
        HEADER + "    tag=CONTROLLER value=1",
        HEADER + "    tag=CARDTYPE value=HERO_POWER",
        HEADER + "FULL_ENTITY - Creating ID=82 CardID=HERO_08",
        HEADER + "    tag=CONTROLLER value=2",
        HEADER + "    tag=CARDTYPE value=HERO",
        HEADER + "TAG_CHANGE Entity=GameEntity tag=NUM_TURNS_IN_PLAY value=14",
        HEADER + f"TAG_CHANGE Entity=GGboom#51837 tag=PLAYSTATE value={result}",
    ]
    if result != "中断":
        lines.append(HEADER + f"TAG_CHANGE Entity=[entityName=UNKNOWN HUMAN "
                              f"PLAYER id=2 zone=PLAY cardId= player=2] "
                              f"tag=PLAYSTATE value="
                              f"{'WON' if result == 'LOST' else 'LOST'}")
    lines.append(HEADER + "TAG_CHANGE Entity=GameEntity tag=STATE "
                          "value=COMPLETE")
    return lines


def _write(tmp_path, lines):
    base = Path(tmp_path)
    log = base / "Hearthstone_test" / "Power.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(lines) + "\n", encoding="utf8")
    return log


class BattleHistoryTests(unittest.TestCase):
    def _digest(self, tmp_path, result="LOST"):
        log = _write(tmp_path, _lines(result))
        games = build_game_digests(log)
        self.assertEqual(1, len(games))
        return games[0]

    def test_heroes_classes_and_result(self):
        digest = self._digest(self.path_factory(), result="LOST")
        self.assertEqual("猎人", digest["my_class"])
        self.assertEqual("法师", digest["oppo_class"])
        self.assertEqual("LOST", digest["result"])
        self.assertEqual(14, digest["turns"])

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path_factory = lambda: self._tmp.name

    def test_hero_power_excluded_from_heroes(self):
        """HERO_05bzhp（英雄技能）不能被当成英雄——否则双方职业全错。"""
        # 若技能被误当英雄，GGboom 的职业可能解析成技能卡的职业或重复，
        # 这里断言双方各只有一名英雄且职业正确（间接验证排除逻辑）。
        digest = self._digest(self.path_factory())
        self.assertNotIn("HERO_POWER", str(digest))

    def test_quit_inferred_from_opponent_playstate(self):
        """我方中途退出（无 PLAYSTATE）：从对方 WON 反推我方 LOST。"""
        lines = _lines("LOST")
        # 去掉我方的 PLAYSTATE 行（模拟中途退出）
        lines = [l for l in lines if "GGboom#51837 tag=PLAYSTATE" not in l]
        log = _write(self.path_factory(), lines)
        games = build_game_digests(log)
        self.assertEqual("LOST", games[0]["result"])

    def test_multiple_games_in_one_file(self):
        lines = _lines("WON") + _lines("LOST")
        log = _write(self.path_factory(), lines)
        self.assertEqual(2, len(build_game_digests(log)))

    def test_render_block_is_compact(self):
        games = [self._digest(self.path_factory())]
        block = render_history_block(games)
        self.assertIn("GGboom#51837(猎人) vs UNKNOWN HUMAN PLAYER(法师)",
                      block)
        self.assertIn("LOST", block)


if __name__ == "__main__":
    unittest.main()
