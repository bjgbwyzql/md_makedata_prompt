#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用 AI 图像标注引擎。

只需填写 配置.yaml（接口地址 / 密钥 / 模型）并选择 任务/*.yaml，
即可批量完成图像标注、COCO 导出、掩码和可视化。
"""

import argparse
import base64
import colorsys
import io
import json
import logging
import math
import mimetypes
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests
import yaml
from PIL import Image, ImageDraw, ImageFont


LOGGER = logging.getLogger("AI标注")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
)


@dataclass
class TaskConfig:
    name: str
    object_noun: str
    objects_key: str
    default_label: str
    category_name: str
    category_supercategory: str
    display_fields: Tuple[str, ...]
    attributes: Dict[str, str]
    system_prompt: str
    request_prompt: str


@dataclass
class VisualizationConfig:
    enabled: bool
    max_side: int
    polygon_line_width: int
    polygon_opacity: int
    font_size: int
    font_path: Optional[Path]
    contact_sheet_enabled: bool
    contact_sheet_columns: int
    tile_max_side: int
    background: Tuple[int, int, int]
    padding: int
    label_height: int

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "VisualizationConfig":
        sheet = raw.get("总览图", {})
        font_path = raw.get("字体文件")
        return cls(
            enabled=bool(raw.get("启用", True)),
            max_side=int(raw.get("最长边", 1600)),
            polygon_line_width=int(raw.get("线宽", 4)),
            polygon_opacity=int(raw.get("透明度", 70)),
            font_size=int(raw.get("字号", 24)),
            font_path=Path(font_path) if font_path else None,
            contact_sheet_enabled=bool(sheet.get("启用", True)),
            contact_sheet_columns=int(sheet.get("列数", 2)),
            tile_max_side=int(sheet.get("单图最长边", 900)),
            background=tuple(int(value) for value in sheet.get("背景色", [24, 26, 32])),
            padding=int(sheet.get("边距", 24)),
            label_height=int(sheet.get("标签高度", 48)),
        )


@dataclass
class Config:
    base_url: str
    api_key: str
    model: str
    timeout: int
    max_retries: int
    use_json_mode: bool
    max_tokens: Optional[int]
    thinking: Optional[str]
    use_system_proxy: bool
    proxy: str
    verify_ssl: bool
    input_dir: Path
    cleaned_dir: Path
    result_dir: Path
    json_dir: Path
    mask_dir: Path
    visualization_dir: Path
    max_side: int
    jpeg_quality: int
    workers: int
    skip_processed: bool
    supported_extensions: Tuple[str, ...]
    save_cleaned_image: bool
    save_raw_json: bool
    save_coco: bool
    coco_filename: str
    save_mask: bool
    mask_foreground: int
    visualization: VisualizationConfig
    task: TaskConfig

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], base: Path, task: "TaskConfig") -> "Config":
        api = raw.get("接口", {}) or {}
        paths = raw.get("路径", {}) or {}
        image = raw.get("图片处理", {}) or {}
        processing = raw.get("批量处理", {}) or {}
        output = raw.get("输出", {}) or {}
        network = raw.get("网络", {}) or {}

        base_url = (os.environ.get("ANNOTATOR_BASE_URL") or str(api.get("地址", ""))).strip()
        api_key = (os.environ.get("ANNOTATOR_API_KEY") or str(api.get("密钥", ""))).strip()
        model = (os.environ.get("ANNOTATOR_MODEL") or str(api.get("模型", ""))).strip()
        if not base_url:
            raise ValueError("缺少 接口.地址。请打开 配置.yaml 填写接口地址。")
        if not api_key or "填写" in api_key:
            raise ValueError("还没有填写密钥。请打开 配置.yaml 填写 接口.密钥。")
        if not model:
            raise ValueError("缺少 接口.模型。请打开 配置.yaml 填写模型名称。")

        def resolve(value: Any, default: Path) -> Path:
            path = Path(value) if value else default
            return path if path.is_absolute() else base / path

        result_dir = resolve(paths.get("结果目录"), Path("结果"))
        json_raw = paths.get("原始JSON目录")
        mask_raw = paths.get("掩码目录")
        visualization_raw = paths.get("可视化目录")

        exts = tuple(
            ext.lower() if str(ext).startswith(".") else "." + str(ext).lower()
            for ext in processing.get("支持的扩展名", [".jpg", ".jpeg", ".png"])
        )
        thinking_raw = api.get("思考模式")

        return cls(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            model=model,
            timeout=int(api.get("超时秒", 180)),
            max_retries=int(api.get("重试次数", 3)),
            use_json_mode=bool(api.get("使用JSON模式", True)),
            max_tokens=int(api["最大输出Token"]) if api.get("最大输出Token") else None,
            thinking=str(thinking_raw).strip() if thinking_raw else None,
            use_system_proxy=bool(network.get("使用系统代理", False)),
            proxy=str(network.get("代理地址", "") or ""),
            verify_ssl=bool(network.get("校验SSL", True)),
            input_dir=resolve(paths.get("输入图片"), Path("输入图片")),
            cleaned_dir=resolve(paths.get("清洗后图片"), Path("清洗后图片")),
            result_dir=result_dir,
            json_dir=resolve(json_raw, result_dir / "原始JSON"),
            mask_dir=resolve(mask_raw, result_dir / "掩码"),
            visualization_dir=resolve(visualization_raw, result_dir / "可视化"),
            max_side=int(image.get("最长边", 2048)),
            jpeg_quality=int(image.get("JPEG质量", 90)),
            workers=int(processing.get("并发数", 1)),
            skip_processed=bool(processing.get("跳过已处理", True)),
            supported_extensions=exts,
            save_cleaned_image=bool(output.get("保存清洗图", True)),
            save_raw_json=bool(output.get("保存原始JSON", True)),
            save_coco=bool(output.get("保存COCO", True)),
            coco_filename=str(output.get("COCO文件名", "标注结果.json")),
            save_mask=bool(output.get("保存掩码", True)),
            mask_foreground=max(0, min(255, int(output.get("掩码前景值", 255)))),
            visualization=VisualizationConfig.from_dict(raw.get("可视化", {}) or {}),
            task=task,
        )


def load_task(path: Path) -> TaskConfig:
    if not path.exists():
        raise FileNotFoundError(f"任务文件不存在：{path}")
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"任务文件必须是 YAML 映射格式：{path}")
    system_prompt = str(raw.get("提示词", "")).strip()
    if not system_prompt:
        raise ValueError(f"任务文件缺少 提示词：{path}")
    attributes = {
        str(name): str(default if default is not None else "unknown")
        for name, default in (raw.get("属性字段") or {}).items()
    }
    display_raw = raw.get("显示字段")
    display_fields = tuple(str(item) for item in (display_raw if display_raw else attributes.keys()))
    objects_key = str(raw.get("输出字段", "objects")).strip() or "objects"
    return TaskConfig(
        name=str(raw.get("名称", path.stem)),
        object_noun=str(raw.get("对象名称", "目标")),
        objects_key=objects_key,
        default_label=str(raw.get("默认标签", "object")),
        category_name=str(raw.get("类别名称", objects_key)),
        category_supercategory=str(raw.get("类别父类", "object")),
        display_fields=display_fields,
        attributes=attributes,
        system_prompt=system_prompt,
        request_prompt=str(raw.get("请求提示", "")).strip(),
    )


def load_config(path: Path, task_override: Optional[str] = None) -> Config:
    if not path.exists():
        raise FileNotFoundError(f"找不到配置文件：{path}。请确认 配置.yaml 与 标注引擎.py 在同一个文件夹。")
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"配置文件必须是 YAML 映射格式：{path}")

    base = path.resolve().parent
    task_value = task_override or raw.get("任务文件") or "任务/黑白mask.yaml"
    task_path = Path(task_value)
    if not task_path.is_absolute():
        task_path = base / task_path
    task = load_task(task_path)

    config = Config.from_dict(raw, base, task)

    if not config.input_dir.exists():
        config.input_dir.mkdir(parents=True, exist_ok=True)
        raise FileNotFoundError(
            f"输入图片目录不存在，已自动创建：{config.input_dir}\n"
            "请把待标注的图片放进去，然后重新运行。"
        )
    return config


def list_images(config: Config) -> List[Path]:
    return sorted(
        [
            path
            for path in config.input_dir.iterdir()
            if path.is_file() and path.suffix.lower() in config.supported_extensions
        ],
        key=lambda item: item.name.lower(),
    )


def sanitize_image(source: Path, destination: Path, max_side: int, jpeg_quality: int) -> Tuple[int, int]:
    """解码后重新保存，顺带去掉 EXIF / IPTC / XMP 等元数据。"""
    with Image.open(source) as opened:
        opened.load()
        orientation = opened.getexif().get(274, 1)
        image = opened

        transforms = {
            2: lambda item: item.transpose(Image.Transpose.FLIP_LEFT_RIGHT),
            3: lambda item: item.transpose(Image.Transpose.ROTATE_180),
            4: lambda item: item.transpose(Image.Transpose.FLIP_TOP_BOTTOM),
            5: lambda item: item.transpose(Image.Transpose.TRANSPOSE).transpose(
                Image.Transpose.FLIP_LEFT_RIGHT
            ),
            6: lambda item: item.transpose(Image.Transpose.ROTATE_270),
            7: lambda item: item.transpose(Image.Transpose.TRANSVERSE).transpose(
                Image.Transpose.FLIP_LEFT_RIGHT
            ),
            8: lambda item: item.transpose(Image.Transpose.ROTATE_90),
        }
        transform = transforms.get(orientation)
        if transform:
            image = transform(image)

        if max_side > 0 and max(image.size) > max_side:
            image = image.copy()
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.suffix.lower() in {".jpg", ".jpeg"}:
            if image.mode != "RGB":
                background = Image.new("RGB", image.size, (255, 255, 255))
                if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                    background.paste(image, mask=image.convert("RGBA").getchannel("A"))
                else:
                    background.paste(image.convert("RGB"))
                image = background
            image.save(destination, format="JPEG", quality=jpeg_quality, optimize=True)
        else:
            if image.mode not in {"RGB", "RGBA", "L"}:
                image = image.convert("RGBA")
            image.save(destination, format="PNG", optimize=True)

        return image.size


def oriented_image_size(source: Path) -> Tuple[int, int]:
    """返回按 EXIF 方向校正后的显示宽高。"""
    with Image.open(source) as opened:
        width, height = opened.size
        if opened.getexif().get(274, 1) in {5, 6, 7, 8}:
            width, height = height, width
        return width, height


def normalized_objects(result: Dict[str, Any], task: TaskConfig) -> List[Dict[str, Any]]:
    objects: List[Dict[str, Any]] = []
    for raw_object in result.get(task.objects_key, []):
        item = dict(raw_object)
        source = item.get("segmentation_normalized") or item["segmentation"]
        item["segmentation_normalized"] = [[float(x), float(y)] for x, y in source]
        objects.append(item)
    return objects


def mime_type_for(path: Path) -> str:
    guessed = mimetypes.guess_type(path.name)[0]
    if guessed:
        return guessed
    return "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"


def image_to_data_uri(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type_for(path)};base64,{encoded}"


def extract_json(text: str) -> Dict[str, Any]:
    trimmed = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", trimmed, flags=re.DOTALL)
    if fence:
        trimmed = fence.group(1).strip()
    try:
        parsed = json.loads(trimmed)
    except json.JSONDecodeError:
        start = trimmed.find("{")
        end = trimmed.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("模型返回内容中没有 JSON 对象")
        try:
            parsed = json.loads(trimmed[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"模型返回的 JSON 无法解析：{exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("模型返回的 JSON 必须是对象")
    return parsed


def validate_point(point: Any) -> bool:
    return (
        isinstance(point, (list, tuple))
        and len(point) == 2
        and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in point)
        and all(0.0 <= float(value) <= 1.0 for value in point)
    )


def normalize_segmentation(
    segmentation: Sequence[Sequence[float]],
    image_size: Tuple[int, int],
) -> Tuple[List[List[float]], int]:
    """兼容部分模型混用 0-1 归一化坐标和像素坐标的情况。"""
    width, height = image_size
    repaired: List[List[float]] = []
    pixel_count = 0
    for point in segmentation:
        x, y = float(point[0]), float(point[1])
        if x > 1.0:
            x /= width
            pixel_count += 1
        if y > 1.0:
            y /= height
            pixel_count += 1
        repaired.append([max(0.0, min(1.0, x)), max(0.0, min(1.0, y))])
    return repaired, pixel_count


def clean_objects(
    raw: Dict[str, Any],
    task: TaskConfig,
    image_size: Optional[Tuple[int, int]] = None,
) -> Dict[str, Any]:
    objects_raw = raw.get(task.objects_key, [])
    if not isinstance(objects_raw, list):
        raise ValueError(f"字段 '{task.objects_key}' 必须是数组")

    objects: List[Dict[str, Any]] = []
    for index, item in enumerate(objects_raw, start=1):
        if not isinstance(item, dict):
            LOGGER.warning("忽略第 %d 个%s：不是对象", index, task.object_noun)
            continue

        segmentation = item.get("segmentation")
        if not isinstance(segmentation, list) or len(segmentation) < 3:
            LOGGER.warning("忽略第 %d 个%s：多边形少于 3 个点", index, task.object_noun)
            continue
        if image_size:
            segmentation, pixel_count = normalize_segmentation(segmentation, image_size)
            if pixel_count:
                LOGGER.info(
                    "第 %d 个%s：%d 个坐标是像素值，已自动转换为归一化坐标",
                    index,
                    task.object_noun,
                    pixel_count,
                )
        if not all(validate_point(point) for point in segmentation):
            LOGGER.warning("忽略第 %d 个%s：坐标无效或超出 0-1 范围", index, task.object_noun)
            continue

        try:
            object_id = int(item.get("id", index))
        except (TypeError, ValueError):
            object_id = index

        confidence = item.get("confidence", 0.0)
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 0.0

        cleaned: Dict[str, Any] = {
            "id": object_id,
            "label": str(item.get("label", task.default_label)),
        }
        for field_name, default_value in task.attributes.items():
            cleaned[field_name] = str(item.get(field_name, default_value))
        cleaned.update(
            {
                "confidence": confidence,
                "occluded": bool(item.get("occluded", False)),
                "truncated": bool(item.get("truncated", False)),
                "segmentation": [[float(x), float(y)] for x, y in segmentation],
                "notes": str(item.get("notes", "")),
            }
        )
        objects.append(cleaned)

    quality = raw.get("image_quality", {})
    if not isinstance(quality, dict):
        quality = {}
    return {task.objects_key: objects, "image_quality": quality}


class ApiError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def call_model(
    config: Config,
    image_uri: str,
    image_name: str,
    image_size: Optional[Tuple[int, int]] = None,
) -> Dict[str, Any]:
    endpoint = f"{config.base_url}/chat/completions"
    task = config.task
    session = requests.Session()
    session.trust_env = config.use_system_proxy
    if config.proxy:
        session.proxies.update({"http": config.proxy, "https": config.proxy})

    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    content: List[Dict[str, Any]] = [
        {"type": "text", "text": task.request_prompt},
        {"type": "image_url", "image_url": {"url": image_uri}},
    ]
    use_json_mode = config.use_json_mode
    thinking = config.thinking
    last_error: Optional[str] = None
    attempt = 0

    while attempt < config.max_retries:
        attempt += 1
        payload: Dict[str, Any] = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": task.system_prompt},
                {"role": "user", "content": content},
            ],
            "temperature": 0.0,
        }
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}
        if config.max_tokens:
            payload["max_tokens"] = config.max_tokens
        if thinking:
            payload["thinking"] = {"type": thinking}

        try:
            response = session.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=config.timeout,
                verify=config.verify_ssl,
            )
            if response.status_code >= 400:
                raise ApiError(f"HTTP {response.status_code}: {response.text[:1000]}", response.status_code)
            body = response.json()
            text = body["choices"][0]["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("模型没有返回文本内容")
            return clean_objects(extract_json(text), task, image_size)
        except ApiError as exc:
            last_error = str(exc)
            if exc.status == 400 and (use_json_mode or thinking):
                LOGGER.warning("接口拒绝了 JSON 模式 / 思考模式（HTTP 400），自动关闭后重试：%s", exc)
                use_json_mode = False
                thinking = None
                continue
            LOGGER.warning("第 %d/%d 次请求失败（%s）：%s", attempt, config.max_retries, image_name, exc)
            if attempt < config.max_retries:
                time.sleep(min(2 ** attempt, 15))
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            last_error = str(exc)
            LOGGER.warning("第 %d/%d 次请求失败（%s）：%s", attempt, config.max_retries, image_name, exc)
            if attempt < config.max_retries:
                time.sleep(min(2 ** attempt, 15))

    raise RuntimeError(last_error or "未知接口错误")


def polygon_to_coco(segmentation: Iterable[Iterable[float]]) -> Tuple[List[float], List[float], float]:
    points = [(float(x), float(y)) for x, y in segmentation]
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    bbox = [
        round(min(xs), 2),
        round(min(ys), 2),
        round(max(xs) - min(xs), 2),
        round(max(ys) - min(ys), 2),
    ]
    area = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        area += x1 * y2 - x2 * y1
    return [round(value, 2) for point in points for value in point], bbox, round(abs(area) / 2.0, 2)


def write_coco(config: Config, records: List[Dict[str, Any]]) -> Path:
    task = config.task
    images: List[Dict[str, Any]] = []
    annotations: List[Dict[str, Any]] = []
    image_id = 1
    annotation_id = 1

    for record in records:
        images.append(
            {
                "id": image_id,
                "file_name": record["original_file"],
                "width": int(record["original_width"]),
                "height": int(record["original_height"]),
            }
        )
        for raw_object in record["result"].get(task.objects_key, []):
            segmentation, bbox, area = polygon_to_coco(raw_object["segmentation"])
            attributes = {
                field_name: raw_object.get(field_name, task.attributes[field_name])
                for field_name in task.attributes
            }
            attributes.update(
                {
                    "occluded": raw_object["occluded"],
                    "truncated": raw_object["truncated"],
                    "notes": raw_object["notes"],
                }
            )
            annotations.append(
                {
                    "id": annotation_id,
                    "image_id": image_id,
                    "category_id": 1,
                    "segmentation": [segmentation],
                    "bbox": bbox,
                    "area": area,
                    "iscrowd": 0,
                    "score": raw_object["confidence"],
                    "attributes": attributes,
                }
            )
            annotation_id += 1
        image_id += 1

    coco = {
        "info": {"description": f"AI-assisted {task.name} annotations"},
        "licenses": [],
        "images": images,
        "annotations": annotations,
        "categories": [
            {"id": 1, "name": task.category_name, "supercategory": task.category_supercategory}
        ],
    }
    output_path = config.result_dir / config.coco_filename
    output_path.write_text(json.dumps(coco, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def raw_result_path(config: Config, source: Path) -> Path:
    return config.json_dir / f"{source.name}.json"


def mask_path(config: Config, source: Path) -> Path:
    return config.mask_dir / f"{source.stem}_mask.png"


def original_payload(
    source: Path,
    cleaned_width: int,
    cleaned_height: int,
    result: Dict[str, Any],
    cleaned_name: Optional[str],
    model: str,
    task: TaskConfig,
) -> Dict[str, Any]:
    original_width, original_height = oriented_image_size(source)
    objects: List[Dict[str, Any]] = []
    for raw_object in normalized_objects(result, task):
        item = dict(raw_object)
        normalized = item.pop("segmentation_normalized")
        item["segmentation"] = [
            [round(float(x) * original_width, 2), round(float(y) * original_height, 2)]
            for x, y in normalized
        ]
        item["segmentation_normalized"] = normalized
        objects.append(item)

    return {
        "original_file": source.name,
        "cleaned_file": cleaned_name,
        "width": original_width,
        "height": original_height,
        "original_width": original_width,
        "original_height": original_height,
        "cleaned_width": cleaned_width,
        "cleaned_height": cleaned_height,
        "coordinate_system": "original_image_pixels",
        "model": model,
        "task": task.name,
        "result": {task.objects_key: objects, "image_quality": result.get("image_quality", {})},
    }


def save_mask(config: Config, source: Path, record: Dict[str, Any]) -> Path:
    width = int(record["original_width"])
    height = int(record["original_height"])
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)

    for raw_object in record["result"].get(config.task.objects_key, []):
        points = [(float(x), float(y)) for x, y in raw_object["segmentation"]]
        if len(points) >= 3:
            draw.polygon(points, fill=config.mask_foreground)

    output_path = mask_path(config, source)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mask.save(output_path, format="PNG", optimize=True)
    return output_path


def process_one(config: Config, source: Path) -> Dict[str, Any]:
    cleaned_path = config.cleaned_dir / source.name
    LOGGER.info("清洗图片：%s", source.name)
    width, height = sanitize_image(source, cleaned_path, config.max_side, config.jpeg_quality)
    LOGGER.info("调用模型：%s（%dx%d）", source.name, width, height)
    result = call_model(config, image_to_data_uri(cleaned_path), source.name, (width, height))
    payload = original_payload(
        source,
        width,
        height,
        result,
        cleaned_path.name if config.save_cleaned_image else None,
        config.model,
        config.task,
    )
    if config.save_mask:
        mask_output = save_mask(config, source, payload)
        LOGGER.info("已保存原图尺寸掩码：%s", mask_output)
    if config.save_raw_json:
        output_path = raw_result_path(config, source)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def load_existing_records(config: Config, images: List[Path]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for source in images:
        path = raw_result_path(config, source)
        if not config.skip_processed or not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("original_file") != source.name or "result" not in payload:
                continue
            payload_task = payload.get("task")
            if payload_task and payload_task != config.task.name:
                LOGGER.warning(
                    "已有结果属于任务“%s”，与当前任务“%s”不一致，将重新处理：%s",
                    payload_task,
                    config.task.name,
                    source.name,
                )
                continue
            if "original_width" not in payload:
                payload = original_payload(
                    source,
                    int(payload["width"]),
                    int(payload["height"]),
                    payload["result"],
                    payload.get("cleaned_file"),
                    payload.get("model", config.model),
                    config.task,
                )
                if config.save_raw_json:
                    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                if config.save_mask:
                    mask_output = save_mask(config, source, payload)
                    LOGGER.info("已把旧结果迁移为原图尺寸掩码：%s", mask_output)
            records.append(payload)
            LOGGER.info("跳过已处理图片：%s", source.name)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            LOGGER.warning("无法复用已有结果 %s：%s", source.name, exc)
    return records


def instance_color(instance_id: int) -> Tuple[int, int, int]:
    hue = ((instance_id - 1) * 137.508 / 360.0) % 1.0
    rgb = colorsys.hsv_to_rgb(hue, 0.78, 0.96)
    return tuple(int(round(value * 255)) for value in rgb)


def load_font(config: Config, size: Optional[int] = None) -> ImageFont.FreeTypeFont:
    font_size = size or config.visualization.font_size
    candidates: List[Path] = []
    if config.visualization.font_path:
        candidates.append(config.visualization.font_path)
    candidates.extend(Path(candidate) for candidate in FONT_CANDIDATES)
    for path in candidates:
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), font_size)
        except OSError as exc:
            LOGGER.warning("字体加载失败 %s：%s", path, exc)
    return ImageFont.load_default(size=font_size)


def scaled_size(width: int, height: int, max_side: int) -> Tuple[int, int]:
    if max_side <= 0 or max(width, height) <= max_side:
        return width, height
    scale = max_side / float(max(width, height))
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def draw_annotations(
    image: Image.Image,
    annotations: Sequence[Dict[str, Any]],
    config: Config,
    coordinate_width: Optional[int] = None,
    coordinate_height: Optional[int] = None,
) -> Image.Image:
    image = image.convert("RGB")
    source_width = int(coordinate_width or image.width)
    source_height = int(coordinate_height or image.height)
    target_size = scaled_size(image.width, image.height, config.visualization.max_side)
    if target_size != image.size:
        image = image.resize(target_size, Image.Resampling.LANCZOS)
    scale_x = image.width / float(source_width)
    scale_y = image.height / float(source_height)

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = load_font(config)
    opacity = max(0, min(255, config.visualization.polygon_opacity))

    for annotation in annotations:
        groups = annotation.get("segmentation", [])
        if not isinstance(groups, list):
            continue
        color = instance_color(int(annotation.get("id", 1)))
        attributes = annotation.get("attributes", {})
        if not isinstance(attributes, dict):
            attributes = {}
        score = float(annotation.get("score", 0.0))
        label = f"#{int(annotation.get('id', 0))} {score:.2f}"
        details = [
            str(attributes.get(field_name, "")).strip()
            for field_name in config.task.display_fields
        ]
        details = [value for value in details if value and value.lower() != "unknown"]
        if details:
            label += " " + " / ".join(details)

        min_x = image.width
        min_y = image.height
        for group in groups:
            if not isinstance(group, list) or len(group) < 6:
                continue
            points: List[Tuple[float, float]] = []
            for index in range(0, len(group), 2):
                x = float(group[index]) * scale_x
                y = float(group[index + 1]) * scale_y
                points.append((x, y))
                min_x = min(min_x, x)
                min_y = min(min_y, y)
            draw.polygon(
                points,
                fill=color + (opacity,),
                outline=color + (255,),
                width=config.visualization.polygon_line_width,
            )

        text_bbox = draw.textbbox((0, 0), label, font=font)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]
        padding = max(4, config.visualization.polygon_line_width)
        x = min(max(0, min_x), image.width - text_width - padding * 2)
        y = min_y - text_height - padding * 2
        if y < 0:
            y = min(max(0, min_y), image.height - text_height - padding * 2)
        rectangle = (x, y, x + text_width + padding * 2, y + text_height + padding * 2)
        draw.rectangle(rectangle, fill=(12, 14, 18, 205))
        draw.text((x + padding, y + padding), label, font=font, fill=(255, 255, 255, 255))

    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


def group_annotations_by_image(
    images: Sequence[Dict[str, Any]],
    annotations: Sequence[Dict[str, Any]],
) -> Dict[int, List[Dict[str, Any]]]:
    grouped: Dict[int, List[Dict[str, Any]]] = {int(image["id"]): [] for image in images}
    for annotation in annotations:
        grouped.setdefault(int(annotation["image_id"]), []).append(annotation)
    for image_id in grouped:
        grouped[image_id].sort(key=lambda item: int(item.get("id", 0)))
    return grouped


def make_contact_sheet(
    rendered: Sequence[Tuple[str, Image.Image, int]],
    config: Config,
    output_path: Path,
) -> None:
    if not rendered:
        return

    columns = max(1, config.visualization.contact_sheet_columns)
    padding = config.visualization.padding
    label_height = config.visualization.label_height
    tiles: List[Tuple[Image.Image, str, int]] = []
    tile_width = 0
    tile_height = 0

    for file_name, image, count in rendered:
        target = scaled_size(image.width, image.height, config.visualization.tile_max_side)
        tile = image.resize(target, Image.Resampling.LANCZOS)
        tiles.append((tile, file_name, count))
        tile_width = max(tile_width, tile.width)
        tile_height = max(tile_height, tile.height)

    rows = math.ceil(len(tiles) / columns)
    sheet_width = columns * tile_width + (columns + 1) * padding
    sheet_height = rows * (tile_height + label_height) + (rows + 1) * padding
    sheet = Image.new("RGB", (sheet_width, sheet_height), config.visualization.background)
    draw = ImageDraw.Draw(sheet)
    font = load_font(config, max(18, min(32, label_height - 14)))

    for index, (tile, file_name, count) in enumerate(tiles):
        column = index % columns
        row = index // columns
        x = padding + column * (tile_width + padding)
        y = padding + row * (tile_height + label_height + padding)
        sheet.paste(tile, (x + (tile_width - tile.width) // 2, y))
        draw.text(
            (x + 4, y + tile_height + 12),
            f"{file_name}  |  {config.task.object_noun}: {count}",
            font=font,
            fill=(232, 236, 241),
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, format="JPEG", quality=92, optimize=True)


def visualize_results(config: Config) -> None:
    annotation_path = config.result_dir / config.coco_filename
    if not annotation_path.exists():
        LOGGER.warning("跳过可视化：找不到标注文件 %s", annotation_path)
        return

    with annotation_path.open("r", encoding="utf-8") as handle:
        coco = json.load(handle)
    images = coco.get("images", [])
    annotations = coco.get("annotations", [])
    if not images:
        LOGGER.warning("跳过可视化：标注文件里没有图片")
        return

    grouped = group_annotations_by_image(images, annotations)
    config.visualization_dir.mkdir(parents=True, exist_ok=True)
    rendered: List[Tuple[str, Image.Image, int]] = []

    for image_info in images:
        file_name = str(image_info["file_name"])
        candidates = [config.input_dir / file_name, config.cleaned_dir / file_name]
        source = next((path for path in candidates if path.exists()), None)
        if source is None:
            LOGGER.warning("找不到可视化原图：%s", file_name)
            continue

        with Image.open(source) as opened:
            opened.load()
            image_annotations = grouped.get(int(image_info["id"]), [])
            rendered_image = draw_annotations(
                opened,
                image_annotations,
                config,
                int(image_info["width"]),
                int(image_info["height"]),
            )

        output_path = config.visualization_dir / f"{source.name}_overlay.jpg"
        rendered_image.save(output_path, format="JPEG", quality=92, optimize=True)
        rendered.append((source.name, rendered_image, len(image_annotations)))
        LOGGER.info("已保存可视化：%s", output_path)

    if config.visualization.contact_sheet_enabled:
        contact_path = config.visualization_dir / "总览图.jpg"
        make_contact_sheet(rendered, config, contact_path)
        LOGGER.info("已保存总览图：%s", contact_path)


def run_segmentation(config: Config) -> List[str]:
    config.cleaned_dir.mkdir(parents=True, exist_ok=True)
    config.result_dir.mkdir(parents=True, exist_ok=True)
    images = list_images(config)
    if not images:
        LOGGER.warning("输入图片目录里没有支持的图片：%s", config.input_dir)
        return []

    LOGGER.info("任务：%s；在 %s 找到 %d 张图片", config.task.name, config.input_dir, len(images))
    records = load_existing_records(config, images)
    processed_names = {record["original_file"] for record in records}
    pending = [image for image in images if image.name not in processed_names]
    LOGGER.info("待处理 %d 张，已处理 %d 张", len(pending), len(records))

    failures: List[str] = []
    with ThreadPoolExecutor(max_workers=max(1, config.workers)) as executor:
        futures = {executor.submit(process_one, config, image): image for image in pending}
        for future in as_completed(futures):
            image = futures[future]
            try:
                records.append(future.result())
            except Exception as exc:
                failures.append(image.name)
                LOGGER.error("处理失败 %s：%s", image.name, exc)

    if config.save_coco:
        coco_path = write_coco(config, records)
        LOGGER.info("已保存 COCO 标注：%s", coco_path)

    status = {
        "task": config.task.name,
        "total_found": len(images),
        "succeeded": len(records),
        "failed": len(failures),
        "failed_files": failures,
    }
    status_path = config.result_dir / "处理状态.json"
    status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info("标注结束：成功 %d 张，失败 %d 张", len(records), len(failures))
    return failures


def run_check(config: Config) -> int:
    LOGGER.info("接口自检：%s（模型 %s）", config.base_url, config.model)
    image = Image.new("RGB", (256, 256), (245, 245, 245))
    draw = ImageDraw.Draw(image)
    draw.rectangle((64, 64, 192, 192), fill=(200, 200, 200))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    image_uri = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

    started = time.time()
    result = call_model(config, image_uri, "接口自检", (256, 256))
    elapsed = time.time() - started
    count = len(result.get(config.task.objects_key, []))
    LOGGER.info("接口可用，耗时 %.1f 秒，返回 JSON 中 %s 数量：%d", elapsed, config.task.object_noun, count)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="通用 AI 图像标注：读取 配置.yaml 和 任务/*.yaml，批量标注图片。"
    )
    parser.add_argument(
        "--配置",
        dest="config",
        type=Path,
        default=Path(__file__).with_name("配置.yaml"),
        help="配置文件路径，默认使用脚本旁的 配置.yaml",
    )
    parser.add_argument(
        "--任务",
        dest="task",
        help="临时覆盖任务文件，例如 任务/检测框.yaml",
    )
    parser.add_argument(
        "--检查",
        dest="check",
        action="store_true",
        help="只验证接口地址、密钥和模型是否可用，不处理图片",
    )
    parser.add_argument("--详细日志", dest="verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format=LOG_FORMAT)
    try:
        config = load_config(args.config, args.task)
        if args.check:
            return run_check(config)
        failures = run_segmentation(config)
        if config.visualization.enabled:
            visualize_results(config)
        else:
            LOGGER.info("配置中已关闭可视化")
        return 0 if not failures else 1
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.error("运行失败：%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
