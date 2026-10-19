# -*- coding: utf-8 -*-
"""任务断点状态管理：json 读写 + 历史 running 任务清理。

合并自 ag_forest_generate.py / ag_stable.py 的 load_state/save_state 逻辑。
状态文件统一放 output/tasks/，不进 git。
"""

import json
import os
import tempfile
import time


def load_state(state_file: str) -> dict:
    if not os.path.exists(state_file):
        return {}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        print(f"状态文件损坏，忽略重来: {state_file}")
        return {}


def save_state(state_file: str, state: dict) -> None:
    """原子写入（临时文件 + os.replace），避免中断产生半截 json。"""
    state_dir = os.path.dirname(state_file) or "."
    os.makedirs(state_dir, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".tmp_tasks_", suffix=".json", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, state_file)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def mark_stale_running_failed(state: dict, stale_seconds: int) -> int:
    """把超时未完成的 running 任务标记为 failed（手动停止残留）。返回处理数。"""
    if not stale_seconds or stale_seconds <= 0:
        return 0
    now = time.time()
    count = 0
    for info in state.values():
        if (
            info.get("status") == "running"
            and now - float(info.get("timestamp", 0)) > stale_seconds
        ):
            info["status"] = "failed"
            info["error"] = f"历史 running 超过 {stale_seconds} 秒，疑似手动停止残留"
            count += 1
    if count:
        print(f"检测到 {count} 个历史残留 running 任务，已标记为 failed")
    return count


def get_running_keys(state: dict) -> list:
    return [k for k, v in state.items() if v.get("status") == "running"]
