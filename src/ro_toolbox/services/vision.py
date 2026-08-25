"""影像辨識服務。

實作規劃：OpenCV matchTemplate 找圖 + HSV 遮罩判血條顏色。
"""

from __future__ import annotations


class VisionEngine:
    def find_template(self, haystack, needle, threshold: float = 0.9):  # pragma: no cover
        raise NotImplementedError("待實作：cv2.matchTemplate")
