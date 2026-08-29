"""整個 `src/` 掃一遍：呼叫共用小工具的參數對不對得上。

## 這在防什麼

實機炸過（2026-08-29，補水跑完要跳通知的時候）：

    TypeError: show_notice() takes 2 positional arguments but 3 were given

`show_notice(title, message)` 是模組層級函式，但有四個呼叫點寫成
`show_notice(self, title, message)`（像 `QMessageBox.warning(parent, ...)`
那樣多帶了 parent）。Python 要**執行到那一行**才會發現，而那一行是
「補水跑完」才會走到的路 —— 平常的測試都是 monkeypatch 掉它，
`lambda *a: None` 什麼都收，所以 1158 個測試全綠。

這裡不執行程式，只用 AST 找出呼叫點，再拿真的 `inspect.signature`
去 `bind()` 看參數個數對不對。少一個字都會被抓到。

要納入檢查的函式列在 `_WATCHED`：挑「很多地方在呼叫、簽章又容易被
誤以為要傳 parent／self」的那些。
"""

from __future__ import annotations

import ast
import inspect
import pathlib

from ro_toolbox.ui.widgets.toast import show_notice, show_toast

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

#: 名字 → 真正的函式。名字就是原始碼裡呼叫時用的那個。
_WATCHED = {
    "show_notice": show_notice,
    "show_toast": show_toast,
}


class _Sentinel:
    """`bind()` 只看數量與名字，值是什麼不重要。"""


def _bad_calls(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        target = _WATCHED.get(node.func.id)
        if target is None:
            continue
        if any(isinstance(a, ast.Starred) for a in node.args):
            continue                        # 展開的參數數不出來，跳過
        if any(k.arg is None for k in node.keywords):
            continue                        # **kwargs 同上
        args = [_Sentinel()] * len(node.args)
        kwargs = {k.arg: _Sentinel() for k in node.keywords}
        try:
            inspect.signature(target).bind(*args, **kwargs)
        except TypeError as exc:
            bad.append(f"{path.name}:{node.lineno} {node.func.id}(…) —— {exc}")
    return bad


def test_watched_helpers_are_called_with_the_right_arguments():
    bad = []
    for path in sorted(SRC.rglob("*.py")):
        bad += _bad_calls(path)
    assert not bad, (
        "參數對不上 —— 這種錯要執行到那一行才會炸，而那一行多半是"
        "「跑完才會走到」的路（實機炸過：補水跳通知）：\n  " + "\n  ".join(bad)
    )


def test_the_check_actually_catches_the_real_bug(tmp_path):
    """把當初那個寫法重現一次，確認抓得到。"""
    path = tmp_path / "broken.py"
    path.write_text(
        'show_notice(self, "補水完成", f"{who}：買好了")\n', encoding="utf-8"
    )
    bad = _bad_calls(path)
    assert len(bad) == 1 and "show_notice" in bad[0]


def test_a_correct_call_is_not_flagged(tmp_path):
    path = tmp_path / "fine.py"
    path.write_text(
        'show_notice("補水完成", f"{who}：買好了")\n'
        'show_toast("標題", "內容", seconds=3)\n',
        encoding="utf-8",
    )
    assert _bad_calls(path) == []
