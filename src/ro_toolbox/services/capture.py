"""螢幕擷取服務。

實作規劃：用 mss 依視窗座標擷取，回傳 numpy BGRA 陣列。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    left: int
    top: int
    width: int
    height: int


class ScreenCapture:
    def grab(self, region: Region):  # pragma: no cover - 尚未實作
        """擷取指定區域，回傳影像陣列。"""
        raise NotImplementedError("待實作：pip install -e .[automation] 後以 mss 實作")
