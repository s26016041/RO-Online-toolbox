"""前置沒解的 NPC 傳送：踩過就記起來，之後算路線跳過。

使用者指定（2026-08-31）：「有前置才能使用的 NPC 傳送也要記得把有它的路徑刪除」。
"""

from __future__ import annotations

import pytest

from ro_toolbox.services import hop_blocklist
from ro_toolbox.services.travel import Hop


@pytest.fixture(autouse=True)
def _own_file(tmp_path, monkeypatch):
    """每條測試自己一個檔案 —— 絕對不准碰使用者真的設定。"""
    monkeypatch.setattr(hop_blocklist, "_path", lambda: tmp_path / "blocked.json")


def _hop(npc="伊甸園傳送師"):
    return Hop("prontera", 124, 76, "moc_para01", 30, 30, npc=npc, npc_id=811)


def test_one_failure_is_not_enough():
    """⚠ 一次可能只是意外（選單沒送到、被打斷、沒認出 GID）。

    把偶發當永久，會**安靜地砍掉一條本來走得通的路**，
    而使用者只會看到「怎麼繞遠路」。
    """
    assert hop_blocklist.remember("狐狐狸", _hop(), "跟 NPC 講不通") == 1
    assert hop_blocklist.blocked("狐狐狸") == set()


def test_two_failures_block_the_hop():
    """踩第二次就封起來 —— 之後算路線直接跳過。"""
    hop_blocklist.remember("狐狐狸", _hop(), "跟 NPC 講不通")
    hop_blocklist.remember("狐狐狸", _hop(), "跟 NPC 講不通")
    assert hop_blocklist.blocked("狐狐狸") == {("prontera", 124, 76)}


def test_it_is_remembered_per_character():
    """★ 前置是**每個角色**的事：入會的用得了，沒入會的用不了。"""
    for _ in range(2):
        hop_blocklist.remember("狐狐狸", _hop(), "跟 NPC 講不通")
    assert hop_blocklist.blocked("白狐") == set(), "別人的前置跟我無關"


def test_a_plain_warp_is_never_remembered():
    """⛔ 走過去就會傳送的傳點不記 —— 那種失敗多半是暫時的（卡住、走錯格），
    封起來反而會把好好的路砍掉。"""
    plain = Hop("prontera", 10, 10, "prt_fild08", 20, 20)      # 沒有 npc
    assert hop_blocklist.remember("狐狐狸", plain, "走不過去") == 0
    assert hop_blocklist.blocked("狐狐狸") == set()


def test_forgetting_gives_the_route_back():
    """前置解掉之後那條路就通了 —— 不該被舊紀錄永遠擋著。"""
    for _ in range(2):
        hop_blocklist.remember("狐狐狸", _hop(), "跟 NPC 講不通")
    hop_blocklist.forget("狐狐狸")
    assert hop_blocklist.blocked("狐狐狸") == set()


def test_a_broken_file_is_not_allowed_to_block_anything(tmp_path):
    """壞檔案一律當成「沒有記錄」，不准擋住整個尋路。"""
    (tmp_path / "blocked.json").write_text("{壞掉的", encoding="utf-8")
    assert hop_blocklist.blocked("狐狐狸") == set()


def test_the_traveler_skips_what_it_learned(monkeypatch):
    """★ 算路線時真的會跳過學到的那幾段。"""
    from ro_toolbox.services.travel import Traveler
    from ro_toolbox.services.walker import Walker

    for _ in range(2):
        hop_blocklist.remember("狐狐狸", _hop(), "跟 NPC 講不通")
    traveler = Traveler(Walker(lambda *_a: None), lambda: 0.0)
    traveler.set_character("狐狐狸")
    assert ("prontera", 124, 76) in traveler._avoid
