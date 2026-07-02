"""通过删除有限元单元生成多孔模型。

运行方式：

    python src/create_porous_mesh.py --config config.yaml

本程序不再对 STEP 几何做布尔运算，而是处理已经划分好的有限元网格：

    完整实体 inp 网格
    → 读取节点和单元
    → 随机生成球孔/椭球孔参数
    → 计算每个单元中心
    → 判断单元中心是否落入孔洞
    → 删除孔洞内单元
    → 输出新的多孔 inp

适用场景：
    Abaqus / HyperMesh / Gmsh 已经能对完整实体划分网格；
    你希望快速得到多孔有限元模型，而不是多孔 STEP 几何。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml

LOGGER = logging.getLogger("porous_mesh")


@dataclass(frozen=True)
class Pore:
    """保存一个孔洞的参数。"""

    pore_id: int
    center: np.ndarray
    diameter: float
    radius: float
    shape: str
    scale_x: float = 1.0
    scale_y: float = 1.0
    scale_z: float = 1.0
    irregularity_strength: float = 0.0
    irregularity_seed: int = 0
    irregularity_modes: int = 4

    @property
    def bounding_radius(self) -> float:
        """返回孔洞最大包络半径，用于孔间距判断。"""
        # 对不规则孔，外边界会在基础半径附近上下扰动。
        # 这里用“最大可能外包半径”做空间筛选，避免分区并行时漏删跨分区孔洞。
        return self.radius * max(self.scale_x, self.scale_y, self.scale_z) * (1.0 + max(0.0, self.irregularity_strength))


@dataclass
class ElementBlock:
    """保存一个 Abaqus *Element 块。"""

    header: str
    element_type: str
    start_line: int
    end_line: int
    elements: list[tuple[int, list[int]]]


@dataclass
class InpMesh:
    """保存 inp 文件中的节点、单元和原始文本行。"""

    lines: list[str]
    node_block: tuple[int, int]
    nodes: dict[int, np.ndarray]
    element_blocks: list[ElementBlock]


def parse_args() -> argparse.Namespace:
    """读取命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    return parser.parse_args()


def resolve_path(project_root: Path, value: str) -> Path:
    """将配置中的相对路径转换为绝对路径。"""
    path = Path(value)
    return path if path.is_absolute() else project_root / path


def configure_logging(output_dir: Path) -> Path:
    """配置终端日志和文件日志。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "porous_mesh.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, mode="w", encoding="utf-8"),
        ],
        force=True,
    )
    return log_path


def validate_config(cfg: dict) -> None:
    """检查配置文件参数。"""
    required = [
        "input_inp",
        "output_inp",
        "output_pores_csv",
        "output_report_json",
        "random_seed",
        "target_porosity",
        "porosity_tolerance",
        "min_pore_diameter",
        "max_pore_diameter",
        "max_pore_count",
        "max_sampling_attempts",
        "hole_shape",
        "allow_overlap",
        "overlap_factor",
        "connectivity_mode",
        "delete_rule",
        "remove_unused_nodes",
    ]
    missing = [key for key in required if key not in cfg]
    if missing:
        raise ValueError(f"配置缺少字段: {', '.join(missing)}")
    if cfg["hole_shape"] not in {"sphere", "ellipsoid", "irregular"}:
        raise ValueError("hole_shape 必须是 sphere、ellipsoid 或 irregular")
    if cfg["connectivity_mode"] not in {"free", "connected"}:
        raise ValueError("connectivity_mode 必须是 free 或 connected")
    if cfg["delete_rule"] != "centroid":
        raise ValueError("第一版仅支持 delete_rule: centroid")
    if not 0 < float(cfg["target_porosity"]) < 1:
        raise ValueError("target_porosity 必须在 0 和 1 之间")
    if float(cfg["min_pore_diameter"]) <= 0:
        raise ValueError("min_pore_diameter 必须大于 0")
    if float(cfg["max_pore_diameter"]) < float(cfg["min_pore_diameter"]):
        raise ValueError("max_pore_diameter 不能小于 min_pore_diameter")
    if cfg["connectivity_mode"] == "connected" and not bool(cfg["allow_overlap"]):
        raise ValueError("connectivity_mode=connected 时必须 allow_overlap=true")
    split = cfg.get("domain_split", {"nx": 1, "ny": 1, "nz": 1})
    for axis in ("nx", "ny", "nz"):
        if int(split.get(axis, 1)) <= 0:
            raise ValueError("domain_split 中 nx/ny/nz 必须为正整数")
    if int(cfg.get("num_workers", 1)) <= 0:
        raise ValueError("num_workers 必须为正整数")
    if not 0 <= float(cfg.get("irregularity_strength", 0.25)) < 0.8:
        raise ValueError("irregularity_strength 建议在 [0, 0.8) 范围内")
    if int(cfg.get("irregularity_modes", 4)) <= 0:
        raise ValueError("irregularity_modes 必须为正整数")


def is_keyword(line: str) -> bool:
    """判断 inp 行是否为 Abaqus 关键字行。"""
    return line.lstrip().startswith("*")


def parse_element_type(header: str) -> str:
    """从 *Element 头部解析单元类型。"""
    for item in header.split(","):
        item = item.strip()
        if item.lower().startswith("type="):
            return item.split("=", 1)[1].strip().upper()
    return "UNKNOWN"


def parse_inp(path: Path) -> InpMesh:
    """读取 Abaqus inp 文件中的节点和单元。

    支持常见格式：
        *Node
        node_id, x, y, z

        *Element, type=C3D4
        elem_id, n1, n2, n3, n4
    """
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    nodes: dict[int, np.ndarray] = {}
    element_blocks: list[ElementBlock] = []
    node_block: tuple[int, int] | None = None

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        lower = stripped.lower()

        if lower.startswith("*node"):
            start = i
            i += 1
            while i < len(lines) and not is_keyword(lines[i]):
                line = lines[i].strip()
                if line and not line.startswith("**"):
                    parts = [p.strip() for p in line.split(",") if p.strip()]
                    if len(parts) >= 4:
                        node_id = int(parts[0])
                        nodes[node_id] = np.array(
                            [float(parts[1]), float(parts[2]), float(parts[3])],
                            dtype=float,
                        )
                i += 1
            node_block = (start, i)
            continue

        if lower.startswith("*element"):
            start = i
            header = lines[i]
            element_type = parse_element_type(header)
            elements: list[tuple[int, list[int]]] = []
            i += 1
            while i < len(lines) and not is_keyword(lines[i]):
                line = lines[i].strip()
                if line and not line.startswith("**"):
                    parts = [p.strip() for p in line.split(",") if p.strip()]
                    if len(parts) >= 2:
                        elem_id = int(parts[0])
                        conn = [int(p) for p in parts[1:]]
                        elements.append((elem_id, conn))
                i += 1
            element_blocks.append(
                ElementBlock(
                    header=header,
                    element_type=element_type,
                    start_line=start,
                    end_line=i,
                    elements=elements,
                )
            )
            continue

        i += 1

    if node_block is None:
        raise ValueError("inp 中未找到 *Node 块")
    if not nodes:
        raise ValueError("inp 中未读取到节点")
    if not element_blocks:
        raise ValueError("inp 中未找到 *Element 块")

    return InpMesh(lines=lines, node_block=node_block, nodes=nodes, element_blocks=element_blocks)


def estimate_element_measure(coords: np.ndarray) -> float:
    """估算单元体积。

    对四面体 C3D4 使用精确体积；
    对其他实体单元用节点包围盒体积近似，仅用于孔隙率估算。
    删除判断仍然基于单元中心。
    """
    if len(coords) == 4:
        a, b, c, d = coords
        return abs(float(np.dot(b - a, np.cross(c - a, d - a)))) / 6.0
    lower = coords.min(axis=0)
    upper = coords.max(axis=0)
    volume = float(np.prod(upper - lower))
    return max(volume, 0.0)


def flatten_elements(mesh: InpMesh) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[tuple[int, int]]]:
    """把所有单元展开，计算单元中心和近似体积。

    返回：
        element_ids: 单元编号数组
        centroids: 单元中心坐标
        volumes: 单元体积或近似体积
        block_map: 每个单元对应的 (block_index, local_index)
    """
    element_ids: list[int] = []
    centroids: list[np.ndarray] = []
    volumes: list[float] = []
    block_map: list[tuple[int, int]] = []

    for block_index, block in enumerate(mesh.element_blocks):
        for local_index, (elem_id, conn) in enumerate(block.elements):
            try:
                coords = np.array([mesh.nodes[nid] for nid in conn], dtype=float)
            except KeyError as exc:
                raise ValueError(f"单元 {elem_id} 引用了不存在的节点 {exc}") from exc
            element_ids.append(elem_id)
            centroids.append(coords.mean(axis=0))
            volumes.append(estimate_element_measure(coords))
            block_map.append((block_index, local_index))

    return (
        np.array(element_ids, dtype=int),
        np.vstack(centroids),
        np.array(volumes, dtype=float),
        block_map,
    )


def sample_pore(
    pore_id: int,
    rng: np.random.Generator,
    lower: np.ndarray,
    upper: np.ndarray,
    cfg: dict,
    existing: list[Pore],
) -> Pore:
    """随机生成一个孔洞。

    free 模式：孔中心在整体包围盒中均匀采样；
    connected 模式：第一个孔随机，后续孔围绕已有孔生成，保证有机会连通。
    """
    diameter = float(rng.uniform(cfg["min_pore_diameter"], cfg["max_pore_diameter"]))
    radius = diameter / 2.0
    scales = [1.0, 1.0, 1.0]
    if cfg["hole_shape"] == "ellipsoid":
        ranges = cfg["ellipsoid_scale_range"]
        scales = [float(rng.uniform(*ranges[axis])) for axis in ("x", "y", "z")]
    irregularity_strength = 0.0
    irregularity_seed = 0
    irregularity_modes = int(cfg.get("irregularity_modes", 4))
    if cfg["hole_shape"] == "irregular":
        # irregular 表示“近似指定孔径的不规则孔”：
        # 直径仍然用 min/max_pore_diameter 控制，但孔边界会按照方向产生轻微起伏。
        # 最终删除的是整颗有限元单元，所以孔壁会自然沿网格边界形成不规则形态。
        irregularity_strength = float(cfg.get("irregularity_strength", 0.25))
        irregularity_seed = int(rng.integers(1, 2_147_483_647))

    if cfg["connectivity_mode"] == "connected" and existing:
        parent = existing[int(rng.integers(0, len(existing)))]
        direction = rng.normal(size=3)
        direction /= np.linalg.norm(direction) if np.linalg.norm(direction) > 0 else 1.0
        radii = parent.bounding_radius + radius * max(scales)
        distance = float(rng.uniform(radii * (1.0 - float(cfg["overlap_factor"])) + 1e-6, radii * 0.95))
        center = parent.center + direction * distance
    else:
        center = rng.uniform(lower, upper)

    return Pore(
        pore_id=pore_id,
        center=np.array(center, dtype=float),
        diameter=diameter,
        radius=radius,
        shape=cfg["hole_shape"],
        scale_x=scales[0],
        scale_y=scales[1],
        scale_z=scales[2],
        irregularity_strength=irregularity_strength,
        irregularity_seed=irregularity_seed,
        irregularity_modes=irregularity_modes,
    )


def pore_spacing_ok(pore: Pore, existing: list[Pore], cfg: dict) -> bool:
    """检查孔洞之间是否满足重叠/不重叠要求。"""
    if not existing:
        return True

    allow_overlap = bool(cfg["allow_overlap"])
    overlap_factor = float(cfg["overlap_factor"])
    connected = False

    for other in existing:
        distance = float(np.linalg.norm(pore.center - other.center))
        radii = pore.bounding_radius + other.bounding_radius
        if allow_overlap:
            minimum = radii * (1.0 - overlap_factor)
            if distance <= minimum:
                return False
            if distance < radii:
                connected = True
        else:
            if distance <= radii:
                return False

    if cfg["connectivity_mode"] == "connected":
        return connected
    return True


def element_inside_pore(centroids: np.ndarray, pore: Pore) -> np.ndarray:
    """判断单元中心是否落入某个孔洞。

    sphere/ellipsoid:
        使用标准二次曲面方程判断，边界比较规则。

    irregular:
        先把点转换到孔洞局部归一化坐标，再根据方向计算一个扰动后的边界半径。
        这样等效孔径仍接近 diameter，但不同方向上的半径会有大有小。
        因为本程序最终删除的是整个有限元单元，所以孔壁还会沿网格单元边界自然变得不规则。
    """
    radii = np.array(
        [pore.radius * pore.scale_x, pore.radius * pore.scale_y, pore.radius * pore.scale_z],
        dtype=float,
    )
    normalized = (centroids - pore.center) / radii
    value = np.sum(normalized**2, axis=1)
    if pore.shape != "irregular" or pore.irregularity_strength <= 0:
        return value <= 1.0

    distance = np.sqrt(value)
    direction = normalized / np.maximum(distance[:, None], 1.0e-12)
    boundary = irregular_boundary_multiplier(direction, pore)
    return distance <= boundary


def irregular_boundary_multiplier(directions: np.ndarray, pore: Pore) -> np.ndarray:
    """计算不规则孔在不同方向上的边界倍率。

    返回值大约在 1±irregularity_strength 之间：
    - 大于 1：该方向孔洞略微向外鼓出；
    - 小于 1：该方向孔洞略微向内收缩。

    这个函数是确定性的：同一个 pore.irregularity_seed 会得到同一个孔形状，
    因此相同随机种子、相同配置、相同网格下结果可以重复。
    """
    local_rng = np.random.default_rng(int(pore.irregularity_seed))
    modes = max(1, int(pore.irregularity_modes))
    weights = local_rng.normal(0.0, 1.0, size=(modes, 3))
    phases = local_rng.uniform(0.0, 2.0 * math.pi, size=modes)
    frequencies = local_rng.integers(1, 5, size=modes)

    noise = np.zeros(len(directions), dtype=float)
    for mode_index in range(modes):
        projection = directions @ weights[mode_index]
        noise += np.sin(frequencies[mode_index] * projection + phases[mode_index])

    noise /= max(1, modes)
    # tanh 把极端值压住，避免局部半径过大或过小导致孔径严重偏离设置范围。
    noise = np.tanh(1.8 * noise)
    strength = float(pore.irregularity_strength)
    return np.clip(1.0 + strength * noise, 1.0 - strength, 1.0 + strength)


def generate_pores_and_deleted_elements(
    centroids: np.ndarray,
    volumes: np.ndarray,
    cfg: dict,
) -> tuple[list[Pore], np.ndarray, float]:
    """生成孔洞，并返回需要删除的单元 mask。"""
    rng = np.random.default_rng(int(cfg["random_seed"]))
    lower = centroids.min(axis=0)
    upper = centroids.max(axis=0)
    total_volume = float(volumes.sum())
    target_low = max(0.0, float(cfg["target_porosity"]) - float(cfg["porosity_tolerance"]))
    target_high = min(1.0, float(cfg["target_porosity"]) + float(cfg["porosity_tolerance"]))

    deleted = np.zeros(len(centroids), dtype=bool)
    pores: list[Pore] = []
    attempts = 0

    while attempts < int(cfg["max_sampling_attempts"]) and len(pores) < int(cfg["max_pore_count"]):
        attempts += 1
        pore = sample_pore(len(pores) + 1, rng, lower, upper, cfg, pores)
        if not pore_spacing_ok(pore, pores, cfg):
            continue

        inside = element_inside_pore(centroids, pore)
        new_delete = inside & ~deleted
        if not np.any(new_delete):
            continue

        deleted |= inside
        pores.append(pore)

        actual = float(volumes[deleted].sum() / total_volume)
        if len(pores) % 20 == 0:
            LOGGER.info("孔数=%d, 采样=%d, 近似孔隙率=%.4f", len(pores), attempts, actual)
        if target_low <= actual <= target_high or actual >= target_low:
            break

    actual_porosity = float(volumes[deleted].sum() / total_volume)
    LOGGER.info("最终孔数=%d, 采样=%d, 删除单元=%d, 近似孔隙率=%.4f", len(pores), attempts, int(deleted.sum()), actual_porosity)
    return pores, deleted, actual_porosity


def expected_pore_volume(cfg: dict) -> float:
    """估算单个孔洞的平均体积，用于先生成全局孔洞列表。

    并行分区版本的关键原则是：孔洞只在全局生成一次。
    每个分区只负责判断自己区域内的单元是否落入这些全局孔洞，
    不能在每个分区里各自随机生成孔，否则会导致孔洞重复、孔隙率不一致。
    """
    dmin = float(cfg["min_pore_diameter"])
    dmax = float(cfg["max_pore_diameter"])
    if dmin == dmax:
        mean_d3 = dmin**3
    else:
        mean_d3 = (dmax**4 - dmin**4) / (4.0 * (dmax - dmin))
    volume = math.pi * mean_d3 / 6.0
    if cfg["hole_shape"] == "ellipsoid":
        ranges = cfg["ellipsoid_scale_range"]
        scale = np.prod([np.mean(ranges[axis]) for axis in ("x", "y", "z")])
        volume *= float(scale)
    return max(volume, 1.0e-30)


def generate_global_pores(centroids: np.ndarray, volumes: np.ndarray, cfg: dict) -> list[Pore]:
    """先生成全局孔洞列表，不在每个分区内分别随机生成孔洞。"""
    rng = np.random.default_rng(int(cfg["random_seed"]))
    lower = centroids.min(axis=0)
    upper = centroids.max(axis=0)
    total_volume = float(volumes.sum())
    estimated_count = int(math.ceil(total_volume * float(cfg["target_porosity"]) / expected_pore_volume(cfg)))
    target_count = min(int(cfg["max_pore_count"]), max(1, estimated_count))

    pores: list[Pore] = []
    attempts = 0
    while attempts < int(cfg["max_sampling_attempts"]) and len(pores) < target_count:
        attempts += 1
        pore = sample_pore(len(pores) + 1, rng, lower, upper, cfg, pores)
        if not pore_spacing_ok(pore, pores, cfg):
            continue
        pores.append(pore)

    LOGGER.info("全局孔洞生成完成: 估算孔数=%d, 实际孔数=%d, 采样次数=%d", estimated_count, len(pores), attempts)
    return pores


def build_domain_tasks(
    element_ids: np.ndarray,
    centroids: np.ndarray,
    pores: list[Pore],
    cfg: dict,
) -> list[dict]:
    """按照单元中心包围盒创建空间分区任务。

    注意：这里拆分的是“计算任务”，不是拆分模型。
    原始 inp 仍然只读一次，最终也只写一个完整 porous_model.inp。
    每个子任务只返回自己负责区域中应删除的单元编号。
    """
    split = cfg.get("domain_split", {"nx": 1, "ny": 1, "nz": 1})
    nx, ny, nz = int(split.get("nx", 1)), int(split.get("ny", 1)), int(split.get("nz", 1))
    lower = centroids.min(axis=0)
    upper = centroids.max(axis=0)
    span = np.maximum(upper - lower, 1.0e-12)
    max_radius = max((p.bounding_radius for p in pores), default=0.0)

    tasks: list[dict] = []
    part_id = 0
    for ix in range(nx):
        for iy in range(ny):
            for iz in range(nz):
                part_id += 1
                dmin = lower + span * np.array([ix / nx, iy / ny, iz / nz], dtype=float)
                dmax = lower + span * np.array([(ix + 1) / nx, (iy + 1) / ny, (iz + 1) / nz], dtype=float)

                mask = np.ones(len(centroids), dtype=bool)
                for axis, (index, count) in enumerate(((ix, nx), (iy, ny), (iz, nz))):
                    if index == count - 1:
                        mask &= (centroids[:, axis] >= dmin[axis]) & (centroids[:, axis] <= dmax[axis])
                    else:
                        mask &= (centroids[:, axis] >= dmin[axis]) & (centroids[:, axis] < dmax[axis])
                local_indices = np.nonzero(mask)[0]

                # halo 缓冲区：分区筛孔时把区域向外扩 max_pore_bounding_radius。
                # 这样跨越分区边界的孔洞也会被相邻分区看到，避免漏删边界附近单元。
                halo_min = dmin - max_radius
                halo_max = dmax + max_radius
                relevant_pores = [
                    pore
                    for pore in pores
                    if np.all(pore.center + pore.bounding_radius >= halo_min)
                    and np.all(pore.center - pore.bounding_radius <= halo_max)
                ]

                tasks.append(
                    {
                        "part_id": part_id,
                        "domain_min": dmin,
                        "domain_max": dmax,
                        "element_ids": element_ids[local_indices],
                        "centroids": centroids[local_indices],
                        "pores": relevant_pores,
                    }
                )
    return tasks


def process_domain_task(task: dict) -> dict:
    """子进程执行的分区删单元判断。

    输入的是本分区的单元中心和与本分区 halo 相交的孔洞。
    输出只包含 deleted_element_ids，不写 inp，不修改全局模型。
    """
    element_ids = task["element_ids"]
    centroids = task["centroids"]
    pores = task["pores"]
    deleted = np.zeros(len(centroids), dtype=bool)
    for pore in pores:
        deleted |= element_inside_pore(centroids, pore)
    deleted_ids = element_ids[deleted].astype(int).tolist()
    return {
        "part_id": int(task["part_id"]),
        "element_count": int(len(element_ids)),
        "related_pore_count": int(len(pores)),
        "deleted_count": int(len(deleted_ids)),
        "deleted_element_ids": deleted_ids,
    }


def delete_elements_by_domain_parallel(
    element_ids: np.ndarray,
    centroids: np.ndarray,
    pores: list[Pore],
    cfg: dict,
    output_dir: Path,
) -> tuple[np.ndarray, list[dict]]:
    """空间分区并行判断删除单元。

    并行只发生在“单元中心是否落入孔洞”的计算阶段；
    Abaqus inp 模型不会被切开，也不会输出多个小模型。
    所有分区完成后，对 deleted_element_ids 做 set union，
    再统一从原 inp 中删除单元并输出一个完整 inp。
    """
    tasks = build_domain_tasks(element_ids, centroids, pores, cfg)
    num_workers = int(cfg.get("num_workers", 1))
    LOGGER.info("启用空间分区并行: 分区数=%d, num_workers=%d", len(tasks), num_workers)

    all_deleted: set[int] = set()
    partition_stats: list[dict] = []
    debug_dir = output_dir / "domain_debug"
    debug_write = bool(cfg.get("debug_write_partition_deleted_ids", False))
    if debug_write:
        debug_dir.mkdir(parents=True, exist_ok=True)

    with concurrent.futures.ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(process_domain_task, task) for task in tasks]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            deleted_ids = result.pop("deleted_element_ids")
            all_deleted.update(int(eid) for eid in deleted_ids)
            partition_stats.append(result)
            if debug_write:
                part_path = debug_dir / f"deleted_ids_part_{result['part_id']:04d}.txt"
                part_path.write_text("\n".join(str(eid) for eid in deleted_ids), encoding="utf-8")
            LOGGER.info(
                "分区 %d 完成: 单元=%d, 相关孔=%d, 删除=%d",
                result["part_id"],
                result["element_count"],
                result["related_pore_count"],
                result["deleted_count"],
            )

    deleted_mask = np.isin(element_ids, np.array(sorted(all_deleted), dtype=int))
    partition_stats.sort(key=lambda item: item["part_id"])
    return deleted_mask, partition_stats


def format_node_line(node_id: int, coord: np.ndarray) -> str:
    """格式化节点行。"""
    return f"{node_id}, {coord[0]:.10g}, {coord[1]:.10g}, {coord[2]:.10g}"


def format_element_line(elem_id: int, conn: list[int]) -> str:
    """格式化单元行。"""
    return ", ".join([str(elem_id), *[str(nid) for nid in conn]])


def format_elset_lines(name: str, element_ids: list[int], line_width: int = 16) -> list[str]:
    """生成 Abaqus ELSET 文本行。"""
    lines = [f"*Elset, elset={name}"]
    for start in range(0, len(element_ids), line_width):
        lines.append(", ".join(str(eid) for eid in element_ids[start : start + line_width]))
    return lines


from collections import deque

def remove_floating_islands(
    mesh: InpMesh, deleted: np.ndarray, block_map: list[tuple[int, int]]
) -> tuple[np.ndarray, int]:
    """通过图遍历(BFS)找到网格的最大连通域，清理所有未连接的孤立单元块（孤岛）。"""
    
    # 1. 提取所有目前存活的单元索引
    surviving_indices = np.where(~deleted)[0]
    if len(surviving_indices) == 0:
        return deleted, 0

    LOGGER.info("正在构建单元邻接图以检测孤岛...")
    # 建立 node_id -> surviving_global_index 的映射，用于快速查找相邻单元
    node_to_survivors: dict[int, list[int]] = {}
    survivor_to_nodes: dict[int, list[int]] = {}

    for global_index in surviving_indices:
        block_index, local_index = block_map[global_index]
        # 获取该单元的所有节点编号
        conn = mesh.element_blocks[block_index].elements[local_index][1]
        survivor_to_nodes[global_index] = conn
        for nid in conn:
            if nid not in node_to_survivors:
                node_to_survivors[nid] = []
            node_to_survivors[nid].append(global_index)

    LOGGER.info("开始执行连通域搜索(BFS)...")
    visited = set()
    components: list[list[int]] = []

    # 2. 遍历所有存活单元，划分子图
    for start_idx in surviving_indices:
        if start_idx in visited:
            continue

        # 发现一个新的连通块
        current_component = []
        queue = deque([start_idx])
        visited.add(start_idx)

        while queue:
            curr_idx = queue.popleft()
            current_component.append(curr_idx)

            # 遍历该单元的所有节点，找到共享这些节点的“邻居单元”
            for nid in survivor_to_nodes[curr_idx]:
                for neighbor_idx in node_to_survivors[nid]:
                    if neighbor_idx not in visited:
                        visited.add(neighbor_idx)
                        queue.append(neighbor_idx)

        components.append(current_component)

    # 3. 按包含的单元数量排序，提取最大连通域
    components.sort(key=len, reverse=True)
    largest_component = components[0]
    largest_set = set(largest_component)

    # 4. 将不在最大连通域内的所有零碎单元，全部追加标记为“删除”
    island_count = 0
    for global_index in surviving_indices:
        if global_index not in largest_set:
            deleted[global_index] = True
            island_count += 1

    return deleted, island_count

def write_filtered_inp(
    mesh: InpMesh,
    output_path: Path,
    deleted: np.ndarray,
    block_map: list[tuple[int, int]],
    cfg: dict,
) -> tuple[list[int], list[int]]:
    """写出删除单元后的 inp 文件。"""
    deleted_element_ids: list[int] = []
    remaining_element_ids: list[int] = []

    deleted_by_block: dict[int, set[int]] = {}
    for global_index, is_deleted in enumerate(deleted):
        block_index, local_index = block_map[global_index]
        elem_id = mesh.element_blocks[block_index].elements[local_index][0]
        if is_deleted:
            deleted_by_block.setdefault(block_index, set()).add(local_index)
            deleted_element_ids.append(elem_id)
        else:
            remaining_element_ids.append(elem_id)

    used_nodes: set[int] = set()
    for block_index, block in enumerate(mesh.element_blocks):
        deleted_local = deleted_by_block.get(block_index, set())
        for local_index, (_elem_id, conn) in enumerate(block.elements):
            if local_index not in deleted_local:
                used_nodes.update(conn)

    remove_unused_nodes = bool(cfg["remove_unused_nodes"])
    node_start, node_end = mesh.node_block
    skip_ranges = [(node_start, node_end)] + [(b.start_line, b.end_line) for b in mesh.element_blocks]

    output_lines: list[str] = []
    i = 0
    while i < len(mesh.lines):
        matched = False
        if i == node_start:
            output_lines.append(mesh.lines[node_start])
            for node_id in sorted(mesh.nodes):
                if (not remove_unused_nodes) or node_id in used_nodes:
                    output_lines.append(format_node_line(node_id, mesh.nodes[node_id]))
            i = node_end
            matched = True

        for block_index, block in enumerate(mesh.element_blocks):
            if i == block.start_line:
                output_lines.append(block.header)
                deleted_local = deleted_by_block.get(block_index, set())
                for local_index, (elem_id, conn) in enumerate(block.elements):
                    if local_index not in deleted_local:
                        output_lines.append(format_element_line(elem_id, conn))
                i = block.end_line
                matched = True
                break

        if matched:
            continue

        in_replaced_range = any(start <= i < end for start, end in skip_ranges)
        if not in_replaced_range:
            output_lines.append(mesh.lines[i])
        i += 1

    output_lines.append("** Generated by porous_mesh_element_delete")
    if bool(cfg.get("write_remaining_element_set", True)):
        output_lines.extend(format_elset_lines("POROUS_REMAINING_ELEMENTS", remaining_element_ids))
    if bool(cfg.get("write_removed_element_set", True)):
        output_lines.extend(format_elset_lines("POROUS_REMOVED_ELEMENTS", deleted_element_ids))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    return remaining_element_ids, deleted_element_ids


def write_pores_csv(path: Path, pores: list[Pore]) -> None:
    """保存孔洞参数表。"""
    rows = []
    for pore in pores:
        rows.append(
            {
                "pore_id": pore.pore_id,
                "x": pore.center[0],
                "y": pore.center[1],
                "z": pore.center[2],
                "diameter": pore.diameter,
                "radius": pore.radius,
                "shape": pore.shape,
                "scale_x": pore.scale_x,
                "scale_y": pore.scale_y,
                "scale_z": pore.scale_z,
                "irregularity_strength": pore.irregularity_strength,
                "irregularity_seed": pore.irregularity_seed,
                "irregularity_modes": pore.irregularity_modes,
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False)


def plot_distribution(path: Path, pores: list[Pore]) -> None:
    """保存孔径分布图。"""
    fig, ax = plt.subplots(figsize=(7, 4.5))
    diameters = [p.diameter for p in pores]
    if diameters:
        bins = min(20, max(5, round(math.sqrt(len(diameters)))))
        ax.hist(diameters, bins=bins, color="#3a7dbb", edgecolor="white")
    else:
        ax.text(0.5, 0.5, "No pores", ha="center", va="center")
    ax.set_xlabel("Pore diameter")
    ax.set_ylabel("Count")
    ax.set_title("Pore size distribution")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def run(config_path: Path) -> dict:
    """执行完整流程。"""
    start_time = time.perf_counter()
    config_path = config_path.resolve()
    project_root = config_path.parent
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    validate_config(cfg)

    input_inp = resolve_path(project_root, cfg["input_inp"])
    output_inp = resolve_path(project_root, cfg["output_inp"])
    output_csv = resolve_path(project_root, cfg["output_pores_csv"])
    output_report = resolve_path(project_root, cfg["output_report_json"])
    output_png = resolve_path(project_root, cfg["output_distribution_png"])
    log_path = configure_logging(output_inp.parent)

    LOGGER.info("读取完整实体 inp 网格: %s", input_inp)
    mesh = parse_inp(input_inp)
    element_ids, centroids, volumes, block_map = flatten_elements(mesh)
    LOGGER.info("节点数=%d, 单元数=%d, 单元块数=%d", len(mesh.nodes), len(element_ids), len(mesh.element_blocks))

    use_domain_parallel = bool(cfg.get("use_domain_parallel", False))
    partition_stats: list[dict] = []
    if use_domain_parallel:
        pores = generate_global_pores(centroids, volumes, cfg)
        deleted, partition_stats = delete_elements_by_domain_parallel(
            element_ids,
            centroids,
            pores,
            cfg,
            output_inp.parent,
        )
        actual_porosity = float(volumes[deleted].sum() / float(volumes.sum()))
        LOGGER.info("并行分区合并完成: 删除单元=%d, 近似孔隙率=%.4f", int(deleted.sum()), actual_porosity)
    else:
        pores, deleted, actual_porosity = generate_pores_and_deleted_elements(centroids, volumes, cfg)
# -----------------------------------------------------------------
    # --- 2. 新增：孤岛清理与孔隙率重算 ---
    LOGGER.info("准备执行孤立网格(Floating Islands)清理...")
    deleted, island_count = remove_floating_islands(mesh, deleted, block_map)
    
    if island_count > 0:
        actual_porosity = float(volumes[deleted].sum() / float(volumes.sum()))
        LOGGER.info("已成功清理 %d 个游离孤岛单元！修正后实际孔隙率=%.4f", island_count, actual_porosity)
    else:
        LOGGER.info("未发现孤立单元，模型完全连通。")
    # -----------------------------------------------------------------
    remaining_ids, deleted_ids = write_filtered_inp(mesh, output_inp, deleted, block_map, cfg)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    write_pores_csv(output_csv, pores)
    plot_distribution(output_png, pores)

    split = cfg.get("domain_split", {"nx": 1, "ny": 1, "nz": 1})
    domain_count = int(split.get("nx", 1)) * int(split.get("ny", 1)) * int(split.get("nz", 1))
    elapsed = time.perf_counter() - start_time

    report = {
        "method": "finite_element_element_deletion",
        "input_inp": str(input_inp),
        "output_inp": str(output_inp),
        "target_porosity": float(cfg["target_porosity"]),
        "actual_porosity_approx": actual_porosity,
        "original_element_count": int(len(element_ids)),
        "remaining_element_count": int(len(remaining_ids)),
        "deleted_element_count": int(len(deleted_ids)),
        "island_elements_removed": island_count,
        "domain_parallel_enabled": use_domain_parallel,
        "domain_split": {
            "nx": int(split.get("nx", 1)),
            "ny": int(split.get("ny", 1)),
            "nz": int(split.get("nz", 1)),
        },
        "domain_count": domain_count,
        "num_workers": int(cfg.get("num_workers", 1)),
        "partition_stats": partition_stats,
        "total_runtime_seconds": elapsed,
        "pore_count": len(pores),
        "min_pore_diameter": float(cfg["min_pore_diameter"]),
        "max_pore_diameter": float(cfg["max_pore_diameter"]),
        "hole_shape": cfg["hole_shape"],
        "irregularity_strength": float(cfg.get("irregularity_strength", 0.0)),
        "irregularity_modes": int(cfg.get("irregularity_modes", 4)),
        "connectivity_mode": cfg["connectivity_mode"],
        "allow_overlap": bool(cfg["allow_overlap"]),
        "random_seed": int(cfg["random_seed"]),
        "log_file": str(log_path),
    }
    output_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info("完成。输出 inp: %s", output_inp)
    LOGGER.info("报告: %s", output_report)
    return report


def main() -> None:
    """命令行入口。"""
    args = parse_args()
    run(Path(args.config))


if __name__ == "__main__":
    main()
