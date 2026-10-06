# -*- coding: utf-8 -*-
"""SimulationStatus — 仿真终态枚举（M0.4 v1.0 协议）。

结果载体 ``SimulationResult`` 的权威定义在 ``runner.py``（M0.1 兼容面,
png/gif/md 为 Path 字段, ``to_dict()`` 序列化为 str）。本模块只保留协议
词表, 供 runner 与上层（M1 学习编排器）引用同一组终态字符串。
"""

from __future__ import annotations

from enum import Enum


class SimulationStatus(str, Enum):
    """五种终态。"""

    COMPLETED = "completed"
    PARAM_ERROR = "param_error"
    EXECUTION_FAILED = "execution_failed"
    TIMEOUT = "timeout"
    SECURITY_REJECTED = "security_rejected"
