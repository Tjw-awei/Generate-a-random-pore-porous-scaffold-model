"""生成一个简单立方体 STEP，供新手验证环境和程序是否正常。

示例：
    python tests/make_cube_step.py --size 10 --output input/cube_10mm.step

本脚本只创建测试输入，不参与真实骨模型的随机开孔。
"""

import argparse
from pathlib import Path

import cadquery as cq

# 项目根目录：本文件位于 tests/，所以 parents[1] 就是项目根目录。
ROOT = Path(__file__).resolve().parents[1]

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--size", type=float, default=10.0, help="立方体边长")
parser.add_argument("--output", default="input/cube_10mm.step", help="输出 STEP 路径")
args = parser.parse_args()

# 相对输出路径始终相对于项目根目录，而不是当前命令窗口所在目录。
target = ROOT / args.output
target.parent.mkdir(parents=True, exist_ok=True)

# 在 XY 工作平面上创建 size × size × size 的实体立方体并导出 STEP。
cq.exporters.export(
    cq.Workplane("XY").box(args.size, args.size, args.size).val(), str(target)
)
print(target)

