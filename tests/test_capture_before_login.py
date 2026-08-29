"""擷取器在「還沒登入」狀態下的行為。

兩個踩過的坑，各釘一條：

1. `start()` 曾經在 UI 執行緒上抽封包長度表（掃遊戲 11.5MB 記憶體，實測 857ms），
   按下「開始擷取」畫面就凍住，Windows 判定沒回應（AppHangTransient / python.exe）。
2. 擷取器曾經要求「已經連上伺服器」才准啟動，於是登入封包永遠抓不到 ——
   要開始抓就得先有連線，而要看的正是那條連線怎麼建立的。
"""

from __future__ import annotations

import collections
import threading
import time

from ro_toolbox.services.packet_capture import PacketCapture

#: 測試用的封包內容：opcode 0x0064 + "hello"。用 bytes([...]) 寫，
#: 避免產生檔案時反斜線跳脫被吃掉（踩過）。
_PAYLOAD = bytes([0x64, 0x00]) + b"hello"


def _frame(
    src_port: int,
    dst_port: int,
    payload: bytes,
    src_ip: str = "10.0.0.1",
    dst_ip: str = "203.0.113.7",
) -> bytes:
    """組一個最小的 IPv4 + TCP 封包。

    ⚠ **沒有乙太網路表頭。** 封包來源是 WinDivert，它給的是 IP 封包
    （Npcap 時代給的是乙太網路影格，前面多 14 bytes）。這裡要跟正式路徑
    一致，不然測的是一個不存在的格式（[PKT-058]）。
    """
    ip = bytearray(20)
    ip[0] = 0x45          # version 4, IHL 5（20 bytes）
    ip[9] = 6             # protocol = TCP
    ip[12:16] = bytes(int(part) for part in src_ip.split("."))
    ip[16:20] = bytes(int(part) for part in dst_ip.split("."))
    tcp = bytearray(20)
    tcp[0:2] = src_port.to_bytes(2, "big")
    tcp[2:4] = dst_port.to_bytes(2, "big")
    tcp[12] = 5 << 4      # data offset 5（20 bytes）
    return bytes(ip) + bytes(tcp) + payload


def _capture(**kwargs) -> tuple[PacketCapture, list]:
    seen = []
    cap = PacketCapture(kwargs.pop("pid", 4242), seen.append)
    # 長度表已經就緒＝穩定狀態。還沒就緒時的行為另外測（見檔尾）。
    cap._lengths_ready.set()
    for key, value in kwargs.items():
        setattr(cap, key, value)
    return cap, seen


def test_length_table_is_not_extracted_on_the_calling_thread():
    """抽長度表要掃遊戲 11.5MB 記憶體（實測 857ms），不准放在 start() 裡 ——
    那是 UI 執行緒，會變成「按下去就當機」（[PKT-045]）。

    而且**讀取迴圈不等它**：等的話按下開始之後 0.8 秒都收不到東西，
    使用者按停止再開始就一直看到空畫面。那段期間的影格先暫存（見檔尾）。
    """
    cap, _seen = _capture()
    order = []
    cap._load_lengths = lambda: order.append("lengths")

    cap._stop.clear()
    worker = threading.Thread(target=cap._watch, daemon=True)
    worker.start()
    time.sleep(0.05)
    cap._stop.set()
    worker.join(3.0)

    assert order == ["lengths"], "看門狗只負責抽長度表，不負責開讀取迴圈"


def test_packets_are_attributed_by_port_before_the_server_is_known():
    """還沒連線時用行程佔用的本機連接埠認人，這樣登入的第一個封包也收得到。"""
    cap, seen = _capture(_server_ip="", _pid_ports=frozenset({51000}))

    cap._handle_frame(_frame(51000, 6900, b"\x64\x00hello"))   # 這個行程送出的
    cap._handle_frame(_frame(6900, 51000, b"\x69\x00hi"))      # 回給這個行程的
    cap._handle_frame(_frame(52000, 443, b"\x00\x01other"))    # 別人的流量

    assert len(seen) == 2
    assert [p.outbound for p in seen] == [True, False]


def test_the_server_address_alone_never_claims_a_frame():
    """⚠⚠ **多開的時候「連到同一台伺服器」不代表「是我的」。**

    實測 2026-08-29：兩個客戶端同時在同一張地圖，`find_server()` 兩邊都回
    `219.84.200.101:10009` —— 位址、連接埠全都一樣，唯一分得出來的只有
    **本機連接埠**（作業系統保證唯一）。舊版有一條「來源或目的是伺服器位址
    就算我的」的捷徑，於是兩條連線的位元組被串進同一個重組緩衝，切出來的
    封包互相污染（15 秒實測誤收 81 個影格，佔 41%）。

    症狀完全不像封包問題，全都長得像記憶體讀錯：負重 101%、打到空氣、
    換圖之後尋路從別人的座標開始算然後安靜卡住（[PKT-085]）。
    """
    cap, seen = _capture(_server_ip="203.0.113.7", _pid_ports=frozenset({51000}))

    cap._handle_frame(_frame(51000, 6900, b"\x64\x00mine"))    # 我的連線
    cap._handle_frame(_frame(64080, 6900, b"\x64\x00nope"))    # 隔壁那隻，同一台伺服器
    cap._handle_frame(
        _frame(1234, 5678, b"\x64\x00nope", src_ip="10.0.0.1", dst_ip="198.51.100.9")
    )  # 與伺服器無關的流量

    assert len(seen) == 1
    assert seen[0].outbound is True
    assert seen[0].payload == b"mine"


def test_empty_payload_is_ignored():
    """純 ACK 沒有內容，不該變成一個封包。"""
    cap, seen = _capture(_server_ip="", _pid_ports=frozenset({51000}))
    cap._handle_frame(_frame(51000, 6900, b""))
    assert seen == []


# ---- find_server 不准鎖到 GameGuard 的 HTTPS -------------------------------


def _endpoints(monkeypatch, pairs):
    from ro_toolbox.services import ro_capture

    monkeypatch.setattr(ro_capture, "remote_endpoints_of", lambda _pid: set(pairs))
    return ro_capture


def test_gameguard_https_is_not_mistaken_for_the_game(monkeypatch):
    """停在登入畫面時 Ragexe 唯一的連線是 GameGuard 的 443。

    舊版「排序後取第一個非私有位址」會鎖上它，然後忠實地抓 TLS 密文、
    切出一堆垃圾 opcode —— 畫面上看起來就是「抓封包壞了什麼都沒有」。
    正確答案是 None：還沒登入（[PKT-044]），繼續等。
    """
    ro_capture = _endpoints(monkeypatch, [("43.201.119.82", 443)])
    assert ro_capture.find_server(1234) is None


def test_game_connection_wins_over_the_https_one(monkeypatch):
    """兩條並存時要挑遊戲那條，而且不能被 IP 排序左右。

    43.x 排在 175.x 前面，舊版就是這樣挑錯的。
    """
    ro_capture = _endpoints(
        monkeypatch, [("43.201.119.82", 443), ("175.99.88.7", 6900)]
    )
    assert ro_capture.find_server(1234) == ("175.99.88.7", 6900)


def test_private_addresses_are_still_excluded(monkeypatch):
    ro_capture = _endpoints(
        monkeypatch, [("192.168.1.1", 6900), ("127.0.0.1", 6900), ("175.99.88.7", 10022)]
    )
    assert ro_capture.find_server(1234) == ("175.99.88.7", 10022)


def test_http_port_is_excluded_too(monkeypatch):
    ro_capture = _endpoints(monkeypatch, [("1.2.3.4", 80)])
    assert ro_capture.find_server(1234) is None


def test_web_traffic_is_dropped_while_waiting_for_the_game(monkeypatch):
    """還沒認出伺服器時是用本機連接埠認人 —— 那段也要排掉 80／443，
    否則 GameGuard 的 TLS 會被當成遊戲封包切成垃圾。"""
    cap, seen = _capture(_server_ip="", _pid_ports=frozenset({57001}))
    cap._handle_frame(_frame(57001, 443, b"\x16\x03\x01\x00\x05hello"))   # TLS
    cap._handle_frame(_frame(443, 57001, b"\x16\x03\x03\x00\x05hi"))      # TLS
    assert seen == []

    cap._handle_frame(_frame(57001, 6900, b"\x64\x00real"))               # 遊戲
    assert len(seen) == 1


# ---- 換伺服器時不准有空窗 --------------------------------------------------


def test_new_connection_is_captured_before_the_watchdog_notices():
    """客戶端從登入伺服器換到角色伺服器時，**角色清單就在那條新連線的第一批封包裡**。

    只用位址過濾的話，看門狗要一秒才換位址，那一包整個不見 ——
    2026-08-25 的實機擷取裡就是這樣，289 個封包裡完全沒有角色名。
    位址與連接埠取聯集之後，新連線一出現就收得到。
    """
    cap, seen = _capture(
        _server_ip="175.99.88.7",              # 看門狗還鎖在舊的登入伺服器
        _pid_ports=frozenset({51000, 52000}),  # 52000 是剛開的新連線
    )
    cap._handle_frame(
        _frame(60000, 52000, b"\x6b\x00chars", src_ip="203.0.113.9", dst_ip="10.0.0.1")
    )
    assert len(seen) == 1
    assert seen[0].outbound is False


def test_old_server_still_captured_after_the_switch():
    """舊連線上還沒收完的封包也要繼續收，不能因為換了就丟。

    ⚠ 靠的是**本機連接埠**：舊的 socket 只要還開著，它的本機埠就還在
    `local_ports_of(pid)` 裡（實測換圖後舊連線會留 11 分鐘才收掉，[PKT-063]）。
    不是靠「位址等於某台伺服器」—— 那條在多開時會收到隔壁那隻的封包。
    """
    cap, seen = _capture(_server_ip="175.99.88.7",
                         _pid_ports=frozenset({51000, 52000}))
    cap._handle_frame(
        _frame(51000, 6900, b"\x64\x00old", src_ip="10.0.0.1", dst_ip="175.99.88.7")
    )
    assert len(seen) == 1
    assert seen[0].outbound is True


def test_other_processes_traffic_is_still_excluded():
    """聯集不能寬到把別的程式的流量也收進來。"""
    cap, seen = _capture(_server_ip="175.99.88.7", _pid_ports=frozenset({52000}))
    cap._handle_frame(
        _frame(40000, 8080, b"\x00\x01nope", src_ip="10.0.0.1", dst_ip="198.51.100.9")
    )
    assert seen == []


# ---- 長度表還沒好的那段時間 ------------------------------------------------


def test_frames_are_buffered_until_the_length_table_arrives():
    """讀取迴圈立刻開始收，但沒有長度表切不準包 —— 先存著，抽好再補處理。

    這是「按下開始擷取之後 0.8 秒都收不到東西」的修法：等長度表的是**處理**，
    不是**接收**。實測 extract_lengths 要 857ms（[PKT-045]）。
    """
    cap, seen = _capture()
    cap._lengths_ready.clear()

    cap._handle_frame(_frame(51000, 6900, _PAYLOAD))
    cap._server_ip = ""
    cap._pid_ports = frozenset({51000})
    assert seen == [], "長度表還沒好就不該吐封包"
    assert len(cap._frame_buffer) == 1, "影格要先存著，不能丟"

    cap._lengths_ready.set()
    cap._drain_buffer()
    assert len(seen) == 1, "長度表到了之後要把存著的補處理掉"


def test_buffer_overflow_is_counted_not_silent():
    """暫存區滿了要記數（之後會大聲報），不准安靜地丟封包。"""
    from ro_toolbox.services import packet_capture

    cap, _seen = _capture()
    cap._lengths_ready.clear()
    cap._frame_buffer = [b""] * packet_capture._MAX_BUFFERED_FRAMES
    cap._handle_frame(_frame(51000, 6900, _PAYLOAD))
    assert cap._dropped_while_waiting == 1


def test_stop_releases_the_buffer_and_unblocks_waiters():
    """停止時要把暫存清掉，也要讓還卡在抽長度表的看門狗能收工。"""
    cap, _seen = _capture()
    cap._lengths_ready.clear()
    cap._frame_buffer = [b"x"]
    cap.stop()
    assert cap._frame_buffer == []
    assert cap._lengths_ready.is_set()


# ---- 登入那一包不准漏 ------------------------------------------------------


def test_first_packet_of_a_new_connection_is_replayed():
    """新連線的第一個封包（**帳密 0x0064**）在連接埠被登記之前就飛過去了。

    連接埠集合 0.2 秒才更新一次，而 TCP 交握到送出帳密只隔幾毫秒 ——
    沒有補送機制的話那一包永遠漏掉（實測：使用者的擷取都是從 OTP 才開始）。
    """
    cap, seen = _capture(_server_ip="", _pid_ports=frozenset())

    # 遊戲剛連上，連接埠 52000 還沒被登記
    cap._handle_frame(_frame(52000, 6900, _PAYLOAD, src_ip="10.0.0.1", dst_ip="203.0.113.7"))
    assert seen == [], "還認不出主人，先留著"
    assert len(cap._recent) == 1

    # 看門狗發現新連接埠 → 補送
    cap._pid_ports = frozenset({52000})
    cap._replay(frozenset({52000}))
    assert len(seen) == 1
    assert seen[0].outbound is True
    assert cap._recent == collections.deque()


def test_replay_only_claims_the_new_ports():
    """別的程式的流量不能被順手認領進來。"""
    cap, seen = _capture(_server_ip="", _pid_ports=frozenset())
    cap._handle_frame(_frame(52000, 6900, _PAYLOAD, src_ip="10.0.0.1", dst_ip="203.0.113.7"))
    cap._handle_frame(_frame(40000, 5222, _PAYLOAD, src_ip="10.0.0.1", dst_ip="198.51.100.9"))
    # 真實流程：看門狗**先**更新連接埠集合，再叫 _replay 回頭認領
    cap._pid_ports = frozenset({52000})
    cap._replay(frozenset({52000}))
    assert len(seen) == 1
    assert len(cap._recent) == 1, "沒被認領的要留著，之後可能是別條新連線"


def test_web_traffic_is_not_remembered():
    """GameGuard 的 HTTPS 量很大，留著只是浪費記憶體。"""
    cap, _seen = _capture(_server_ip="", _pid_ports=frozenset())
    cap._handle_frame(_frame(49363, 443, _PAYLOAD, src_ip="10.0.0.1", dst_ip="3.38.77.75"))
    assert len(cap._recent) == 0


# ---- 換圖時兩條連線並存：要挑最新建立的那條 --------------------------------


def _connections(monkeypatch, rows):
    """rows = [(ip, port, 建立時間, 是否 established)]，順序隨便給。"""
    from ro_toolbox.services import ro_capture
    from ro_toolbox.services.process_monitor import Connection

    conns = [Connection(ip=r[0], port=r[1], created=r[2], established=r[3]) for r in rows]
    conns.sort(key=lambda c: c.created, reverse=True)  # connections_of 保證的順序
    monkeypatch.setattr(ro_capture, "connections_of", lambda _pid: conns)
    monkeypatch.setattr(ro_capture, "remote_endpoints_of", lambda _pid: set())
    ro_capture._multi_seen.clear()
    return ro_capture


def test_map_change_picks_the_newest_connection(monkeypatch):
    """換地圖後舊的 map server 連線會留著（實測留了 11 分鐘，[PKT-063]）。

    舊版「排序後取第一條」是擲骰子 —— 挑到舊的就是把走路封包送進一條沒人收的
    連線，而且完全不會報錯。新的 map server 一定是後建立的，所以挑最新的。
    """
    ro_capture = _connections(
        monkeypatch,
        [("219.84.200.102", 10022, 100, True), ("219.84.200.101", 10010, 200, True)],
    )
    assert ro_capture.find_server(1234) == ("219.84.200.101", 10010)


def test_newest_wins_even_when_it_sorts_last(monkeypatch):
    """IP 排序不能左右結果 —— .101 排在 .102 前面，但舊的是 .101。"""
    ro_capture = _connections(
        monkeypatch,
        [("219.84.200.101", 10010, 100, True), ("219.84.200.102", 10022, 200, True)],
    )
    assert ro_capture.find_server(1234) == ("219.84.200.102", 10022)


def test_half_open_connection_loses_to_an_established_one(monkeypatch):
    """最新的那條還沒建立完成時，要用還活著的那條，不是硬挑最新的。"""
    ro_capture = _connections(
        monkeypatch,
        [("219.84.200.102", 10022, 100, True), ("219.84.200.101", 10010, 200, False)],
    )
    assert ro_capture.find_server(1234) == ("219.84.200.102", 10022)


def test_gameguard_https_still_excluded_with_timestamps(monkeypatch):
    """443 就算是最新建立的也不是遊戲連線。"""
    ro_capture = _connections(
        monkeypatch,
        [("175.99.88.7", 10022, 100, True), ("43.201.119.82", 443, 999, True)],
    )
    assert ro_capture.find_server(1234) == ("175.99.88.7", 10022)


def test_multi_connection_notice_is_logged_once_not_every_tick(monkeypatch, caplog):
    """實測一秒印 6 行，把日誌洗掉就等於沒有日誌。只在情況變化時講一次。"""
    import logging

    ro_capture = _connections(
        monkeypatch,
        [("219.84.200.102", 10022, 100, True), ("219.84.200.101", 10010, 200, True)],
    )
    with caplog.at_level(logging.INFO, logger="ro_toolbox.services.ro_capture"):
        for _ in range(10):
            ro_capture.find_server(1234)
    said = [r for r in caplog.records if "多條非網頁連線" in r.message]
    assert len(said) == 1


# ---- 候選要全部給出來，不能只給最新的那條 ----------------------------------


def test_all_candidates_are_offered_not_just_the_newest(monkeypatch):
    """⚠⚠ 實機 2026-08-30（狐狐狸剛開程式按自動尋路）：

        這個行程有多條非網頁連線 [('219.84.200.55', 3000),
                                  ('219.84.200.101', 10010)]
        取最新建立的 ('219.84.200.55', 3000)
        ⚠ 10 秒內複製不到 … 的 socket
        ⚠ 換頻道後找不到新的遊戲 socket，自動尋路已停止

    `.55:3000` 不是地圖伺服器（真正在跑的是 `.101:10010`），但它比較新。
    ⛔ **不准寫死「哪個埠不是遊戲」** —— 那是猜的，改版就壞。
    可以驗證的判準只有「複製得到」，所以候選要全部給出來讓呼叫端一條一條試。
    """
    ro_capture = _connections(monkeypatch, [
        ("219.84.200.101", 10010, 100, True),
        ("219.84.200.55", 3000, 200, True),          # 比較新，但不是遊戲
    ])
    assert ro_capture.find_server(1234) == ("219.84.200.55", 3000)
    assert ro_capture.find_servers(1234) == [
        ("219.84.200.55", 3000),
        ("219.84.200.101", 10010),
    ], "第一個要跟 find_server() 一樣，後面照新到舊接上"


def test_candidates_still_exclude_web_ports(monkeypatch):
    """GameGuard 的 HTTPS 一樣不准進候選（[PKT-038]）。"""
    ro_capture = _connections(monkeypatch, [
        ("219.84.200.101", 10010, 100, True),
        ("43.201.119.82", 443, 200, True),
    ])
    assert ro_capture.find_servers(1234) == [("219.84.200.101", 10010)]
