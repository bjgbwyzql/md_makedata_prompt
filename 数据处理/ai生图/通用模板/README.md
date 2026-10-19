# ortho-data-gen — 合成正射/双时相训练数据管线

为国创岚天双时相变化检测模型制造合成训练数据的工具集。
重构自 `G:\国创岚天\正射\数据制造`（2026-10），旧目录中的实验数据已废弃。

## 管线三步

1. **造底图**（文生图）：`scenes/text2img/` → 生成 4K 无人机正射影像（建筑/屋顶第五立面）
2. **造变化对**（图生图）：`scenes/img2img/` → 以底图为"一期"，局部编辑生成"二期"，
   自动配对复制 before/after，形成双时相样本

## 环境

- Python 3.8+，依赖见 `requirements.txt`
- API Key：设置环境变量 `GRSAI_API_KEY`，或复制 `.env.example` 为 `.env` 填入
  （旧脚本硬编码在 ag_stable.py 里的 key 已废弃，建议去平台撤销更换）

## 用法

场景全部是「一个 run.py + 一份 scenes.yaml」的通用模板，
提示词、API Key、**用哪个场景(scene)**、模型、尺寸、张数、
**输入目录(source_dir)、输出目录(output_root)** 全在 yaml 的 defaults 里配置，
加场景不用写代码。命令行参数仅作临时覆盖；yaml 配好后双击 run.bat 即可跑。

```bash
# 方式一：全 yaml 配置好（defaults.scene / source_dir / output_root）后——
cd scenes/img2img && python run.py          # 或双击 run.bat

# 方式二：命令行临时指定（优先级最高）
cd scenes/img2img
python run.py list                                   # 列出全部场景
python run.py roof_violation --source D:/data/一期屋顶图 --count 99
python run.py garbage_roadside --source D:/data/房区图

# 文生图（2 个场景：building_single / building_roof_pattern）
cd scenes/text2img
python run.py                                        # 零参数，读 defaults.scene
python run.py building_roof_pattern --count 50
```

## 输出

- `output/images/<场景>_<yymmdd_HHMMSS>/` — after 图
- `output/before/<场景>_<时间戳>/` — before 图（与 after 同名配对）
- `output/tasks/<场景>_tasks.json` — 断点状态（可随时中断续跑，历史 running 自动清理）
- 所有 output/ 不进 git

## 加新场景

打开对应目录的 `scenes.yaml`，在 `scenes:` 下复制一段，改三样东西即可：
场景名（键）、`desc`（一句话说明）、`prompt`/`base_prompt`（+ `negative_prompt`、`variants`）。
`core/` 和 `run.py` 都不用动。文生图还可调 `target_size`、`file_prefix`。

## 目录

```
core/    grsai_client.py（API 提交/查询/下载） task_state.py（断点） pipeline.py（批量循环/配对）
scenes/  text2img/ 一个 run.py + scenes.yaml（造底图）
         img2img/  一个 run.py + scenes.yaml（造变化对，14 个场景配置）
output/  运行时产物（images/ before/ tasks/，不进 git）
```

## 提示词经验（迭代教训）

- 屋顶花纹场景：模型爱生成大圆圈/无人机巢，必须写进负向词（见 scenes.yaml 的 building_roof_pattern）
- 阴影类伪变化是负样本关键，只改光照不改地物（见 scenes.yaml 的 shadow_* 系列）
- 垃圾类场景强调"最小改动原则"：禁止模型顺手新建地面/平台，否则配准失败
- 图生图统一 aspectRatio=auto 并把结果缩放回原图尺寸，保证 before/after 配准
