"""跟 NPC 對話：組封包、解選單、**用文字比對挑選項**。

只送封包、不碰記憶體（CLAUDE.md：RO 掛 GameGuard，寫記憶體會被反制）。

## 整條流程（實測擷取，`封包/跟船員說話傳送到柏伊亞嵐島.txt`，2026-08-27）

    ↑ 0x0090  接觸 NPC     GID(4) + 型別(1)=1
    ↓ 0x00B4  對話文字     長度(2) + GID(4) + cp950 文字
    ↓ 0x00B5  等待輸入     GID(4)                    ← 畫面出現「下一步」
    ↑ 0x00B9  按下一步     GID(4)
    ↓ 0x00B7  選單         長度(2) + GID(4) + cp950 文字，選項用 `:` 分隔
    ↑ 0x00B8  選擇         GID(4) + 選項編號(1)      ← **從 1 開始**

實際內容（船員 GID=91）：

    [船員]
    有艘以超高速航行的船早已準備好隨時出發了，不過它不能保證大家的安全就是了!來吧!我們走!
    選單：'柏伊亞嵐島 -> 150 金幣' : '艾爾貝塔 港口-> 500金幣' : '結束' : ''

## ⛔ 絕對不准猜選項編號

選單內容是**伺服器端腳本**產生的，解包資料裡沒有（[DAT-027] 全部翻過）。
猜錯的代價是把人傳到別的島、或花掉玩家的錢 —— 正是規範說的「很有自信的錯」。

**唯一允許的做法：拿目的地的中文名去比對選項文字**，而且

- 比對前把空白全部去掉（選單寫「艾爾貝塔 港口」，我們的表寫「港都 艾爾貝塔」）；
- 我們的地圖名常有前綴（`港都 艾爾貝塔`、`衛星都市 依斯魯得島`），
  取**最後一段空白分隔的主名**來比（`艾爾貝塔`、`依斯魯得島`）；
- **剛好一個選項對得上才動手**。0 個或 2 個以上一律大聲停 —— 分不出來就不賭。
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

#: 送出：接觸 NPC。payload = GID(4) + 型別(1)
CZ_CONTACTNPC = 0x0090
#: 送出：按「下一步」。payload = GID(4)
CZ_REQ_NEXT_SCRIPT = 0x00B9
#: 送出：選單選了第幾項（**從 1 開始**）。payload = GID(4) + 編號(1)
CZ_CHOOSE_MENU = 0x00B8
#: 送出：關閉對話。payload = GID(4)
CZ_CLOSE_DIALOG = 0x0146

#: 接收：對話文字
ZC_SAY_DIALOG = 0x00B4
#: 接收：等待「下一步」
ZC_WAIT_DIALOG = 0x00B5
#: 接收：選單
ZC_MENU_LIST = 0x00B7
#: 接收：關閉對話
ZC_CLOSE_DIALOG = 0x00B6

#: 伺服器送來的文字編碼。台服是 cp950（實測擷取確認）。
TEXT_ENCODING = "cp950"

#: 接觸 NPC 的型別。實測擷取就是 1。
_CONTACT_TYPE = 1

#: **允許在「這一層沒有目的地」時點下去的選項**（多層選單用）。
#:
#: ⚠ 這張表**只准放實機擷取看過的文字**，不准憑印象加。
#: 目前的來源：`封包/卡普拉傳送到吉芬.txt`（2026-08-27）——
#: 卡普拉第一層是
#:
#:     記憶點 : 倉庫服務 : 傳送服務 : 手推車服務 : 查詢其他資訊 : 結束
#:
#: 選「傳送服務」才會進到第二層的城市清單（那一層就有目的地名了）。
#:
#: ⛔ **絕對不能放進來的**：「記憶點」（會改掉玩家的重生點）、「倉庫服務」、
#: 「手推車服務」（要錢）。這張表是白名單，沒列的一律不點 ——
#: 亂點的代價是花掉玩家的錢或改掉他的存檔點，而且是安靜地發生。
SUBMENU_OPTIONS = ("傳送服務",)

#: **只有一個目的地的 NPC，選單是「確定嗎」而不是「選去哪」時**，
#: 允許點下去的選項。
#:
#: 實機看過（2026-08-27，使用者回報的選單內容）：只通往依斯魯得島的那幾隻，
#: 選單是 `使用 : 結束` 或 `回去 : 結束` —— 沒有地名可以比對，因為根本沒得選。
#:
#: ⚠ **這條有前提**：`navi_link` 裡這隻 NPC（同一個座標）**只有一條連結**。
#: 有好幾個目的地卻跳出「確定嗎」的話，代表我們看漏了什麼，一律停手。
#: ⛔ `結束`／`取消` 永遠不准進來。
CONFIRM_OPTIONS = ("使用", "回去")

#: 「離開對話」的選項。⛔ **永遠不准選** —— 選了等於自己把對話關掉還以為成功了。
#: 也是「排除法」的依據：只通一個地方、而且**只剩一個不是離開的選項**時，
#: 那個必然就是「做這件事」，不必每遇到一個新的確認詞就回來加白名單。
EXIT_OPTIONS = ("結束", "取消", "下次再搭")

#: 一次對話最多回答幾層選單。
#:
#: 船員是**一層**（實測）。卡普拉那種「先選傳送服務、再選城市」是兩層以上，
#: 有的還會再問一次「確定嗎」。設上限是怕選單繞圈圈時無限點下去 ——
#: 超過就停手，不要一直亂點別人的 NPC。
MAX_MENUS = 4


def build_contact(gid: int) -> bytes:
    return (
        CZ_CONTACTNPC.to_bytes(2, "little")
        + gid.to_bytes(4, "little")
        + bytes([_CONTACT_TYPE])
    )


def build_next(gid: int) -> bytes:
    return CZ_REQ_NEXT_SCRIPT.to_bytes(2, "little") + gid.to_bytes(4, "little")


def build_choose(gid: int, choice: int) -> bytes:
    """選單第 `choice` 項（**從 1 開始**）。"""
    if not 1 <= choice <= 254:
        raise ValueError(f"選項編號要在 1~254，收到 {choice}")
    return (
        CZ_CHOOSE_MENU.to_bytes(2, "little")
        + gid.to_bytes(4, "little")
        + bytes([choice])
    )


def build_close(gid: int) -> bytes:
    return CZ_CLOSE_DIALOG.to_bytes(2, "little") + gid.to_bytes(4, "little")


def _text(payload: bytes, start: int) -> str:
    """把 payload 從 `start` 起的 cp950 C 字串解出來。"""
    return payload[start:].split(b"\x00")[0].decode(TEXT_ENCODING, "replace")


def parse_menu(payload: bytes) -> tuple[int, list[str]] | None:
    """解 `0x00B7`。回 (NPC GID, 選項清單)。版面不對回 None。

    ⚠ 選項用 `:` 分隔，而且**結尾通常多一個空的**（字串以 `:` 收尾）——
    那個不是選項，算進去會讓編號整個錯掉。
    """
    if len(payload) < 7:
        return None
    gid = int.from_bytes(payload[2:6], "little")
    options = [o.strip() for o in _text(payload, 6).split(":")]
    while options and not options[-1]:
        options.pop()
    if not gid or not options:
        return None
    return gid, options


def parse_say(payload: bytes) -> tuple[int, str] | None:
    """解 `0x00B4`（對話文字）。回 (NPC GID, 文字)。"""
    if len(payload) < 6:
        return None
    return int.from_bytes(payload[2:6], "little"), _text(payload, 6)


def parse_wait(payload: bytes) -> int | None:
    """`0x00B5`（等「下一步」）與 `0x00B6`（等「離開」）的 payload 都只有 GID(4)。

    回 NPC GID；讀不出來回 None。
    """
    if len(payload) < 4:
        return None
    return int.from_bytes(payload[0:4], "little") or None


def core_name(display: str) -> str:
    """地圖中文名的**主名**：去掉前綴、去掉所有空白。

    `港都 艾爾貝塔` → `艾爾貝塔`、`衛星都市 依斯魯得島` → `依斯魯得島`。
    沒有前綴的就是它自己（`柏伊亞嵐島`）。
    """
    parts = [p for p in display.replace("　", " ").split(" ") if p]
    return parts[-1] if parts else ""


def _squash(text: str) -> str:
    return text.replace("　", "").replace(" ", "")


#: 選項尾巴的價錢。**不是所有 NPC 都用 `->` 分隔** —— 實機的船長寫成
#: 「發樂斯燈塔-2800z」，只切 `->` 的話價錢會黏在地名後面，怎麼比都比不中。
#:
#: ⚠ 只在**真的長得像價錢**（數字 ＋ 幣別）時才切。單純看到 `-` 就切會誤傷
#: 名字裡本來就有連字號的地方。
_PRICE_TAIL = re.compile(r"[\s\-–—－]*\d[\d,]*\s*(?:z|zeny|金幣)\s*$", re.IGNORECASE)


def place_of(option: str) -> str:
    """選項裡的**地名部分**：切掉 `->` 之後那一段與尾巴的價錢，空白去掉。

    實機看過的長相（船員／卡普拉／船長）：

        '普隆德拉 -> 120 z'      → '普隆德拉'
        '吉芬        -> 120 z'   → '吉芬'
        '艾爾貝塔 港口-> 500金幣' → '艾爾貝塔港口'
        '柏伊亞嵐島 -> 150 金幣'  → '柏伊亞嵐島'
        '發樂斯燈塔-2800z'        → '發樂斯燈塔'   ← 沒有箭頭的那種
        '下次再搭'               → '下次再搭'
    """
    return _squash(_PRICE_TAIL.sub("", option.split("->")[0]))


#: 相似度至少要這麼像才敢點。低於這個就當成「選單裡沒有這個地方」。
#:
#: ⚠ 這是**最後一道**手段（完全相同、包含都試過了）。使用者的要求：
#: 「遊戲給的跟我們要的中文不同，那就選最像的」。但點錯的代價是真的
#: （傳到別的島、花掉他的錢），所以還要求跟第二名拉開差距 —— 兩個都像
#: 就是分不出來，寧可停手。
_FUZZY_MIN = 0.5
#: 最像的要比第二像的多這麼多，才算「分得出來」。
_FUZZY_MARGIN = 0.15


def _overlap(a: str, b: str) -> float:
    """最長共同片段占**比較短那個**的比例（0~1）。

    為什麼需要它：我們表裡的中文名常常黏著一長串前綴
    （），而選單只寫地名（）。
    整串比的話分母被前綴稀釋（0.43，比不過門檻），
    但「共同片段  占短的那個 3/4」就看得出它們在講同一個地方。

    ⚠ 兩個字以下不算 —— 中文兩個字撞在一起太容易，那不叫像。
    """
    from difflib import SequenceMatcher

    if not a or not b:
        return 0.0
    match = SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b))
    if match.size < 3:
        return 0.0
    return match.size / min(len(a), len(b))


def _similarity(place: str, full: str, core: str) -> float:
    """選項地名跟目的地中文名有多像（0~1）。取所有比法裡最像的那個。

    -  的字元比對：錯一兩個字仍然很像，完全不同的地名分數很低
      （實測「普隆德拉」vs「艾爾貝塔」= 0.0）。
    - ：我們的名字黏著前綴時，整串比會被稀釋（見上）。
    """
    from difflib import SequenceMatcher

    return max(
        SequenceMatcher(None, place, full).ratio(),
        SequenceMatcher(None, place, core).ratio(),
        _overlap(place, full),
        _overlap(place, core),
    )


def _closest(places, full, core, only=None):
    """最像的那個選項。回 (編號從 1 開始, 相似度, 跟第二名的差距)。

    不夠像、或跟第二名差不多像，一律回 None —— **分不出來就不賭**。
    """
    scored = [
        (index, _similarity(place, full, core))
        for index, place in enumerate(places, start=1)
        if place and (only is None or index in only)
    ]
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[1], reverse=True)
    index, score = scored[0]
    runner_up = scored[1][1] if len(scored) > 1 else 0.0
    margin = score - runner_up
    if score < _FUZZY_MIN or margin < _FUZZY_MARGIN:
        return None
    return index, score, margin


def _is_exit(place: str) -> bool:
    return any(word in place for word in EXIT_OPTIONS)


def _assign(places: dict[int, str], names: list[tuple[str, str]]) -> dict[int, tuple[int, float]]:
    """把**選項**跟**目的地**配對（一對一）。回 {目的地序號: (選項編號, 分數)}。

    貪婪法：全場最像的那一對先配走，兩邊都劃掉，再找下一對。
    最後**剛好各剩一個**的話就把它們配起來（分數記 0，代表是排除法配的）——
    譯名完全不同時（`gef_fild10` 我們叫「獸人村」、選單寫「吉芬野外」）
    就是靠這一步。
    """
    pairs = []
    for option, place in places.items():
        for index, (full, core) in enumerate(names):
            score = _similarity(place, full, core)
            if score > 0:
                pairs.append((score, option, index))
    pairs.sort(reverse=True)

    used_options: set[int] = set()
    used_names: set[int] = set()
    out: dict[int, tuple[int, float]] = {}
    for score, option, index in pairs:
        if option in used_options or index in used_names:
            continue
        used_options.add(option)
        used_names.add(index)
        out[index] = (option, score)

    left_options = [o for o in places if o not in used_options]
    left_names = [n for n in range(len(names)) if n not in used_names]
    if len(left_options) == 1 and len(left_names) == 1:
        out[left_names[0]] = (left_options[0], 0.0)
    return out


def pick_against(
    options: list[str], display_name: str, others: list[str]
) -> tuple[int | None, str]:
    """知道這隻 NPC **還通往哪些地方**時，用「配對」挑選項。

    ## 為什麼這比「像不像」可靠得多

    導航資料（`navi_link_tw.lub`）記得每一隻 NPC 通往哪些地圖 ——
    實機查得到：普隆德拉那隻卡普拉 @(146,89) 通往
    `alberta / gef_fild10 / geffen / izlude / morocc / payon / prt_mk`。

    所以問題不是「這個選項像不像我要去的地方」（門檻訂高會漏、訂低會亂點），
    而是「**這幾個選項分別在講哪一個目的地**」—— 候選是有限的、互相又不像，
    配出來的答案穩定得多，而且**剩下的那一個可以用排除法認出來**。

    使用者的話：「常常會不知道這個地圖傳到哪，但我們卻是知道他可以傳到
    我們要去哪張地圖」——就是這件事。

    ## 什麼時候才敢用配出來的答案

    - 我們那一格自己就夠像（`_FUZZY_MIN`）→ 直接用。
    - 自己不像，但**其他目的地每一個都配得很有把握** → 排除法，剩下的就是我們的。
    - 其他的也配不好 → 回 None，讓呼叫端停手。**分不出來就不賭。**
    """
    names = [(_squash(display_name), _squash(core_name(display_name)))]
    names += [(_squash(name), _squash(core_name(name))) for name in others]
    places = {
        index: place_of(opt) for index, opt in enumerate(options, start=1)
        if place_of(opt) and not _is_exit(place_of(opt))
    }
    if not places:
        return None, ""

    assigned = _assign(places, names)
    mine = assigned.get(0)
    if mine is None:
        return None, ""
    option, score = mine
    if score >= _FUZZY_MIN:
        return option, (
            f"第 {option} 項「{options[option - 1]}」配到「{names[0][1]}」"
            f"（{score:.0%}；這隻 NPC 通往的 {len(names)} 個地方我們查得到）"
        )
    rivals = [assigned.get(i) for i in range(1, len(names))]
    if rivals and all(hit is not None and hit[1] >= _FUZZY_MIN for hit in rivals):
        return option, (
            f"第 {option} 項「{options[option - 1]}」是**排除法**選的："
            f"這隻 NPC 的另外 {len(rivals)} 個目的地都對到別的選項了，只剩它"
        )
    return None, ""


def pick_option(
    options: list[str], display_name: str, others: list[str] | None = None
) -> tuple[int | None, str]:
    """挑出通往 `display_name` 的選項。回 (編號從 1 開始, 說明)。

    **剛好一個對得上才回編號**；0 個或 2 個以上回 None —— 分不出來就不賭
    （猜錯是把人傳到別的島或花掉他的錢）。

    ## 為什麼要兩個方向都比

    我們的地圖名跟 NPC 選單寫的**不會一模一樣**，而且差在哪兩邊都有可能：

    | 地圖 | 我們的表 | 選單寫的 |
    |---|---|---|
    | prontera | `盧恩 米德加茲王國  首都普隆德拉` | `普隆德拉 -> 120 z` |
    | alberta | `港都 艾爾貝塔` | `艾爾貝塔 港口-> 500金幣` |

    prontera 是**我們的比較長**（主名還黏著「首都」），alberta 是**選單的比較長**。
    所以先試精確相等，再試「誰包含誰」。

    ## 四層，一層比一層寬

        ① 完全相同   place == 我們的全名或主名
        ② 包含       誰包含誰都算
        ③ **比賽**   知道這隻 NPC 還通往哪些地方時：這個選項在講**哪一個**
                     目的地？候選有限又互相不像，比「像不像」可靠（見
                     `pick_against`；`others` 從 `npc_links_on_map` 來）
        ④ **最像的** difflib 相似度（使用者要求：「遊戲給的跟我們要的中文
                     不同，那就選最像的」）

    ③ 是 2026-08-31 加的。實機回報：「自動尋路跟 NPC 說話常常會不知道
    這個地圖傳到哪，但我們卻知道他可以傳到我們要去的地圖」—— 就是①②
    都落空的情況（譯名差一兩個字）。

    ⚠ 每一層都要求**分得出來**：①②要剛好一個（對到多個時用相似度在那幾個
    之中挑，差距夠大才算），③要夠像（`_FUZZY_MIN`）而且跟第二名拉開
    （`_FUZZY_MARGIN`）。兩個都像就是分不出來，停手 —— 點錯是把人傳到
    別的島、還花掉他的錢。
    """
    full = _squash(display_name)
    core = _squash(core_name(display_name))
    if not core:
        return None, "沒有可比對的地圖中文名"
    places = [place_of(opt) for opt in options]

    exact = [i for i, place in enumerate(places, start=1) if place in (full, core)]
    loose = [
        i for i, place in enumerate(places, start=1)
        if place and (core in place or place in full)
    ]
    for hits, how in ((exact, "完全相同"), (loose, "包含")):
        if len(hits) == 1:
            return hits[0], f"第 {hits[0]} 項「{options[hits[0] - 1]}」{how}於「{core}」"
        if len(hits) > 1:
            # 對到好幾個（例如選單同時有「吉芬」與「吉芬地城」）——
            # 用相似度在**這幾個之中**挑，差距夠大才算數。
            best = _closest(places, full, core, only=hits)
            if best is not None:
                index, score, margin = best
                return index, (
                    f"第 {index} 項「{options[index - 1]}」{how}於「{core}」，"
                    f"而且是 {len(hits)} 個裡最像的（{score:.0%}，"
                    f"比第二名多 {margin:.0%}）"
                )
            return None, f"「{core}」對到 {len(hits)} 個選項，分不出來：{options}"

    # ★ 知道這隻 NPC 還通往哪些地方的話，先用「比賽」——那比「像不像」可靠：
    #   候選是有限的，而且互相都不像（見 `pick_against`）。
    if others:
        index, why = pick_against(options, display_name, others)
        if index is not None:
            return index, why

    # ★ 最後才用**相似度**：遊戲寫的中文跟我們表裡的不一樣時（改版換譯名、
    #   多／少一個字），照字面比會整個落空 —— 使用者的要求是「那就選最像的」。
    best = _closest(places, full, core)
    if best is not None:
        index, score, margin = best
        return index, (
            f"第 {index} 項「{options[index - 1]}」跟「{core}」最像"
            f"（{score:.0%}，比第二名多 {margin:.0%}）"
        )
    return None, f"選單裡沒有「{core}」，也沒有夠像的：{options}"


def pick_submenu(options: list[str]) -> tuple[int | None, str]:
    """這一層沒有目的地時，挑一個「往下一層」的選項。回 (編號從 1 開始, 說明)。

    **只認 `SUBMENU_OPTIONS` 白名單**（實機擷取看過的文字），而且**只准剛好
    一個對上**。沒列的一律不點 —— 卡普拉第一層還有「記憶點」（會改掉玩家的
    重生點）、「倉庫服務」、「手推車服務」（要錢），亂點的代價很實在。
    """
    hits = [
        i for i, opt in enumerate(options, start=1)
        if any(key in _squash(opt) for key in SUBMENU_OPTIONS)
    ]
    if len(hits) == 1:
        return hits[0], f"第 {hits[0]} 項「{options[hits[0] - 1]}」"
    if not hits:
        return None, "也沒有可以往下點的選項"
    return None, f"可以往下點的有 {len(hits)} 個，分不出來"


def pick_confirm(options: list[str]) -> tuple[int | None, str]:
    """只有一個目的地時，選單是「確定嗎」—— 挑那個確認選項。

    **只認 `CONFIRM_OPTIONS` 白名單**，而且只准剛好一個對上。
    ⛔ `結束`／`取消` 不在白名單裡，永遠不會被選到。
    """
    hits = [
        i for i, opt in enumerate(options, start=1)
        if _squash(opt) in CONFIRM_OPTIONS
    ]
    if len(hits) == 1:
        return hits[0], f"第 {hits[0]} 項「{options[hits[0] - 1]}」是確認"
    if len(hits) > 1:
        return None, f"確認選項有 {len(hits)} 個，分不出來"
    # 白名單沒中：改用排除法。這隻只通一個地方，選單又只剩一個「不是離開」的
    # 選項 —— 那個必然就是「做這件事」，沒有別的可能。
    rest = [
        i for i, opt in enumerate(options, start=1)
        if _squash(opt) not in EXIT_OPTIONS
    ]
    if len(rest) == 1:
        return rest[0], f"第 {rest[0]} 項「{options[rest[0] - 1]}」是唯一不是離開的選項"
    return None, f"沒有可以確認的選項（{len(rest)} 個非離開選項，分不出來）"


def cost_of(option: str) -> str:
    """選項裡寫的代價（`150 金幣`）。看不出來回空字串 —— 只是拿來提醒人。"""
    import re

    found = re.search(r"(\d[\d,]*)\s*(金幣|z|zeny)", option, re.IGNORECASE)
    return found.group(0) if found else ""


class NpcTalk:
    """跟一個 NPC 走完一次「選目的地」的對話。

    **純狀態機**：擷取執行緒把收到的封包餵進 `feed()`，主迴圈呼叫
    `next_packet()` 拿要送出去的東西。自己不碰 socket、不碰記憶體，
    所以整條邏輯測得起來（測資就是實機擷取的位元組）。

    ⚠ **「過去了」不由這裡判定。** 這裡最多做到「選單選了第幾項送出去」；
    真的到了沒有，由呼叫端看**地圖名有沒有變**（[DAT-026]）。
    這支只負責把對話走完，或**大聲說走不完**。
    """

    #: 送出之後多久沒有任何回應就放棄。只是放棄的上限，不是成功的依據。
    TIMEOUT = 15.0

    def __init__(self, gid: int, want: str, npc: str = "",
                 sole: bool = False, now=None,
                 others: list[str] | None = None) -> None:
        import time as _time

        self._gid = gid
        self._want = want
        #: 這隻 NPC **還通往哪些地方**（中文名，來自 `navi_link` 導航資料）。
        #: 有這份清單就能用「配對」而不是「像不像」來挑選項，還能用排除法 ——
        #: 見 `pick_against`。查不到就是空的，那時候只剩字面比對。
        self._others = list(others or [])
        #: 這隻 NPC 在我們的資料裡**只通往一個地方** —— 那它的選單如果沒有
        #: 地名，多半是「確定嗎」。只有這種情況才准點確認選項。
        self._sole = sole
        self._npc = npc or f"NPC #{gid}"
        self._now = now or _time.monotonic
        self._queue: list[bytes] = [build_contact(gid)]
        self._since = self._now()
        self._menus = 0            # 回答過幾層選單
        self.done = False
        self.failed = False
        self.note = f"跟 NPC #{gid} 對話中…"
        self.cost = ""

    # ---- 擷取執行緒 -------------------------------------------------

    def feed(self, opcode: int, payload: bytes) -> None:
        # ⚠ 選完**不停止監聽**：卡普拉那種是多層選單（先「傳送服務」再選城市），
        # 有的還會再問一次「確定嗎」。選完就關耳朵的話第二層永遠等不到。
        # 真的過去了沒有，一律看**地圖名有沒有變**，由呼叫端判定（[DAT-026]）。
        if self.failed:
            return
        if opcode == ZC_WAIT_DIALOG and parse_wait(payload) == self._gid:
            self._push(build_next(self._gid))
            return
        if opcode == ZC_CLOSE_DIALOG and parse_wait(payload) == self._gid:
            # ⚠⚠ **一定要按掉那個「離開」**，不然事情不會發生。
            #
            # 使用者實機 2026-08-28：選單選對了、封包也送出去了，然後**船就是不開**，
            # 停在原地十分鐘。原因是 RO 的腳本用 `close2;` —— 伺服器送 `0x00B6`
            # 叫客戶端畫出「離開」鈕，**玩家按了、客戶端回 `0x0146`，腳本才會繼續
            # 往下跑到傳送那一行**。我們收了 0x00B6 卻沒回，腳本就永遠卡在那裡。
            #
            # 這不是「多按一個沒差」：不按 = 傳送永遠不會發生，而且完全沒有徵兆
            #（沒有錯誤、沒有拒絕，就只是不動）。
            #
            # ⚠ 只回應**我們正在講話的那隻**（GID 對得上）—— 別人的對話框不要碰。
            self._push(build_close(self._gid))
            return
        if opcode == ZC_SAY_DIALOG:
            got = parse_say(payload)
            if got and got[0] == self._gid:
                self._since = self._now()   # 有回應就重新計時
            return
        if opcode == ZC_MENU_LIST:
            got = parse_menu(payload)
            if got is None or got[0] != self._gid:
                return
            self._menus += 1
            if self._menus > MAX_MENUS:
                self.failed = True
                self.note = f"⚠ 選單超過 {MAX_MENUS} 層，這不像單純的傳送，停手"
                log.warning("%s", self.note)
                return
            self._on_menu(got[1])

    def _on_menu(self, options: list[str]) -> None:
        index, why = pick_option(options, self._want, self._others)
        if index is None:
            # 這一層沒有目的地：可能是多層選單的第一層（卡普拉那種）。
            # **只准點白名單裡的選項**，而且只准剛好一個對上。
            index, sub_why = pick_submenu(options)
            if index is not None:
                self.note = f"跟「{self._npc}」問路：{sub_why}"
                log.info("%s", self.note)
                self._push(build_choose(self._gid, index))
                return
            if self._sole:
                index, ok_why = pick_confirm(options)
                if index is not None:
                    self.cost = cost_of(options[index - 1])
                    money = f"，{self.cost}" if self.cost else ""
                    self.note = (
                        f"找「{self._npc}」傳送到 {self._want}{money}"
                        f"（他只通這一個地方，選單是確認）"
                    )
                    log.info("%s：%s", self.note, ok_why)
                    self._push(build_choose(self._gid, index))
                    self.done = True
                    return
                sub_why = f"{sub_why}；{ok_why}"
            # ⛔ 分不出來就**不准賭**：猜錯是把人傳到別的島、或花掉他的錢。
            self.failed = True
            self.note = f"⚠ 看不懂 NPC 的選單，沒有動作：{why}；{sub_why}"
            log.warning("%s", self.note)
            return
        # 付錢是這趟路本來就要的花費，不是警告 —— 講清楚「找誰、去哪、多少」就好。
        self.cost = cost_of(options[index - 1])
        money = f"，{self.cost}" if self.cost else ""
        self.note = f"找「{self._npc}」傳送到 {self._want}{money}"
        log.info("%s（%s）", self.note, why)
        self._push(build_choose(self._gid, index))
        self.done = True        # 該送的都送了，剩下等地圖變

    def _push(self, data: bytes) -> None:
        self._queue.append(data)
        self._since = self._now()

    # ---- 主迴圈 -----------------------------------------------------

    def next_packet(self) -> bytes | None:
        """要送出去的下一個封包。沒有就回 None。"""
        if self._queue:
            return self._queue.pop(0)
        if self.failed:
            return None
        if self._now() - self._since > self.TIMEOUT and not self.done:
            self.failed = True
            self.note = f"⚠ 跟 NPC #{self._gid} 對話 {self.TIMEOUT:.0f} 秒沒有回應，放棄"
            log.warning("%s", self.note)
        return None
