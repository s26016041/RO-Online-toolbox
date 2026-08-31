"""記住「這個 NPC 傳送我用不了」，之後算路線就不要再走那條。

## 為什麼需要它

導航資料（`navi_link_tw.lub`）只說「這隻 NPC 通往哪張圖」，**沒有說有沒有前置**。
實際上有一票 NPC 傳送是要條件的：

- 伊甸園傳送師 → `moc_para01`（要先加入伊甸園）
- 新婚服務人員 → `jawaii`（要結婚）
- 各種任務解完才開的傳送

排路線時把它們算進去，角色就會走到那隻 NPC 面前然後**卡住**——
使用者原話：「有前置才能使用的 NPC 傳送也要記得把有它的路徑刪除」。

## 為什麼是「記下來」而不是寫死一張表

前置是**每個角色不一樣**的（同一隻伊甸園傳送師，入會的角色用得了、沒入會的
用不了），而且改版會加新的。寫死一張表等於猜；這裡改成**踩到才學**：
真的走過去、講不通、確定過不去了，才把那一段記起來，下次算路線直接跳過。

⚠ **鍵是角色名 ＋ 那一段的身分**（哪張圖的哪一格），不是 PID、不是路線裡的
第幾段（CLAUDE.md：存身分不存位置）。

⚠ **要踩過兩次才算數**。一次可能只是那一輪的意外（選單還沒送到、被打斷、
剛好沒認出 GID）。把偶發當成永久，會安靜地把一條**本來走得通**的路砍掉，
而使用者只會看到「怎麼繞遠路」。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from ro_toolbox.config.paths import user_data_dir

log = logging.getLogger(__name__)

_FILE_NAME = "blocked_hops.json"
#: 同一段要走不通幾次才真的封起來。
NEEDED_FAILURES = 2


@dataclass(frozen=True, slots=True)
class BlockedHop:
    """一段被封起來的 NPC 傳送。"""

    map_name: str
    x: int
    y: int
    npc: str
    to_map: str
    why: str
    fails: int

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.map_name, self.x, self.y)


def _path():
    return user_data_dir() / _FILE_NAME


def _load_all() -> dict[str, dict]:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("讀不到 %s（%s）—— 當成沒有記錄", _FILE_NAME, exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_all(data: dict) -> None:
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("存不了 %s：%s", _FILE_NAME, exc)


def _cell_key(map_name: str, x: int, y: int) -> str:
    return f"{map_name},{x},{y}"


def entries(character: str) -> list[BlockedHop]:
    """這個角色學到的全部（含還沒到門檻的）。"""
    rows = _load_all().get(character, {})
    out = []
    for key, row in rows.items() if isinstance(rows, dict) else []:
        try:
            map_name, x, y = key.rsplit(",", 2)
            out.append(BlockedHop(
                map_name=map_name, x=int(x), y=int(y),
                npc=str(row.get("npc", "")), to_map=str(row.get("to", "")),
                why=str(row.get("why", "")), fails=int(row.get("fails", 0)),
            ))
        except (ValueError, AttributeError):
            continue
    return out


def blocked(character: str) -> set[tuple[str, int, int]]:
    """算路線時要跳過的那些段。**沒到次數門檻的不算**。"""
    if not character:
        return set()
    return {row.key for row in entries(character) if row.fails >= NEEDED_FAILURES}


def remember(character: str, hop, why: str) -> int:
    """記一次「這一段走不通」。回傳累計次數。

    `hop` 是 `travel.Hop`。**只記要跟 NPC 講話的那種** ——
    走過去就會傳送的傳點失敗多半是暫時的（被卡住、走錯格），
    封起來反而會把好好的路砍掉。
    """
    if not character or not getattr(hop, "npc", ""):
        return 0
    data = _load_all()
    rows = data.setdefault(character, {})
    key = _cell_key(hop.from_map, hop.x, hop.y)
    row = rows.get(key) or {}
    fails = int(row.get("fails", 0)) + 1
    rows[key] = {
        "npc": hop.npc,
        "to": hop.to_map,
        "why": why,
        "fails": fails,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_all(data)
    if fails >= NEEDED_FAILURES:
        log.warning(
            "「%s」走不通第 %d 次（%s → %s：%s）—— 以後算路線都跳過這一段",
            hop.npc, fails, hop.from_map, hop.to_map, why,
        )
    else:
        log.info("「%s」走不通第 %d 次（%s）—— 再一次就封起來",
                 hop.npc, fails, why)
    return fails


def forget(character: str, map_name: str = "", x: int = 0, y: int = 0) -> None:
    """把學到的忘掉。不給座標就清掉這個角色的全部。

    給使用者一條退路：前置解掉之後那條路就通了，不該被舊紀錄永遠擋著。
    """
    data = _load_all()
    if character not in data:
        return
    if map_name:
        data[character].pop(_cell_key(map_name, x, y), None)
    else:
        data.pop(character, None)
    _save_all(data)
