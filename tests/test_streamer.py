import os
import time

import pytest

import torrent_streamer
from torrent_streamer import compute_readiness, episode_key, select_episode_file


# ---------------- pure helpers (no libtorrent needed) ---------------- #

def test_select_episode_file_matches_season_episode_in_pack():
    files = [
        (0, "TZ S01/The.Twilight.Zone.S01E01.DVDRip.avi", 300),
        (1, "TZ S01/The.Twilight.Zone.S01E02.DVDRip.avi", 290),
        (2, "TZ S01/The.Twilight.Zone.S01E10.DVDRip.avi", 310),
        (3, "TZ S01/Sample/sample-s01e02.avi", 5),
        (4, "TZ S01/info.nfo", 1),
    ]
    assert select_episode_file(files, "1", "2") == 1
    assert select_episode_file(files, 1, 10) == 2
    # 1x01-style naming
    assert select_episode_file([(0, "tz 1x01.mkv", 10), (1, "tz 1x02.mkv", 10)], 1, 2) == 1


def test_select_episode_file_falls_back_to_largest_video():
    files = [(0, "movie/sample.mkv", 50), (1, "movie/movie.mkv", 900), (2, "movie/cover.jpg", 1)]
    assert select_episode_file(files) == 1
    assert select_episode_file([(0, "readme.txt", 10)]) is None


def test_episode_key_prefers_explicit_key():
    assert episode_key({"key": "A|1", "channel": "B", "id": "2"}) == "A|1"
    assert episode_key({"channel": "B", "id": "2"}) == "B|2"


def test_readiness_complete_file_is_ready():
    r = compute_readiness([True] * 10, 0, 9, 1000, 100, 0, 1000, 600, 1200, 0)
    assert r["ready"] and r["complete"]


def test_readiness_requires_header_and_trailer():
    have = [True] * 9 + [False]  # last piece (trailer) missing
    r = compute_readiness(have, 0, 9, 1000, 100, 0, 900, 0, 1000, 10_000)
    assert not r["ready"]


def test_readiness_partial_buffer_with_fast_download():
    # 100 pieces, 1 s of video per piece; head+tail present, 60 pieces from playhead.
    have = [False] * 100
    for p in list(range(0, 3)) + list(range(98, 100)) + list(range(10, 70)):
        have[p] = True
    r = compute_readiness(have, 0, 99, 100_000, 1000, 0, 65_000, 10, 100, download_rate=10_000)
    assert r["ready"]
    assert r["buffered_seconds"] == pytest.approx(60)
    # Same buffer but the download is too slow to keep up -> not ready.
    r = compute_readiness(have, 0, 99, 100_000, 1000, 0, 65_000, 10, 100, download_rate=10)
    assert not r["ready"]


# ---------------- real libtorrent: seed a local season pack and stream it ---------------- #

lt = pytest.importorskip("libtorrent") if torrent_streamer.lt is not None else None


def _make_pack(tmp_path):
    src = tmp_path / "seed" / "TZ.S01"
    src.mkdir(parents=True)
    payloads = {}
    for n in (1, 2):
        data = os.urandom(600_000 + n * 1000)
        name = f"The.Twilight.Zone.S01E0{n}.DVDRip.avi"
        (src / name).write_bytes(data)
        payloads[n] = (name, data)
    fs = lt.file_storage()
    lt.add_files(fs, str(src))
    ct = lt.create_torrent(fs, 16384)
    lt.set_piece_hashes(ct, str(tmp_path / "seed"))
    torrent_path = tmp_path / "pack.torrent"
    torrent_path.write_bytes(lt.bencode(ct.generate()))
    return torrent_path, payloads


@pytest.mark.skipif(torrent_streamer.lt is None, reason="libtorrent not installed")
def test_streams_one_episode_out_of_a_season_pack(tmp_path):
    torrent_path, payloads = _make_pack(tmp_path)
    seeder = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False,
                         "enable_upnp": False, "enable_natpmp": False})
    atp = lt.add_torrent_params()
    atp.ti = lt.torrent_info(str(torrent_path))
    atp.save_path = str(tmp_path / "seed")
    seed_handle = seeder.add_torrent(atp)
    deadline = time.time() + 20
    while not seed_handle.status().is_seeding and time.time() < deadline:
        time.sleep(0.1)
    assert seed_handle.status().is_seeding

    ts = torrent_streamer.TorrentStreamer(save_root=str(tmp_path / "dl"), listen_port=0)
    ts.session.apply_settings({"enable_dht": False, "enable_lsd": False, "enable_upnp": False, "enable_natpmp": False})
    ep2 = {"channel": "Twilight Zone", "id": "tz-s01e02", "season": "1", "episode": "2", "magnet": str(torrent_path)}
    key = ts.add_episode(ep2)
    assert key == "Twilight Zone|tz-s01e02"
    ts.torrents[ts.episodes[key]["info_hash"]]["handle"].connect_peer(("127.0.0.1", seeder.listen_port()))

    status = {}
    deadline = time.time() + 30
    while time.time() < deadline:
        ts.poll()
        status = ts.episode_status(ep2, 0, 60)
        if status.get("complete"):
            break
        time.sleep(0.2)
    assert status["ready"] and status["complete"], status
    assert status["file_path"].endswith(payloads[2][0])
    with open(status["file_path"], "rb") as fh:
        assert fh.read() == payloads[2][1]
    # Only the wanted episode was downloaded.
    other = os.path.join(os.path.dirname(status["file_path"]), payloads[1][0])
    assert not os.path.exists(other) or os.path.getsize(other) == 0 or open(other, "rb").read() != payloads[1][1]

    ts.mark_episode_viewed(ep2)
    deadline = time.time() + 10
    while os.path.exists(status["file_path"]) and time.time() < deadline:
        time.sleep(0.1)
    assert not os.path.exists(status["file_path"])
    assert not ts.torrents and not ts.episodes
    ts.shutdown()


@pytest.mark.skipif(torrent_streamer.lt is None, reason="libtorrent not installed")
def test_buffer_episodes_evicts_and_respects_max_active(tmp_path):
    torrent_path, _payloads = _make_pack(tmp_path)
    ts = torrent_streamer.TorrentStreamer(save_root=str(tmp_path / "dl"), listen_port=0)
    eps = [
        {"channel": "c", "id": "e1", "season": 1, "episode": 1, "magnet": str(torrent_path)},
        {"channel": "c", "id": "e2", "season": 1, "episode": 2, "magnet": str(torrent_path)},
        {"channel": "c", "id": "e3", "magnet": ""},
    ]
    ts.buffer_episodes(eps, max_active=2)
    assert set(ts.episodes) == {"c|e1", "c|e2"}
    assert len(ts.torrents) == 1  # both episodes share the pack torrent
    ts.poll()
    ts.buffer_episodes(eps[1:2])
    assert set(ts.episodes) == {"c|e2"}
    assert len(ts.torrents) == 1
    ts.buffer_episodes([])
    assert not ts.torrents
    ts.shutdown()


@pytest.mark.skipif(torrent_streamer.lt is None, reason="libtorrent not installed")
def test_status_reports_stall_and_errors(tmp_path):
    torrent_path, _payloads = _make_pack(tmp_path)  # nobody seeds it
    ts = torrent_streamer.TorrentStreamer(save_root=str(tmp_path / "dl"), listen_port=0)
    ep = {"channel": "c", "id": "e1", "season": 1, "episode": 1, "magnet": str(torrent_path)}
    ts.add_episode(ep)
    torrent = next(iter(ts.torrents.values()))
    deadline = time.time() + 10
    while time.time() < deadline and (torrent["handle"].status().paused or torrent["handle"].queue_position() < 0):
        ts.poll()  # wait until libtorrent has actually started it (not just queued)
        time.sleep(0.1)
    ts.poll()
    assert ts.episode_status(ep)["stalled_seconds"] < 5
    torrent["last_progress_at"] -= 600
    ts.poll()  # no progress happened, so the stall timer keeps running
    assert ts.episode_status(ep)["stalled_seconds"] >= 600

    bad = {"channel": "c", "id": "bad", "magnet": "not-a-torrent-source"}
    ts.add_episode(bad)
    assert ts.episode_status(bad)["state"] == "error"
    ts.shutdown()


def _make_single(tmp_path, name):
    src = tmp_path / "seed" / name
    src.mkdir(parents=True)
    (src / f"{name}.avi").write_bytes(os.urandom(200_000))
    fs = lt.file_storage()
    lt.add_files(fs, str(src))
    ct = lt.create_torrent(fs, 16384)
    lt.set_piece_hashes(ct, str(tmp_path / "seed"))
    path = tmp_path / f"{name}.torrent"
    path.write_bytes(lt.bencode(ct.generate()))
    return str(path)


@pytest.mark.skipif(torrent_streamer.lt is None, reason="libtorrent not installed")
def test_queue_follows_priority_and_queued_torrents_are_not_stalled(tmp_path):
    ts = torrent_streamer.TorrentStreamer(save_root=str(tmp_path / "dl"), listen_port=0, active_downloads=2)
    ts.session.apply_settings({"auto_manage_interval": 1, "auto_manage_startup": 0, "dont_count_slow_torrents": False})
    eps = [{"channel": "c", "id": f"e{i}", "magnet": _make_single(tmp_path, f"T{i}")} for i in range(4)]
    ts.buffer_episodes(list(reversed(eps)))
    handles = [ts.torrents[ts.episodes[f"c|e{i}"]["info_hash"]]["handle"] for i in range(4)]

    def positions_after_polls():
        deadline = time.time() + 10
        while time.time() < deadline:
            ts.poll()
            if len(ts._queue_order) == 4:
                break
            time.sleep(0.2)
        return [h.queue_position() for h in handles]

    assert positions_after_polls() == [3, 2, 1, 0]
    ts.buffer_episodes(eps)  # new priorities reorder the queue
    ts.poll()
    assert [h.queue_position() for h in handles] == [0, 1, 2, 3]

    deadline = time.time() + 10
    while time.time() < deadline and not any(h.status().paused for h in handles):
        ts.poll()
        time.sleep(0.2)
    queued = [h for h in handles if h.status().paused]
    assert queued, "libtorrent should queue torrents beyond active_downloads"
    for torrent in ts.torrents.values():
        torrent["last_progress_at"] -= 600
    ts.poll()
    for i, h in enumerate(handles):
        stalled = ts.episode_status(eps[i])["stalled_seconds"]
        assert (stalled < 5) if h in queued else (stalled >= 600)

    ts.focus_episode(eps[0])  # the episode on screen jumps the queue
    assert handles[0].queue_position() == 0
    ts.shutdown()


def test_live_piece_priorities():
    from torrent_streamer import PRIORITY_AIRED, PRIORITY_LIVE, PRIORITY_NORMAL, live_piece_priorities
    assert set(live_piece_priorities(10, 29, None).values()) == {PRIORITY_NORMAL}
    prio = live_piece_priorities(10, 29, 20)
    assert [prio[p] for p in (10, 11, 12)] == [PRIORITY_LIVE] * 3       # container header
    assert all(prio[p] == PRIORITY_AIRED for p in range(13, 20))       # already aired
    assert all(prio[p] == PRIORITY_LIVE for p in range(20, 30))        # live point to the end


def _seed_and_download(tmp_path, ts, ep, torrent_path):
    seeder = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False,
                         "enable_upnp": False, "enable_natpmp": False})
    atp = lt.add_torrent_params()
    atp.ti = lt.torrent_info(str(torrent_path))
    atp.save_path = str(tmp_path / "seed")
    seed_handle = seeder.add_torrent(atp)
    deadline = time.time() + 20
    while not seed_handle.status().is_seeding and time.time() < deadline:
        time.sleep(0.05)  # connecting before the seeder is ready costs a ~15 s reconnect delay
    ts.add_episode(ep)
    ts.torrents[ts.episodes[episode_key(ep)]["info_hash"]]["handle"].connect_peer(("127.0.0.1", seeder.listen_port()))
    deadline = time.time() + 30
    status = {}
    while time.time() < deadline:
        ts.poll()
        status = ts.episode_status(ep, 0, 60)
        if status.get("complete"):
            break
        time.sleep(0.1)
    assert status.get("complete"), status
    return seeder, status


@pytest.mark.skipif(torrent_streamer.lt is None, reason="libtorrent not installed")
def test_resume_data_skips_recheck_and_is_deleted_with_files(tmp_path):
    torrent_path, payloads = _make_pack(tmp_path)
    ep = {"channel": "TZ", "id": "e2", "season": 1, "episode": 2, "magnet": str(torrent_path)}
    ts = torrent_streamer.TorrentStreamer(save_root=str(tmp_path / "dl"), listen_port=0, torrent_cache_dir=str(tmp_path / "cache"))
    seeder, status = _seed_and_download(tmp_path, ts, ep, torrent_path)
    info_hash = ts.episodes["TZ|e2"]["info_hash"]
    ts.shutdown()
    del seeder
    resume = tmp_path / "cache" / f"{info_hash}.resume"
    assert resume.is_file()

    # Damage the middle of the file but keep its size and timestamp. With resume data
    # libtorrent trusts its saved piece map; a full re-check would find the damage.
    path = status["file_path"]
    st = os.stat(path)
    with open(path, "r+b") as fh:
        fh.seek(st.st_size // 2)
        fh.write(b"\0" * 4096)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))

    ts2 = torrent_streamer.TorrentStreamer(save_root=str(tmp_path / "dl"), listen_port=0, torrent_cache_dir=str(tmp_path / "cache"))
    ts2.add_episode(ep)
    deadline = time.time() + 5
    status2 = {}
    while time.time() < deadline:
        ts2.poll()
        status2 = ts2.episode_status(ep, 0, 60)
        if status2.get("complete"):
            break
        time.sleep(0.05)
    assert status2.get("complete"), status2
    ts2.mark_episode_viewed(ep)
    assert not resume.exists()
    ts2.shutdown()


@pytest.mark.skipif(torrent_streamer.lt is None, reason="libtorrent not installed")
def test_airing_episode_downloads_from_live_point(tmp_path):
    torrent_path, _payloads = _make_pack(tmp_path)  # 16 KiB pieces, two ~600 KB files
    ts = torrent_streamer.TorrentStreamer(save_root=str(tmp_path / "dl"), listen_port=0)
    ep = {"channel": "TZ", "id": "e1", "season": 1, "episode": 1, "magnet": str(torrent_path),
          "live_offset": 30.0, "live_duration": 60.0}
    ts.add_episode(ep)

    def settled_priorities():
        for _ in range(20):
            ts.poll()
            time.sleep(0.05)
        return list(ts._file_layout(ts.episodes["TZ|e1"])[1].get_piece_priorities())

    prio = settled_priorities()
    _t, handle, _i, size, offset, piece_length, first, last = ts._file_layout(ts.episodes["TZ|e1"])
    middle = (offset + size // 2) // piece_length
    assert prio[first] == 7 and prio[last] == 7
    assert prio[first + 5] == 1 and prio[middle - 1] == 1      # first half already aired
    assert all(p == 7 for p in prio[middle:last + 1])          # live point onward first
    assert all(p == 0 for p in prio[last + 1:])                 # other episode in the pack not wanted
    # Once the episode is no longer airing, everything is downloaded normally again.
    ts.add_episode(dict(ep, live_offset=0.0))
    assert set(settled_priorities()[first:last + 1]) == {4}
    ts.shutdown()


def test_readiness_min_buffer_is_configurable():
    have = [False] * 100
    for p in list(range(0, 3)) + list(range(98, 100)) + list(range(10, 30)):
        have[p] = True  # 20 s buffered ahead of the playhead
    args = (have, 0, 99, 100_000, 1000, 0, 25_000, 10, 100, 10_000)
    assert not compute_readiness(*args)["ready"]                     # default 45 s
    assert compute_readiness(*args, min_buffer_seconds=15)["ready"]
