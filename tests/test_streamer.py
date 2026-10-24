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
