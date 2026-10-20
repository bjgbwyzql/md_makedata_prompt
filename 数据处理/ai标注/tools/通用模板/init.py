# -*- coding: utf-8 -*-
"""AI 标注模板分发器：把 ai标注/通用模板 复制一份给指定的人。

用法（在 PyCharm 里运行本文件）：
    python init.py                # 读 config.yaml 的 name；name 为空时在控制台提示输入
    python init.py --name 张三    # 命令行指定名字
    或双击 run.bat

配置全部在 config.yaml（名字、模板来源、目标位置、冲突策略、排除项）。
产物：<ai标注>/<名字>/ 下就是完整的标注模板，点进去即可使用。
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
            source = os.path.join(dirpath, fn)
            shutil.copy2(source, os.path.join(target_dir, fn))
            count += 1
            total_bytes += os.path.getsize(source)
    return count, total_bytes


def ask_name(cfg) -> str:
    name = str(cfg.get("name") or "").strip()
    if name:
        return name
    try:
        return input("请输入使用人名字（将作为文件夹名）：").strip()
    except EOFError:
        return ""


def main():
    cfg = load_config()

    parser = argparse.ArgumentParser(description="分发 AI 标注通用模板")
    parser.add_argument(
        "--name",
        default="",
        help="使用人名字（目标文件夹名）；不填则读 config.yaml，仍为空时提示输入",
    )
    args = parser.parse_args()

    name = str(args.name or "").strip() or ask_name(cfg)
    if not name:
        sys.exit("未提供名字：在 config.yaml 里写 name: 张三，或用 --name 张三 运行")

    illegal = str(cfg.get("illegal_name_chars") or '<>:"/\\|?*')
    bad = [c for c in name if c in illegal]
    if bad or name in (".", ".."):
        sys.exit(f"名字不合法（含 {''.join(sorted(set(bad)))} 等字符）: {name}")

    # 路径解析：相对 config.yaml 所在目录
    src = os.path.normpath(os.path.join(HERE, str(cfg.get("template_dir") or "../../通用模板")))
    parent = os.path.normpath(os.path.join(HERE, str(cfg.get("target_parent") or "../..")))
    # 注意区分"键没写"（默认 通用模板）和"写了空串"（平铺到 <名字>/ 下）
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

    # 源模板里的 配置.yaml 含本人的密钥，已排除；这里用示例生成一份干净的配置
    example = os.path.join(dst, "配置示例.yaml")
    target_config = os.path.join(dst, "配置.yaml")
    if os.path.exists(example):
        shutil.copy2(example, target_config)
        print("已根据 配置示例.yaml 生成干净的 配置.yaml（密钥需使用人自己填写）")
    else:
        print("⚠ 未找到 配置示例.yaml，请手动创建 配置.yaml")

    print(f"\n✅ 完成：复制 {n} 个文件（{size_mb/1024/1024:.1f} MB）→ {dst}")
    print("接下来告诉使用人：")
    print(f"  1. 用 PyCharm 打开 {dst}")
    print("  2. 在 requirements.txt 上按提示安装依赖，或执行 pip install -r requirements.txt")
    print("  3. 打开 配置.yaml，填写 接口.地址 / 接口.密钥 / 接口.模型")
    print("  4. 图片放进 输入图片，右键 标注引擎.py → Run")
    print("  5. 第一次建议在运行参数里加 --检查，先验证接口和模型")
    print("  提示：旧密钥、旧结果、旧图片和缓存均已排除，交付的是干净模板")


if __name__ == "__main__":
    main()
