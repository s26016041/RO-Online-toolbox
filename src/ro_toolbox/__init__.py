"""RO Online Toolbox — 桌面自動化工具箱。"""

__version__ = "0.5.52"

APP_NAME = "RO Online Toolbox"
ORG_NAME = "ro-toolbox"
#: Windows 工作列的身分。**工作列不看視窗圖示，看這個** ——
#: 沒設的話，用 python.exe 跑起來的視窗會被歸到 python.exe 底下，
#: 工作列顯示的是 Python 的圖示（見 app.py 的 _claim_taskbar_identity）。
#: 格式是 CompanyName.ProductName，中間不要有空白。
APP_ID = "ro-toolbox.RO-Online-Toolbox"
