# 代码功能地图

普通用户只需要修改 `config.yaml`。本地图用于理解“某个功能究竟在哪段代码中”，不是要求用户修改 Python。

## 1. 总体功能地图

| 功能 | 对应文件 | 对应函数/位置 | 作用说明 | 是否需要用户修改 |
|---|---|---|---|---|
| 命令行入口 | `src/create_porous_model.py` | `parse_args()`、文件末尾 `if __name__...` | 接收 `--config` 并启动完整流程 | 一般不改 |
| 读取配置 | `src/create_porous_model.py` | `run()` 开头 | 读取 YAML、解析路径并检查参数 | 代码不改，只改 config |
| 检查配置 | `src/create_porous_model.py` | `validate_config()` | 防止目标孔隙率、孔径、形状等非法 | 一般不改 |
| 读取 STEP | `src/geometry_utils.py` | `load_single_solid()` | 导入实体、尝试融合多实体并检查体积 | 一般不改 |
| 获取包围盒 | `src/geometry_utils.py` | `bounding_box_limits()` | 提供随机中心的坐标范围 | 一般不改 |
| 保存单孔参数 | `src/geometry_utils.py` | `Pore` | 保存编号、中心、孔径、形状、缩放 | 一般不改 |
| 随机生成孔洞 | `src/create_porous_model.py` | `sample_pore()` | 随机生成中心、孔径和椭球缩放 | 通过 config 修改 |
| 判断中心在内部 | `src/geometry_utils.py` | `is_point_inside()` | OCC 点在实体内判断 | 一般不改 |
| 检查外壁距离 | `src/geometry_utils.py` | `distance_to_boundary()`、`pore_has_clearance()` | 防止孔穿出表面或留下过薄外壁 | 通过 config 修改 |
| 检查孔间重叠 | `src/geometry_utils.py` | `pore_spacing_is_valid()` | 控制孔间距、重叠和相切风险 | 通过 config 修改 |
| 生成球/椭球 | `src/geometry_utils.py` | `make_pore_shape()` | 将孔参数转换为 OCC 三维工具体 | 通过 config 修改形状 |
| 执行布尔差集 | `src/geometry_utils.py` | `cut_pore_batch()` | 批量切孔，失败时逐孔降级 | 一般不改 |
| 估算孔数量 | `src/porosity_utils.py` | `estimate_pore_count()` | 运行前估算目标所需孔数并提示风险 | 一般不改 |
| 计算实际孔隙率 | `src/porosity_utils.py` | `calculate_porosity()` | 按布尔前后真实体积计算 | 一般不改 |
| 孔隙率停止控制 | `src/create_porous_model.py` | `run()` 中 `target_low` 和 while 循环 | 达到目标减容差后停止 | 通过 config 修改 |
| 导出 STEP | `src/create_porous_model.py` | `run()` 的“导出 STEP”段 | 清理并导出实体，然后重新导入检查 | 一般不改 |
| 导出 STL | `src/create_porous_model.py` | `run()` 的“导出 STL”段 | 按公差离散并导出 | 通过 config 修改公差 |
| 清理/检查 STL | `src/porosity_utils.py` | `clean_and_check_stl()` | 删除退化面并检查各壳闭合 | 一般不改 |
| 输出孔参数 CSV | `src/porosity_utils.py` | `write_pore_csv()` | 写入成功孔中心、孔径和形状 | 一般不改 |
| 输出 JSON 报告 | `src/porosity_utils.py` | `write_report()` | 保存体积、孔隙率和检查状态 | 一般不改 |
| 绘制孔径分布 | `src/porosity_utils.py` | `plot_pore_distribution()` | 生成孔径直方图 PNG | 一般不改 |
| 记录运行日志 | `src/create_porous_model.py` | `configure_logging()` | 同时输出到屏幕和日志文件 | 一般不改 |
| 生成测试立方体 | `tests/make_cube_step.py` | 脚本主体 | 创建指定边长的 STEP 立方体 | 用命令参数，不改代码 |

## 2. 用户最关心的控制位置

| 想控制的内容 | 文件 + 函数 + 代码位置说明 | 实际应修改的配置 |
|---|---|---|
| 孔径大小 | `create_porous_model.py` → `sample_pore()`：`rng.uniform(min_pore_diameter, max_pore_diameter)` | `min_pore_diameter`、`max_pore_diameter` |
| 孔洞数量 | `create_porous_model.py` → `run()`：while 条件使用 `max_pore_count`，每个通过检查的孔加入 `accepted` | `max_pore_count`、`max_sampling_attempts` |
| 目标孔隙率 | `create_porous_model.py` → `run()`：`target_low` 和每批布尔后的停止判断 | `target_porosity`、`porosity_tolerance` |
| 是否重叠 | `geometry_utils.py` → `pore_spacing_is_valid()`：比较中心距和包络半径和 | `allow_overlap`、`overlap_factor` |
| 是否在模型内部 | `geometry_utils.py` → `is_point_inside()` 与 `pore_has_clearance()` | `boundary_clearance` |
| 布尔差集 | `geometry_utils.py` → `cut_pore_batch()`：`model.cut(combined).clean()`，失败后 `current.cut(tool)` | `boolean_batch_size` 间接控制 |
| 导出 STEP | `create_porous_model.py` → `run()` 第 7 段：`cq.exporters.export(porous, output_step)` | `output_step` |
| 导出 STL | `create_porous_model.py` → `run()` 第 8 段：带 `tolerance` 的导出 | `output_stl`、`export_stl_tolerance` |
| 生成 CSV | `porosity_utils.py` → `write_pore_csv()`；由 `run()` 第 9 段调用 | 输出目录随 `output_step` |
| 生成 JSON | `porosity_utils.py` → `write_report()`；由 `run()` 第 9 段调用 | 输出目录随 `output_step` |

## 3. 主程序执行顺序

```text
parse_args()
  ↓
run()
  ├─ validate_config()
  ├─ configure_logging()
  ├─ load_single_solid()
  ├─ bounding_box_limits()
  ├─ estimate_pore_count()
  ├─ sample_pore()
  ├─ pore_has_clearance()
  ├─ pore_spacing_is_valid()
  ├─ cut_pore_batch()
  ├─ calculate_porosity()
  ├─ 导出并复检 STEP/STL
  ├─ write_pore_csv()
  ├─ write_report()
  └─ plot_pore_distribution()
```

## 4. 三个 Python 文件的职责边界

- `create_porous_model.py`：像项目经理，决定先做什么、何时停止、输出到哪里。
- `geometry_utils.py`：像 CAD 工程师，负责实体、距离、孔形状和布尔差集。
- `porosity_utils.py`：像数据工程师，负责公式、表格、报告、图片和 STL 质量检查。

只修改 `config.yaml` 就能完成绝大多数实验。只有需要改变算法本身时才应修改 Python。

