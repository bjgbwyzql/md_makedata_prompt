#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
TIF 卫星影像自动配准脚本（以参考影像为基准）

用法：
    - PyCharm / 任意 Python：直接运行本脚本。当前解释器缺少 GDAL / numpy / scipy / PyYAML 时，
      会自动查找本机 QGIS 并切换到它的 Python 重新运行；也可以用环境变量
      TIF_REGISTER_PYTHON 指定任意已装好 GDAL 的 python.exe。
    - 命令行（QGIS 自带 Python）：
      "C:\\Program Files\\QGIS 3.x\\apps\\Python312\\python.exe" tif_register.py -c config.yaml
    - 也可用命令行临时覆盖路径：
      tif_register.py -c config.yaml -r 参考.tif -t 待配准.tif -o 结果.tif --epsg 4527

流程：
    1. 在有效区内分块测量两期影像的局部位移（梯度影像互相关 + 亚像素）
    2. 剔除误匹配控制点，构建平滑位移场
    3. 把待配准影像重采样到参考影像格网（双线性 + NoData 归一化卷积）
    4. 可选：赋 EPSG 坐标系、建金字塔、精度检查、生成红绿叠加对比图

运行时按 STATUS_INTERVAL 指定的秒数轮询输出（任务完成时额外输出一次）：
    [时间] 控制点测量 / 重采样 / 金字塔 进度、速度、预计剩余时间；
    配准前像素差（控制点统计，含像素和米）；
    每个检查点的配准前/配准后偏差与 NCC；
    配准后残余像素差汇总（平均/最大，含像素和米）。

参考影像（配置中的第一张）就是基准：输出格网、范围、分辨率都与它一致。
两幅影像的范围/尺寸/分辨率/坐标系可以不同，脚本会先把待配准影像重采样到参考格网
（临时 VRT，自动删除），再做局部非刚性配准。

路径：支持绝对路径和相对路径；相对路径按配置里的“路径基准”解析
（默认配置文件所在目录，也可设为脚本目录或当前工作目录）。
"""

import os
import re
import subprocess
import sys
import threading
from pathlib import Path


def _ensure_qgis_dll_env() -> None:
    """把 QGIS 的 bin 目录加入 DLL 搜索路径（QGIS Python 和多进程子进程都需要）。"""
    try:
        base = Path(sys.prefix).resolve()
    except Exception:
        return
    for b in (base, base.parent, base.parent.parent):
        for d in (b / "bin", b / "apps" / "gdal" / "bin", b / "apps" / "proj" / "bin"):
            if not d.exists():
                continue
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(str(d))
                except OSError:
                    pass


def _ensure_proj_env() -> None:
    """QGIS 的 Python 默认找不到 proj.db，这里自动补上 PROJ_DATA。"""
    if os.environ.get("PROJ_DATA") and Path(os.environ["PROJ_DATA"]).exists():
        return
    try:
        base = Path(sys.prefix).resolve()
    except Exception:
        return
    for b in (base, base.parent, base.parent.parent):
        for cand in (b / "share" / "proj",
                     b / "apps" / "proj" / "share" / "proj",
                     b / "Library" / "share" / "proj"):
            if (cand / "proj.db").exists():
                os.environ["PROJ_DATA"] = str(cand)
                os.environ.setdefault("PROJ_LIB", str(cand))
                return


def _ready() -> bool:
    """当前解释器是否具备运行本脚本的依赖（Pillow 只用于对比图，不在此检查）。"""
    try:
        import numpy  # noqa: F401
        import scipy  # noqa: F401
        import yaml  # noqa: F401
        import osgeo.gdal  # noqa: F401
        return True
    except Exception:
        return False


def _registry_roots():
    """从 Windows 注册表找 QGIS / OSGeo4W 安装目录。"""
    if os.name != "nt":
        return []
    try:
        import winreg
    except Exception:
        return []
    roots = []
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\QGIS\QGIS3"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\QGIS\QGIS3"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\QGIS\QGIS3"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\OSGeo4W"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\OSGeo4W"),
    ]
    for hive, sub in keys:
        try:
            with winreg.OpenKey(hive, sub) as k:
                n = winreg.QueryInfoKey(k)[1]
                for i in range(n):
                    try:
                        _, value, _ = winreg.EnumValue(k, i)
                    except OSError:
                        continue
                    if isinstance(value, str) and Path(value).is_dir():
                        roots.append(Path(value))
        except OSError:
            continue
    return roots


def _python_ready(py: str) -> bool:
    """验证候选 Python 能否导入 GDAL/numpy/scipy/PyYAML（自动补 QGIS 的 DLL/PROJ 路径）。"""
    p = Path(py)
    dll_dirs = []
    for b in (p.parent, p.parent.parent, p.parent.parent.parent):
        for d in (b / "bin", b / "apps" / "gdal" / "bin", b / "apps" / "proj" / "bin"):
            if d.is_dir():
                dll_dirs.append(str(d))
    proj = None
    for b in (p.parent.parent.parent, p.parent.parent, p.parent):
        cand = b / "share" / "proj"
        if (cand / "proj.db").exists():
            proj = str(cand)
            break
    code = (
        "import os,sys\n"
        "for d in sys.argv[1:]:\n"
        "    if os.path.isdir(d) and hasattr(os,'add_dll_directory'):\n"
        "        os.add_dll_directory(d)\n"
        "import osgeo.gdal, numpy, scipy, yaml\n"
    )
    env = os.environ.copy()
    if proj:
        env["PROJ_DATA"] = proj
        env["PROJ_LIB"] = proj
    if dll_dirs:
        env["PATH"] = os.pathsep.join(dll_dirs + [env.get("PATH", "")])
    try:
        r = subprocess.run([py, "-c", code] + dll_dirs, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def _find_qgis_python():
    env_py = os.environ.get("TIF_REGISTER_PYTHON")
    if env_py and Path(env_py).is_file():
        return env_py

    roots = []
    if os.environ.get("QGIS_PREFIX_PATH"):
        roots.append(Path(os.environ["QGIS_PREFIX_PATH"]))
    roots += _registry_roots()
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        v = os.environ.get(var)
        if v:
            roots.append(Path(v))
            if var == "LOCALAPPDATA":
                roots.append(Path(v) / "Programs")
    for drive in ("C:", "D:", "E:", "F:"):
        p = Path(drive + os.sep)
        if p.exists():
            roots.append(p)
            for sub in ("Program Files", "Program Files (x86)"):
                q = p / sub
                if q.exists():
                    roots.append(q)

    patterns = [
        "QGIS*/apps/Python*/python.exe",
        "QGIS*/bin/python*.exe",
        "OSGeo4W*/apps/Python*/python.exe",
        "OSGeo4W*/bin/python*.exe",
        "apps/Python*/python.exe",
        "OSGeo4W/apps/Python*/python.exe",
    ]
    if os.name != "nt":
        patterns = ["QGIS*/bin/python3*", "apps/Python*/bin/python3*"]

    candidates = []
    seen_roots = set()
    for root in roots:
        try:
            rs = str(root.resolve()).lower()
        except Exception:
            rs = str(root).lower()
        if rs in seen_roots:
            continue
        seen_roots.add(rs)
        for pat in patterns:
            try:
                candidates += sorted(root.glob(pat), reverse=True)
            except Exception:
                pass

    if os.name != "nt":
        for p in ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/opt/local/bin"):
            d = Path(p)
            if d.is_dir():
                candidates += sorted(d.glob("python3*"), reverse=True)
        apps_dir = Path("/Applications")
        if apps_dir.exists():
            for app in apps_dir.glob("QGIS*.app"):
                candidates += sorted(app.glob("Contents/MacOS/bin/python3*"), reverse=True)

    seen = set()
    for p in candidates:
        try:
            rp = str(p.resolve()).lower()
        except Exception:
            rp = str(p).lower()
        if rp in seen or not p.is_file():
            continue
        seen.add(rp)
        if _python_ready(str(p)):
            return str(p)
    return None


def _bootstrap() -> None:
    _ensure_qgis_dll_env()
    _ensure_proj_env()
    if _ready():
        return
    if os.environ.get("TIF_REGISTER_BOOTSTRAPPED") == "1":
        sys.stderr.write(
            "错误：已自动切换解释器，但仍缺少 GDAL/numpy/scipy/PyYAML。\n"
            "请确认 QGIS 安装完整，或用环境变量 TIF_REGISTER_PYTHON 指定可用的 python.exe。\n")
        sys.exit(1)
    qpy = _find_qgis_python()
    if not qpy:
        sys.stderr.write(
            "错误：当前 Python 缺少 GDAL 或必要依赖（numpy / scipy / PyYAML），\n"
            "并且没有在本机找到可用的 QGIS Python。\n\n"
            "解决办法（任选其一）：\n"
            "  1) 安装 QGIS（自带全部依赖），然后重新运行本脚本；\n"
            "  2) 在 PyCharm 里把解释器设为 QGIS 的 python.exe，例如：\n"
            "     C:\\Program Files\\QGIS 3.x\\apps\\Python312\\python.exe\n"
            "  3) 设置环境变量 TIF_REGISTER_PYTHON 指向任意已装好 GDAL 的 python.exe；\n"
            "  4) 用 conda 环境：conda install -c conda-forge gdal numpy scipy pyyaml\n\n"
            "Pillow 只用于生成对比图，不影响配准本身。\n")
        sys.exit(1)
    env = os.environ.copy()
    env["TIF_REGISTER_BOOTSTRAPPED"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    script = str(Path(__file__).resolve())
    print(f"当前 Python 缺少依赖，自动切换到：{qpy}", flush=True)
    rc = subprocess.call([qpy, script] + sys.argv[1:], env=env)
    sys.exit(rc)


_LOCAL_LIBS = Path(__file__).resolve().with_name("pylibs")
if _LOCAL_LIBS.is_dir():
    # 可选的本地依赖（OpenCV，或便携版 GDAL/numpy 等），没有也能正常运行
    sys.path.insert(0, str(_LOCAL_LIBS))

_bootstrap()

import argparse
import math
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Tuple

import numpy as np
from scipy import ndimage
from scipy.signal import fftconvolve
from scipy.spatial import cKDTree
# PyCharm 若使用普通 Python 解释器，会误报 yaml/osgeo 未安装；
# 脚本运行时会自动切换到 QGIS Python，因此这里忽略这类静态检查提示。
# noinspection PyUnresolvedReferences
import yaml

# noinspection PyUnresolvedReferences,PyPep8,PyPep8E402
from osgeo import gdal, gdal_array, osr  # noqa: E402

gdal.UseExceptions()



# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------

class ConfigError(Exception):
    pass


@dataclass
class MeasureCfg:
    scale: int = 2
    tile: int = 2000
    max_shift: float = 40.0
    min_valid: float = 0.995
    min_ncc_gain: float = 0.03
    min_snr: float = 4.0
    outlier_radius: float = 2500.0
    outlier_thr: float = 2.5
    workers: int = 0


@dataclass
class FieldCfg:
    node_step: int = 1500
    radius: float = 9000.0
    weight_sigma: float = 3000.0
    smooth_sigma: float = 0.6
    val_ratio: float = 0.2


@dataclass
class WarpCfg:
    method: str = "双线性"
    block: int = 2048
    nodata: Optional[float] = 255.0
    accel: str = "auto"
    compress: str = "NONE"


@dataclass
class PyramidCfg:
    enabled: bool = True
    levels: List[int] = field(default_factory=lambda: [2, 4, 8, 16, 32, 64, 128, 256])
    method: str = "average"


@dataclass
class CheckCfg:
    enabled: bool = True
    count: int = 6
    size: int = 1200


@dataclass
class Config:
    ref_path: str
    tgt_path: str
    out_path: str
    overwrite: bool = False
    epsg: Optional[int] = None
    fix_ref_crs: bool = False
    measure: MeasureCfg = field(default_factory=MeasureCfg)
    field_cfg: FieldCfg = field(default_factory=FieldCfg)
    warp: WarpCfg = field(default_factory=WarpCfg)
    pyramid: PyramidCfg = field(default_factory=PyramidCfg)
    check: CheckCfg = field(default_factory=CheckCfg)
    write_csv: bool = True
    base_dir: str = ""  # 相对路径的解析基准目录（由“路径基准”计算）


def _sec(d: dict, key: str) -> dict:
    v = d.get(key, {})
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise ConfigError(f"配置节“{key}”必须是键值结构")
    return v


def _num(d: dict, key: str, default, cast=float, allow_none=False):
    v = d.get(key, default)
    if v is None:
        if allow_none:
            return None
        raise ConfigError(f"配置项“{key}”不能为空")
    try:
        return cast(v)
    except Exception:
        raise ConfigError(f"配置项“{key}”需要数字，当前为 {v!r}")


def _boolean(d: dict, key: str, default: bool) -> bool:
    v = d.get(key, default)
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "是", "开", "on")
    return bool(v)


def _text(d: dict, key: str, default: str = "") -> str:
    v = d.get(key, default)
    return "" if v is None else str(v).strip()


def _expand_path(p: str) -> str:
    """展开 ~ 和环境变量（Windows 的 %VAR% 与 $VAR 都支持）。"""
    return os.path.expandvars(os.path.expanduser(str(p).strip()))


def _looks_absolute(p: str) -> bool:
    p = p.strip()
    if not p:
        return False
    if os.path.isabs(p):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", p):          # D:\... 或 D:/...
        return True
    if p.startswith("\\\\") or p.startswith("//"):  # UNC 共享路径
        return True
    if p.startswith("/"):                        # 以根开头的路径
        return True
    return False


def _resolve_path(p: str, base: Path) -> str:
    """绝对路径原样返回；相对路径按 base 解析；支持 ~ 和环境变量。"""
    p = _expand_path(p)
    if "://" in p or p.startswith("/vsi"):
        return p
    if _looks_absolute(p):
        return str(Path(p))
    return str((base / p).resolve())


def _path_base(config_path: Path, mode: str) -> Path:
    """计算相对路径的基准目录。"""
    mode = (mode or "配置文件").strip()
    if mode in ("配置文件", "配置目录", "config", "config_file", "config_dir"):
        return Path(config_path).resolve().parent
    if mode in ("脚本目录", "脚本", "script", "script_dir"):
        return Path(__file__).resolve().parent
    if mode in ("当前工作目录", "当前目录", "cwd", "workdir"):
        return Path.cwd()
    raise ConfigError("路径基准 只能是：配置文件 / 脚本目录 / 当前工作目录")


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(
            f"找不到配置文件：{path}\n"
            "可以把同目录的 config_模板.yaml 复制为 config.yaml 后修改路径，"
            "或用命令行 -r/-t/-o 直接指定影像路径。")
    with open(path, "r", encoding="utf-8-sig") as f:
        try:
            raw = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise ConfigError(
                f"配置文件 YAML 语法错误：{e}\n\n"
                "最常见原因：Windows 路径的反斜杠 \\ 写在双引号里，被 YAML 当成了转义符\n"
                "（例如 \\t、\\卫 会报 unknown escape character）。\n"
                "解决办法：把路径改成正斜杠 /，或改用单引号包裹，例如：\n"
                "  参考影像: 'D:\\data\\参考.tif'\n"
                "  待配准影像: \"D:/data/待配准.tif\"") from e
    if not isinstance(raw, dict):
        raise ConfigError("配置文件格式错误：顶层必须是键值结构")

    ref = _text(raw, "参考影像")
    tgt = _text(raw, "待配准影像")
    out = _text(raw, "输出影像")
    if not ref:
        raise ConfigError("配置缺少“参考影像”")
    if not tgt:
        raise ConfigError("配置缺少“待配准影像”")
    if not out:
        raise ConfigError("配置缺少“输出影像”")

    # 相对路径按“路径基准”解析：默认是配置文件所在目录
    base_mode = _text(raw, "路径基准", "配置文件")
    base = _path_base(path, base_mode)
    ref = _resolve_path(ref, base)
    tgt = _resolve_path(tgt, base)
    out = _resolve_path(out, base)

    crs = _sec(raw, "坐标系")
    m = _sec(raw, "测量")
    f = _sec(raw, "位移场")
    w = _sec(raw, "重采样")
    p = _sec(raw, "金字塔")
    c = _sec(raw, "精度检查")

    cfg = Config(
        ref_path=ref,
        tgt_path=tgt,
        out_path=out,
        overwrite=_boolean(raw, "覆盖已有输出", False),
        epsg=_num(crs, "EPSG代码", None, cast=int, allow_none=True),
        fix_ref_crs=_boolean(crs, "同时修正参考影像", False),
        measure=MeasureCfg(
            scale=_num(m, "读取缩放", 2, cast=int),
            tile=_num(m, "分块像素", 2000, cast=int),
            max_shift=_num(m, "最大位移像素", 40.0),
            min_valid=_num(m, "最小有效比例", 0.995),
            min_ncc_gain=_num(m, "最小NCC提升", 0.03),
            min_snr=_num(m, "最小信噪比", 4.0),
            outlier_radius=_num(m, "局部剔除半径像素", 2500.0),
            outlier_thr=_num(m, "局部剔除阈值像素", 2.5),
            workers=_num(m, "并行进程数", 0, cast=int),
        ),
        field_cfg=FieldCfg(
            node_step=_num(f, "网格步长像素", 1500, cast=int),
            radius=_num(f, "插值半径像素", 9000.0),
            weight_sigma=_num(f, "权重半径像素", 3000.0),
            smooth_sigma=_num(f, "高斯平滑", 0.6),
            val_ratio=_num(f, "验证比例", 0.2),
        ),
        warp=WarpCfg(
            method=_text(w, "方法", "双线性"),
            block=_num(w, "分块大小", 2048, cast=int),
            nodata=_num(w, "空值", 255.0, allow_none=True),
            accel=_text(w, "加速库", "auto"),
            compress=_text(w, "压缩", "NONE").upper(),
        ),
        pyramid=PyramidCfg(
            enabled=_boolean(p, "生成", True),
            levels=[int(x) for x in (p.get("级别") or [2, 4, 8, 16, 32, 64, 128, 256])],
            method=_text(p, "重采样", "average"),
        ),
        check=CheckCfg(
            enabled=_boolean(c, "生成对比图", True),
            count=_num(c, "检查点数量", 6, cast=int),
            size=_num(c, "对比图尺寸像素", 1200, cast=int),
        ),
        write_csv=_boolean(raw, "报告CSV", True),
        base_dir=str(base),
    )

    if cfg.measure.scale < 1:
        raise ConfigError("测量.读取缩放 必须 ≥ 1")
    if cfg.measure.tile < 256:
        raise ConfigError("测量.分块像素 必须 ≥ 256")
    if cfg.warp.method not in ("双线性", "最近邻", "三次卷积"):
        raise ConfigError("重采样.方法 只能是：双线性 / 最近邻 / 三次卷积")
    if cfg.warp.accel.lower() not in ("auto", "opencv", "scipy"):
        raise ConfigError("重采样.加速库 只能是：auto / opencv / scipy")
    if cfg.warp.compress not in ("NONE", "LZW", "DEFLATE", "ZSTD"):
        raise ConfigError("重采样.压缩 只能是：NONE / LZW / DEFLATE / ZSTD")
    if cfg.pyramid.method.lower() not in ("average", "nearest", "bilinear"):
        raise ConfigError("金字塔.重采样 只能是：average / nearest / bilinear")
    return cfg


# ----------------------------------------------------------------------------
# 栅格与相关工具
# ----------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def fmt_min(sec: float) -> str:
    return f"{sec / 60.0:.1f} 分钟" if sec >= 60 else f"{sec:.0f} 秒"


STATUS_INTERVAL = 1.0  # 状态轮询间隔（秒）：只按时间输出，改这个数字即可调整频率


def log_status(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def log_shift_stats(pts: np.ndarray, pixel_size: float, title: str) -> None:
    if pts is None or len(pts) == 0:
        return
    dy = np.abs(pts[:, 2])
    dx = np.abs(pts[:, 3])
    log("")
    log("=" * 70)
    log(title)
    log(f"  样本: {len(pts)} 个")
    log(f"  dy: 平均 {dy.mean():.2f}px | 中位 {np.median(dy):.2f}px | "
        f"P95 {np.percentile(dy, 95):.2f}px | 最大 {dy.max():.2f}px")
    log(f"  dx: 平均 {dx.mean():.2f}px | 中位 {np.median(dx):.2f}px | "
        f"P95 {np.percentile(dx, 95):.2f}px | 最大 {dx.max():.2f}px")
    if pixel_size:
        log(f"  换算（{pixel_size:g} m/px）：平均 |dy|={dy.mean() * pixel_size:.2f} m，"
            f"|dx|={dx.mean() * pixel_size:.2f} m；最大 |dy|={dy.max() * pixel_size:.2f} m，"
            f"|dx|={dx.max() * pixel_size:.2f} m")
    log("=" * 70)
    log("")


def log_check_summary(rows, pixel_size: float, csv_path: str = "") -> None:
    if not rows:
        log("精度检查：没有可用的检查点（窗口内有效像元不足），跳过配准后统计。")
        return
    a = np.array(rows, dtype=float)
    log("")
    log("=" * 70)
    log("配准后残余像素差（检查点，独立于控制点）")
    log(f"  样本: {len(a)} 个检查窗口")
    log(f"  配准前：|dy| 平均 {np.abs(a[:, 2]).mean():.2f}px，"
        f"|dx| 平均 {np.abs(a[:, 3]).mean():.2f}px，NCC {a[:, 6].mean():.3f}")
    log(f"  配准后：|dy| 平均 {np.abs(a[:, 4]).mean():.2f}px，"
        f"|dx| 平均 {np.abs(a[:, 5]).mean():.2f}px，NCC {a[:, 7].mean():.3f}")
    if pixel_size:
        log(f"  换算（{pixel_size:g} m/px）：配准后平均 |dy|="
            f"{np.abs(a[:, 4]).mean() * pixel_size:.2f} m，"
            f"|dx|={np.abs(a[:, 5]).mean() * pixel_size:.2f} m")
    log("=" * 70)
    if csv_path:
        log(f"  逐点明细：{csv_path}")
    log("")


class Progress:
    """后台线程每隔 STATUS_INTERVAL 秒输出一次进度，主流程只更新状态、不负责打印。"""

    def __init__(self, total: int, label: str, unit: str = "块"):
        self.total = max(1, int(total))
        self.label = label
        self.unit = unit
        self.interval = max(0.2, float(STATUS_INTERVAL))
        self.t0 = time.time()
        self._done = 0
        self._extra = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set(self, done: int, extra: str = "") -> None:
        with self._lock:
            self._done = int(done)
            self._extra = extra

    def _format(self) -> str:
        with self._lock:
            done = self._done
            extra = self._extra
        now = time.time()
        pct = done / self.total * 100.0
        el = now - self.t0
        body = f"{self.label} {done}/{self.total}（{pct:.1f}%），已用 {fmt_min(el)}"
        if done > 0 and el > 0:
            rate = done / el
            eta = (self.total - done) / rate if rate > 0 else 0.0
            body += f"，预计剩余 {fmt_min(eta)}，{rate:.1f} {self.unit}/秒"
        else:
            body += "，等待首批结果..."
        tail = f"，{extra}" if extra else ""
        return body + tail

    def _run(self) -> None:
        # 严格按时间轮询：即使分块还没处理完，也会输出当前进度
        while not self._stop.wait(self.interval):
            with self._lock:
                done = self._done
            if done >= self.total:
                continue  # 100% 统一由 finish() 打印，避免重复
            log_status(self._format())

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def finish(self, extra: str = "") -> None:
        with self._lock:
            self._done = self.total
            if extra:
                self._extra = extra
        self.stop()
        log_status(self._format())


def open_ds(path: str, update: bool = False):
    ds = gdal.Open(path, gdal.GA_Update if update else gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"无法打开影像：{path}")
    return ds


def band_nodata(ds, fallback):
    v = ds.GetRasterBand(1).GetNoDataValue()
    return fallback if v is None else v


def valid_mask(arr: np.ndarray, nodata) -> np.ndarray:
    if nodata is None:
        return np.ones(arr.shape[1:], dtype=bool)
    try:
        if np.isnan(nodata):
            return ~np.any(np.isnan(arr), axis=0)
    except TypeError:
        pass
    return ~np.all(arr == nodata, axis=0)


def read_gray(ds, x0, y0, size, out_size, nodata):
    arr = ds.ReadAsArray(int(x0), int(y0), int(size), int(size),
                         buf_xsize=int(out_size), buf_ysize=int(out_size))
    arr = np.asarray(arr, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr[None, ...]
    valid = valid_mask(arr, nodata)
    gray = arr.mean(axis=0)
    if valid.any():
        gray = np.where(valid, gray, np.float32(np.median(gray[valid])))
    return gray.astype(np.float32, copy=False), valid


def grad_img(gray, valid):
    gx = ndimage.sobel(gray, axis=1)
    gy = ndimage.sobel(gray, axis=0)
    e = np.hypot(gx, gy)
    e[~valid] = 0.0
    return e.astype(np.float32, copy=False)


def corr_shift(A, B, max_shift: int):
    """返回 (dy, dx, edge, snr)：B 的内容相对 A 偏移了多少像素（B[y+dy, x+dx] ≈ A[y, x]）。"""
    h, w = A.shape
    win = np.outer(np.hanning(h), np.hanning(w)).astype(np.float32)
    A0 = (A - A.mean()) * win
    B0 = (B - B.mean()) * win
    C = fftconvolve(B0, A0[::-1, ::-1], mode="same")
    cy, cx = h // 2, w // 2
    ms = int(max_shift)
    sub = C[cy - ms:cy + ms + 1, cx - ms:cx + ms + 1]
    py, px = np.unravel_index(int(np.argmax(sub)), sub.shape)

    def subpix(vm, v0, vp):
        den = vm - 2.0 * v0 + vp
        return 0.0 if abs(den) < 1e-12 else 0.5 * (vm - vp) / den

    sy = subpix(sub[py - 1, px], sub[py, px], sub[py + 1, px]) if 0 < py < sub.shape[0] - 1 else 0.0
    sx = subpix(sub[py, px - 1], sub[py, px], sub[py, px + 1]) if 0 < px < sub.shape[1] - 1 else 0.0

    edge = ms - max(abs(py - ms), abs(px - ms))
    tmp = sub.copy()
    tmp[max(0, py - 3):py + 4, max(0, px - 3):px + 4] = 0.0
    snr = float(sub[py, px] / (tmp.std() + 1e-9))
    return float(py - ms + sy), float(px - ms + sx), int(edge), snr


def ncc_shifted(A, B, dy, dx):
    b = ndimage.shift(B, (-dy, -dx), order=1, mode="nearest")
    m = (A > 0) | (b > 0)
    if int(m.sum()) < 100:
        return 0.0
    a = A[m] - A[m].mean()
    bb = b[m] - b[m].mean()
    den = math.sqrt(float((a * a).sum()) * float((bb * bb).sum())) + 1e-9
    return float((a * bb).sum() / den)


# ----------------------------------------------------------------------------
# 控制点测量
# ----------------------------------------------------------------------------

_WORKER = {}


def _worker_init(ref_path, tgt_path, measure_dict, nd_ref, nd_tgt):
    gdal.SetCacheMax(256 * 1024 * 1024)
    _WORKER["ref"] = gdal.Open(ref_path, gdal.GA_ReadOnly)
    _WORKER["tgt"] = gdal.Open(tgt_path, gdal.GA_ReadOnly)
    _WORKER["mc"] = MeasureCfg(**measure_dict)
    _WORKER["nd_ref"] = nd_ref
    _WORKER["nd_tgt"] = nd_tgt


def _worker_task(xy):
    x0, y0 = xy
    try:
        return measure_tile(_WORKER["ref"], _WORKER["tgt"], _WORKER["mc"],
                            _WORKER["nd_ref"], _WORKER["nd_tgt"], x0, y0)
    except Exception as e:  # 单块失败不影响整体
        log(f"  分块 ({x0},{y0}) 测量失败：{e}")
        return None


def measure_tile(ds_ref, ds_tgt, mc: MeasureCfg, nd_ref, nd_tgt, x0, y0):
    tile = mc.tile
    read_sz = max(64, int(round(tile / mc.scale)))
    fx = tile / float(read_sz)

    ga, va = read_gray(ds_ref, x0, y0, tile, read_sz, nd_ref)
    gb, vb = read_gray(ds_tgt, x0, y0, tile, read_sz, nd_tgt)
    if va.mean() < mc.min_valid or vb.mean() < mc.min_valid:
        return None

    ea = grad_img(ga, va)
    eb = grad_img(gb, vb)
    ms = int(round(mc.max_shift / fx))
    ms = min(ms, read_sz // 2 - 4)
    if ms < 3:
        return None

    dy, dx, edge, snr = corr_shift(ea, eb, ms)
    if edge <= 2 or snr < mc.min_snr:
        return None

    n0 = ncc_shifted(ea, eb, 0.0, 0.0)
    n1 = ncc_shifted(ea, eb, dy, dx)
    if n1 - n0 < mc.min_ncc_gain:
        return None

    return (x0 + tile / 2.0, y0 + tile / 2.0, dy * fx, dx * fx, n0, n1, snr)


def candidate_tiles(ref_ds, nd_ref, tile: int, min_valid: float = 0.995) -> List[Tuple[int, int]]:
    W, H = ref_ds.RasterXSize, ref_ds.RasterYSize
    low_w = max(64, min(512, W))
    low_h = max(1, int(round(H * low_w / W)))
    low = ref_ds.ReadAsArray(buf_xsize=low_w, buf_ysize=low_h)
    low = np.asarray(low)
    if low.ndim == 2:
        low = low[None, ...]
    valid = ~np.all(low == nd_ref, axis=0) if nd_ref is not None else np.ones(low.shape[1:], bool)
    valid = ndimage.binary_erosion(valid, iterations=1)

    sx = W / float(low_w)
    sy = H / float(low_h)
    tiles = []
    for y0 in range(0, H - tile + 1, tile):
        for x0 in range(0, W - tile + 1, tile):
            lx0 = int(x0 / sx)
            ly0 = int(y0 / sy)
            lx1 = min(low_w - 1, int(math.ceil((x0 + tile) / sx)))
            ly1 = min(low_h - 1, int(math.ceil((y0 + tile) / sy)))
            win = valid[ly0:ly1 + 1, lx0:lx1 + 1]
            if win.size and float(win.mean()) >= min_valid:
                tiles.append((x0, y0))
    return tiles


def measure_control_points(cfg: Config, ref_path: str, tgt_path: str,
                           nd_ref, nd_tgt, tiles: List[Tuple[int, int]]) -> np.ndarray:
    workers = cfg.measure.workers if cfg.measure.workers > 0 else min(8, os.cpu_count() or 1)
    n = len(tiles)
    results = []

    def extra_text():
        valid = [r for r in results if r is not None]
        if not valid:
            return "有效 0"
        arr = np.asarray(valid, dtype=float)
        return (f"有效 {len(valid)}，当前 |dy|中位 {np.median(np.abs(arr[:, 2])):.2f}px，"
                f"|dx|中位 {np.median(np.abs(arr[:, 3])):.2f}px")

    if workers > 1:
        进度 = Progress(n, "控制点测量")
        log_status(f"开始测量控制点：{n} 个分块，{workers} 个进程")
        try:
            with ProcessPoolExecutor(max_workers=workers, initializer=_worker_init,
                                     initargs=(ref_path, tgt_path, asdict(cfg.measure),
                                               nd_ref, nd_tgt)) as ex:
                for k, res in enumerate(ex.map(_worker_task, tiles, chunksize=4), 1):
                    results.append(res)
                    进度.set(k, extra_text())
            进度.finish(extra_text())
        except Exception as e:
            进度.stop()
            log_status(f"并行测量不可用（{e}），改为单进程测量")
            results = []
            workers = 1

    if workers <= 1:
        进度 = Progress(n, "控制点测量")
        ds_ref = open_ds(ref_path)
        ds_tgt = open_ds(tgt_path)
        try:
            for k, (x0, y0) in enumerate(tiles, 1):
                results.append(measure_tile(ds_ref, ds_tgt, cfg.measure, nd_ref, nd_tgt, x0, y0))
                进度.set(k, extra_text())
            进度.finish(extra_text())
        finally:
            进度.stop()

    pts = np.array([r for r in results if r is not None], dtype=float)
    if pts.size == 0:
        raise RuntimeError("没有测到任何有效控制点。请尝试：减小“分块像素”、增大“读取缩放”为 1、"
                           "放宽“最小NCC提升/最小信噪比”，或检查两期影像是否覆盖同一区域。")
    log_status(f"控制点测量完成：{len(pts)}/{n} 个分块有效")
    return pts


def clean_outliers(pts: np.ndarray, radius: float, thr: float, min_neighbors: int = 4):
    if len(pts) < 6:
        return pts, 0
    P = pts[:, :2]
    tree = cKDTree(P)
    keep = np.ones(len(pts), dtype=bool)
    for i in range(len(pts)):
        idx = [j for j in tree.query_ball_point(P[i], radius) if j != i]
        if len(idx) >= min_neighbors:
            med = np.median(pts[idx, 2:4], axis=0)
            if math.hypot(*(pts[i, 2:4] - med)) > thr:
                keep[i] = False
    return pts[keep], int((~keep).sum())


# ----------------------------------------------------------------------------
# 位移场
# ----------------------------------------------------------------------------

def build_field(pts: np.ndarray, W: int, H: int, fcfg: FieldCfg):
    P = pts[:, :2]
    V = pts[:, 2:4]
    step = fcfg.node_step
    nx = int(math.ceil(W / step)) + 2
    ny = int(math.ceil(H / step)) + 2
    gx, gy = np.meshgrid(np.arange(nx) * step, np.arange(ny) * step)
    nodes = np.column_stack([gx.ravel(), gy.ravel()])
    tree = cKDTree(P)
    out = np.zeros((len(nodes), 2), dtype=np.float64)
    filled = np.zeros(len(nodes), dtype=bool)
    for k, nd in enumerate(nodes):
        idx = tree.query_ball_point(nd, fcfg.radius)
        if not idx:
            continue
        d = np.linalg.norm(P[idx] - nd, axis=1)
        w = np.exp(-0.5 * (d / max(fcfg.weight_sigma, 1e-6)) ** 2)
        sw = w.sum()
        if sw <= 0:
            continue
        out[k] = (V[idx] * w[:, None]).sum(axis=0) / sw
        filled[k] = True
    if not filled.all():
        nearest = tree.query(nodes[~filled], k=1)[1]
        out[~filled] = V[nearest]
    grid = out.reshape(ny, nx, 2)
    if fcfg.smooth_sigma > 0:
        grid = ndimage.gaussian_filter(grid, sigma=(fcfg.smooth_sigma, fcfg.smooth_sigma, 0))
    return grid.astype(np.float64), float(step)


def sample_field(grid: np.ndarray, step: float, X: np.ndarray, Y: np.ndarray):
    # 注意：结果顺序是 (dy, dx)
    dy = ndimage.map_coordinates(grid[..., 0], [Y / step, X / step], order=1, mode="nearest")
    dx = ndimage.map_coordinates(grid[..., 1], [Y / step, X / step], order=1, mode="nearest")
    return dy, dx


def field_validation(pts: np.ndarray, cfg: Config, W: int, H: int):
    ratio = cfg.field_cfg.val_ratio
    n = len(pts)
    if ratio <= 0 or n < 10:
        return None
    rng = np.random.default_rng(20261026)
    idx = rng.permutation(n)
    n_val = max(1, int(round(n * ratio)))
    val_idx = idx[:n_val]
    train_idx = idx[n_val:]
    if len(train_idx) < 6:
        return None
    grid, step = build_field(pts[train_idx], W, H, cfg.field_cfg)
    pdy, pdx = sample_field(grid, step, pts[val_idx, 0], pts[val_idx, 1])
    err_y = pts[val_idx, 2] - pdy
    err_x = pts[val_idx, 3] - pdx
    return {
        "n": int(n_val),
        "rmse_dy": float(np.sqrt(np.mean(err_y ** 2))),
        "rmse_dx": float(np.sqrt(np.mean(err_x ** 2))),
        "max_dy": float(np.max(np.abs(err_y))),
        "max_dx": float(np.max(np.abs(err_x))),
    }


# ----------------------------------------------------------------------------
# 重采样
# ----------------------------------------------------------------------------

def np_dtype_of(gdal_type: int):
    return np.dtype(gdal_array.GDALTypeCodeToNumericTypeCode(gdal_type))


def to_dtype(v: np.ndarray, dtype: np.dtype) -> np.ndarray:
    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        v = np.clip(np.rint(v), info.min, info.max)
    return v.astype(dtype)


def load_cv2(mode: str):
    if mode.lower() == "scipy":
        return None
    try:
        import cv2  # noqa: F401
        return cv2
    except Exception as e:
        if mode.lower() == "opencv":
            raise RuntimeError("配置要求 OpenCV，但导入 cv2 失败。请安装 opencv-python-headless，"
                               "或在配置中把“加速库”改成 auto/scipy。") from e
        return None


def warp_scipy(arr, valid, row, col, method, bh, bw, np_dtype, out_nd):
    order = {"双线性": 1, "最近邻": 0, "三次卷积": 3}[method]
    bands = arr.shape[0]
    out = np.empty((bands, bh, bw), dtype=np_dtype)
    if valid is None:
        for b in range(bands):
            v = ndimage.map_coordinates(arr[b], [row, col], order=order,
                                        mode="constant", cval=0.0)
            out[b] = to_dtype(v, np_dtype)
        return out

    wgt = ndimage.map_coordinates(valid, [row, col], order=1, mode="constant", cval=0.0)
    wgt = np.maximum(wgt, 0.0)
    for b in range(bands):
        num = ndimage.map_coordinates(arr[b] * valid, [row, col], order=order,
                                      mode="constant", cval=0.0)
        v = np.zeros((bh, bw), dtype=np.float32)
        np.divide(num, wgt, out=v, where=wgt > 1e-4)
        v = to_dtype(v, np_dtype)
        v[wgt <= 0.5] = out_nd
        out[b] = v
    return out


def warp_cv2(arr, valid, row, col, method, cv2, bh, bw, np_dtype, out_nd):
    interp = {"双线性": cv2.INTER_LINEAR, "最近邻": cv2.INTER_NEAREST,
              "三次卷积": cv2.INTER_CUBIC}[method]
    map_x = np.ascontiguousarray(col, dtype=np.float32)
    map_y = np.ascontiguousarray(row, dtype=np.float32)
    bands = arr.shape[0]
    src = np.ascontiguousarray(np.moveaxis(arr, 0, -1))

    if bands <= 4:
        warped = cv2.remap(src, map_x, map_y, interp,
                           borderMode=cv2.BORDER_CONSTANT, borderValue=(0,) * bands)
        warped = np.asarray(warped)
        if warped.ndim == 2:
            warped = warped[..., None]
    else:
        warped = np.empty((bh, bw, bands), dtype=np.float32)
        for b in range(bands):
            warped[..., b] = cv2.remap(src[..., b], map_x, map_y, interp,
                                       borderMode=cv2.BORDER_CONSTANT, borderValue=0)

    out = np.empty((bands, bh, bw), dtype=np_dtype)
    if valid is None:
        for b in range(bands):
            out[b] = to_dtype(warped[..., b], np_dtype)
        return out

    wgt = cv2.remap(valid, map_x, map_y, cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    wgt = np.maximum(np.asarray(wgt), 0.0)
    for b in range(bands):
        v = np.zeros((bh, bw), dtype=np.float32)
        np.divide(warped[..., b], wgt, out=v, where=wgt > 1e-4)
        v = to_dtype(v, np_dtype)
        v[wgt <= 0.5] = out_nd
        out[b] = v
    return out


def warp_target(cfg: Config, ds_tgt, grid, step, gt, W, H, out_path: str,
                wkt: str, srs_label: str, nd_tgt):
    bands = ds_tgt.RasterCount
    dtype = ds_tgt.GetRasterBand(1).DataType
    np_dtype = np_dtype_of(dtype)
    out_nd = nd_tgt if nd_tgt is not None else cfg.warp.nodata
    if out_nd is not None and np.issubdtype(np_dtype, np.floating):
        out_nd = float(out_nd)

    opts = ["TILED=YES", "BLOCKXSIZE=256", "BLOCKYSIZE=256",
            "BIGTIFF=IF_SAFER", "INTERLEAVE=PIXEL"]
    if cfg.warp.compress != "NONE":
        opts.append(f"COMPRESS={cfg.warp.compress}")

    tmp = out_path + ".part"
    if os.path.exists(tmp):
        os.remove(tmp)
    driver = gdal.GetDriverByName("GTiff")
    dst = driver.Create(tmp, W, H, bands, dtype, options=opts)
    if dst is None:
        raise RuntimeError("创建输出文件失败")
    dst.SetGeoTransform(gt)
    dst.SetProjection(wkt)
    for b in range(1, bands + 1):
        if out_nd is not None:
            dst.GetRasterBand(b).SetNoDataValue(out_nd)

    cv2 = load_cv2(cfg.warp.accel)
    log(f"重采样：{cfg.warp.method}，分块 {cfg.warp.block}，"
        f"加速：{'OpenCV' if cv2 is not None else 'SciPy'}，"
        f"坐标系：{srs_label}")

    block = max(256, int(cfg.warp.block))
    n_blocks = ((W + block - 1) // block) * ((H + block - 1) // block)
    bytes_per_px = max(1, gdal.GetDataTypeSize(dtype) // 8) * bands
    expected_gb = W * H * bytes_per_px / 1e9
    log_status(f"开始重采样：共 {n_blocks} 块，未压缩输出约 {expected_gb:.1f} GB")
    进度 = Progress(n_blocks, "重采样")
    done = 0
    try:
        for y0 in range(0, H, block):
            bh = min(block, H - y0)
            for x0 in range(0, W, block):
                bw = min(block, W - x0)
                yy, xx = np.meshgrid(
                    np.arange(y0, y0 + bh, dtype=np.float64),
                    np.arange(x0, x0 + bw, dtype=np.float64), indexing="ij")
                dy, dx = sample_field(grid, step, xx, yy)
                iy = yy + dy
                ix = xx + dx

                xs = min(max(0, int(np.floor(ix.min())) - 2), W - 1)
                ys = min(max(0, int(np.floor(iy.min())) - 2), H - 1)
                xe = min(W, max(xs + 1, int(np.ceil(ix.max())) + 3))
                ye = min(H, max(ys + 1, int(np.ceil(iy.max())) + 3))

                arr = ds_tgt.ReadAsArray(xs, ys, xe - xs, ye - ys)
                arr = np.asarray(arr)
                if arr.ndim == 2:
                    arr = arr[None, ...]
                arr = arr.astype(np.float32, copy=False)
                row = iy - ys
                col = ix - xs

                if out_nd is not None:
                    valid = (~np.all(arr == out_nd, axis=0)).astype(np.float32)
                else:
                    valid = None

                if cv2 is not None:
                    out = warp_cv2(arr, valid, row, col, cfg.warp.method,
                                   cv2, bh, bw, np_dtype, out_nd)
                else:
                    out = warp_scipy(arr, valid, row, col, cfg.warp.method,
                                     bh, bw, np_dtype, out_nd)

                dst.WriteArray(out, x0, y0)
                done += 1
                try:
                    written_gb = os.path.getsize(tmp) / 1e9
                except OSError:
                    written_gb = 0.0
                written = (f"{written_gb:.2f} GB" if written_gb >= 0.1
                           else f"{written_gb * 1000:.0f} MB")
                进度.set(done, f"已写入 {written}")

        try:
            written_gb = os.path.getsize(tmp) / 1e9
        except OSError:
            written_gb = 0.0
        written = (f"{written_gb:.2f} GB" if written_gb >= 0.1
                   else f"{written_gb * 1000:.0f} MB")
        进度.finish(f"已写入 {written}")
    except Exception:
        进度.stop()
        dst = None
        if os.path.exists(tmp):
            os.remove(tmp)
        raise

    dst = None
    os.replace(tmp, out_path)
    log_status(f"重采样完成：{out_path}")


def build_overviews(cfg: Config, path: str):
    ds = gdal.Open(path)
    W, H = ds.RasterXSize, ds.RasterYSize
    levels = [int(l) for l in cfg.pyramid.levels if 1 < int(l) < max(W, H)]
    ds = None
    if not levels:
        return
    ds = gdal.Open(path, gdal.GA_Update)
    method = {"average": "AVERAGE", "nearest": "NEAREST", "bilinear": "BILINEAR"}[
        cfg.pyramid.method.lower()]
    log(f"建立金字塔：级别 {levels}，方法 {method}")
    state = {"t0": time.time(), "last": 0.0}

    def _ov_cb(complete, message, data):
        now = time.time()
        if now - data["last"] >= STATUS_INTERVAL or complete >= 0.999:
            log_status(f"金字塔 {complete * 100:.0f}%，已用 {fmt_min(now - data['t0'])}")
            data["last"] = now
        return 1

    try:
        ds.BuildOverviews(method, levels, callback=_ov_cb, callback_data=state,
                          options=["NUM_THREADS=ALL_CPUS"])
    except TypeError:  # 旧版 GDAL 没有 options 参数
        try:
            ds.BuildOverviews(method, levels, callback=_ov_cb, callback_data=state)
        except TypeError:
            ds.BuildOverviews(method, levels)
    ds = None
    log_status("金字塔完成")


# ----------------------------------------------------------------------------
# 精度检查 / 对比图 / 报告
# ----------------------------------------------------------------------------

def pick_check_windows(pts: np.ndarray, count: int, W: int, H: int, size: int):
    if count <= 0:
        return []
    size = int(max(64, min(size, W, H)))
    order = np.argsort(pts[:, 1])
    pts = pts[order]
    if len(pts) <= count:
        idx = np.arange(len(pts))
    else:
        idx = np.linspace(0, len(pts) - 1, count).astype(int)
    out = []
    for i in idx:
        x0 = int(min(max(0, pts[i, 0] - size / 2), W - size))
        y0 = int(min(max(0, pts[i, 1] - size / 2), H - size))
        out.append((x0, y0))
    seen = set()
    uniq = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def check_alignment(ref_ds, img_ds, x0, y0, size, nd_ref, nd_img):
    ga, va = read_gray(ref_ds, x0, y0, size, size, nd_ref)
    gb, vb = read_gray(img_ds, x0, y0, size, size, nd_img)
    if va.mean() < 0.99 or vb.mean() < 0.99:
        return None
    ea = grad_img(ga, va)
    eb = grad_img(gb, vb)
    ms = max(5, min(40, size // 3))
    dy, dx, edge, snr = corr_shift(ea, eb, ms)
    n = ncc_shifted(ea, eb, 0.0, 0.0)
    return dy, dx, n


def save_composites(out_stem: str, ref_ds, out_ds, windows, size, nd_ref, nd_out):
    try:
        from PIL import Image  # 延迟导入：Windows 多进程子进程里 QGIS 的 Pillow DLL 可能不可用
    except Exception as e:
        log(f"警告：无法导入 Pillow，跳过对比图生成（{e}）")
        return Path(out_stem + "_对比图"), 0
    folder = Path(out_stem + "_对比图")
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("对比_*.png"):
        old.unlink()
    saved = 0
    for i, (x0, y0) in enumerate(windows, 1):
        ga, va = read_gray(ref_ds, x0, y0, size, size, nd_ref)
        gb, vb = read_gray(out_ds, x0, y0, size, size, nd_out)
        if not va.any() or not vb.any():
            continue

        def stretch(x, v):
            lo, hi = np.percentile(x[v], 3), np.percentile(x[v], 97)
            return np.clip((x - lo) / (hi - lo + 1e-6), 0, 1)

        rgb = np.stack([stretch(ga, va), stretch(gb, vb), np.zeros_like(ga)], axis=-1)
        Image.fromarray((rgb * 255).astype(np.uint8)).save(
            folder / f"对比_{i:02d}_x{x0}_y{y0}.png")
        saved += 1
    return folder, saved


def write_csv(path: str, rows, header: str):
    np.savetxt(path, rows, delimiter=",", header=header, comments="", encoding="utf-8-sig")


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------

def _srs_of(ds):
    wkt = ds.GetProjection()
    if not wkt:
        return None
    srs = osr.SpatialReference()
    srs.ImportFromWkt(wkt)
    return srs


def grid_matches(ref_ds, tgt_ds) -> bool:
    if (ref_ds.RasterXSize, ref_ds.RasterYSize) != (tgt_ds.RasterXSize, tgt_ds.RasterYSize):
        return False
    ga = ref_ds.GetGeoTransform()
    gb = tgt_ds.GetGeoTransform()
    return all(abs(a - b) <= 1e-6 for a, b in zip(ga, gb))


def crs_state(ref_ds, tgt_ds) -> str:
    a = _srs_of(ref_ds)
    b = _srs_of(tgt_ds)
    if a is None and b is None:
        return "same"
    if a is None or b is None:
        return "unknown"
    if bool(a.IsSame(b)):
        return "same"
    if a.IsProjected() and b.IsProjected():
        return "different"
    return "unknown"


def create_aligned_vrt(ref_ds, tgt_path, vrt_path, dst_wkt, nd_src, nd_dst, W, H):
    """把待配准影像重采样到参考影像格网，生成临时 VRT（不占磁盘）。"""
    gt = ref_ds.GetGeoTransform()
    if abs(gt[2]) > 1e-9 or abs(gt[4]) > 1e-9:
        raise ConfigError("参考影像不是北向上的（含旋转项），暂不支持自动对齐到不同格网。")
    minx, maxy = gt[0], gt[3]
    maxx = gt[0] + W * gt[1]
    miny = gt[3] + H * gt[5]
    kwargs = dict(
        format="VRT",
        outputBounds=(minx, miny, maxx, maxy),
        width=W, height=H, resampleAlg="bilinear",
        srcNodata=nd_src, dstNodata=nd_dst,
        multithread=True, warpOptions=["NUM_THREADS=ALL_CPUS"])
    if dst_wkt:
        kwargs["dstSRS"] = dst_wkt
    opts = gdal.WarpOptions(**kwargs)
    if os.path.exists(vrt_path):
        os.remove(vrt_path)
    ds = gdal.Warp(vrt_path, tgt_path, options=opts)
    if ds is None:
        raise RuntimeError("gdal.Warp 未能生成临时 VRT")
    return ds


def run(cfg: Config, measure_only: bool = False) -> None:
    t_start = time.time()
    ref_path = str(Path(cfg.ref_path))
    tgt_path = str(Path(cfg.tgt_path))
    out_path = str(Path(cfg.out_path))

    for p, name in ((ref_path, "参考影像"), (tgt_path, "待配准影像")):
        if not Path(p).exists():
            raise ConfigError(
                f"{name}不存在：{p}\n"
                f"当前“路径基准”为：{cfg.base_dir}（相对路径按它解析）。"
                "可以使用绝对路径，或检查“路径基准”和相对路径是否正确。")
    if Path(out_path).resolve() in (Path(ref_path).resolve(), Path(tgt_path).resolve()):
        raise ConfigError("输出影像不能与输入影像相同")
    if Path(out_path).exists() and not cfg.overwrite:
        raise ConfigError(f"输出文件已存在：{out_path}。如需覆盖，请把“覆盖已有输出”设为 true。")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    log("=" * 78)
    log("TIF 卫星影像自动配准（以“参考影像”为基准）")
    log(f"参考影像（基准/第一张）：{ref_path}")
    log(f"待配准影像（第二张）：{tgt_path}")
    log(f"输出影像：{out_path}")
    log(f"路径基准：{cfg.base_dir}（相对路径按此目录解析）")
    log("=" * 78)

    ds_ref = None
    ds_tgt = None
    out_ds = None
    vrt_path = None
    try:
        ds_ref = open_ds(ref_path)
        ds_tgt = open_ds(tgt_path)
        W, H = ds_ref.RasterXSize, ds_ref.RasterYSize
        gt_ref = ds_ref.GetGeoTransform()
        nd_ref = band_nodata(ds_ref, cfg.warp.nodata)
        nd_tgt = band_nodata(ds_tgt, cfg.warp.nodata)
        log(f"参考影像：{W} x {H}，{ds_ref.RasterCount} 波段，像素 {gt_ref[1]} x {abs(gt_ref[5])}，NoData {nd_ref}")
        log(f"待配准影像：{ds_tgt.RasterXSize} x {ds_tgt.RasterYSize}，{ds_tgt.RasterCount} 波段，NoData {nd_tgt}")
        log_status(f"开始处理：读取缩放 {cfg.measure.scale}，分块 {cfg.measure.tile}px，"
                   f"并行 {cfg.measure.workers or '自动'}，加速 {cfg.warp.accel}")

        # ---- 输出坐标系 ----
        if cfg.epsg is not None:
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(int(cfg.epsg))
            out_wkt = srs.ExportToWkt()
            srs_label = f"EPSG:{int(cfg.epsg)}（{srs.GetName()}）"
        else:
            out_wkt = ds_ref.GetProjection()
            srs = osr.SpatialReference()
            srs.ImportFromWkt(out_wkt)
            auth = srs.GetAuthorityCode(None)
            srs_label = f"EPSG:{auth}（{srs.GetName()}）" if auth else f"{srs.GetName()}（无 EPSG）"
            if not auth:
                log("警告：参考影像的坐标系没有 EPSG 代码，输出可能无法与 EPSG 数据直接叠加。"
                    "建议在配置里填写“坐标系.EPSG代码”。")

        # ---- 必要时先对齐到参考格网 ----
        tgt_work_path = tgt_path
        dst_nd = nd_tgt if nd_tgt is not None else cfg.warp.nodata
        state = crs_state(ds_ref, ds_tgt)
        if not grid_matches(ds_ref, ds_tgt):
            if state == "unknown":
                log("警告：两幅影像的坐标系标识不同或缺失，将按同一坐标处理；"
                    "如结果有系统偏移，请设置正确的 EPSG 或先统一源影像坐标系。")
            log("两幅影像的尺寸/格网不一致，正在把待配准影像重采样到参考格网（临时 VRT）...")
            vrt_path = out_path + ".align.vrt"
            try:
                ds_tgt = create_aligned_vrt(ds_ref, tgt_path, vrt_path,
                                            out_wkt if state == "different" else None,
                                            nd_tgt, dst_nd, W, H)
            except Exception as e:
                raise ConfigError(
                    f"自动对齐待配准影像失败：{e}。请确认两幅影像有重叠，并在配置中填写正确的"
                    "“坐标系.EPSG代码”（参考影像坐标系必须能正确参与转换）。") from e
            tgt_work_path = vrt_path
            nd_tgt = band_nodata(ds_tgt, dst_nd)
            log("已对齐到参考格网：待配准影像覆盖范围以外将输出为空值。")
        else:
            if state == "different":
                log("两幅影像坐标系不同，正在把待配准影像重采样到参考坐标系（临时 VRT）...")
                vrt_path = out_path + ".align.vrt"
                try:
                    ds_tgt = create_aligned_vrt(ds_ref, tgt_path, vrt_path, out_wkt,
                                                nd_tgt, dst_nd, W, H)
                except Exception as e:
                    raise ConfigError(f"坐标系转换失败：{e}。请检查“坐标系.EPSG代码”。") from e
                tgt_work_path = vrt_path
                nd_tgt = band_nodata(ds_tgt, dst_nd)
            elif state == "unknown":
                log("警告：两幅影像的坐标系标识不同或缺失，但尺寸/格网一致，按同一坐标处理。"
                    "如叠加后仍有系统偏移，请在配置中设置正确的 EPSG。")

        # ---- 自动缩小分块 ----
        limit = max(256, min(W, H) // 3)
        if cfg.measure.tile > limit:
            log(f"影像较小，测量分块从 {cfg.measure.tile} 自动调整为 {limit}")
            cfg.measure.tile = limit

        # ---- 控制点测量 ----
        tiles = candidate_tiles(ds_ref, nd_ref, cfg.measure.tile, cfg.measure.min_valid)
        log(f"候选分块：{len(tiles)} 个（分块 {cfg.measure.tile} 像素，读取缩放 {cfg.measure.scale}）")
        if not tiles:
            raise RuntimeError("有效区内没有可用的分块。请减小“测量.分块像素”或检查 NoData。")

        pts = measure_control_points(cfg, ref_path, tgt_work_path, nd_ref, nd_tgt, tiles)
        pts, removed = clean_outliers(pts, cfg.measure.outlier_radius, cfg.measure.outlier_thr)
        log(f"控制点：{len(pts)} 个有效（剔除误匹配 {removed} 个）")
        if len(pts) < 6:
            raise RuntimeError("有效控制点太少，无法构建位移场。请放宽测量阈值或增大分块数量。")
        log_shift_stats(pts, abs(gt_ref[1]), "配准前像素差（控制点，第二张相对第一张）")

        val = field_validation(pts, cfg, W, H)
        if val:
            log(f"留出验证（{val['n']} 个点）：位移场拟合 RMSE "
                f"dy={val['rmse_dy']:.2f}px dx={val['rmse_dx']:.2f}px，"
                f"最大 |dy|={val['max_dy']:.2f}px |dx|={val['max_dx']:.2f}px")

        grid, step = build_field(pts, W, H, cfg.field_cfg)
        log(f"位移场：网格步长 {step:.0f}px，dx 范围 [{pts[:, 3].min():.2f}, {pts[:, 3].max():.2f}]，"
            f"dy 范围 [{pts[:, 2].min():.2f}, {pts[:, 2].max():.2f}] 像素")

        out_stem = str(Path(out_path).with_suffix(""))
        if cfg.write_csv:
            write_csv(out_stem + "_控制点.csv", pts[:, :7],
                      "X坐标,Y坐标,dy像素,dx像素,NCC配准前,NCC配准后,信噪比")
            log(f"控制点已保存：{out_stem}_控制点.csv")
        np.savez(out_stem + "_位移场.npz", grid=grid, step=step)
        log(f"位移场已保存：{out_stem}_位移场.npz")

        if measure_only:
            log(f"\n仅测量模式结束，用时 {fmt_min(time.time() - t_start)}")
            return

        # ---- 重采样 ----
        warp_target(cfg, ds_tgt, grid, step, gt_ref, W, H, out_path, out_wkt, srs_label, nd_tgt)

        # ---- 精度检查 ----
        accuracy_rows = []
        used_windows = []
        candidates = cfg.check.count * 3 if cfg.check.count > 0 else 0
        windows = pick_check_windows(pts, candidates, W, H, cfg.check.size)
        if windows:
            out_ds = gdal.Open(out_path)
            nd_out = band_nodata(out_ds, nd_tgt)
            log(f"精度检查：目标 {cfg.check.count} 个检查点（候选窗口 {len(windows)} 个），"
                f"窗口 {cfg.check.size}px")
            for x0, y0 in windows:
                before = check_alignment(ds_ref, ds_tgt, x0, y0, cfg.check.size, nd_ref, nd_tgt)
                after = check_alignment(ds_ref, out_ds, x0, y0, cfg.check.size, nd_ref, nd_out)
                if before is None or after is None:
                    continue
                dyo, dxo, no = before
                dyw, dxw, nw = after
                accuracy_rows.append((x0, y0, dyo, dxo, dyw, dxw, no, nw))
                used_windows.append((x0, y0))
                log(f"  点({x0},{y0})：配准前 ({dyo:5.2f},{dxo:5.2f})px NCC={no:.3f}  →  "
                    f"配准后 ({dyw:5.2f},{dxw:5.2f})px NCC={nw:.3f}")
                if cfg.check.count > 0 and len(accuracy_rows) >= cfg.check.count:
                    break
            if len(accuracy_rows) < cfg.check.count:
                log_status(f"有效检查点不足：{len(accuracy_rows)}/{cfg.check.count} 个"
                           "（其余窗口包含空值），统计按实际样本计算。")

            check_csv = ""
            if accuracy_rows and cfg.write_csv:
                check_csv = out_stem + "_精度验证.csv"
                write_csv(check_csv, np.array(accuracy_rows),
                          "x0,y0,配准前dy,配准前dx,配准后dy,配准后dx,配准前NCC,配准后NCC")
                log(f"精度验证已保存：{check_csv}")
            log_check_summary(accuracy_rows, abs(gt_ref[1]), check_csv)

            if cfg.check.enabled and used_windows:
                folder, n_saved = save_composites(out_stem, ds_ref, out_ds, used_windows,
                                                  cfg.check.size, nd_ref, nd_out)
                log(f"对比图已保存：{folder}（{n_saved} 张，红=参考，绿=配准结果，重合处偏黄）")
            out_ds = None
        else:
            log("精度检查未启用（检查点数量为 0），跳过配准后残余统计。")

        # ---- 金字塔 ----
        if cfg.pyramid.enabled:
            build_overviews(cfg, out_path)

        # ---- 可选：修正参考影像的坐标系标识 ----
        if cfg.fix_ref_crs and cfg.epsg is not None:
            ds_ref = None
            ds_tgt = None
            ds = gdal.Open(ref_path, gdal.GA_Update)
            ds.SetProjection(out_wkt)
            ds = None
            log(f"已把参考影像的坐标系标识改为 EPSG:{cfg.epsg}（仅元数据，像素未变）")

        ds_ref = None
        ds_tgt = None

        a = np.array(accuracy_rows) if accuracy_rows else np.empty((0, 8))
        log("\n" + "=" * 78)
        log("配准完成")
        log(f"输出文件：{out_path}")
        log(f"坐标系：{srs_label}")
        log(f"控制点：{len(pts)} 个；剔除外点：{removed} 个")
        if val:
            log(f"位移场拟合 RMSE：dy={val['rmse_dy']:.2f}px，dx={val['rmse_dx']:.2f}px")
        if len(a):
            log(f"配准前平均偏差：|dy|={np.abs(a[:, 2]).mean():.2f}px，|dx|={np.abs(a[:, 3]).mean():.2f}px；"
                f"NCC={a[:, 6].mean():.3f}")
            log(f"配准后残余偏差：|dy|={np.abs(a[:, 4]).mean():.2f}px，|dx|={np.abs(a[:, 5]).mean():.2f}px；"
                f"NCC={a[:, 7].mean():.3f}")
        log(f"总用时：{fmt_min(time.time() - t_start)}")
        log("=" * 78)
    finally:
        out_ds = None
        ds_tgt = None
        ds_ref = None
        if vrt_path and os.path.exists(vrt_path):
            try:
                os.remove(vrt_path)
            except OSError:
                pass
        if vrt_path and os.path.exists(vrt_path + ".aux.xml"):
            try:
                os.remove(vrt_path + ".aux.xml")
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description="TIF 卫星影像自动配准（以参考影像为基准）")
    parser.add_argument("-c", "--config", default=str(Path(__file__).with_name("config.yaml")),
                        help="配置文件路径（默认：脚本同目录的 config.yaml）")
    parser.add_argument("-r", "--ref",
                        help="参考影像路径（覆盖配置文件；相对路径按“路径基准”解析）")
    parser.add_argument("-t", "--tgt",
                        help="待配准影像路径（覆盖配置文件；相对路径按“路径基准”解析）")
    parser.add_argument("-o", "--out",
                        help="输出影像路径（覆盖配置文件；相对路径按“路径基准”解析）")
    parser.add_argument("--epsg", type=int, help="输出坐标系 EPSG（覆盖配置文件）")
    parser.add_argument("--measure-only", action="store_true",
                        help="只测量控制点并构建位移场，不重采样")
    args = parser.parse_args()

    try:
        cfg = load_config(Path(args.config))
        base = Path(cfg.base_dir) if cfg.base_dir else Path.cwd()
        if args.ref:
            cfg.ref_path = _resolve_path(args.ref, base)
        if args.tgt:
            cfg.tgt_path = _resolve_path(args.tgt, base)
        if args.out:
            cfg.out_path = _resolve_path(args.out, base)
        if args.epsg is not None:
            cfg.epsg = args.epsg
        run(cfg, measure_only=args.measure_only)
    except ConfigError as e:
        log(f"配置错误：{e}")
        sys.exit(2)
    except Exception as e:
        log(f"运行失败：{e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
