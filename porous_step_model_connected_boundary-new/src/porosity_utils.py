"""孔隙率计算和结果文件输出工具。

本文件负责数学计算和非 STEP 输出：估算孔数、计算实际孔隙率、写 CSV/JSON、
绘制孔径分布图，以及清理并检查 STL。普通用户一般无需修改本文件。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import matplotlib

# 使用无窗口绘图后端，使程序在服务器或没有图形界面的电脑上也能保存 PNG。
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import trimesh

from geometry_utils import Pore


def expected_sphere_volume(min_diameter: float, max_diameter: float) -> float:
    """计算均匀孔径分布下单个球孔的期望体积。

    参数单位与 STEP 一致，返回体积单位是“模型单位的三次方”。该值仅用于运行前
    估算孔数，不决定最终孔隙率；最终值仍由真实布尔后的实体体积计算。
    """
    if min_diameter == max_diameter:
        mean_d3 = min_diameter**3
    else:
        # 对均匀分布 D~U(a,b) 计算 E[D^3]。
        mean_d3 = (max_diameter**4 - min_diameter**4) / (
            4.0 * (max_diameter - min_diameter)
        )
    # 球体体积 V = πD³/6。
    return math.pi * mean_d3 / 6.0


def estimate_pore_count(
    original_volume: float,
    target_porosity: float,
    min_diameter: float,
    max_diameter: float,
    shape_volume_scale: float = 1.0,
) -> int:
    """根据原模型体积、目标孔隙率和平均孔体积估算所需孔数。

    shape_volume_scale 用于近似考虑椭球三个方向缩放。该估算忽略孔洞重叠和边界
    拒绝，因此只是提示值；如果估算值超过 max_pore_count，程序会发出警告。
    """
    expected = expected_sphere_volume(min_diameter, max_diameter) * shape_volume_scale
    return max(1, math.ceil(original_volume * target_porosity / expected))


def calculate_porosity(original_volume: float, porous_volume: float) -> float:
    """根据布尔前后真实体积计算实际孔隙率。

    公式：actual_porosity = (original_volume - porous_volume) / original_volume。
    返回值被限制在 0～1，避免极小的数值误差产生负值或超过 1。
    """
    return max(0.0, min(1.0, (original_volume - porous_volume) / original_volume))


def write_pore_csv(path: Path, pores: list[Pore]) -> None:
    """把所有布尔成功的孔洞写入 pore_centers.csv。

    CSV 包含孔编号、中心坐标、基础直径、基础半径和孔形状，可用于复核或复现孔位。
    """
    columns = ["pore_id", "x", "y", "z", "diameter", "radius", "shape"]
    rows = [
        {
            "pore_id": p.pore_id,
            "x": p.x,
            "y": p.y,
            "z": p.z,
            "diameter": p.diameter,
            "radius": p.radius,
            "shape": p.shape,
        }
        for p in pores
    ]
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False)


def write_report(path: Path, report: dict) -> None:
    """以 UTF-8 JSON 格式保存孔隙率、孔数和几何检查结果。"""
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def plot_pore_distribution(path: Path, pores: list[Pore]) -> None:
    """根据成功孔洞直径绘制并保存孔径分布直方图。"""
    fig, ax = plt.subplots(figsize=(7, 4.5))
    diameters = [p.diameter for p in pores]
    if diameters:
        # 孔数少时至少使用 5 个柱，孔数多时最多使用 20 个柱。
        bins = min(20, max(5, round(math.sqrt(len(diameters)))))
        ax.hist(diameters, bins=bins, color="#3478b8", edgecolor="white")
    else:
        ax.text(0.5, 0.5, "No successful pores", ha="center", va="center")
    ax.set_xlabel("Pore diameter (model units)")
    ax.set_ylabel("Count")
    ax.set_title("Pore size distribution")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def clean_and_check_stl(path: Path) -> dict:
    """清理 STL 退化三角形，并检查每个独立壳体是否闭合。

    多孔实体的 STL 通常包含一个外壳和多个内孔壳，因此 component_count 大于 1
    是正常现象。只有所有壳体均闭合时，stl_watertight 才返回 True。
    """
    mesh = trimesh.load_mesh(path, process=False)
    before = len(mesh.faces)

    # OCC 离散偶尔会产生面积接近 0 的退化三角形，删除它们可避免误判不闭合。
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.remove_unreferenced_vertices()
    mesh.merge_vertices()
    mesh.export(path)

    components = mesh.split(only_watertight=False)
    return {
        "stl_removed_degenerate_faces": before - len(mesh.faces),
        "stl_component_count": len(components),
        "stl_watertight": bool(components)
        and all(part.is_watertight for part in components),
    }

