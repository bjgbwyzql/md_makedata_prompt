# -*- coding: utf-8 -*-
"""grsai 绘图接口客户端：提交任务 / 查询状态 / 下载图片 / 额度错误识别。

合并自 ag_forest_generate.py 与 ag_stable.py 中的重复实现。
API Key 只从环境变量 GRSAI_API_KEY（或 .env）读取，不再硬编码。
"""

import base64
import os
import re
import tempfile

import requests

API_BASE_URL = "https://grsai.dakka.com.cn"
DRAW_SUBMIT_PATH = "/v1/draw/completions"
DRAW_RESULT_PATH = "/v1/draw/result"
DEFAULT_MODEL = "gpt-image-2"  # gpt-image-2 / nano-banana-2 / nano-banana-fast
NEGATIVE_PROMPT_FIELD = "negativePrompt"

HTTP_SESSION = requests.Session()
HTTP_SESSION.trust_env = False

_QUOTA_KEYWORDS = [
    "积分不足", "余额不足", "额度不足", "积分用尽", "余额用尽", "欠费", "未充值",
    "quota", "Quota", "exceed", "Exceed", "limit", "Limit",
    "insufficient", "Insufficient", "balance",
]


def load_api_key() -> str:
    """读取密钥：环境变量优先，其次仓库根目录 .env。找不到则报错退出。"""
    key = os.getenv("GRSAI_API_KEY", "").strip()
    if key:
        return key

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_file = os.path.join(repo_root, ".env")
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            text = f.read()
        match = re.search(r"^GRSAI_API_KEY\s*=\s*(\S+)", text, re.MULTILINE)
        if match:
            return match.group(1).strip().strip('"').strip("'")

    raise RuntimeError(
        "未找到 API Key。请设置环境变量 GRSAI_API_KEY，"
        "或复制 .env.example 为 .env 并填入密钥。"
    )


def check_api_quota_error(error_text) -> bool:
    """识别积分/额度不足类错误（关键词匹配）。"""
    text = str(error_text)
    return any(keyword in text for keyword in _QUOTA_KEYWORDS)


def get_image_base64(file_path: str):
    """读取本地图片并转换为带 Header 的 Base64 字符串（图生图用）。"""
    if not os.path.exists(file_path):
        print(f"图片文件不存在: {file_path}")
        return None
    ext = file_path.rsplit(".", 1)[-1].lower()
    ext = "jpeg" if ext == "jpg" else ext
    try:
        with open(file_path, "rb") as image_file:
            encoded = base64.b64encode(image_file.read()).decode("utf-8")
        return f"data:image/{ext};base64,{encoded}"
    except Exception as e:
        print(f"读取图片失败 {file_path}: {e}")
        return None


def submit_task(api_key, prompt, image_path=None, model=DEFAULT_MODEL,
                image_size="4K", aspect_ratio="auto", negative_prompt="",
                timeout=30, tag=""):
    """提交绘画任务，返回 Task ID；失败返回 None。

    image_path 为空 → 文生图；有值 → 图生图（原图以 base64 随 urls 提交）。
    """
    url = f"{API_BASE_URL}{DRAW_SUBMIT_PATH}"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

    payload = {
        "model": model,
        "prompt": prompt,
        "aspectRatio": aspect_ratio,
        "imageSize": image_size,
        "webHook": "-1",  # 立即返回 ID，不阻塞等待
    }
    if image_path:
        image_data = get_image_base64(image_path)
        if not image_data:
            print(f"提交失败：图片读取失败 {image_path}")
            return None
        payload["urls"] = [image_data]
    if negative_prompt and negative_prompt.strip():
        payload[NEGATIVE_PROMPT_FIELD] = negative_prompt

    try:
        response = HTTP_SESSION.post(url, headers=headers, json=payload, timeout=timeout)
        response.raise_for_status()
        res_json = response.json()
    except requests.RequestException as e:
        print(f"[提交异常] {tag}: {e}")
        return None
    except ValueError as e:
        print(f"[提交异常] {tag}: 响应不是 JSON: {e}")
        return None

    if res_json.get("code") == 0:
        task_id = res_json.get("data", {}).get("id")
        print(f"[提交成功] {tag} -> ID: {task_id}")
        return task_id

    error_msg = res_json.get("msg", "未知错误")
    if check_api_quota_error(error_msg):
        print(f"🔴 [严重错误] {tag}: API 积分/额度不足！{error_msg}")
    else:
        print(f"[提交失败] {tag}: code={res_json.get('code')}, msg={error_msg}")
    return None


def check_task_status(api_key, task_id, timeout=30):
    """查询任务状态，返回响应 dict；异常返回 None。"""
    url = f"{API_BASE_URL}{DRAW_RESULT_PATH}"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = HTTP_SESSION.post(
            url, headers=headers, json={"id": task_id}, timeout=timeout
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"查询任务异常 {task_id}: {e}")
        return None
    except ValueError as e:
        print(f"查询任务异常 {task_id}: 响应 JSON 解析失败: {e}")
        return None


def download_image(url, save_path, timeout=30):
    """下载并保存图片（原子写入临时文件再改名，避免半截图）。"""
    save_dir = os.path.dirname(save_path)
    os.makedirs(save_dir, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(suffix=".part", dir=save_dir)
    try:
        response = HTTP_SESSION.get(url, timeout=timeout)
        response.raise_for_status()
        with os.fdopen(fd, "wb") as f:
            f.write(response.content)
        os.replace(temp_path, save_path)
        print(f"图片已保存: {save_path}")
        return True
    except requests.RequestException as e:
        print(f"下载失败(网络): {save_path} - {e}")
        return False
    except OSError as e:
        print(f"保存失败: {save_path} - {e}")
        return False
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
