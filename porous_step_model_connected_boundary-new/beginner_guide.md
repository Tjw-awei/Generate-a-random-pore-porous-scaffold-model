# 新手操作手册

本手册假设你完全不懂 Python。请按顺序执行，不要跳步。项目目录为：

```text
D:\MX\porous_step_model
```

## 第 0 步：只做一次的软件准备

安装 Python 3.10 或 3.11。Windows 安装时勾选 `Add Python to PATH`。打开 PowerShell，检查：

```powershell
python --version
pip --version
```

进入项目并安装依赖：

```powershell
cd D:\MX\porous_step_model
pip install -r requirements.txt
```

以后每次运行不必重复安装，除非更换了 Python 环境。

## 第 1 步：准备 bone.step

准备一个 STEP 格式的骨模型。输入模型应当是封闭三维实体，不能只是没有体积的曲面外壳。建议先在原 CAD、HyperMesh 或其他几何软件中检查：

- 能否计算体积；
- 是否只有一个主要实体；
- 是否有自由边、重复面或自相交；
- 模型长度单位是否为 mm。

保留原始模型备份，不要只保存一份。

## 第 2 步：放入 input 文件夹

把骨模型复制到：

```text
D:\MX\porous_step_model\input\bone.step
```

如果原文件名是 `femur.step`，可以复制后重命名为 `bone.step`。也可以不改名，但必须同步修改 `config.yaml` 中的 `input_step`。

## 第 3 步：打开 config.yaml

用记事本、VS Code 或其他纯文本编辑器打开：

```text
D:\MX\porous_step_model\config.yaml
```

不要用 Word 编辑。YAML 对缩进敏感，特别是 `ellipsoid_scale_range` 下的 x/y/z 必须保留两个空格缩进。

确认路径：

```yaml
input_step: "input/bone.step"
output_step: "output/bone_porous.step"
output_stl: "output/bone_porous.stl"
```

## 第 4 步：设置目标孔隙率

真实骨第一次建议从 5% 开始：

```yaml
target_porosity: 0.05
porosity_tolerance: 0.01
```

这里 `0.05` 表示 5%，`0.20` 表示 20%。程序达到 `目标值 - 容差` 后即可停止，所以 5%±1% 的可接受下限是 4%。

不要第一次就设置 40%。正确顺序是：

```text
5% → 检查几何 → 10% → 检查几何 → 20% → 继续调试
```

## 第 5 步：设置孔径范围

如果骨模型单位为 mm、整体尺寸为几十到上百 mm，可以从下面开始：

```yaml
min_pore_diameter: 1.0
max_pore_diameter: 3.0
```

孔径必须明显小于骨头局部厚度。最大孔径过大会穿透薄区或导致孔位很难找到；孔径过小会产生大量短边，布尔和 Abaqus 网格都更困难。

第一次还建议设置：

```yaml
max_pore_count: 300
max_sampling_attempts: 100000
allow_overlap: false
overlap_factor: 0.10
boundary_clearance: 1.0
hole_shape: "sphere"
boolean_batch_size: 5
export_stl_tolerance: 0.10
```

## 第 6 步：运行程序

打开 PowerShell：

```powershell
cd D:\MX\porous_step_model
python src/create_porous_model.py --config config.yaml
```

运行时会不断显示：成功孔数、失败孔数、采样次数和当前孔隙率。不要在布尔运算过程中强行关闭窗口。

看到类似下面内容表示主流程结束：

```text
STEP 往返导入检查通过。
STL 检查: {... 'stl_watertight': True}
完成: {...}
```

## 第 7 步：查看输出文件

打开 `output/`，正常应有：

| 文件 | 用途 |
|---|---|
| `bone_porous.step` | 最重要的多孔实体，供 CAD/Abaqus/HyperMesh 使用 |
| `bone_porous.stl` | 三角面模型，供预览和部分网格工具使用 |
| `pore_centers.csv` | 每个成功孔的坐标和孔径 |
| `porosity_report.json` | 孔隙率和几何检查报告 |
| `pore_size_distribution.png` | 孔径分布图 |
| `porous_model.log` | 全部运行过程和错误信息 |

每次使用相同输出文件名运行都会覆盖旧结果。重要结果请另存或修改输出文件名。

## 第 8 步：检查孔隙率报告

用记事本打开 `output/porosity_report.json`，检查：

1. `actual_porosity`：实际孔隙率是否在目标范围内；
2. `pore_count`：成功孔数是否大于 0；
3. `failed_pore_count`：失败孔是否过多；
4. `geometry_valid_before_export`：应为 `true`；
5. `step_roundtrip_valid`：用于后续实体网格时应为 `true`；
6. `stl_watertight`：STL 每个壳体是否闭合。

例如：

```json
"target_porosity": 0.10,
"actual_porosity": 0.091,
"step_roundtrip_valid": true,
"stl_watertight": true
```

表示目标 10%±1%，实际 9.1%，并且几何检查通过。

## 第 9 步：导入 Abaqus / HyperMesh

### Abaqus/CAE

1. 打开 Abaqus/CAE；
2. 选择 `File → Import → Part`；
3. 选择 `output/bone_porous.step`；
4. 选择 3D、Deformable；
5. 导入后在 Part 模块检查几何；
6. 设置全局 seed；
7. 对孔周围设置局部细化；
8. 使用四面体网格并检查畸变、负体积和最小尺寸。

### HyperMesh

1. 选择 `Import Geometry`；
2. 导入 `bone_porous.step`；
3. 检查 free edges、duplicate surfaces、small edges；
4. 确认模型形成 solid；
5. 清理后生成 tetra mesh。

优先导入 STEP，不要因为 STL 看起来正常就忽略 `step_roundtrip_valid=false`。

## 第 10 步：如果失败应该怎么办

按下面顺序排查：

| 问题 | 首先怎么做 |
|---|---|
| 找不到 STEP | 检查文件是否为 `input/bone.step`，检查 config 路径 |
| 没有实体或体积 | 返回 CAD/HyperMesh，把曲面缝合为 solid |
| 达不到孔隙率 | 增大 `max_pore_count`，或适当增大孔径 |
| 很难找到孔位 | 减小孔径或 `boundary_clearance` |
| 布尔失败多 | `allow_overlap: false`，`boolean_batch_size: 5` |
| STEP 往返失败 | 不要网格；关闭重叠、增大孔径/安全距离后重做 |
| Abaqus 网格失败 | 清理短边、加厚薄壁、增大孔径并细化孔周围网格 |
| 不知道具体原因 | 打开 `output/porous_model.log`，从第一条 ERROR/WARNING 看起 |

最稳妥的恢复参数是：低孔隙率、球孔、不重叠、小批量、较大的边界安全距离。

## 附：最小立方体测试

如果不确定软件是否安装正确，先不要使用骨模型。生成 10 mm 立方体：

```powershell
cd D:\MX\porous_step_model
python tests/make_cube_step.py --size 10 --output input/cube_10mm.step
```

准备一个测试配置，把输入改为 `input/cube_10mm.step`，目标设为 5%，孔径设为 0.5～1.0 mm，然后运行主程序。正常应生成六类输出，并且 JSON 中成功孔数大于 0、STEP 往返检查通过。

