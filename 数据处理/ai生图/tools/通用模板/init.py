# -*- coding: utf-8 -*-
"""通用模板分发器：把仓库根的「通用模板」复制一份给新人。

用法（在本目录下）：
    python init.py                # 用 config.yaml 里的 name
    python init.py --name 张三    # 命令行指定新人名字
    或双击 run.bat

配置全部在 config.yaml（名字、模板来源、目标位置、冲突策略、排除项）。
产物：<仓库根>/<新人名字>/通用模板/…，一套直接可跑的代码+YAML 配置。
"""

import argparse
import datetime
import fnmatch
import os
import shutil
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.yaml")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit(f"缺少配置文件: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def is_excluded(rel_path: str, patterns) -> bool:
    """按 config.yaml 的 exclude glob 判断（统一用 / 分隔）。"""
    rel = rel_path.replace("\\", "/")
    for pat in patterns:
        pat = pat.replace("\\", "/")
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat.rstrip("/") + "/**"):
            return True
        # 目录本身命中（如 **/__pycache__）时，其下内容一并排除
        if pat.endswith("**"):
            base = pat[:-2].rstrip("/")
            if rel == base or rel.startswith(base + "/"):
                return True
    return False


def copy_template(src_root: str, dst_root: str, patterns) -> tuple:
    """复制目录树并跳过排除项，返回 (复制文件数, 复制字节数)。"""
    count = total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(src_root):
        rel_dir = os.path.relpath(dirpath, src_root)
        # 原地剪掉被排除的子目录，不再深入
        dirnames[:] = [
            d for d in dirnames
            if not is_excluded(os.path.join(rel_dir, d), patterns)
        ]
        target_dir = os.path.join(dst_root, rel_dir) if rel_dir != "." else dst_root
        os.makedirs(target_dir, exist_ok=True)
        for fn in filenames:
            rel_file = os.path.join(rel_dir, fn) if rel_dir != "." else fn
            if is_excluded(rel_file, patterns):
                continue
            s = os.path.join(dirpath, fn)
            shutil.copy2(s, os.path.join(target_dir, fn))
            count += 1
            total_bytes += os.path.getsize(s)
    return count, total_bytes


def main():
    cfg = load_config()

    parser = argparse.ArgumentParser(description="分发通用模板给新人")
    parser.add_argument("--name", default=str(cfg.get("name") or "").strip(),
                        help="新人名字（目标文件夹名）；缺省读 config.yaml")
    args = parser.parse_args()

    name = str(args.name or "").strip()
    if not name:
        sys.exit("未提供新人名字：config.yaml 里写 name: xxx，或运行 --name 张三")

    illegal = str(cfg.get("illegal_name_chars") or '<>:"/\\|?*')
    bad = [c for c in name if c in illegal]
    if bad or name in (".", ".."):
        sys.exit(f"名字不合法（含 {''.join(sorted(set(bad)))} 等字符）: {name}")

    # 路径解析：相对 config.yaml 所在目录
    src = os.path.normpath(os.path.join(HERE, str(cfg.get("template_dir") or "../../通用模板")))
    parent = os.path.normpath(os.path.join(HERE, str(cfg.get("target_parent") or "../..")))
    # 注意区分"键没写"（默认 通用模板）和"写了空串"（平铺到 <name>/ 下）
    raw_subdir = cfg.get("dest_subdir", "通用模板")
    subdir = str(raw_subdir if raw_subdir is not None else "通用模板").strip()
    if not os.path.isdir(src):
        sys.exit(f"模板目录不存在: {src}")
    dst = os.path.join(parent, name, subdir) if subdir else os.path.join(parent, name)
    workspace = os.path.join(parent, name)
    print(f"模板来源: {src}")
    print(f"目标位置: {dst}")

    if os.path.exists(workspace):
        policy = str(cfg.get("on_conflict") or "abort").lower()
        if policy == "abort":
            sys.exit(
                f"目标已存在: {workspace}\n"
                "处理办法：换名字（--name 张三-02），或手动删除/改名旧工作区后重跑；"
                "也可在 config.yaml 把 on_conflict 改为 backup 自动备份旧目录。"
            )
        if policy == "backup":
            stamp = datetime.datetime.now().strftime("%y%m%d_%H%M%S")
            backup = f"{workspace}_备份_{stamp}"
            os.rename(workspace, backup)
            print(f"⚠ 旧工作区已备份为: {backup}")
        else:
            sys.exit(f"on_conflict 配置无效: {policy}（可选 abort / backup）")

    patterns = cfg.get("exclude") or []
    n, size_mb = copy_template(src, dst, patterns)
    print(f"\n✅ 完成：复制 {n} 个文件（{size_mb/1024/1024:.1f} MB）→ {dst}")
    print("接下来告诉新人：")
    print(f"  1. pip install -r requirements.txt（requests Pillow PyYAML）")
    print(f"  2. 把 {os.path.join(workspace, subdir, '.env.example') if subdir else os.path.join(workspace, '.env.example')} 复制为 .env 填自己的 GRSAI_API_KEY")
    print("  3. 进 scenes\\img2img|text2img 改 scenes.yaml（scene/source_dir/output_root），双击 run.bat")
    print("  4. 提示：复制时已排除 __pycache__ 与 output 旧产物，交付的是干净模板")


if __name__ == "__main__":
    main()
