"""实验注册表：扫描 / 发现 / 读取实验定义（M0.2）。

搜索路径优先级（先到先得，不合并）：
1. 显式注入的 ``search_paths``
2. 仓库 ``experiments/<domain>/<experiment_id>/experiment.yaml``
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.learning.simulation.experiment_schema import (
    ExperimentMeta,
    ExperimentSchemaError,
    parse_experiment_file,
)

_DEFAULT_EXPERIMENTS_DIR = Path(__file__).resolve().parents[3] / "experiments"


class ExperimentRegistry:
    """扫描 search paths 并提供 list/get 接口（只读, 零副作用）。"""

    def __init__(self, search_paths: list[Path] | None = None):
        self._paths = [Path(p) for p in (search_paths or [_DEFAULT_EXPERIMENTS_DIR])]

    def list_experiments(self) -> list[ExperimentMeta]:
        """扫描全部 search paths，返回去重后的实验列表（按 id 排序）。"""
        seen: dict[str, ExperimentMeta] = {}
        for path in self._paths:
            if not path.is_dir():
                continue
            for yaml_path in sorted(path.rglob("experiment.yaml")):
                try:
                    meta = parse_experiment_file(yaml_path)
                    if meta.experiment_id not in seen:
                        seen[meta.experiment_id] = meta
                except ExperimentSchemaError:
                    continue  # 坏定义跳过，不断扫描
        return sorted(seen.values(), key=lambda m: m.experiment_id)

    def get_experiment(self, experiment_id: str) -> ExperimentMeta | None:
        """按 experiment_id 查找；不存在返回 None。"""
        for meta in self.list_experiments():
            if meta.experiment_id == experiment_id:
                return meta
        return None
