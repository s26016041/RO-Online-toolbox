"""鍵鼠輸入服務。

實作規劃：RO 屬於 DirectInput 遊戲，一般 SendInput 常被忽略，
預計用 pydirectinput（掃描碼）送鍵，滑鼠走 win32 SendInput。
"""

from __future__ import annotations


class InputController:
    def press(self, key: str) -> None:  # pragma: no cover - 尚未實作
        raise NotImplementedError("待實作：以 pydirectinput 送掃描碼")

    def click(self, x: int, y: int) -> None:  # pragma: no cover - 尚未實作
        raise NotImplementedError("待實作")
