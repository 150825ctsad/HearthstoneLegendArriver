import json
import os
from functools import lru_cache
from pathlib import Path

import requests

from config import DOWNLOAD_TIMEOUT_SECONDS, JSON_URL
from print_info import *


# 下载/重下载都设超时，避免主循环被网络卡死。（定义于 config.py）


def download_json(json_path):
    file = requests.get(JSON_URL, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    file.raise_for_status()

    with open(json_path, "wb") as f:
        f.write(file.content)


def read_json(re_download=False):
    dir_path = os.path.dirname(__file__)
    if dir_path == "":
        dir_path = "."
    json_path = dir_path + "/cards.json"

    if not os.path.exists(json_path):
        sys_print("未找到cards.json,试图通过网络下载文件")
        download_json(json_path)
    elif re_download:
        sys_print("疑似有新版本炉石数据，正在重新下载最新文件")
        download_json(json_path)
    else:
        sys_print("cards.json已存在")

    with open(json_path, "r", encoding="utf8") as f:
        json_string = f.read()
        json_list = json.loads(json_string)
        json_dict = {}
        for item in json_list:
            json_dict[item["id"]] = item
        return json_dict


# 懒加载：首次 query 时才读取（cards.json 缺失时首次会联网下载一次）。
# 原先在模块导入时同步联网，无网/CI 环境会卡在 import。
JSON_DICT = {}
_load_attempted = False
# 未知卡牌触发"重新下载最新数据"最多一次（本次会话），
# 避免主循环里每遇到一张新卡就反复全量下载。
_reload_attempted = False


def query_json_dict(key):
    global JSON_DICT, _load_attempted, _reload_attempted

    if key == "":
        return "Unknown"

    if not _load_attempted:
        _load_attempted = True
        try:
            JSON_DICT = read_json(re_download=False)
        except Exception as exc:
            error_print(f"加载卡牌数据失败：{type(exc).__name__}: {exc}")
            JSON_DICT = {}

    if key in JSON_DICT:
        return JSON_DICT[key]["name"]

    # 认为是炉石更新了，出现了新卡，需要重新下载一次；
    # 若重下载后仍没有，就用占位名继续，且本次会话不再尝试。
    if not _reload_attempted:
        _reload_attempted = True
        try:
            JSON_DICT = read_json(re_download=True)
        except Exception as exc:
            error_print(f"重新下载卡牌数据失败：{type(exc).__name__}: {exc}")
        if key in JSON_DICT:
            return JSON_DICT[key]["name"]

    error_print(f"出现未识别卡牌，使用占位名称继续：{key}")
    return f"Unknown:{key}"


# ---------------------------------------------------------------- 卡牌文字
@lru_cache(maxsize=1)
def _card_texts() -> dict:
    """cardId → 规则文字（cards.json 的 text 字段；读取失败返回空表）。"""
    try:
        with (Path(__file__).resolve().parent / "cards.json").open(
                encoding="utf8") as source:
            data = json.load(source)
        return {str(card.get("id") or ""): str(card.get("text") or "")
                for card in data if card.get("id")}
    except Exception:
        return {}


def card_text(card_id) -> str:
    """卡牌规则文字（去 HTML 标记与占位符）；查不到返回空串。"""
    text = _card_texts().get(str(card_id or ""), "")
    if not text:
        return ""
    # HearthstoneJSON 的文字带 <b>/<i> 等标记与 #/$ 占位符，粗略清洗。
    out, skip = [], False
    for char in text:
        if char == "<":
            skip = True
        elif char == ">":
            skip = False
        elif not skip:
            out.append(char)
    return "".join(out).replace("#", "").strip()


def is_assembly_card(card_id) -> bool:
    """是否拼装卡（37.0 新关键词）：规则文字含「拼装」。

    cards.json 的 mechanics 字段对拼装卡不可靠（实测 BE_553 为 None），
    文字标记是唯一稳定特征。
    """
    return "拼装" in card_text(card_id)


if __name__ == "__main__":
    JSON_DICT = read_json()
    with open("id-name.txt", "w", encoding="utf8") as f:
        for key, val in JSON_DICT.items():
            f.write(key + " " + val["name"] + "\n")

    query_json_dict("SW_085t")
