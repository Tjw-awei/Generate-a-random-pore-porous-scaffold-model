"""随机多孔 STEP 模型生成主程序。

运行命令：
    python src/create_porous_model.py --config config.yaml

采用先融合（Compound）后切削的全局一次性布尔策略，提升复杂模型生成成功率。
普通用户只需修改 config.yaml，不需要修改本文件。
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from pathlib import Path

import cadquery as cq
import numpy as np
import yaml

from geometry_utils import (
    Pore,
    bounding_box_limits,
    cut_all_pores_once,
    load_single_solid,
    pore_has_clearance,
    pore_network_is_connected,
    pore_spacing_is_valid,
)
from porosity_utils import (
    calculate_porosity,
    clean_and_check_stl,
    estimate_pore_count,
    plot_pore_distribution,
    write_pore_csv,
    write_report,
)

LOGGER = logging.getLogger("porous_model")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml", help="YAML 配置文件")
    return parser.parse_args()


def resolve_path(project_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root / path


def validate_config(cfg: dict) -> None:
    required = [
        "input_step",
        "output_step",
        "output_stl",
        "random_seed",
        "target_porosity",
        "porosity_tolerance",
        "min_pore_diameter",
        "max_pore_diameter",
        "max_pore_count",
        "max_sampling_attempts",
        "allow_overlap",
        "overlap_factor",
        "boundary_clearance",
        "hole_shape",
        "export_stl_tolerance",
    ]
    missing = [key for key in required if key not in cfg]
    if missing:
        raise ValueError(f"配置缺少字段: {', '.join(missing)}")
    if not 0 < float(cfg["target_porosity"]) < 1:
        raise ValueError("target_porosity 必须在 0 和 1 之间")
    if float(cfg["min_pore_diameter"]) <= 0:
        raise ValueError("min_pore_diameter 必须大于 0")
    if float(cfg["max_pore_diameter"]) < float(cfg["min_pore_diameter"]):
        raise ValueError("max_pore_diameter 不能小于 min_pore_diameter")
    if cfg.get("pore_diameter_distribution", "uniform") != "uniform":
        raise ValueError("第一版仅支持 uniform 孔径分布")
    if cfg["hole_shape"] not in {"sphere", "ellipsoid"}:
        raise ValueError("hole_shape 必须是 sphere 或 ellipsoid")
    if not 0 <= float(cfg["overlap_factor"]) < 1:
        raise ValueError("overlap_factor 必须在 [0, 1) 内")
    connectivity_mode = cfg.get("connectivity_mode", "connected")
    if connectivity_mode not in {"connected", "free"}:
        raise ValueError("connectivity_mode 必须是 connected 或 free")
    if connectivity_mode == "connected" and not bool(cfg["allow_overlap"]):
        raise ValueError("connectivity_mode=connected 时，allow_overlap 必须为 true")

    split = cfg.get("boolean_partition", {"nx": 1, "ny": 1, "nz": 1})
    for axis in ("nx", "ny", "nz"):
        if int(split.get(axis, 1)) < 1:
            raise ValueError(f"boolean_partition.{axis} 必须大于等于 1")
    if int(cfg.get("max_boolean_failures", 3)) < 0:
        raise ValueError("max_boolean_failures 不能小于 0")


def configure_logging(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "porous_model.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
        force=True,
    )
    return log_path


def configure_parallelism(cfg: dict) -> dict:
    enabled = bool(cfg.get("enable_occ_parallel", True))
    requested_threads = int(cfg.get("cpu_threads", 0) or 0)
    status = {
        "enable_occ_parallel": enabled,
        "requested_cpu_threads": requested_threads,
        "occ_parallel_configured": False,
        "occ_thread_pool_threads": None,
        "note": "OCC boolean may still contain serial steps; CPU usage is not guaranteed to reach 100%.",
    }

    if not enabled:
        LOGGER.info("OCC 并行布尔未启用。")
        return status

    if requested_threads > 0:
        for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
            os.environ[name] = str(requested_threads)

    try:
        from OCP.BOPAlgo import BOPAlgo_Options
        from OCP.OSD import OSD_ThreadPool

        BOPAlgo_Options.SetParallelMode_s(True)
        pool = OSD_ThreadPool.DefaultPool_s()
        if requested_threads > 0:
            pool.SetNbDefaultThreadsToLaunch(requested_threads)
        status["occ_parallel_configured"] = True
        status["occ_thread_pool_threads"] = int(pool.NbDefaultThreadsToLaunch())
        LOGGER.info("已启用 OCC 并行布尔模式；OCC 默认线程数=%s", status["occ_thread_pool_threads"])
    except Exception as exc:
        status["note"] = f"OCC parallel setup failed: {exc}"
        LOGGER.warning("OCC 并行设置失败，将继续使用默认模式: %s", exc)

    return status


def sample_pore(
    pore_id: int, rng: np.random.Generator, lower: np.ndarray, upper: np.ndarray, cfg: dict
) -> Pore:
    diameter = float(rng.uniform(cfg["min_pore_diameter"], cfg["max_pore_diameter"]))
    center = rng.uniform(lower, upper)
    scales = [1.0, 1.0, 1.0]
    if cfg["hole_shape"] == "ellipsoid":
        ranges = cfg["ellipsoid_scale_range"]
        scales = [float(rng.uniform(*ranges[axis])) for axis in ("x", "y", "z")]

    return Pore(
        pore_id=pore_id,
        x=float(center[0]),
        y=float(center[1]),
        z=float(center[2]),
        diameter=diameter,
        radius=diameter / 2.0,
        shape=cfg["hole_shape"],
        scale_x=scales[0],
        scale_y=scales[1],
        scale_z=scales[2],
    )


def random_unit_vector(rng: np.random.Generator) -> np.ndarray:
    vector = rng.normal(size=3)
    norm = float(np.linalg.norm(vector))
    if norm == 0:
        return np.array([1.0, 0.0, 0.0])
    return vector / norm


def choose_frontier_parent(existing: list[Pore], rng: np.random.Generator, sample_size: int = 200) -> Pore:
    if len(existing) <= sample_size:
        candidates = existing
    else:
        indices = rng.choice(len(existing), size=sample_size, replace=False)
        candidates = [existing[int(index)] for index in indices]

    centers = np.array([p.center for p in existing], dtype=float)
    centroid = centers.mean(axis=0)
    mean_radius = float(np.mean([p.bounding_radius for p in existing]))

    best_parent = candidates[0]
    best_score = -np.inf
    for parent in candidates:
        outward_score = float(np.linalg.norm(parent.center - centroid))
        local_density = 0
        for other in candidates:
            if other is parent:
                continue
            distance = float(np.linalg.norm(parent.center - other.center))
            if distance < 3.0 * mean_radius:
                local_density += 1

        score = outward_score - 0.25 * mean_radius * local_density
        if score > best_score:
            best_score = score
            best_parent = parent

    return best_parent


def sample_connected_frontier_pore(
    pore_id: int, rng: np.random.Generator, lower: np.ndarray, upper: np.ndarray, cfg: dict, existing: list[Pore]
) -> Pore:
    if cfg.get("connectivity_mode", "connected") == "free":
        return sample_pore(pore_id, rng, lower, upper, cfg)

    if not existing or cfg.get("connected_sampling_strategy", "frontier") == "global":
        return sample_pore(pore_id, rng, lower, upper, cfg)

    pore = sample_pore(pore_id, rng, lower, upper, cfg)
    parent = choose_frontier_parent(existing, rng)

    radii = pore.bounding_radius + parent.bounding_radius
    shallow_overlap_band = max(1e-4, 0.03 * radii)
    min_distance = radii * (1.0 - float(cfg["overlap_factor"])) + 1e-4
    max_distance = radii - shallow_overlap_band - 1e-4

    if min_distance >= max_distance:
        min_distance = radii * 0.70
        max_distance = radii * 0.95

    center = parent.center + random_unit_vector(rng) * float(rng.uniform(min_distance, max_distance))

    return Pore(
        pore_id=pore.pore_id,
        x=float(center[0]),
        y=float(center[1]),
        z=float(center[2]),
        diameter=pore.diameter,
        radius=pore.radius,
        shape=pore.shape,
        scale_x=pore.scale_x,
        scale_y=pore.scale_y,
        scale_z=pore.scale_z,
    )


def build_pore_spatial_partitions(
    pores: list[Pore],
    lower: np.ndarray,
    upper: np.ndarray,
    cfg: dict,
) -> list[dict]:
    """把全局孔洞按空间位置分到 nx × ny × nz 个区域。

    这里的“分区”只拆分布尔计算任务，不把 STEP 实体真的切成多个小模型。
    最终模型始终是一个完整实体。这样做的目的，是避免一次布尔里塞入过多球孔，
    让每次布尔面对的孔数量更少，并且日志能明确显示当前处理到哪个空间区域。
    """
    split = cfg.get("boolean_partition", {"nx": 1, "ny": 1, "nz": 1})
    nx = max(1, int(split.get("nx", 1)))
    ny = max(1, int(split.get("ny", 1)))
    nz = max(1, int(split.get("nz", 1)))
    span = np.maximum(upper - lower, 1.0e-12)

    buckets: dict[tuple[int, int, int], list[Pore]] = {
        (ix, iy, iz): [] for ix in range(nx) for iy in range(ny) for iz in range(nz)
    }

    for pore in pores:
        relative = np.clip((pore.center - lower) / span, 0.0, 0.999999999)
        ix = min(nx - 1, int(relative[0] * nx))
        iy = min(ny - 1, int(relative[1] * ny))
        iz = min(nz - 1, int(relative[2] * nz))
        buckets[(ix, iy, iz)].append(pore)

    partitions: list[dict] = []
    part_id = 0
    for ix in range(nx):
        for iy in range(ny):
            for iz in range(nz):
                part_id += 1
                dmin = lower + span * np.array([ix / nx, iy / ny, iz / nz], dtype=float)
                dmax = lower + span * np.array([(ix + 1) / nx, (iy + 1) / ny, (iz + 1) / nz], dtype=float)
                partitions.append(
                    {
                        "part_id": part_id,
                        "index": (ix, iy, iz),
                        "domain_min": dmin,
                        "domain_max": dmax,
                        "pores": buckets[(ix, iy, iz)],
                    }
                )
    return partitions


def cut_pores_by_spatial_partitions(
    model: cq.Shape,
    pores: list[Pore],
    lower: np.ndarray,
    upper: np.ndarray,
    original_volume: float,
    cfg: dict,
) -> tuple[cq.Shape, list[Pore], list[Pore], dict]:
    """按空间分区逐区执行布尔切孔。

    这比“一次性把所有孔打包后切掉”更慢一些但更稳，也能显示明确进度。
    如果连续或累计布尔失败达到 max_boolean_failures，就停止继续切孔，
    返回当前已经成功切过的模型，由主流程直接导出当前 STEP。
    """
    partitions = build_pore_spatial_partitions(pores, lower, upper, cfg)
    total_partitions = len(partitions)
    total_pores = len(pores)
    max_failures = int(cfg.get("max_boolean_failures", 3))
    stop_on_failure_limit = bool(cfg.get("export_partial_on_boolean_failure", True))

    current = model
    successful: list[Pore] = []
    failed: list[Pore] = []
    failure_count = 0
    partition_stats: list[dict] = []
    stopped_early = False
    stop_reason = ""

    LOGGER.info(
        "启用空间分区布尔：分区数=%d，总孔数=%d，失败上限=%d",
        total_partitions,
        total_pores,
        max_failures,
    )

    for part_no, part in enumerate(partitions, start=1):
        part_pores: list[Pore] = part["pores"]
        if not part_pores:
            partition_stats.append(
                {
                    "part_id": part["part_id"],
                    "index": list(part["index"]),
                    "pore_count": 0,
                    "status": "skipped_empty",
                }
            )
            LOGGER.info("分区 %d/%d 无孔，跳过。", part_no, total_partitions)
            continue

        before_volume = float(current.Volume())
        LOGGER.info(
            "开始分区 %d/%d index=%s，孔数=%d，当前体积=%.6f，当前孔隙率=%.4f",
            part_no,
            total_partitions,
            part["index"],
            len(part_pores),
            before_volume,
            (original_volume - before_volume) / original_volume,
        )

        try:
            current, succeeded_part, failed_part = cut_all_pores_once(current, part_pores)
            successful.extend(succeeded_part)
            failed.extend(failed_part)
            after_volume = float(current.Volume())
            partition_porosity = (original_volume - after_volume) / original_volume
            LOGGER.info(
                "完成分区 %d/%d：成功孔=%d，建模失败孔=%d，体积 %.6f -> %.6f，累计孔隙率=%.4f",
                part_no,
                total_partitions,
                len(succeeded_part),
                len(failed_part),
                before_volume,
                after_volume,
                partition_porosity,
            )
            partition_stats.append(
                {
                    "part_id": part["part_id"],
                    "index": list(part["index"]),
                    "pore_count": len(part_pores),
                    "status": "success",
                    "succeeded_pore_count": len(succeeded_part),
                    "failed_pore_count": len(failed_part),
                    "volume_before": before_volume,
                    "volume_after": after_volume,
                    "porosity_after": partition_porosity,
                }
            )
        except Exception as exc:
            failure_count += 1
            failed.extend(part_pores)
            LOGGER.warning(
                "分区 %d/%d 布尔失败：%s。累计布尔失败=%d/%d",
                part_no,
                total_partitions,
                exc,
                failure_count,
                max_failures,
            )
            partition_stats.append(
                {
                    "part_id": part["part_id"],
                    "index": list(part["index"]),
                    "pore_count": len(part_pores),
                    "status": "boolean_failed",
                    "error": str(exc),
                    "failure_count": failure_count,
                }
            )
            if stop_on_failure_limit and failure_count >= max_failures:
                stopped_early = True
                stop_reason = (
                    f"布尔失败次数达到上限 {failure_count}/{max_failures}，"
                    "已停止继续布尔，将导出当前已成功切孔的模型。"
                )
                LOGGER.warning(stop_reason)
                break

    status = {
        "boolean_strategy": "spatial_partition",
        "partition_count": total_partitions,
        "boolean_failure_count": failure_count,
        "max_boolean_failures": max_failures,
        "stopped_early": stopped_early,
        "stop_reason": stop_reason,
        "partition_stats": partition_stats,
    }
    return current, successful, failed, status


def run(config_path: Path) -> dict:
    # ---------- 1. 读取配置并解析输入/输出路径 ----------
    config_path = config_path.resolve()
    project_root = config_path.parent
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    validate_config(cfg)

    input_step = resolve_path(project_root, cfg["input_step"])
    output_step = resolve_path(project_root, cfg["output_step"])
    output_stl = resolve_path(project_root, cfg["output_stl"])
    output_dir = output_step.parent
    log_path = configure_logging(output_dir)
    output_stl.parent.mkdir(parents=True, exist_ok=True)
    parallel_status = configure_parallelism(cfg)

    # ---------- 2. 读取并检查原始 STEP ----------
    LOGGER.info("读取 STEP: %s", input_step)
    if not input_step.exists():
        raise FileNotFoundError(f"找不到输入 STEP: {input_step}")
    original = load_single_solid(str(input_step))
    original_volume = float(original.Volume())
    lower, upper = bounding_box_limits(original)
    LOGGER.info("原始体积: %.6f；包围盒: %s -> %s", original_volume, lower, upper)

    # ---------- 3. 运行前状态预估 ----------
    require_connected = cfg.get("connectivity_mode", "connected") == "connected"
    target_removed_vol = original_volume * float(cfg["target_porosity"])
    
    # 因为允许重叠，所以多个球体的理论体积之和会大于实际挖去的体积。
    # 我们加入一个重叠补偿系数 (compensation)，让采样体积稍微溢出。
    compensation = 1.15 if bool(cfg["allow_overlap"]) else 1.0
    target_theoretical_vol = target_removed_vol * compensation
    
    LOGGER.info("目标移除体积: %.4f，理论采样阈值: %.4f (补偿系数: %.2f)", 
                target_removed_vol, target_theoretical_vol, compensation)

    # ---------- 4. 纯坐标随机采样与几何筛选 ----------
    rng = np.random.default_rng(int(cfg["random_seed"]))
    accepted: list[Pore] = []
    attempts = 0
    next_id = 1
    current_theoretical_vol = 0.0

    while (
        len(accepted) < int(cfg["max_pore_count"])
        and attempts < int(cfg["max_sampling_attempts"])
    ):
        attempts += 1
        pore = sample_connected_frontier_pore(next_id, rng, lower, upper, cfg, accepted)

        if not pore_has_clearance(original, pore, float(cfg["boundary_clearance"])):
            continue

        if not pore_spacing_is_valid(
            pore, accepted, bool(cfg["allow_overlap"]), float(cfg["overlap_factor"]), require_connected
        ):
            continue

        accepted.append(pore)
        next_id += 1

        # 累加每个被接受孔的理论包围体积
        vol = (4/3) * math.pi * (pore.radius**3) * pore.scale_x * pore.scale_y * pore.scale_z
        current_theoretical_vol += vol

        if current_theoretical_vol >= target_theoretical_vol:
            LOGGER.info("理论孔体积已达标 (%.4f)，停止采样。共计孔数: %d", current_theoretical_vol, len(accepted))
            break

    # ---------- 5. 全局执行一次性布尔切削 ----------
    # use_spatial_boolean_partition=true 时，不再把所有孔一次性塞进一个巨大的布尔差集。
    # 程序会按孔洞中心所在空间区域分批切割：模型本身仍然是一个完整 STEP 实体，
    # 只是把“计算任务”拆小，从而减少单次 OCC 布尔面对的孔洞数量，并且日志会显示当前处理到哪个分区。
    if bool(cfg.get("use_spatial_boolean_partition", True)):
        LOGGER.info("准备执行空间分区布尔切割。")
        porous, successful, failed, boolean_status = cut_pores_by_spatial_partitions(
            original, accepted, lower, upper, original_volume, cfg
        )
    else:
        LOGGER.info("准备执行一刀切布尔。")
        porous, successful, failed = cut_all_pores_once(original, accepted)
        boolean_status = {
            "boolean_strategy": "global_once",
            "partition_count": 1,
            "boolean_failure_count": 0,
            "max_boolean_failures": int(cfg.get("max_boolean_failures", 3)),
            "stopped_early": False,
            "stop_reason": "",
            "partition_stats": [],
        }
    failed_count = len(failed)

    # ---------- 6. 计算最终孔隙率并检查 ----------
    porous_volume = float(porous.Volume())
    actual_porosity = calculate_porosity(original_volume, porous_volume)

    if boolean_status.get("stopped_early"):
        LOGGER.warning(
            "布尔失败次数达到上限，已停止继续切孔；后续将导出当前已经成功切孔的模型。原因：%s",
            boolean_status.get("stop_reason", ""),
        )
    
    target_low = cfg["target_porosity"] - cfg["porosity_tolerance"]
    if actual_porosity < target_low:
        LOGGER.warning("未达到目标容差下限 %.4f；当前 %.4f。这由于重叠损失导致，建议调高参数 target_porosity 再次运行。", target_low, actual_porosity)
    elif actual_porosity > cfg["target_porosity"] + cfg["porosity_tolerance"]:
        LOGGER.warning("孔隙率超过目标容差上限。建议降低 target_porosity 参数。")
    else:
        LOGGER.info("最终孔隙率达标: %.4f", actual_porosity)

    # ---------- 7. 导出 STEP 检查 ----------
    LOGGER.info("导出 STEP: %s", output_step)
    porous = porous.clean()
    cq.exporters.export(porous, str(output_step))
    step_roundtrip_valid = False
    step_roundtrip_relative_volume_error = None
    try:
        checked = load_single_solid(str(output_step))
        relative_error = abs(float(checked.Volume()) - porous_volume) / original_volume
        step_roundtrip_relative_volume_error = relative_error
        if relative_error > 1e-4:
            LOGGER.warning("STEP 往返体积偏差 %.3g，建议在下游软件中修复几何。", relative_error)
        else:
            step_roundtrip_valid = True
            LOGGER.info("STEP 往返导入检查通过。")
    except Exception as exc:
        LOGGER.warning("STEP 往返导入检查失败: %s", exc)

    # ---------- 8. 导出与 STL 检查 ----------
    LOGGER.info("导出 STL: %s", output_stl)
    cq.exporters.export(porous, str(output_stl), tolerance=float(cfg["export_stl_tolerance"]), angularTolerance=0.1)
    try:
        stl_check = clean_and_check_stl(output_stl)
        LOGGER.info("STL 检查: %s", stl_check)
    except Exception as exc:
        stl_check = {"stl_watertight": False, "stl_check_error": str(exc)}
        LOGGER.warning("STL 清理/闭合检查失败: %s", exc)

    # ---------- 9. 报告写入 ----------
    write_pore_csv(output_dir / "pore_centers.csv", successful)
    report = {
        "original_volume": original_volume,
        "porous_volume": porous_volume,
        "removed_volume": original_volume - porous_volume,
        "target_porosity": float(cfg["target_porosity"]),
        "actual_porosity": actual_porosity,
        "pore_count": len(successful),
        "failed_pore_count": failed_count,
        "sampling_attempts": attempts,
        "geometry_valid_before_export": bool(porous.isValid()),
        "step_roundtrip_valid": step_roundtrip_valid,
        "step_roundtrip_relative_volume_error": step_roundtrip_relative_volume_error,
        **stl_check,
        "min_pore_diameter": float(cfg["min_pore_diameter"]),
        "max_pore_diameter": float(cfg["max_pore_diameter"]),
        "random_seed": int(cfg["random_seed"]),
        "parallel_status": parallel_status,
        "boolean_status": boolean_status,
        "use_spatial_boolean_partition": bool(cfg.get("use_spatial_boolean_partition", True)),
        "accepted_pore_count_before_boolean": len(accepted),
        "log_file": str(log_path),
    }
    write_report(output_dir / "porosity_report.json", report)
    plot_pore_distribution(output_dir / "pore_size_distribution.png", successful)
    LOGGER.info("全部完成！")
    return report

if __name__ == "__main__":
    try:
        run(Path(parse_args().config))
    except Exception:
        LOGGER.exception("多孔模型生成失败")
        raise SystemExit(1)
