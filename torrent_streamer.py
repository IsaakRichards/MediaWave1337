"""
torrent_streamer.py

Enhanced torrent streamer with piece/file-priority support for streaming and
support to mark episodes as viewed (delete files and remove torrents so they
can be re-downloaded later).
"""

from typing import Dict, Optional, List
import logging
import time
import os
import threading

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

try:
    import libtorrent as lt
except Exception as exc:  # pragma: no cover - libtorrent may not be present in test env
    lt = None


class TorrentStreamerError(Exception):
    pass


class TorrentStreamer:
    def __init__(self, save_root: str = "data/videos", listen_port=(6881, 6891), session_settings: Optional[dict] = None, metadata_timeout: int = 30):
        if lt is None:
            raise TorrentStreamerError("python-libtorrent (libtorrent) is required but not installed")
        self.save_root = save_root
        self.session = lt.session()
        self.session.listen_on(listen_port[0], listen_port[1])
        # Minimal alert mask
        self.session.set_alert_mask(lt.alert.category_t.status_notification)
        self.torrents: Dict[str, Dict] = {}  # magnet -> {handle, save_path, added_at, rel_path}
        self.metadata_timeout = metadata_timeout
        logger.info("TorrentStreamer session started, saving to: %s", save_root)

    def _full_path(self, rel_save_path: Optional[str]) -> str:
        if not rel_save_path:
            return self.save_root
        if os.path.isabs(rel_save_path):
            return rel_save_path
        return os.path.join(self.save_root, rel_save_path)

    def _metadata_worker(self, magnet_uri: str, handle, rel_save_path: Optional[str]):
        """Background worker that waits for metadata and then prioritizes the
        specific file for streaming (high file priority and sequential download).
        """
        try:
            t0 = time.time()
            while not handle.has_metadata():
                if time.time() - t0 > self.metadata_timeout:
                    logger.warning("Timeout waiting for metadata for %s", magnet_uri)
                    return
                time.sleep(0.5)
            ti = handle.get_torrent_info()
            files = ti.files()
            file_idx = None
            # try to match rel_save_path to one of the torrent file paths
            if rel_save_path:
                for i in range(files.num_files()):
                    fp = files.file_path(i)
                    # match by suffix to allow different save root/path forms
                    if fp.endswith(rel_save_path) or fp == rel_save_path:
                        file_idx = i
                        break
            # if not found and torrent has a single file, pick 0
            if file_idx is None and files.num_files() == 1:
                file_idx = 0

            if file_idx is not None:
                # set file priorities: 0 = don't download, 1..7 prioritized
                priorities = [0] * files.num_files()
                priorities[file_idx] = 7
                try:
                    handle.prioritize_files(priorities)
                    logger.info("Set file priority for %s (file index %s)", magnet_uri, file_idx)
                except Exception:
                    logger.exception("Failed to call prioritize_files on handle for %s", magnet_uri)
            # enable sequential download for smoother streaming if available
            try:
                if hasattr(handle, "set_sequential_download"):
                    handle.set_sequential_download(True)
                    logger.debug("Enabled sequential download for %s", magnet_uri)
            except Exception:
                logger.exception("Failed to set sequential download for %s", magnet_uri)
        except Exception:
            logger.exception("Exception in metadata worker for %s", magnet_uri)

    def add_magnet(self, magnet_uri: str, rel_save_path: Optional[str] = None) -> 'lt.torrent_handle':
        """Add a magnet URI and start downloading it. rel_save_path is the file
        path relative to save_root that we want prioritized for streaming.
        Returns the libtorrent torrent_handle. If magnet already added, returns existing handle.
        """
        if magnet_uri in self.torrents:
            logger.debug("Magnet already added: %s", magnet_uri)
            return self.torrents[magnet_uri]["handle"]

        save_path_dir = self.save_root
        if rel_save_path:
            save_dir = os.path.dirname(rel_save_path)
            if save_dir:
                os.makedirs(os.path.join(self.save_root, save_dir), exist_ok=True)
            save_path_dir = os.path.join(self.save_root, os.path.dirname(rel_save_path)) or self.save_root

        params = {
            'save_path': save_path_dir,
            'storage_mode': lt.storage_mode_t.storage_mode_sparse,
        }
        logger.info("Adding magnet: %s -> %s", magnet_uri, save_path_dir)
        handle = lt.add_magnet_uri(self.session, magnet_uri, params)
        self.torrents[magnet_uri] = {"handle": handle, "save_path": save_path_dir, "added_at": time.time(), "rel_path": rel_save_path}

        # Start background thread to prioritize on metadata ready
        th = threading.Thread(target=self._metadata_worker, args=(magnet_uri, handle, rel_save_path), daemon=True)
        th.start()
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

    def mark_episode_viewed(self, ep: Dict) -> None:
        """Delete the episode's file (if present) and remove any associated torrent so
        that it will be re-downloaded later when buffered again.

        ep should be a dict containing at least 'magnet' and 'save_path' (relative or absolute).
        """
        magnet = ep.get("magnet")
        rel_save = ep.get("save_path")
        full_path = self._full_path(rel_save)

        # Delete the file if it exists
        try:
            if os.path.exists(full_path):
                os.remove(full_path)
                logger.info("Deleted viewed file: %s", full_path)
                # attempt to remove parent directory if empty
                parent = os.path.dirname(full_path)
                try:
                    if parent and os.path.isdir(parent) and not os.listdir(parent):
                        os.rmdir(parent)
                        logger.debug("Removed empty directory: %s", parent)
                except Exception:
                    logger.debug("Could not remove parent directory: %s", parent)
            else:
                logger.debug("Viewed file not present (nothing to delete): %s", full_path)
        except Exception:
            logger.exception("Failed to delete file for viewed episode: %s", full_path)

        # Remove torrent if currently added
        if magnet and magnet in self.torrents:
            logger.info("Removing torrent for viewed episode: %s", magnet)
            self.remove_magnet(magnet)

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
            # If file already exists on disk, we assume it's already downloaded and skip adding torrent
            fullpath = self._full_path(rel_save)
            if os.path.exists(fullpath) and magnet not in self.torrents:
                logger.info("File already present on disk, skipping add: %s", fullpath)
                keep.append(magnet)
                continue
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
            logger.info("TorrentStreamer shutdown complete")
        except Exception:
            logger.exception("Error during TorrentStreamer shutdown")


if __name__ == "__main__":
    print("torrent_streamer.py loaded. Use TorrentStreamer in your application.")
