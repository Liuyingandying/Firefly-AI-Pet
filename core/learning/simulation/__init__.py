"""Firefly Learning Mode M0.1 — 均匀平面波仿真核心（纯数据, 无 LLM/UI/Memory）。"""

from core.learning.simulation.runner import SimulationResult, SimulationRunner
from core.learning.simulation.schema import SimulationParams

__all__ = ["SimulationParams", "SimulationResult", "SimulationRunner"]
