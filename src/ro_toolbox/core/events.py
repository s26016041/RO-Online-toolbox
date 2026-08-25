"""自動化引擎對外的狀態定義。放這裡讓 UI 不必 import 引擎實作。"""

from __future__ import annotations

from enum import Enum


class EngineState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"

    @property
    def label(self) -> str:
        return {
            EngineState.IDLE: "待機",
            EngineState.RUNNING: "執行中",
            EngineState.PAUSED: "已暫停",
            EngineState.STOPPING: "停止中",
        }[self]
