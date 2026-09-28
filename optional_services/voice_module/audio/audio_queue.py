# -*- coding: utf-8 -*-
"""Firefly Audio Queue: 优先级语音播放队列 (sounddevice)。

能力:
- 排队播放: 同优先级 FIFO, 高优先级先播
- 中断当前: stop_current() 立即停止正在播放的语音
- 优先级抢占: enqueue(priority >= URGENT) 自动打断当前 + 丢弃更低优先级待播项
- 事件回调: on_event(kind, item) → 供 LCD 桌宠口型/表情联动

无音频设备环境自动降级为"模拟播放"(按音频时长 sleep), 逻辑链路仍可测试。
"""

from __future__ import annotations

import heapq
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

PRIORITY_NORMAL = 1
PRIORITY_URGENT = 10

try:
    import sounddevice as sd

    _ = sd.query_devices()  # 触发设备探测
    _SD_OK = True
except Exception as e:  # noqa: BLE001
    sd = None
    _SD_OK = False
    logger.warning("sounddevice 不可用, AudioQueue 进入模拟播放模式: %s", e)


@dataclass
class AudioItem:
    audio: np.ndarray                 # float32 mono, 已含情绪后处理
    sample_rate: int
    text: str = ""
    emotion: str = "neutral"
    priority: int = PRIORITY_NORMAL
    seq: int = 0
    session_id: str | None = None     # v1.3.1: 所属播放会话, 会话被取消后晚到即丢弃
    meta: dict = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return len(self.audio) / max(self.sample_rate, 1)


EventCallback = Callable[[str, Optional[AudioItem]], None]


class AudioQueue:
    def __init__(self, on_event: Optional[EventCallback] = None):
        self.on_event = on_event
        self._heap: List[tuple] = []          # (-priority, seq) → item
        self._seq = 0
        self._cv = threading.Condition()
        self._current: Optional[AudioItem] = None
        self._stop_flag = threading.Event()
        self._interrupt_version = 0
        self._dropped: List[AudioItem] = []
        self.cancelled_sessions: set[str] = set()   # v1.3.1: 已取消会话, 其晚到段直接丢弃
        self._worker = threading.Thread(target=self._run, name="firefly-audio-queue", daemon=True)
        self._worker.start()

    # ---------------- 对外接口 ----------------

    def enqueue(self, item: AudioItem, preempt: Optional[bool] = None) -> None:
        """入队。priority>=URGENT 默认抢占: 停当前 + 丢更低优先级待播。"""
        if item.session_id and item.session_id in self.cancelled_sessions:
            self._emit("dropped", item)      # v1.3.1: 已取消会话的晚到结果
            return
        if preempt is None:
            preempt = item.priority >= PRIORITY_URGENT
        with self._cv:
            self._seq += 1
            item.seq = self._seq
            if preempt:
                self._interrupt_version += 1   # 让 worker 跳出当前播放
                self._stop_flag.set()
                if sd is not None:
                    try:
                        sd.stop()
                    except Exception:  # noqa: BLE001
                        pass
                kept, dropped = [], []
                while self._heap:
                    _, _, it = heapq.heappop(self._heap)
                    (dropped if it.priority < item.priority else kept).append(it)
                self._heap = []
                for it in kept:
                    heapq.heappush(self._heap, (-it.priority, it.seq, it))
                self._dropped.extend(dropped)
                for it in dropped:
                    self._emit("dropped", it)
            heapq.heappush(self._heap, (-item.priority, item.seq, item))
            self._cv.notify()

    def stop_current(self) -> bool:
        """仅中断当前播放, 不清队列。

        v1.3.2: 不在此线程调用 sd.stop() — 与 worker 的 sd.wait()/close()
        并发会触发 PortAudio access violation (进程静默死亡)。
        只置标志, 由 worker 轮询到后在自己的线程里停流。
        """
        if self._current is None:
            return False
        self._interrupt_version += 1
        self._stop_flag.set()
        with self._cv:
            self._cv.notify()
        return True

    def clear(self) -> int:
        """清空待播队列 (不影响当前播放)。返回清除条数。"""
        with self._cv:
            n = len(self._heap)
            while self._heap:
                _, _, it = heapq.heappop(self._heap)
                self._dropped.append(it)
                self._emit("dropped", it)
            return n

    def cancel_session(self, session_id: str) -> None:
        """v1.3.1: 标记会话取消 — 停当前播 + 清待播 + 丢弃该会话晚到段。"""
        self.cancelled_sessions.add(session_id)
        self.stop_current()
        self.clear()

    def prune_sessions(self, keep: int = 8) -> None:
        """防集合无限增长: 只保留最近 keep 个取消会话。"""
        if len(self.cancelled_sessions) > keep:
            for sid in sorted(self.cancelled_sessions)[:-keep]:
                self.cancelled_sessions.discard(sid)

    def pause(self) -> None:
        """暂停取队 (勿扰模式): 新入队项积压, 播完当前即停。"""
        self._paused = True

    def resume(self) -> None:
        with self._cv:
            self._paused = False
            self._cv.notify()

    @property
    def paused(self) -> bool:
        return getattr(self, "_paused", False)

    def status(self) -> dict:
        with self._cv:
            cur = self._current
            return {
                "playing": None if cur is None else {"text": cur.text, "emotion": cur.emotion, "priority": cur.priority},
                "queued": len(self._heap),
                "dropped_total": len(self._dropped),
                "device": "sounddevice" if _SD_OK else "simulated(no device)",
            }

    def wait_idle(self, timeout: float = 60.0) -> bool:
        """阻塞直到播完所有队列 (测试用)。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._cv:
                if not self._heap and self._current is None:
                    return True
            time.sleep(0.05)
        return False

    # ---------------- 内部 ----------------

    def _emit(self, kind: str, item: Optional[AudioItem]) -> None:
        if self.on_event:
            try:
                self.on_event(kind, item)
            except Exception:  # noqa: BLE001  回调不阻断播放
                logger.exception("on_event 回调异常")

    def _play_blocking(self, item: AudioItem) -> None:
        version = self._interrupt_version
        self._current = item
        self._emit("start", item)
        if _SD_OK and sd is not None:
            try:
                sd.play(np.asarray(item.audio, dtype=np.float32), item.sample_rate)
                # v1.3.2: 轮询流状态代替 sd.wait() — 中断只由 worker 线程自己
                # 调 sd.stop(), 消除与外部线程并发停流导致的 access violation
                deadline = time.monotonic() + max(item.duration_s * 1.5 + 5.0, 10.0)
                while sd.get_stream().active:
                    if (self._interrupt_version != version
                            or self._stop_flag.is_set()
                            or time.monotonic() > deadline):
                        break
                    time.sleep(0.03)
                if (self._interrupt_version != version
                        or self._stop_flag.is_set()
                        or sd.get_stream().active):
                    sd.stop()   # 仅 worker 线程停止自己的流
            except Exception as e:  # noqa: BLE001
                logger.warning("播放失败, 降级模拟: %s", e)
                time.sleep(min(item.duration_s, 30.0))
        else:
            time.sleep(min(item.duration_s, 30.0))   # 模拟播放 (无设备/CI)
        # sd.stop() 后 wait() 返回; 是否被打断看版本号变化
        interrupted = self._interrupt_version != version
        self._stop_flag.clear()
        self._current = None
        self._emit("interrupted" if interrupted else "finish", item)

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._heap or getattr(self, "_paused", False):
                    self._current = None
                    self._cv.wait(timeout=0.5)
                _, _, item = heapq.heappop(self._heap)
            self._play_blocking(item)


_queue: Optional[AudioQueue] = None
_qlock = threading.Lock()


def get_audio_queue(on_event: Optional[EventCallback] = None) -> AudioQueue:
    global _queue
    with _qlock:
        if _queue is None:
            _queue = AudioQueue(on_event=on_event)
        elif on_event and _queue.on_event is None:
            _queue.on_event = on_event
        return _queue
