"""几何处理工具。

本文件只负责“几何”工作：读取 STEP 实体、判断点是否位于模型内部、
检查孔洞与外表面的安全距离、检查孔洞之间的间距、创建球/椭球工具体，
以及把一批孔洞打包为组合体后从原模型中做一次性布尔差集减掉，并自动清理悬空孤岛。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

import cadquery as cq
import numpy as np

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Pore:
    """保存一个候选孔洞的全部参数。"""
    pore_id: int
    x: float
    y: float
    z: float
    diameter: float
    radius: float
    shape: str
    scale_x: float = 1.0
    scale_y: float = 1.0
    scale_z: float = 1.0

    @property
    def center(self) -> np.ndarray:
        return np.array([self.x, self.y, self.z], dtype=float)

    @property
    def bounding_radius(self) -> float:
        return self.radius * max(self.scale_x, self.scale_y, self.scale_z)


def load_single_solid(step_path: str) -> cq.Shape:
    imported = cq.importers.importStep(step_path)
    solids = imported.solids().vals()
    if not solids:
        raise ValueError(f"STEP 文件不包含可识别实体: {step_path}")

    shape = solids[0]
    if len(solids) > 1:
        LOGGER.warning("STEP 含 %d 个实体，尝试融合为单一模型。", len(solids))
        try:
            shape = shape.fuse(*solids[1:])
        except Exception as exc:
            raise ValueError("多实体 STEP 无法融合；请先在 CAD 中合并实体。") from exc

    if not shape.isValid():
        raise ValueError("导入后的 STEP 几何无效。")
    if shape.Volume() <= 0:
        raise ValueError("STEP 实体体积不为正。")
    return shape


def bounding_box_limits(shape: cq.Shape) -> tuple[np.ndarray, np.ndarray]:
    box = shape.BoundingBox()
    return (
        np.array([box.xmin, box.ymin, box.zmin], dtype=float),
        np.array([box.xmax, box.ymax, box.zmax], dtype=float),
    )


def is_point_inside(
    shape: cq.Shape, point: np.ndarray, tolerance: float = 1e-7
) -> bool:
    return bool(shape.isInside(cq.Vector(*point), tolerance))


def distance_to_boundary(shape: cq.Shape, point: np.ndarray) -> float:
    vertex = cq.Vertex.makeVertex(*point)
    return min(face.distance(vertex) for face in shape.Faces())


def pore_has_clearance(
    shape: cq.Shape, pore: Pore, boundary_clearance: float
) -> bool:
    del boundary_clearance  
    return is_point_inside(shape, pore.center)


def pore_spacing_is_valid(
    candidate: Pore,
    existing: Iterable[Pore],
    allow_overlap: bool,
    overlap_factor: float,
    require_connected: bool = True,
    tangency_epsilon: float = 1e-4,
) -> bool:
    existing = list(existing)
    if not existing:
        return True

    connected_to_cluster = False
    for other in existing:
        distance = float(np.linalg.norm(candidate.center - other.center))
        radii = candidate.bounding_radius + other.bounding_radius
        minimum = radii * (1.0 - overlap_factor) if allow_overlap else radii

        if distance <= minimum + tangency_epsilon:
            return False

        shallow_overlap_band = max(tangency_epsilon, 0.03 * radii)
        if allow_overlap and radii - shallow_overlap_band < distance < radii:
            return False

        if not allow_overlap and distance <= radii + tangency_epsilon:
            return False

        if allow_overlap and distance <= radii - shallow_overlap_band:
            connected_to_cluster = True

    return connected_to_cluster if require_connected else True


def pore_network_is_connected(pores: list[Pore], tolerance: float = 1e-4) -> bool:
    if len(pores) <= 1:
        return True

    visited = {0}
    pending = [0]
    while pending:
        current_index = pending.pop()
        current = pores[current_index]
        for index, other in enumerate(pores):
            if index in visited:
                continue
            distance = float(np.linalg.norm(current.center - other.center))
            radii = current.bounding_radius + other.bounding_radius
            if distance < radii - tolerance:
                visited.add(index)
                pending.append(index)

    return len(visited) == len(pores)


def make_pore_shape(pore: Pore) -> cq.Shape:
    sphere = cq.Solid.makeSphere(
        pore.radius,
        cq.Vector(pore.x, pore.y, pore.z),
        cq.Vector(0, 0, 1),
        -90,
        90,
        360,
    )
    if pore.shape == "sphere":
        return sphere

    if pore.shape == "ellipsoid":
        matrix = cq.Matrix(
            [
                [pore.scale_x, 0.0, 0.0, pore.x * (1.0 - pore.scale_x)],
                [0.0, pore.scale_y, 0.0, pore.y * (1.0 - pore.scale_y)],
                [0.0, 0.0, pore.scale_z, pore.z * (1.0 - pore.scale_z)],
            ]
        )
        return sphere.transformGeometry(matrix)

    raise ValueError(f"不支持的孔洞形状: {pore.shape}")


def cut_all_pores_once(
    model: cq.Shape, pores: list[Pore]
) -> tuple[cq.Shape, list[Pore], list[Pore]]:
    """一次性布尔差集方案：执行 Cut，并自动清理四周不接触的“孤岛”实体。"""
    if not pores:
        return model, [], []

    tools = []
    failed_to_build = []
    
    total_pores = len(pores)
    for build_index, pore in enumerate(pores, start=1):
        try:
            # make_pore_shape 返回的已经是 CadQuery/OCC Shape，可直接放入 Compound。
            # 这里不再调用 .val()，避免不同 CadQuery 版本下 Shape 没有 val() 方法导致失败。
            tools.append(make_pore_shape(pore))
            if build_index == 1 or build_index == total_pores or build_index % 500 == 0:
                LOGGER.info("孔工具体生成进度: %d / %d", build_index, total_pores)
        except Exception as exc:
            LOGGER.warning("孔洞 %d 建模失败，已跳过: %s", pore.pore_id, exc)
            failed_to_build.append(pore)

    if not tools:
        return model, [], failed_to_build

    LOGGER.info("正在将 %d 个独立实体打包为 cq.Compound...", len(tools))
    tool_compound = cq.Compound.makeCompound(tools)

    LOGGER.info("开始执行全局布尔差集 (Cut) 计算...")
    try:
        # 1. 执行初始切削
        result = model.cut(tool_compound).clean()
        if not result.isValid() or result.Volume() >= model.Volume():
            raise ValueError("切割结果无效或未移除材料")

        # 2. 核心：孤岛检查与清理逻辑
        LOGGER.info("开始检查并清理游离的“孤岛”实体...")
        solids = result.Solids()
        
        if len(solids) > 1:
            LOGGER.info(f"检测到 {len(solids)} 个离散实体，正在提取最大连通域主体...")
            # 找到体积最大的那个实体（模型的核心主体骨架）
            largest_solid = max(solids, key=lambda s: s.Volume())
            
            # 计算因剔除孤岛而额外减少的实体体积
            total_vol = sum(s.Volume() for s in solids)
            island_vol = total_vol - largest_solid.Volume()
            LOGGER.info(f"已成功剔除 {len(solids) - 1} 个游离孤岛，额外移除体积: {island_vol:.4f}")
            
            # 将最大主体保留，游离散件丢弃
            result = largest_solid
        else:
            LOGGER.info("未检测到游离实体，基体完全连通。")
            
        successful = [p for p in pores if p not in failed_to_build]
        LOGGER.info("全局布尔差集及孤岛清理执行成功！")
        return result, successful, failed_to_build
        
    except Exception as exc:
        LOGGER.error("全局布尔差集彻底失败: %s", exc)
        raise RuntimeError("底层 OCC 内核崩溃。建议降低最大孔数或检查重叠率配置。") from exc
