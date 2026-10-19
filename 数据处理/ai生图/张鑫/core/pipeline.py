# -*- coding: utf-8 -*-
"""批量生成引擎：提交 → 轮询 → 下载 →（可选）配对复制 before 图。

文生图（make_base_images）与图生图造变化对（make_change_pairs）共用。
场景配置只需提供一个 dict（见 core/scenecfg.py 说明）。
"""

import os
import random
import shutil
import time
from datetime import datetime

from PIL import Image

from . import grsai_client as api
from .task_state import (
    get_running_keys,
    load_state,
    mark_stale_running_failed,
    save_state,
)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
DEFAULT_OUTPUT_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output"
)


# ---------- 小工具 ----------
def ensure_unique_path(file_path: str) -> str:
    """目标存在时追加 _1/_2… 避免覆盖。"""
    if not os.path.exists(file_path):
        return file_path
    base, ext = os.path.splitext(file_path)
    counter = 1
    while True:
        candidate = f"{base}_{counter}{ext}"
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def safe_filename(name: str) -> str:
    """Windows 兼容文件名。"""
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name.strip().strip(".")


def make_run_dir(output_root: str, scene_name: str) -> str:
    """每次运行创建独立目录：output/images/<场景>_<yymmdd_HHMMSS>/。"""
    run_id = datetime.now().strftime("%y%m%d_%H%M%S")
    run_dir = os.path.join(output_root, "images", f"{scene_name}_{run_id}")
    os.makedirs(run_dir, exist_ok=True)
    return run_dir, run_id


def normalize_to_size(image_path: str, width: int, height: int, mode="RGB") -> None:
    """中心裁剪 + 缩放到固定尺寸（文生图统一分辨率用）。"""
    with Image.open(image_path) as image:
        image = image.convert(mode)
        target_ratio = width / height
        source_ratio = image.width / image.height
        if source_ratio > target_ratio:
            new_width = round(image.height * target_ratio)
            left = (image.width - new_width) // 2
            image = image.crop((left, 0, left + new_width, image.height))
        elif source_ratio < target_ratio:
            new_height = round(image.width / target_ratio)
            top = (image.height - new_height) // 2
            image = image.crop((0, top, image.width, top + new_height))
        if image.size != (width, height):
            image = image.resize((width, height), Image.Resampling.LANCZOS)
        image.save(image_path, format="PNG")


def resize_to_match(image_path: str, match_path: str) -> bool:
    """把生成图缩放成与原图同尺寸（图生图配准用）。"""
    if not match_path or not os.path.exists(match_path):
        return False
    try:
        with Image.open(match_path) as match_img:
            target_size = match_img.size
        with Image.open(image_path) as result_img:
            if result_img.size == target_size:
                return True
            resized = result_img.convert("RGB").resize(target_size, Image.Resampling.LANCZOS)
            resized.save(image_path)
        return True
    except Exception as e:
        print(f"生成图尺寸对齐失败: {image_path} -> {e}")
        return False


# ---------- 轮询下载 ----------
def _poll_and_download(cfg, state, task_keys, run_dir, run_id):
    """轮询一批任务并下载；返回是否发生额度错误。"""
    if not task_keys:
        return False

    api_key = cfg["api_key"]
    poll_interval = cfg.get("poll_interval", 3)
    poll_max = cfg.get("poll_max", 300)
    timeout = cfg.get("request_timeout", 30)
    copy_before = cfg.get("copy_before", False)
    started = time.time()
    quota_error = False

    print(f"开始轮询 {len(task_keys)} 个任务...")
    while True:
        all_done = True
        modified = False

        for key in task_keys:
            info = state.get(key)
            if not info or info.get("status") != "running":
                continue
            all_done = False

            res = api.check_task_status(api_key, info["id"], timeout=timeout)
            if not res:
                continue
            if res.get("code") != 0:
                msg = res.get("msg", "未知错误")
                if api.check_api_quota_error(msg):
                    print(f"🔴 [严重错误] {key}: API 积分/额度不足！{msg}")
                    quota_error = True
                continue

            body = res.get("data", {})
            status = body.get("status")
            progress = body.get("progress", 0)

            if status == "succeeded":
                results = body.get("results", [])
                img_url = results[0].get("url") if results else ""
                if not img_url:
                    info["status"] = "failed"
                    info["error"] = "成功但无图片链接"
                    modified = True
                    continue

                save_path = ensure_unique_path(
                    os.path.join(run_dir, info["output_name"])
                )
                if api.download_image(img_url, save_path, timeout=timeout):
                    # 尺寸处理：文生图归一到固定尺寸；图生图对齐回原图
                    if cfg.get("target_size"):
                        normalize_to_size(save_path, *cfg["target_size"])
                    elif cfg.get("align_to_source") and info.get("src_path"):
                        resize_to_match(save_path, info["src_path"])

                    info["status"] = "succeeded"
                    info["result_path"] = save_path

                    if copy_before and info.get("src_path"):
                        before_dir = os.path.join(
                            os.path.dirname(run_dir), "..", "before",
                            f"{cfg['scene']}_{run_id}",
                        )
                        before_dir = os.path.normpath(before_dir)
                        os.makedirs(before_dir, exist_ok=True)
                        before_path = ensure_unique_path(
                            os.path.join(before_dir, os.path.basename(save_path))
                        )
                        shutil.copy2(info["src_path"], before_path)
                        info["before_path"] = before_path
                else:
                    info["status"] = "failed"
                    info["error"] = "下载失败"
                modified = True

            elif status == "failed":
                err = f"failure_reason={body.get('failure_reason')}, error={body.get('error')}"
                if api.check_api_quota_error(err):
                    quota_error = True
                info["status"] = "failed"
                info["error"] = err
                modified = True
            else:
                print(f"⏳ {key}: {status} {progress}%")

        if modified:
            save_state(cfg["state_file"], state)

        if poll_max and poll_max > 0 and time.time() - started >= poll_max:
            print(f"本轮轮询超过 {poll_max} 秒，剩余任务标记为 failed")
            for key in task_keys:
                info = state.get(key)
                if info and info.get("status") == "running":
                    info["status"] = "failed"
                    info["error"] = f"轮询超过 {poll_max} 秒"
            save_state(cfg["state_file"], state)
            return quota_error

        if all_done or quota_error:
            ok = sum(1 for k in task_keys if state.get(k, {}).get("status") == "succeeded")
            bad = sum(1 for k in task_keys if state.get(k, {}).get("status") == "failed")
            print(f"📈 本批统计：成功 {ok}，失败 {bad}，输出: {run_dir}")
            return quota_error

        time.sleep(poll_interval)


# ---------- 对外入口 ----------
def make_base_images(cfg):
    """文生图批量造底图。cfg 需含: scene/prompt_builder/target_count 等。"""
    cfg.setdefault("api_key", api.load_api_key())
    run_dir, run_id = make_run_dir(cfg["output_root"], cfg["scene"])
    state = load_state(cfg["state_file"])
    mark_stale_running_failed(state, cfg.get("stale_running_seconds", 1800))

    running = get_running_keys(state)
    if running:
        print(f"恢复 {len(running)} 个历史任务，先轮询下载")
        if _poll_and_download(cfg, state, running, run_dir, run_id):
            return

    total = int(cfg.get("target_count", 10))
    batch_size = max(1, int(cfg.get("batch_size", 5)))
    print(f"[开始] 场景 {cfg['scene']}：计划生成 {total} 张 → {run_dir}")

    submitted = 0
    seq = 0
    while submitted < total:
        batch_n = min(batch_size, total - submitted)
        batch_keys = []
        print(f"\n[提交批次] {submitted + 1}-{submitted + batch_n}/{total}")

        for _ in range(batch_n):
            seq += 1
            key = f"{run_id}_{seq:04d}"
            output_name = safe_filename(f"{cfg['file_prefix']}_{run_id}_{seq:04d}.png")
            prompt = cfg["prompt_builder"](seq)
            task_id = api.submit_task(
                cfg["api_key"], prompt,
                image_path=None,
                model=cfg.get("model", api.DEFAULT_MODEL),
                image_size=cfg.get("image_size", "4K"),
                aspect_ratio=cfg.get("aspect_ratio", "4:3"),
                negative_prompt=cfg.get("negative_prompt", ""),
                timeout=cfg.get("request_timeout", 30),
                tag=key,
            )
            if task_id:
                state[key] = {
                    "id": task_id,
                    "status": "running",
                    "timestamp": time.time(),
                    "prompt": prompt,
                    "output_name": output_name,
                }
                save_state(cfg["state_file"], state)
                batch_keys.append(key)
            time.sleep(cfg.get("submit_interval", 2))

        submitted += batch_n
        if batch_keys and _poll_and_download(cfg, state, batch_keys, run_dir, run_id):
            print("[停止] 检测到 API 额度不足")
            return

    print(f"\n[完成] 输出目录: {run_dir}")


def make_change_pairs(cfg):
    """图生图造双时相变化对：底图 → 变化后图 + 复制原图为变化前图。"""
    cfg.setdefault("api_key", api.load_api_key())

    source_dir = cfg["source_dir"]
    if not os.path.isdir(source_dir):
        print(f"底图目录不存在: {source_dir}")
        return

    files = [
        os.path.join(source_dir, n)
        for n in sorted(os.listdir(source_dir))
        if os.path.splitext(n)[1].lower() in IMAGE_EXTENSIONS
    ]
    if not files:
        print("底图目录里没有图片")
        return

    if cfg.get("random_seed") is not None:
        random.seed(cfg["random_seed"])

    desired = int(cfg.get("target_count", 0))
    if desired <= 0:
        sources = list(files)
    elif len(files) >= desired:
        sources = random.sample(files, desired)
    else:
        sources = list(files) + random.choices(files, k=desired - len(files))

    if cfg.get("shuffle", True) and len(sources) > 1:
        random.shuffle(sources)

    run_dir, run_id = make_run_dir(cfg["output_root"], cfg["scene"])
    state = load_state(cfg["state_file"])
    mark_stale_running_failed(state, cfg.get("stale_running_seconds", 300))

    running = get_running_keys(state)
    if running and _poll_and_download(cfg, state, running, run_dir, run_id):
        return

    batch_size = max(1, int(cfg.get("batch_size", 5)))
    print(f"[开始] 场景 {cfg['scene']}：计划生成 {len(sources)} 对 → {run_dir}")

    prompt = cfg["prompt"]
    done = 0
    seq = 0
    while done < len(sources):
        batch = sources[done:done + batch_size]
        batch_keys = []
        print(f"\n[提交批次] {done + 1}-{done + len(batch)}/{len(sources)}")

        for img_path in batch:
            seq += 1
            stem = os.path.splitext(os.path.basename(img_path))[0]
            key = safe_filename(f"{run_id}_{seq:04d}_{stem}")
            task_id = api.submit_task(
                cfg["api_key"], prompt,
                image_path=img_path,
                model=cfg.get("model", api.DEFAULT_MODEL),
                image_size=cfg.get("image_size", "4K"),
                aspect_ratio="auto",
                negative_prompt=cfg.get("negative_prompt", ""),
                timeout=cfg.get("request_timeout", 30),
                tag=key,
            )
            if task_id:
                state[key] = {
                    "id": task_id,
                    "status": "running",
                    "timestamp": time.time(),
                    "src_path": img_path,
                    "output_name": safe_filename(f"{stem}.png"),
                }
                save_state(cfg["state_file"], state)
                batch_keys.append(key)
            time.sleep(cfg.get("submit_interval", 2))

        done += len(batch)
        if batch_keys and _poll_and_download(cfg, state, batch_keys, run_dir, run_id):
            print("[停止] 检测到 API 额度不足")
            return

    before_hint = os.path.normpath(os.path.join(
        os.path.dirname(run_dir), "..", "before",
        "{}_{}".format(cfg["scene"], run_id)))
    print("\n[完成] after 图: {}\n        before 图: {}".format(run_dir, before_hint))
