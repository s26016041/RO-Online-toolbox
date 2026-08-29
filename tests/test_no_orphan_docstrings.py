"""整個 `src/` 掃一遍：有沒有「孤兒說明字串」。

## 這在防什麼

實機炸過（2026-08-29，使用者按下「補水」走到商人面前）：

    AttributeError: 'RestockBot' object has no attribute '_home_count'

原因是編輯的時候把 `def _home_count(self) -> int:` 那一行吃掉了，
剩下的函式本體黏在上一個函式的尾巴，而它的說明字串就變成一個
**單獨的字串運算式**：

    def _go_back(self) -> None:
        ...
        self._say(...)
        <一個裸字串>                    ← 孤兒，本來是 _home_count 的說明
        if not self._home_item:
            return 0

Python 完全接受這種寫法（一個沒有副作用的運算式而已），ruff 也不抓，
1147 個測試照樣全綠 —— 直到使用者走到商人面前才炸。而且壞掉的那一整段
**跟著上一個函式一起執行**，所以症狀還可能是「順序莫名其妙」而不是崩潰。

## 判準

合法的裸字串只有兩種：

1. 模組／類別／函式的**第一個**運算式 —— 正常的說明字串。
2. 緊接在賦值後面的一句 —— 屬性說明字串（PEP 224 風格，這個 repo 在用，
   例：`WALKABLE_TYPES = frozenset({0})` 下面那句）。

其他位置的獨立字串運算式一律當 bug。真的要寫長註解請用 `#`。
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


def _is_str_expr(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _orphans_in_body(body: list[ast.stmt], *, allow_first: bool) -> list[int]:
    """這一串陳述句裡有幾個孤兒字串。`allow_first` = 這裡容得下說明字串。"""
    bad = []
    for index, node in enumerate(body):
        if not _is_str_expr(node):
            continue
        if index == 0 and allow_first:
            continue                                  # 說明字串
        previous = body[index - 1] if index else None
        if isinstance(previous, (ast.Assign, ast.AnnAssign)):
            continue                                  # 屬性說明字串
        bad.append(node.lineno)
    return bad


def _orphans(path: pathlib.Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = []
    for node in ast.walk(tree):
        holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        if isinstance(node, holders):
            bad += _orphans_in_body(node.body, allow_first=True)
            continue
        # if / for / while / with / try：這些裡面沒有說明字串這回事
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                bad += _orphans_in_body(block, allow_first=False)
    return sorted(set(bad))


def test_no_orphan_docstrings_anywhere_in_src():
    bad = []
    for path in sorted(SRC.rglob("*.py")):
        for line in _orphans(path):
            bad.append(f"{path.relative_to(SRC.parent)}:{line}")
    assert not bad, (
        "有孤兒說明字串 —— 多半是 `def` 那一行被吃掉了，函式本體黏在上一個"
        "函式的尾巴（實機炸過：RestockBot._home_count）：\n  " + "\n  ".join(bad)
    )


def test_the_check_actually_catches_the_real_bug():
    """把當初那個壞掉的形狀重現一次，確認這個檢查抓得到。"""
    import tempfile

    broken = (
        "def _go_back(self):\n"
        '    """走回出發時那張圖。"""\n'
        "    self._say('走回去')\n"
        '    """背包裡有幾個回程道具。"""\n'
        "    if not self._home_item:\n"
        "        return 0\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "broken.py"
        path.write_text(broken, encoding="utf-8")
        assert _orphans(path) == [4]


def test_attribute_docstrings_are_not_flagged():
    """這個 repo 到處都是 `欄位 = 值` 後面接一句說明，那是合法的。"""
    import tempfile

    fine = (
        '"""模組說明。"""\n'
        "WALKABLE_TYPES = frozenset({0})\n"
        '"""可站立的地形類型。"""\n'
        "\n"
        "class A:\n"
        '    """類別說明。"""\n'
        "    seq: int\n"
        '    """流水號。"""\n'
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "fine.py"
        path.write_text(fine, encoding="utf-8")
        assert _orphans(path) == []
