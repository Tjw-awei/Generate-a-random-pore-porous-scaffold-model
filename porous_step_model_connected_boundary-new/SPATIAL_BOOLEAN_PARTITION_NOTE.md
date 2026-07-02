# 空间分区布尔加速说明

这版是在 `porous_step_model_connected_boundary-new` 现有逻辑上做的针对性修改：

1. 不改变原来的随机孔洞生成方法；
2. 不改变孔径、孔隙率、连通/非连通等原有参数逻辑；
3. 不把 STEP 模型真的切成多个小模型；
4. 只把“布尔切孔计算任务”按空间区域拆开，逐个区域执行 Cut；
5. 如果布尔失败次数达到上限，就停止继续切孔，并导出当前已经成功切孔的模型。

## 为什么这样做

原来的做法是先生成所有孔洞，再把所有孔洞合成一个很大的 Compound，最后对原始模型执行一次整体布尔差集。

这种方法逻辑简单，但当孔洞数量很多时，OpenCascade/OCC 布尔运算会非常慢，而且中间没有明显进度，看起来像卡住。

现在的做法是：

```text
生成全局孔洞列表
→ 按孔洞中心把孔分到 nx × ny × nz 个空间区域
→ 第 1 个区域执行一次布尔
→ 第 2 个区域执行一次布尔
→ ...
→ 所有区域完成后导出一个完整 STEP/STL
```

注意：最终模型仍然是一个完整模型，不会输出多个分区小模型。

## 新增参数

在 `config.yaml` 里新增：

```yaml
use_spatial_boolean_partition: true

boolean_partition:
  nx: 2
  ny: 2
  nz: 2

max_boolean_failures: 3
export_partial_on_boolean_failure: true
```

## 参数怎么调

`boolean_partition` 控制分区数量：

- `nx: 1, ny: 1, nz: 1`：等同于不分区；
- `nx: 2, ny: 2, nz: 2`：8 个分区，推荐先用；
- `nx: 3, ny: 3, nz: 3`：27 个分区，孔很多时可以试；
- 分区太多会让每次布尔更小，但总次数更多，不一定总是更快。

`max_boolean_failures` 控制失败上限：

- 例如设为 `3`，表示只要有 3 个分区布尔失败，就停止继续布尔；
- 程序不会直接崩溃，会导出当前已经成功处理的模型。

## 运行时怎么看进度

运行：

```powershell
python src/create_porous_model.py --config config.yaml
```

日志中会显示类似：

```text
启用空间分区布尔：分区数=8，总孔数=xxx，失败上限=3
开始分区 1/8 index=(0, 0, 0)，孔数=xxx
孔工具体生成进度: 500 / 2300
完成分区 1/8 ...
开始分区 2/8 ...
```

如果某个分区失败，会显示 warning；如果失败达到上限，会停止继续切孔并导出当前模型。

## 建议初始设置

对于 100 mm 立方体、孔数量较多的情况，建议先试：

```yaml
use_spatial_boolean_partition: true
boolean_partition:
  nx: 2
  ny: 2
  nz: 2
max_boolean_failures: 3
```

如果仍然慢，可以试：

```yaml
boolean_partition:
  nx: 3
  ny: 3
  nz: 3
```

如果分区太多反而变慢，再退回 `2 × 2 × 2`。

