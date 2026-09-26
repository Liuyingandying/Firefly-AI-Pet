"""Firefly Learning Focus Extension — 学习专注能力插件。

定位：Firefly Agent 的学习域能力插件（规划 × 记忆 × 评估）。
- 规划：知识图谱 + 任务序列（LearningMAP 思想，自研实现）
- 记忆：学习者画像 profile.json + 事件日志 events.jsonl（Inno Agent L1 思想，自研实现）
- 评估：作答证据 -> 掌握度折算（确定性规则；线上可由宿主 LLM 判定）

不引入新 Agent 运行时：教学讲解与作答判定由 Firefly Agent（LLM）负责，
本插件只做结构化决策与持久化，通过 Extension API v2 接入：

- ``initialize(context)`` -> 保存宿主注入的 PluginContext，装配学习记忆/规划器/评估器
- ``start()``             -> 发布 plugin.status=READY，启动 QTimer 复习提醒
- ``open()``              -> Quick Tools 激活：展示学习状态与推荐下一步
- ``stop()``              -> 停止定时器，发布 plugin.status=OFFLINE
- ``shutdown()``          -> 落盘画像，释放资源

存储策略：
- 权威状态（profile.json / events.jsonl）写本地 data_dir（默认 ~/.firefly/learning_focus）
- ExtensionMemory 用于轻量「记得」：目标、掌握度变化等可检索事实（宿主 memory 服务）
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

from core.extension_api import (
    EXT_STATUS_OFFLINE,
    EXT_STATUS_READY,
    EXT_STATUS_WORKING,
    FireflyExtension,
    PluginContext,
)
from core.quick_tools import QuickToolManifest

from learning_focus.learner.evaluator import Evaluator
from learning_focus.learner.state import StateEngine
from learning_focus.memory.learning_memory import LearningMemory
from learning_focus.planner.knowledge_graph import AUTOMATIC_CONTROL_GRAPH, KnowledgeGraph
from learning_focus.planner.planner import LearningPlanner, summarize_state

log = logging.getLogger("firefly.quick_tools.learning_focus")

PLUGIN_ID = "learning-focus"
PLUGIN_VERSION = "0.1.0"
DEFAULT_DATA_DIR = Path.home() / ".firefly" / "learning_focus"
DATA_DIR_ENV = "LEARNING_FOCUS_DATA_DIR"
REMINDER_INTERVAL_MIN = 60  # QTimer 复习提醒周期（分钟）


class LearningFocusExtension(FireflyExtension):
    capabilities = ("learning",)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.memory: LearningMemory | None = None
        self.planner = LearningPlanner(engine=StateEngine())
        self.evaluator = Evaluator()
        self.graph: KnowledgeGraph = AUTOMATIC_CONTROL_GRAPH
        self.data_dir: Path = DEFAULT_DATA_DIR
        self._reminder_timer: QTimer | None = None

    # -- manifest -----------------------------------------------------------

    @property
    def manifest(self) -> QuickToolManifest:
        return QuickToolManifest(
            id=PLUGIN_ID,
            name="Learning Enhancements",
            description="规划 · 学习画像 · 复习提醒等附加能力（关闭后仍可使用「学习模式」）",
            icon="book",
            version=PLUGIN_VERSION,
            capabilities=self.capabilities,
            min_api="2",
            config_defaults={"data_dir": str(DEFAULT_DATA_DIR)},
            author="Firefly",
        )

    @property
    def capability(self) -> str:
        return "learning"

    @property
    def capability_note(self) -> str:
        return "规划 · 学习画像 · 复习提醒等附加能力；关闭后仍可使用「学习模式」"

    # -- lifecycle ----------------------------------------------------------

    def initialize(self, context: PluginContext) -> None:
        super().initialize(context)
        self.data_dir = Path(
            os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR
        ).expanduser()
        self.memory = LearningMemory(self.data_dir, engine=StateEngine())
        self.memory.load_profile(learner_id="default")

    def start(self) -> None:
        self.publish_status(EXT_STATUS_READY, "learning focus ready")
        self._start_reminder_timer()

    def open(self) -> None:
        """Quick Tools 激活：展示当前学习状态与推荐下一步。"""
        try:
            state = self.enter_learning()
        except Exception as exc:  # never crash the caller (Quick Tools)
            log.warning("learning focus open failed: %s", type(exc).__name__)
            self.publish_status(EXT_STATUS_OFFLINE, "学习插件暂不可用")
            self._present("Learning Focus", "学习插件暂不可用，请稍后重试。")
            return
        self.publish_status(EXT_STATUS_READY, "learning focus ready")
        self._present("Learning Focus", state.get("text", "当前无可展示状态。"))

    def stop(self) -> None:
        self._stop_reminder_timer()
        if self.memory is not None:
            self.memory.save_profile()
        self.publish_status(EXT_STATUS_OFFLINE, "learning focus stopped")

    def shutdown(self) -> None:
        """释放资源并落盘画像。"""
        self._stop_reminder_timer()
        if self.memory is not None:
            self.memory.save_profile()
        self._reminder_timer = None

    # -- capability entry points (宿主 Skill 调用) --------------------------

    def enter_learning(self, goal: str | None = None) -> dict:
        """进入学习模式：返回状态摘要 + 推荐任务（含展示文本）。"""
        memory = self._require_memory()
        summary = summarize_state(memory.profile, self.graph)
        plan = self.planner.plan(memory.profile, self.graph)
        nxt = self.planner.choose_next(plan)
        text = self._format_state_text(summary, nxt)
        return {
            "domain": self.graph.domain,
            "summary": summary,
            "plan": [item.to_dict() for item in plan],
            "next": nxt.to_dict() if nxt else None,
            "text": text,
        }

    def submit_answer(
        self,
        node_id: str,
        user_answer: str,
        expected: str,
        detail: str = "",
    ) -> dict:
        """作答评估：证据 -> 掌握度更新 -> 落盘。"""
        memory = self._require_memory()
        evidence = self.evaluator.evaluate(node_id, user_answer, expected, detail)
        result = memory.apply_evidence(evidence, graph=self.graph)
        self._remember_evidence(result)
        return result

    def review_due_items(self) -> list[dict]:
        """复习到期查询（供宿主 QTimer / 提醒逻辑调用）。"""
        memory = self._require_memory()
        due = []
        for node in self.graph.nodes.values():
            st = memory.profile.get_state(node.id)
            if st.session_count and st.status == "review_due":
                due.append({"node_id": node.id, "name": node.name, "mastery": round(st.mastery, 3)})
        return due

    # -- reminder (QTimer) --------------------------------------------------

    def _start_reminder_timer(self) -> None:
        self._stop_reminder_timer()
        timer = QTimer(self)
        timer.setInterval(REMINDER_INTERVAL_MIN * 60 * 1000)
        timer.timeout.connect(self._on_reminder_tick)
        timer.start()
        self._reminder_timer = timer

    def _stop_reminder_timer(self) -> None:
        if self._reminder_timer is not None:
            try:
                self._reminder_timer.stop()
            except RuntimeError:
                pass
            self._reminder_timer = None

    def _on_reminder_tick(self) -> None:
        """周期检查复习到期项；有到期项时经宿主记忆「记得」提醒。"""
        due = self.review_due_items()
        if not due:
            return
        memory_service = getattr(self.context, "memory", None) if self.context else None
        if memory_service is not None:
            try:
                names = "、".join(item["name"] for item in due[:5])
                memory_service.remember(
                    f"学习复习提醒：{self.graph.domain} 有到期知识点：{names}",
                    category="reminder",
                    trigger="learning_focus",
                )
            except Exception:  # memory must never break the reminder loop
                log.debug("learning focus reminder remember failed", exc_info=True)
        self.publish_status(EXT_STATUS_WORKING, f"复习提醒：{len(due)} 个知识点到期")

    # -- helpers -------------------------------------------------------------

    def _require_memory(self) -> LearningMemory:
        if self.memory is None:
            self.initialize(self.context or PluginContext(registry=None, parent=self))
        return self.memory

    def _remember_evidence(self, result: dict) -> None:
        """把掌握度变化写入宿主 ExtensionMemory（轻量事实，可检索）。"""
        memory_service = getattr(self.context, "memory", None) if self.context else None
        if memory_service is None:
            return
        try:
            memory_service.remember(
                f"学习评估：{result['node_id']} {result['outcome']} "
                f"掌握度 {result['mastery_before']:.2f} -> {result['mastery_after']:.2f}",
                category="user_fact",
                trigger="learning_focus",
            )
        except Exception:
            log.debug("learning focus remember failed", exc_info=True)

    def _format_state_text(self, summary: dict, nxt) -> str:
        lines = [
            f"领域：{self.graph.domain}",
            f"已掌握：{', '.join(summary['mastered']) or '（无）'}",
            f"薄弱：{', '.join(summary['weak']) or '（无）'}",
            f"未接触：{', '.join(summary['unseen']) or '（无）'}",
            f"复习到期：{', '.join(summary['review_due']) or '（无）'}",
        ]
        if nxt is not None:
            node = self.graph.nodes.get(nxt.target_id or nxt.node_id)
            name = node.name if node else nxt.node_id
            lines.append(f"推荐：{nxt.action}「{name}」—— {nxt.reason}")
        else:
            lines.append("推荐：当前目标已全部掌握，可设置新目标。")
        return "\n".join(lines)

    # -- UI seams (overridable in tests) -------------------------------------

    def _present(self, title: str, text: str) -> None:
        QMessageBox.information(None, title, text)


def create_plugin(parent=None) -> LearningFocusExtension:
    return LearningFocusExtension(parent)
