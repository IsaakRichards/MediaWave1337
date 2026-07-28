import pytest
from unittest import mock

import torrent_streamer


class DummyHandle:
    pass


@pytest.fixture(autouse=True)
def patch_libtorrent(monkeypatch):
    # Mock libtorrent module if not present
    fake_lt = mock.MagicMock()
    fake_session = mock.MagicMock()
    fake_lt.session.return_value = fake_session
    # storage_mode_t and add_magnet_uri
    fake_lt.storage_mode_t.storage_mode_sparse = 0
    fake_handle = DummyHandle()
    fake_lt.add_magnet_uri.return_value = fake_handle
    monkeypatch.setitem(torrent_streamer.__dict__, 'lt', fake_lt)
    yield


def test_add_and_remove_magnet(tmp_path):
    ts = torrent_streamer.TorrentStreamer(save_root=str(tmp_path))
    mag = "magnet:?xt=urn:btih:TEST"
    handle = ts.add_magnet(mag, rel_save_path="chan/ep1.mp4")
    assert mag in ts.torrents
    assert ts.torrents[mag]['handle'] == handle
    ts.remove_magnet(mag)
    assert mag not in ts.torrents


def test_buffer_episodes_eviction(tmp_path):
    ts = torrent_streamer.TorrentStreamer(save_root=str(tmp_path))
    eps = [
        {"magnet": f"magnet:?xt=urn:btih:TEST{i}", "save_path": f"chan/ep{i}.mp4"}
        for i in range(5)
    ]
    ts.buffer_episodes(eps, max_active=3)
    # Only first 3 should be present
    assert len(ts.torrents) == 3
