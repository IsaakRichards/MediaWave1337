"""
torrent_streamer.py

A lightweight integration with python-libtorrent to manage torrent downloads and
buffering for sequential playback.

Key features:
- Creates a libtorrent session
- Adds magnet URIs and starts downloading them into specified save paths
- Keeps a small active buffer window and removes torrents outside the buffer

This module is intentionally simple; it starts downloads and relies on libtorrent's
piece picking to fetch data. For more advanced streaming (piece-priority for
immediate playback), you'd want per-file piece prioritization and read-while-writing
logic.
"""

from typing import Dict, Optional, List
import logging
import time
import os

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

try:
    import libtorrent as lt
except Exception as exc:  # pragma: no cover - libtorrent may not be present in test env
    lt = None


class TorrentStreamerError(Exception):
    pass


class TorrentStreamer:
    def __init__(self, save_root: str = "data/videos", listen_port=(6881, 6891), session_settings: Optional[dict] = None):
        if lt is None:
            raise TorrentStreamerError("python-libtorrent (libtorrent) is required but not installed")
        self.save_root = save_root
        self.session = lt.session()
        self.session.listen_on(listen_port[0], listen_port[1])
        # Minimal alert mask
        self.session.set_alert_mask(lt.alert.category_t.status_notification)
        self.torrents: Dict[str, Dict] = {}  # magnet -> {handle, save_path, added_at}
        logger.info("TorrentStreamer session started, saving to: %s", save_root)

    def add_magnet(self, magnet_uri: str, rel_save_path: Optional[str] = None) -> lt.torrent_handle:
        """Add a magnet URI and start downloading it. rel_save_path is relative to save_root.
        Returns the libtorrent torrent_handle.
        If torrent already added, returns existing handle.
        """
        if magnet_uri in self.torrents:
            logger.debug("Magnet already added: %s", magnet_uri)
            return self.torrents[magnet_uri]["handle"]

        save_path = self.save_root
        if rel_save_path:
            # create directory
            save_dir = os.path.dirname(rel_save_path)
            if save_dir:
                os.makedirs(os.path.join(self.save_root, save_dir), exist_ok=True)
            save_path = os.path.join(self.save_root, os.path.dirname(rel_save_path)) or self.save_root

        params = {
            'save_path': save_path,
            'storage_mode': lt.storage_mode_t.storage_mode_sparse,
        }
        logger.info("Adding magnet: %s -> %s", magnet_uri, save_path)
        handle = lt.add_magnet_uri(self.session, magnet_uri, params)
        self.torrents[magnet_uri] = {"handle": handle, "save_path": save_path, "added_at": time.time(), "rel_path": rel_save_path}
        return handle

    def remove_magnet(self, magnet_uri: str) -> None:
        info = self.torrents.get(magnet_uri)
        if not info:
            return
        handle = info["handle"]
        try:
            self.session.remove_torrent(handle)
            logger.info("Removed torrent for magnet: %s", magnet_uri)
        except Exception:
            logger.exception("Failed to remove torrent for %s", magnet_uri)
        finally:
            del self.torrents[magnet_uri]

    def _ensure_not_more_than(self, keep_magnets: List[str]) -> None:
        """Remove any torrents we are tracking that are not in keep_magnets."""
        for mag in list(self.torrents.keys()):
            if mag not in keep_magnets:
                logger.debug("Evicting magnet from buffer: %s", mag)
                self.remove_magnet(mag)

    def buffer_episodes(self, episodes: List[Dict], max_active: Optional[int] = None) -> None:
        """Ensure the given episodes (list of dicts with 'magnet' and optional 'save_path')
        are being downloaded. Evict other torrents that are not in this list.

        max_active: if provided, ensures we only keep that many active torrents (evict older ones by added_at).
        """
        keep = []
        for ep in episodes:
            magnet = ep.get("magnet")
            if not magnet:
                logger.warning("Episode missing magnet; skipping buffering: %s", ep.get("title"))
                continue
            rel_save = ep.get("save_path")
            if magnet not in self.torrents:
                try:
                    self.add_magnet(magnet, rel_save)
                except Exception:
                    logger.exception("Failed to add magnet: %s", magnet)
                    continue
            keep.append(magnet)

        # Optionally enforce max_active count
        if max_active and len(keep) > max_active:
            # sort by order in keep (which should be playback order), trim to max_active
            keep = keep[:max_active]
        self._ensure_not_more_than(keep)

    def shutdown(self) -> None:
        # remove all torrents and pause session
        try:
            for mag in list(self.torrents.keys()):
                self.remove_magnet(mag)
            # no explicit session.stop in libtorrent python binding; let GC handle it
            logger.info("TorrentStreamer shutdown complete")
        except Exception:
            logger.exception("Error during TorrentStreamer shutdown")


if __name__ == "__main__":
    print("torrent_streamer.py loaded. Use TorrentStreamer in your application.")
