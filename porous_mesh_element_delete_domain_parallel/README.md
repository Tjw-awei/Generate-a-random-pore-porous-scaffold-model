# 多孔有限元模型生成：空间分区并行删单元版

本项目是在 `porous_mesh_element_delete` 基础上单独复制出来的并行版本。

原项目不覆盖，新项目位置：

```text
D:\MX\porous_mesh_element_delete_domain_parallel
```

## 核心思路

本程序不再对 STEP 做布尔减孔，而是在已经划好的 Abaqus `.inp` 网格上删除单元：

```text
读取完整 inp
→ 解析节点和单元
→ 计算每个单元中心
→ 全局生成孔洞列表
→ 按空间区域拆分单元中心
→ 多进程并行判断各分区内哪些单元落入孔洞
→ 合并所有分区 deleted_element_ids
→ 从原始 inp 中统一删除单元
→ 输出一个完整 porous_model.inp
```

重点：拆分的是“计算任务”，不是把模型真的切成多个小模型。  
最终只输出一个完整 `.inp`：

```text
output/porous_model.inp
```

## 为什么要加 halo 缓冲区

孔洞可能跨越两个空间分区边界。  
如果某个分区只看自己范围内的孔洞，边界附近单元可能漏删。

因此每个分区筛选孔洞时，会把自己的区域向外扩一圈：

```text
halo = max_pore_bounding_radius
```

这样跨区域孔洞也会被相邻分区识别到。

## 运行方法

```powershell
cd D:\MX\porous_mesh_element_delete_domain_parallel
python -m pip install -r requirements.txt
python src/create_porous_mesh.py --config config.yaml
```

## 关键配置

```yaml
use_domain_parallel: true

domain_split:
  nx: 2
  ny: 2
  nz: 2

num_workers: 8
```

含义：

| 参数 | 作用 |
|---|---|
| `use_domain_parallel` | 是否启用空间分区并行 |
| `domain_split.nx` | x 方向分区数 |
| `domain_split.ny` | y 方向分区数 |
| `domain_split.nz` | z 方向分区数 |
| `num_workers` | 并行进程数 |

例如：

```yaml
domain_split:
  nx: 3
  ny: 3
  nz: 2
num_workers: 12
```

表示总共 18 个分区，用 12 个进程并行处理。

## 大模型建议

如果模型很大，先从：

```yaml
domain_split:
  nx: 2
  ny: 2
  nz: 2
num_workers: 8
```

开始。

如果 CPU 还有余量，可以增加：

```yaml
num_workers: 12
```

或增加分区数：

```yaml
domain_split:
  nx: 3
  ny: 3
  nz: 3
```

注意：分区太多会增加任务调度和数据传输开销，不一定越多越快。

## 默认关闭大 ELSET 输出

大模型中不要写入几百万个单元编号集合，否则写文件会非常慢。

因此默认：

```yaml
write_removed_element_set: false
write_remaining_element_set: false
```

如果只是调试小模型，可以改成 true。

## 输出文件

| 文件 | 说明 |
|---|---|
| `output/porous_model.inp` | 最终完整多孔有限元模型 |
| `output/pore_parameters.csv` | 全局孔洞参数 |
| `output/report.json` | 运行报告 |
| `output/pore_size_distribution.png` | 孔径分布 |
| `output/porous_mesh.log` | 运行日志 |

`report.json` 中包含：

```text
原始单元数
删除单元数
保留单元数
目标孔隙率
实际近似孔隙率
分区数量
num_workers
每个分区处理单元数
每个分区删除单元数
总运行时间
```

## 小测试

项目中带了一个小测试：

```powershell
python src/create_porous_mesh.py --config config_test_small.yaml
```

该测试只用于验证程序能跑通，不代表真实网格精度。

