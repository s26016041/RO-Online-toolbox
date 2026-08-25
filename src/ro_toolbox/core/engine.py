"""自動化引擎骨架。

目前只有狀態機，實際的偵測 / 決策 / 送鍵邏輯待實作。
設計原則：引擎不依賴任何 UI 類別，方便單獨測試。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal

from ro_toolbox.core.events import EngineState

log = logging.getLogger(__name__)


class AutomationEngine(QObject):
    state_changed = Signal(EngineState)

    def __init__(self) -> None:
        super().__init__()
        self._state = EngineState.IDLE

    @property
    def state(self) -> EngineState:
        return self._state

    def _set_state(self, state: EngineState) -> None:
        if state is self._state:
            return
        self._state = state
        log.info("引擎狀態：%s", state.label)
        self.state_changed.emit(state)

    def start(self) -> None:
        # TODO: 建立 worker、掛上 capture/vision/input 服務後啟動主迴圈
        self._set_state(EngineState.RUNNING)

    def pause(self) -> None:
        self._set_state(EngineState.PAUSED)

    def stop(self) -> None:
        # TODO: 通知 worker 停止並等待收尾
        self._set_state(EngineState.IDLE)
