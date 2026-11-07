# -*- coding: utf-8 -*-
"""决策点采集器：把每次实际执行的决策写入 JSONL（案例库/行为克隆的数据源）。

每条记录 = 一次真实执行的决策：
    {时间, 对局代次, 回合, 来源(box|llm|random), 状态特征,
     合法菜单(可选), 所选动作, 结果验证}
用途：
  * 案例库检索（轻量检索器按状态特征匹配历史决策）
  * 行为克隆数据集（盒子推荐 = 专家标注）
  * 离线复盘分析
写入永不抛异常（数据采集不能影响对局）；单文件 JSONL 逐行追加。
"""

import json
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = _ROOT / "logs" / "training_data.jsonl"


class DecisionRecorder:
    """线程安全的决策点记录器（append-only JSONL）。"""

    def __init__(self, path=DEFAULT_PATH, enabled=True):
        self.path = Path(path)
        self.enabled = bool(enabled)
        self._lock = threading.Lock()

    def record(self, **fields) -> bool:
        """追加一条决策记录；任何失败静默返回 False（不影响对局）。"""
        if not self.enabled:
            return False
        fields.setdefault("time", time.strftime("%Y-%m-%d %H:%M:%S"))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(fields, ensure_ascii=False, default=str)
            with self._lock:
                with self.path.open("a", encoding="utf8") as sink:
                    sink.write(line + "\n")
            return True
        except Exception:
            return False
