# -*- coding: utf-8 -*-
"""实验定义 schema 与校验（M0.2 资产注册表）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ExperimentSchemaError(ValueError):
    """experiment.yaml 校验失败。"""


@dataclass(frozen=True)
class ExperimentMeta:
    """一个实验的完整元数据。"""

    experiment_id: str
    display_name: str
    template_id: str
    description: str = ""
    author: str = ""
    version: str = "1.0.0"
    concept_ids: tuple[str, ...] = ()
    default_params: dict = field(default_factory=dict)
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "display_name": self.display_name,
            "template_id": self.template_id,
            "description": self.description,
            "author": self.author,
            "version": self.version,
            "concept_ids": list(self.concept_ids),
            "default_params": dict(self.default_params),
            "tags": list(self.tags),
        }


def parse_experiment(data: Any, *, source: str = "") -> ExperimentMeta:
    """校验 raw YAML dict → ExperimentMeta（非法抛 ExperimentSchemaError）。"""
    prefix = f"{source}: " if source else ""
    if not isinstance(data, dict):
        raise ExperimentSchemaError(f"{prefix}experiment 定义必须是映射")

    experiment_id = str(data.get("experiment_id") or "").strip()
    if not _ID_RE.match(experiment_id):
        raise ExperimentSchemaError(
            f"{prefix}experiment_id 非法: {experiment_id!r}"
        )

    display_name = str(data.get("display_name") or "").strip()
    if not display_name:
        raise ExperimentSchemaError(f"{prefix}display_name 缺失")

    template = str(data.get("template") or "").strip()
    if not template:
        raise ExperimentSchemaError(f"{prefix}template 缺失")

    concept_ids = tuple(
        str(c).strip() for c in (data.get("concept_ids") or []) if str(c).strip()
    )
    tags = tuple(str(t).strip() for t in data.get("tags") or ())
    default_params = data.get("default_params")
    if default_params is not None and not isinstance(default_params, dict):
        raise ExperimentSchemaError(f"{prefix}default_params 必须是映射")

    return ExperimentMeta(
        experiment_id=experiment_id,
        display_name=display_name,
        template_id=template,
        description=str(data.get("description") or "").strip(),
        author=str(data.get("author") or "").strip(),
        version=str(data.get("version") or "1.0.0").strip(),
        concept_ids=concept_ids,
        default_params=default_params if isinstance(default_params, dict) else {},
        tags=tags,
    )


def parse_experiment_file(path: Path) -> ExperimentMeta:
    """从 YAML 文件解析实验定义。"""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ExperimentSchemaError(f"{path}: 读取失败: {exc}") from exc
    return parse_experiment(data, source=str(path))
