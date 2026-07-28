import pytest
from unittest import mock
import os

import torrent_streamer


class DummyHandle:
    pass


@pytest.fixture(autouse=True)
def patch_libtorrent(monkeypatch, tmp_path):
    # Mock libtorrent module if not present
    fake_lt = mock.MagicMock()
    fake_session = mock.MagicMock()
    fake_lt.session.return_value = fake_session
    # storage_mode_t and add_magnet_uri
    fake_lt.storage_mode_t.storage_mode_sparse = 0

    # Create a fake handle with expected methods used by the streamer
    fake_handle = mock.MagicMock()
    fake_handle.has_metadata.return_value = True

    # fake torrent_info and files
    fake_ti = mock.MagicMock()
    fake_files = mock.MagicMock()
    fake_files.num_files.return_value = 1
    fake_files.file_path.return_value = "chan/ep1.mp4"
    fake_ti.files.return_value = fake_files
    fake_handle.get_torrent_info.return_value = fake_ti

    fake_lt.add_magnet_uri.return_value = fake_handle
    monkeypatch.setitem(torrent_streamer.__dict__, 'lt', fake_lt)
    yield


def test_add_and_remove_magnet_and_mark_viewed(tmp_path):
    ts = torrent_streamer.TorrentStreamer(save_root=str(tmp_path))
    mag = "magnet:?xt=urn:btih:TEST"
    rel = "chan/ep1.mp4"
    # create dummy file to simulate downloaded file
    full = os.path.join(str(tmp_path), rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(b"dummy")

    handle = ts.add_magnet(mag, rel_save_path=rel)
    assert mag in ts.torrents
    # mark viewed should delete the file and remove torrent
    ep = {"magnet": mag, "save_path": rel}
    ts.mark_episode_viewed(ep)
    assert mag not in ts.torrents
    assert not os.path.exists(full)


def test_buffer_episodes_prioritizes(tmp_path):
    ts = torrent_streamer.TorrentStreamer(save_root=str(tmp_path))
    eps = [
        {"magnet": "magnet:?xt=urn:btih:TEST0", "save_path": "chan/ep0.mp4"},
        {"magnet": "magnet:?xt=urn:btih:TEST1", "save_path": "chan/ep1.mp4"},
        {"magnet": "magnet:?xt=urn:btih:TEST2", "save_path": "chan/ep2.mp4"},
    ]
    ts.buffer_episodes(eps, max_active=2)
    # Ensure we only kept max_active torrents
    assert len(ts.torrents) == 2
