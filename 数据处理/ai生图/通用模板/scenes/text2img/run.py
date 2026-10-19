# -*- coding: utf-8 -*-
"""text2img 通用场景模板：文生图批量造底图。

需要改动的东西（提示词、变体池、API Key、尺寸、张数等）都在同目录
scenes.yaml 里，新增场景 = 在 yaml 的 scenes: 下加一段，不用碰本文件。

用法（在 scenes/text2img 目录下）：
    python run.py list                                 # 列出所有场景
    python run.py building_single --count 300          # 跑一个场景
    python run.py building_roof_pattern --count 50 --batch 1

输出（自动创建）：
    仓库根 output/images/<场景>_<时间戳>/   生成底图（按 target_size 裁剪归一）
    仓库根 output/tasks/<场景>_tasks.json   断点状态，Ctrl+C 后重跑自动续
"""

import argparse
import os
import random
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

import yaml  # noqa: E402

from core import grsai_client as api  # noqa: E402
from core.pipeline import DEFAULT_OUTPUT_ROOT, make_base_images  # noqa: E402

YAML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenes.yaml")


def load_yaml():
    if not os.path.exists(YAML_PATH):
        sys.exit(f"缺少配置文件: {YAML_PATH}")
    with open(YAML_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("defaults") or {}, data.get("scenes") or {}


def resolve_api_key(defaults):
    """优先取 yaml 里的 api_key（非空），否则退回环境变量 GRSAI_API_KEY / .env。"""
    key = str(defaults.get("api_key") or "").strip()
    if key:
        return key
    return api.load_api_key()


def main():
    parser = argparse.ArgumentParser(description="text2img 底图生成（配置见 scenes.yaml）")
    parser.add_argument("scene", nargs="?", default=None,
                        help="场景名；不传则用 scenes.yaml defaults 里的 scene 配置。传 list 列出全部场景")
    parser.add_argument("--count", type=int, help="计划生成张数（覆盖 yaml）")
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

    # 场景选择：命令行参数优先，否则读 defaults.scene —— 只改 yaml 一行
    # scene: xxx 就能零命令行运行（双击 bat / 直接 python run.py）
    scene_name = args.scene or str(defaults.get("scene") or "").strip()
    if not scene_name:
        sys.exit("未指定场景：命令行传场景名，或在 scenes.yaml 的 defaults 里写 scene: <场景名>"
                 f"（可选: {', '.join(scenes) or '(空)'}）")
    if scene_name not in scenes:
        sys.exit(f"未找到场景 '{scene_name}'，可选: {', '.join(scenes) or '(空)'}")
    scene = scenes[scene_name]

    base_prompt = str(scene.get("base_prompt") or "").strip()
    if not base_prompt:
        sys.exit(f"场景 '{scene_name}' 缺少 base_prompt，请检查 scenes.yaml")
    variants = [str(v).strip() for v in (scene.get("variants") or []) if str(v).strip()]
    negative_prompt = str(scene.get("negative_prompt") or "")

    merged = {**{k: v for k, v in defaults.items()}, **{k: v for k, v in scene.items()}}
    if args.count is not None:
        merged["target_count"] = args.count
    if args.batch is not None:
        merged["batch_size"] = args.batch
    if args.size:
        merged["image_size"] = args.size

    # 拼提示词：主体 + 随机变体 + 差异性要求（与原 ag_forest_generate 一致）
    def build_prompt(index: int) -> str:
        if variants:
            variant = random.choice(variants)
            return (
                f"{base_prompt}"
                f"本张图的具体场景：{variant}。"
                f"生成为第 {index} 张数据，要求和其他图片有明显差异，"
                "但仍然保持真实无人机正射影像风格。"
            )
        return base_prompt

    target_size = merged.get("target_size") or []
    if target_size and len(target_size) != 2:
        sys.exit("target_size 必须是 [宽, 高] 两个数")

    # 输出根目录：yaml 可配绝对路径或相对仓库根的路径，默认仓库内 output/
    # （其下自动建 images/ 与 tasks/）
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
        "file_prefix": str(merged.get("file_prefix", scene_name)),
        "prompt_builder": build_prompt,
        "negative_prompt": negative_prompt,
        "target_count": int(merged.get("target_count", 100)),
        "batch_size": int(merged.get("batch_size", 5)),
        "image_size": str(merged.get("image_size", "4K")),
        "aspect_ratio": str(merged.get("aspect_ratio", "4:3")),
        "model": str(merged.get("model", api.DEFAULT_MODEL)),
        "submit_interval": merged.get("submit_interval", 2),
        "poll_interval": merged.get("poll_interval", 3),
        "poll_max": merged.get("poll_max", 300),
        "request_timeout": merged.get("request_timeout", 30),
        "stale_running_seconds": merged.get("stale_running_seconds", 1800),
    }
    if target_size:
        cfg["target_size"] = (int(target_size[0]), int(target_size[1]))
    if merged.get("random_seed") is not None:
        random.seed(merged["random_seed"])

    make_base_images(cfg)


if __name__ == "__main__":
    main()
