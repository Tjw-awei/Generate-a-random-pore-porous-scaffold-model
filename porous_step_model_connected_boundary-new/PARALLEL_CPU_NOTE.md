# CPU 并行版说明

本文件夹是从：

```text
D:\MX\porous_step_model_connected_boundary
```

复制出来的新版本：

```text
D:\MX\porous_step_model_connected_boundary-new
```

原文件夹没有被覆盖。

## 已增加的并行设置

在 `config.yaml` 末尾新增：

```yaml
enable_occ_parallel: true
cpu_threads: 0
```

含义：

- `enable_occ_parallel: true`：开启 OpenCascade/OCC 布尔运算内部并行模式；
- `cpu_threads: 0`：使用 OCC 默认线程数；
- `cpu_threads: 8`：手动指定最多约 8 个线程；
- `cpu_threads: 16`：手动指定最多约 16 个线程。

## 为什么 CPU 仍然可能跑不满

CadQuery 的 STEP 布尔差集不是普通数组计算，而是复杂几何拓扑计算。

即使开启 OCC 并行，仍然可能有很多串行步骤：

```text
几何相交
交线计算
拓扑重建
clean()
体积计算
STEP/STL 导出
```

所以 CPU 使用率不一定能达到 100%。这属于 OCC 布尔内核的正常现象，不是程序没运行。

## 推荐调参

先用：

```yaml
enable_occ_parallel: true
cpu_threads: 0
```

如果工作站 CPU 仍然很低，可以试：

```yaml
cpu_threads: 8
```

或者：

```yaml
cpu_threads: 16
```

如果布尔失败变多，降低：

```yaml
boolean_batch_size: 5
```

如果模型比较简单，可以试：

```yaml
boolean_batch_size: 20
```

或：

```yaml
boolean_batch_size: 50
```

## 真正提速最明显的设置

相比单纯加线程，下面这些通常更有效：

```yaml
min_pore_diameter: 更大
max_pore_diameter: 更大
max_pore_count: 更小
target_porosity: 更低
boolean_batch_size: 适中
```

原因是孔越小、孔越多，布尔拓扑越复杂。

## 运行方式

```powershell
cd D:\MX\porous_step_model_connected_boundary-new
python src/create_porous_model.py --config config.yaml
```

运行日志里如果看到类似：

```text
已启用 OCC 并行布尔模式；OCC 默认线程数=...
```

说明并行开关已经生效。

