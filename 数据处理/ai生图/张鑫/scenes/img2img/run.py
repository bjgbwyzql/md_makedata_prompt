# -*- coding: utf-8 -*-
"""img2img 通用场景模板：图生图造双时相变化对。

所有需要改动的东西（提示词、API Key、模型、尺寸、批大小等）都在同目录
scenes.yaml 里，新增场景 = 在 yaml 的 scenes: 下加一段，不用碰本文件。

用法（在 scenes/img2img 目录下）：
    python run.py list                          # 列出所有可用场景
    python run.py roof_violation --source D:/data/一期图   # 跑一个场景
    python run.py roof_complete --source D:/data/一期图 --count 99 --batch 5

输出（自动创建）：
    仓库根 output/images/<场景>_<时间戳>/   after 图（生成结果）
    仓库根 output/before/<场景>_<时间戳>/   before 图（复制的底图，与 after 同名配对）
    仓库根 output/tasks/<场景>_tasks.json   断点状态，Ctrl+C 后重跑自动续
"""

import argparse
import os
import sys

# 把仓库根加入模块路径（core 在上一层）
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

import yaml  # noqa: E402

from core import grsai_client as api  # noqa: E402
from core.pipeline import DEFAULT_OUTPUT_ROOT, make_change_pairs  # noqa: E402

YAML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenes.yaml")

# yaml 场景段落里属于"配置"的键，其余键视为提示词/说明
CONFIG_KEYS = {
    "source_dir", "output_root", "target_count", "batch_size", "image_size", "model",
    "submit_interval", "poll_interval", "poll_max", "request_timeout",
    "stale_running_seconds", "random_seed", "shuffle", "copy_before",
    "align_to_source", "output_name_prefix",
}


def load_yaml():
    if not os.path.exists(YAML_PATH):
        sys.exit(f"缺少配置文件: {YAML_PATH}")
    with open(YAML_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("defaults") or {}, data.get("scenes") or {}


def resolve_api_key(defaults):
    """优先取 yaml 里的 api_key（非空），否则退回环境变量 GRSAI_API_KEY。"""
    key = str(defaults.get("api_key") or "").strip()
    if key:
        return key
    return api.load_api_key()  # 环境变量 / .env，都没有则报错


def main():
    parser = argparse.ArgumentParser(description="img2img 变化对生成（配置见 scenes.yaml）")
    parser.add_argument("scene", nargs="?", default=None,
                        help="场景名；不传则用 scenes.yaml defaults 里的 scene 配置。传 list 列出全部场景")
    parser.add_argument("--source", help="一期底图目录（覆盖 yaml 中的 source_dir）")
    parser.add_argument("--count", type=int, help="目标生成对数（覆盖 yaml；<=0 每张底图各1次）")
    parser.add_argument("--batch", type=int, help="批大小（覆盖 yaml）")
    parser.add_argument("--size", help="图像尺寸 1K/2K/4K（覆盖 yaml）")
    args = parser.parse_args()

    defaults, scenes = load_yaml()

    if args.scene == "list":
        print("可用场景（scenes.yaml）：")
        for name, sc in scenes.items():
            desc = str(sc.get("desc", "")).strip().replace("\n", " ")
            print(f"  {name:28s} {desc}")
        return

    # 场景选择：命令行参数优先，否则读 defaults.scene —— 这样只改 yaml
    # 里一行 scene: xxx 就能"零命令行"运行（双击 bat / 直接 python run.py）
    scene_name = args.scene or str(defaults.get("scene") or "").strip()
    if not scene_name:
        sys.exit("未指定场景：命令行传场景名，或在 scenes.yaml 的 defaults 里写 scene: <场景名>"
                 f"（可选: {', '.join(scenes) or '(空)'}）")
    if scene_name not in scenes:
        sys.exit(f"未找到场景 '{scene_name}'，可选: {', '.join(scenes) or '(空)'}")
    scene = scenes[scene_name]

    # 三层合并：pipeline 内置默认 < yaml defaults < yaml 场景 < 命令行
    merged = {k: v for k, v in defaults.items() if k in CONFIG_KEYS}
    merged.update({k: v for k, v in scene.items() if k in CONFIG_KEYS})
    if args.source:
        merged["source_dir"] = args.source
    if args.count is not None:
        merged["target_count"] = args.count
    if args.batch is not None:
        merged["batch_size"] = args.batch
    if args.size:
        merged["image_size"] = args.size

    source_dir = str(merged.get("source_dir") or "").strip()
    if not source_dir:
        sys.exit("未指定底图目录：--source 传入，或在 scenes.yaml 的 defaults/场景里写 source_dir")
    if not str(scene.get("prompt") or "").strip():
        sys.exit(f"场景 '{scene_name}' 缺少 prompt，请检查 scenes.yaml")

    # 输出根目录：yaml 可配绝对路径或相对仓库根的路径，默认仓库内 output/
    # （其下自动建 images/ 与 before/ 与 tasks/）
    output_root = str(merged.get("output_root") or "").strip()
    if not output_root:
        output_root = DEFAULT_OUTPUT_ROOT
    elif not os.path.isabs(output_root):
        output_root = os.path.join(REPO_ROOT, output_root)
    output_root = os.path.normpath(output_root)

    cfg = {
        "scene": scene_name,
        "api_key": resolve_api_key(defaults),
        "state_file": os.path.join(output_root, "tasks", f"{scene_name}_tasks.json"),
        "output_root": output_root,
        "source_dir": os.path.abspath(source_dir),
        "prompt": scene["prompt"],
        "negative_prompt": scene.get("negative_prompt", ""),
        "copy_before": merged.get("copy_before", True),     # 配对输出 before 图
        "align_to_source": merged.get("align_to_source", True),  # after 缩放回原图尺寸
        "target_count": int(merged.get("target_count", -1)),
        "batch_size": int(merged.get("batch_size", 5)),
        "image_size": str(merged.get("image_size", "4K")),
        "model": str(merged.get("model", api.DEFAULT_MODEL)),
        "submit_interval": merged.get("submit_interval", 2),
        "poll_interval": merged.get("poll_interval", 3),
        "poll_max": merged.get("poll_max", 300),
        "request_timeout": merged.get("request_timeout", 30),
        "stale_running_seconds": merged.get("stale_running_seconds", 300),
    }
    if merged.get("random_seed") is not None:
        cfg["random_seed"] = merged["random_seed"]
    if "shuffle" in merged:
        cfg["shuffle"] = bool(merged["shuffle"])

    make_change_pairs(cfg)


if __name__ == "__main__":
    main()
