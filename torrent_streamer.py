"""
torrent_streamer.py

libtorrent-backed episode streamer for MediaWave1337.

Episodes are plain dicts (as produced by torrent_channels.parse_channel_file)
with at least a ``magnet`` source and ideally ``channel``, ``id``, ``season``
and ``episode``. The source may be a magnet URI, an http(s) link to a .torrent
file (as Jackett/Prowlarr often return), or a local .torrent path.

Torrents are tracked by info-hash, so several episodes can share one torrent
(season packs): each episode selects its own file inside the torrent and only
selected files are downloaded.

The streamer is single-threaded by design: call ``poll()`` regularly (the app
does this from a QTimer, tools/load_channels.py from its main loop). Only the
network fetch of .torrent URLs runs on a worker thread, and it hands results
back through a queue that ``poll()`` drains.
"""

from typing import Dict, Optional, List, Any, Tuple
import hashlib
import logging
import os
import queue
import re
import threading
import time

logger = logging.getLogger(__name__)

try:
    import libtorrent as lt
except Exception:  # pragma: no cover - libtorrent may not be present in test env
    lt = None

try:
    import requests
except Exception:  # pragma: no cover
    requests = None


VIDEO_EXTS = (
    ".mp4", ".mkv", ".avi", ".m4v", ".mov", ".mpg", ".mpeg", ".ts", ".m2ts",
    ".wmv", ".divx", ".ogm", ".webm", ".vob", ".flv",
)

# Seconds of contiguous data ahead of the playhead required before tuning in
# to a partially downloaded file.
MIN_BUFFER_SECONDS = 45.0
# Pieces at the start/end of a file that must be present before playback, so
# container headers and trailing indexes (MP4 moov, AVI idx1, MKV cues) exist.
HEAD_PIECES = 3
TAIL_PIECES = 2
# Pieces ahead of the playhead that get streaming deadlines.
DEADLINE_WINDOW_PIECES = 24
# How often resume data (which pieces are on disk) is saved, so restarts skip re-checking files.
RESUME_SAVE_INTERVAL = 60.0
# Piece priorities for an episode that is airing: from the live point on first, the part already aired last.
PRIORITY_LIVE = 7
PRIORITY_NORMAL = 4
PRIORITY_AIRED = 1


class TorrentStreamerError(Exception):
    pass


def _layout(ti):
    """file_storage of a torrent_info (libtorrent 2.1 renamed files() to layout())."""
    return ti.layout() if hasattr(ti, "layout") else ti.files()


def episode_key(ep: Dict) -> str:
    if ep.get("key"):
        return str(ep["key"])
    return f"{ep.get('channel', '')}|{ep.get('id') or ep.get('title') or ep.get('magnet', '')}"


def _episode_patterns(season: Any, episode: Any) -> List["re.Pattern"]:
    try:
        s = int(season)
        e = int(episode)
    except (TypeError, ValueError):
        return []
    return [
        re.compile(rf"(?<![a-z0-9])s0*{s}[ ._-]*e0*{e}(?![0-9])", re.IGNORECASE),
        re.compile(rf"(?<![0-9]){s}x0*{e}(?![0-9])", re.IGNORECASE),
        re.compile(rf"season[ ._-]*0*{s}\D+?(?:episode|ep)[ ._-]*0*{e}(?![0-9])", re.IGNORECASE),
    ]


def select_episode_file(files: List[Tuple[int, str, int]], season: Any = None, episode: Any = None) -> Optional[int]:
    """Pick the file index inside a torrent that holds the wanted episode.

    files: list of (index, path_inside_torrent, size_bytes).
    Prefers video files whose name matches SxxEyy / NxNN for the given
    season/episode, then falls back to the largest video file. Samples are
    ignored. Returns None for torrents with no usable file.
    """
    if not files:
        return None

    def is_sample(path: str) -> bool:
        name = os.path.basename(path).lower()
        parts = re.split(r"[\\/]", path.lower())
        return "sample" in parts[:-1] or bool(re.search(r"(?<![a-z])sample(?![a-z])", name))

    videos = [f for f in files if f[1].lower().endswith(VIDEO_EXTS) and not is_sample(f[1])]
    if not videos:
        videos = [f for f in files if f[1].lower().endswith(VIDEO_EXTS)]
    if not videos:
        return None

    patterns = _episode_patterns(season, episode)
    if patterns and len(videos) > 1:
        matches = [f for f in videos if any(p.search(f[1]) for p in patterns)]
        if matches:
            return max(matches, key=lambda f: f[2])[0]
        logger.warning("No file matched S%sE%s; falling back to the largest video file", season, episode)
    return max(videos, key=lambda f: f[2])[0]


def live_piece_priorities(first_piece: int, last_piece: int, live_piece: Optional[int]) -> Dict[int, int]:
    """Priority per piece of one file. With a live position, pieces from it to the
    end (plus the container header/trailer) come first and the already-aired part
    last; without one, every piece gets the normal priority."""
    if live_piece is None or live_piece <= first_piece:
        return {p: PRIORITY_NORMAL for p in range(first_piece, last_piece + 1)}
    live_piece = min(live_piece, last_piece)
    priorities = {}
    for p in range(first_piece, last_piece + 1):
        priorities[p] = PRIORITY_LIVE if p >= live_piece else PRIORITY_AIRED
    for p in list(range(first_piece, min(last_piece, first_piece + HEAD_PIECES - 1) + 1)) + \
            list(range(max(first_piece, last_piece - TAIL_PIECES + 1), last_piece + 1)):
        priorities[p] = PRIORITY_LIVE
    return priorities


def compute_readiness(
    have: List[bool],
    first_piece: int,
    last_piece: int,
    file_size: int,
    piece_length: int,
    file_offset_in_torrent: int,
    downloaded_bytes: int,
    offset_seconds: float,
    duration_seconds: float,
    download_rate: float,
    min_buffer_seconds: float = MIN_BUFFER_SECONDS,
) -> Dict[str, Any]:
    """Decide whether a (possibly partial) file can be played from offset_seconds.

    Pure function so it can be tested without libtorrent. ``have`` is the
    torrent-wide piece bitfield.
    """
    total = max(1, file_size)
    progress = min(1.0, downloaded_bytes / total)
    if downloaded_bytes >= file_size > 0:
        return {"ready": True, "complete": True, "progress": 1.0, "buffered_seconds": float("inf")}

    def has(p: int) -> bool:
        return 0 <= p < len(have) and bool(have[p])

    head_end = min(last_piece, first_piece + HEAD_PIECES - 1)
    head_ok = all(has(p) for p in range(first_piece, head_end + 1))
    tail_start = max(first_piece, last_piece - TAIL_PIECES + 1)
    tail_ok = all(has(p) for p in range(tail_start, last_piece + 1))

    result = {"ready": False, "complete": False, "progress": progress, "buffered_seconds": 0.0}
    if duration_seconds <= 0 or not head_ok or not tail_ok:
        return result

    bytes_per_second = file_size / duration_seconds
    fraction = min(max(offset_seconds / duration_seconds, 0.0), 1.0)
    offset_byte = file_offset_in_torrent + int(file_size * fraction)
    start_piece = max(first_piece, min(last_piece, offset_byte // max(1, piece_length)))

    contiguous = 0
    piece = start_piece
    while piece <= last_piece and has(piece):
        contiguous += 1
        piece += 1
    buffered_bytes = contiguous * piece_length
    buffered_seconds = buffered_bytes / max(1.0, bytes_per_second)
    remaining_seconds = max(0.0, duration_seconds - offset_seconds)
    missing_pieces = sum(1 for p in range(start_piece, last_piece + 1) if not has(p))
    missing_bytes = missing_pieces * piece_length

    result["buffered_seconds"] = buffered_seconds
    if piece > last_piece:
        # Everything from the playhead to the end is present.
        result["ready"] = True
        return result
    keeps_up = download_rate > 0 and (download_rate * remaining_seconds * 0.85) >= missing_bytes
    result["ready"] = buffered_seconds >= min_buffer_seconds and keeps_up
    return result


class TorrentStreamer:
    def __init__(
        self,
        save_root: str = "data/videos",
        listen_port: int = 6881,
        torrent_cache_dir: Optional[str] = None,
        download_rate_limit: int = 0,
        upload_rate_limit: int = 0,
        delete_evicted: bool = True,
        active_downloads: int = 8,
        min_buffer_seconds: float = MIN_BUFFER_SECONDS,
    ):
        if lt is None:
            raise TorrentStreamerError("libtorrent is required but not installed (pip install libtorrent)")
        self.save_root = os.path.abspath(save_root)
        self.torrent_cache_dir = os.path.abspath(torrent_cache_dir or os.path.join(self.save_root, ".torrents"))
        self.delete_evicted = delete_evicted
        self.min_buffer_seconds = float(min_buffer_seconds)
        os.makedirs(self.save_root, exist_ok=True)
        os.makedirs(self.torrent_cache_dir, exist_ok=True)
        settings = {
            "listen_interfaces": f"0.0.0.0:{int(listen_port)},[::]:{int(listen_port)}",
            "alert_mask": int(lt.alert.category_t.error_notification | lt.alert.category_t.status_notification),
            "download_rate_limit": int(download_rate_limit),
            "upload_rate_limit": int(upload_rate_limit),
            # libtorrent queues all but 3 downloads by default; allow more, and
            # buffer_episodes() orders the queue so the important ones run first.
            "active_downloads": int(active_downloads),
            "active_seeds": 100,
        }
        self.session = lt.session(settings)
        # info_hash -> {"handle", "save_path", "added_at", "metadata_saved"}
        self.torrents: Dict[str, Dict[str, Any]] = {}
        # episode key -> {"ep", "info_hash", "file_index", "error", "state"}
        self.episodes: Dict[str, Dict[str, Any]] = {}
        self._fetch_results: "queue.Queue[Tuple[str, Any, Optional[str]]]" = queue.Queue()
        self._focus: Optional[Tuple[str, float, float]] = None
        self._queue_order: List[str] = []
        self._priority_keys: List[str] = []
        self._last_resume_save = time.time()
        logger.info("TorrentStreamer session started, saving to: %s", self.save_root)

    # ---------------- paths ---------------- #

    def episode_save_dir(self, ep: Dict) -> str:
        """Directory torrents for this episode are saved into."""
        custom = (ep.get("save_path") or "").strip()
        if custom:
            if custom.lower().endswith(VIDEO_EXTS):
                custom = os.path.dirname(custom)
            # Legacy channel files used repo-relative "data/videos/..." paths.
            for prefix in ("data/videos/", "data\\videos\\"):
                if custom.startswith(prefix):
                    custom = custom[len(prefix):]
            if custom:
                return custom if os.path.isabs(custom) else os.path.join(self.save_root, custom)
        channel = re.sub(r"[^A-Za-z0-9._ -]+", "_", str(ep.get("channel") or "misc")).strip() or "misc"
        return os.path.join(self.save_root, channel)

    def _cache_path(self, name: str) -> str:
        return os.path.join(self.torrent_cache_dir, f"{name}.torrent")

    def _resume_path(self, info_hash: str) -> str:
        return os.path.join(self.torrent_cache_dir, f"{info_hash}.resume")

    @staticmethod
    def _params_hash(atp) -> Optional[str]:
        try:
            if atp.ti is not None:
                return str(atp.ti.info_hashes().get_best())
            return str(atp.info_hashes.get_best())
        except Exception:
            return None

    def _with_resume_data(self, atp):
        """Swap in saved resume data (pieces already on disk) when we have it."""
        info_hash = self._params_hash(atp)
        path = self._resume_path(info_hash) if info_hash else ""
        if not path or not os.path.isfile(path):
            return atp
        try:
            with open(path, "rb") as fh:
                resumed = lt.read_resume_data(fh.read())
        except Exception:
            logger.debug("Ignoring unreadable resume data %s", path)
            return atp
        if resumed.ti is None and atp.ti is not None:
            resumed.ti = atp.ti
        resumed.trackers = list(resumed.trackers) + [t for t in atp.trackers if t not in resumed.trackers]
        resumed.flags = (resumed.flags & ~lt.torrent_flags.paused) | lt.torrent_flags.auto_managed
        return resumed

    def _write_resume(self, alert) -> None:
        try:
            info_hash = str(alert.params.info_hashes.get_best())
            if info_hash not in self.torrents:
                return
            with open(self._resume_path(info_hash), "wb") as fh:
                fh.write(lt.write_resume_data_buf(alert.params))
        except Exception:
            logger.debug("Could not write resume data", exc_info=True)

    def save_resume_data(self, only_modified: bool = True) -> int:
        """Ask libtorrent to save resume data; returns how many requests were made."""
        flags = lt.torrent_handle.save_info_dict
        if only_modified:
            flags |= lt.torrent_handle.only_if_modified
        requested = 0
        for torrent in self.torrents.values():
            handle = torrent["handle"]
            if handle.is_valid() and handle.status().has_metadata and (not only_modified or handle.need_save_resume_data()):
                handle.save_resume_data(flags)
                requested += 1
        return requested

    # ---------------- adding sources ---------------- #

    @staticmethod
    def _handle_hash(handle) -> str:
        try:
            return str(handle.info_hashes().get_best())
        except Exception:
            return str(handle.info_hash())

    def _magnet_params(self, magnet: str):
        atp = lt.parse_magnet_uri(magnet)
        try:
            info_hash = str(atp.info_hashes.get_best())
        except Exception:
            info_hash = str(atp.info_hash)
        cached = self._cache_path(info_hash)
        if os.path.isfile(cached):
            try:
                atp.ti = lt.torrent_info(cached)
            except Exception:
                logger.debug("Ignoring unreadable cached metadata %s", cached)
        return atp

    def _torrent_file_params(self, data: bytes):
        atp = lt.add_torrent_params()
        atp.ti = lt.torrent_info(lt.bdecode(data))
        return atp

    def _fetch_torrent_url(self, key: str, url: str) -> None:
        """Worker thread: download a .torrent (or follow a redirect to a magnet)."""
        cached = self._cache_path(hashlib.sha1(url.encode("utf-8")).hexdigest())
        try:
            if os.path.isfile(cached):
                with open(cached, "rb") as fh:
                    self._fetch_results.put((key, fh.read(), None))
                return
            if requests is None:
                raise TorrentStreamerError("requests is required to fetch .torrent URLs")
            target = url
            for _ in range(5):
                response = requests.get(target, timeout=30, allow_redirects=False)
                location = response.headers.get("Location", "")
                if response.is_redirect and location:
                    if location.startswith("magnet:"):
                        self._fetch_results.put((key, location, None))
                        return
                    target = requests.compat.urljoin(target, location)
                    continue
                response.raise_for_status()
                data = response.content
                with open(cached, "wb") as fh:
                    fh.write(data)
                self._fetch_results.put((key, data, None))
                return
            raise TorrentStreamerError("Too many redirects")
        except Exception as exc:
            self._fetch_results.put((key, None, f"Could not fetch torrent: {exc}"))

    def _attach(self, key: str, atp) -> None:
        info = self.episodes.get(key)
        if info is None:
            return
        save_path = self.episode_save_dir(info["ep"])
        atp = self._with_resume_data(atp)
        atp.save_path = save_path
        os.makedirs(atp.save_path, exist_ok=True)
        atp.storage_mode = lt.storage_mode_t.storage_mode_sparse
        # Download nothing until poll() knows which file the episode needs.
        atp.flags |= lt.torrent_flags.sequential_download | lt.torrent_flags.default_dont_download
        handle = self.session.add_torrent(atp)
        info_hash = self._handle_hash(handle)
        if info_hash not in self.torrents:
            now = time.time()
            self.torrents[info_hash] = {
                "handle": handle, "save_path": atp.save_path, "added_at": now, "metadata_saved": False,
                "last_progress_at": now, "progress_marker": None, "piece_priorities": None,
            }
            logger.info("Added torrent %s -> %s", info_hash, atp.save_path)
        info["info_hash"] = info_hash
        info["state"] = "metadata"

    def add_episode(self, ep: Dict) -> str:
        """Start fetching an episode. Returns its key. Safe to call repeatedly."""
        key = episode_key(ep)
        existing = self.episodes.get(key)
        source = (ep.get("magnet") or "").strip()
        if existing and existing["ep"].get("magnet") == ep.get("magnet"):
            existing["ep"] = dict(ep)
            return key
        if existing:
            self.remove_episode(key, delete_files=False)
        info = {"ep": dict(ep), "info_hash": None, "file_index": None, "error": None, "state": "resolving"}
        self.episodes[key] = info
        if not source:
            info["state"] = "error"
            info["error"] = "No magnet link yet"
            return key
        try:
            if source.startswith("magnet:"):
                self._attach(key, self._magnet_params(source))
            elif source.lower().startswith(("http://", "https://")):
                threading.Thread(target=self._fetch_torrent_url, args=(key, source), daemon=True).start()
            elif os.path.isfile(source):
                with open(source, "rb") as fh:
                    self._attach(key, self._torrent_file_params(fh.read()))
            else:
                raise TorrentStreamerError(f"Unsupported torrent source: {source[:60]}")
        except Exception as exc:
            logger.exception("Failed to add episode %s", key)
            info["state"] = "error"
            info["error"] = str(exc)
        return key

    # ---------------- removal ---------------- #

    def _episodes_for_hash(self, info_hash: str) -> List[str]:
        return [k for k, v in self.episodes.items() if v.get("info_hash") == info_hash]

    def remove_episode(self, key: str, delete_files: bool = True) -> None:
        info = self.episodes.pop(key, None)
        if not info or not info.get("info_hash"):
            return
        info_hash = info["info_hash"]
        torrent = self.torrents.get(info_hash)
        if not torrent:
            return
        if not self._episodes_for_hash(info_hash):
            flags = lt.options_t.delete_files if delete_files else 0
            if delete_files:
                try:
                    os.remove(self._resume_path(info_hash))
                except OSError:
                    pass
            try:
                self.session.remove_torrent(torrent["handle"], flags)
                logger.info("Removed torrent %s%s", info_hash, " and its files" if delete_files else "")
            except Exception:
                logger.exception("Failed to remove torrent %s", info_hash)
            del self.torrents[info_hash]
            return
        # Other episodes still use this torrent (season pack): drop just this file.
        index = info.get("file_index")
        if index is None:
            return
        still_wanted = any(self.episodes[k].get("file_index") == index for k in self._episodes_for_hash(info_hash))
        if still_wanted:
            return
        handle = torrent["handle"]
        try:
            handle.file_priority(index, 0)
        except Exception:
            logger.debug("Could not lower priority of file %s", index)
        if delete_files:
            path = self._file_path(torrent, index)
            if path and os.path.isfile(path):
                try:
                    os.remove(path)
                    logger.info("Deleted file %s", path)
                except OSError:
                    logger.debug("Could not delete %s", path)

    def mark_episode_viewed(self, ep: Dict) -> None:
        """Delete the episode's downloaded file and drop its torrent so it can be
        fetched again the next time it enters the buffer window."""
        self.remove_episode(episode_key(ep), delete_files=True)

    def buffer_episodes(self, episodes: List[Dict], max_active: Optional[int] = None) -> None:
        """Make sure exactly these episodes are downloading, evicting everything
        else. The list is in priority order: earlier episodes get download slots first."""
        wanted = [ep for ep in episodes if (ep.get("magnet") or "").strip()]
        if max_active:
            wanted = wanted[:max_active]
        keep = []
        for ep in wanted:
            keep.append(self.add_episode(ep))
        for key in list(self.episodes.keys()):
            if key not in keep:
                logger.debug("Evicting episode from buffer: %s", key)
                self.remove_episode(key, delete_files=self.delete_evicted)
        self._apply_queue_order(keep)

    def _apply_queue_order(self, keys: Optional[List[str]] = None) -> None:
        """Order libtorrent's download queue by episode priority. Only torrents
        that are in the queue (still downloading) have a position; re-checked on
        every poll because torrents enter the queue once their files are selected."""
        if keys is not None:
            self._priority_keys = list(keys)
        order: List[str] = []
        for key in self._priority_keys:
            info_hash = (self.episodes.get(key) or {}).get("info_hash")
            torrent = self.torrents.get(info_hash or "")
            if torrent and info_hash not in order and torrent["handle"].queue_position() >= 0:
                order.append(info_hash)
        if order == self._queue_order:
            return
        current = sorted(order, key=lambda h: self.torrents[h]["handle"].queue_position())
        if current != order:
            # Moving each torrent to the bottom in turn leaves the queue in list order.
            for info_hash in order:
                try:
                    self.torrents[info_hash]["handle"].queue_position_bottom()
                except Exception:
                    logger.debug("Could not reorder %s", info_hash)
        self._queue_order = order

    # ---------------- polling ---------------- #

    def _file_path(self, torrent: Dict, index: int) -> Optional[str]:
        ti = torrent["handle"].torrent_file()
        if ti is None:
            return None
        return os.path.join(torrent["save_path"], _layout(ti).file_path(index))

    def _save_metadata(self, info_hash: str, torrent: Dict) -> None:
        if torrent.get("metadata_saved"):
            return
        torrent["metadata_saved"] = True
        path = self._cache_path(info_hash)
        if os.path.isfile(path):
            return
        try:
            ti = torrent["handle"].torrent_file()
            data = lt.bencode({b"info": lt.bdecode(ti.info_section())})
            with open(path, "wb") as fh:
                fh.write(data)
        except Exception:
            logger.debug("Could not cache metadata for %s", info_hash)

    def _apply_file_priorities(self, info_hash: str) -> None:
        torrent = self.torrents[info_hash]
        handle = torrent["handle"]
        ti = handle.torrent_file()
        if ti is None:
            return
        wanted = {self.episodes[k]["file_index"] for k in self._episodes_for_hash(info_hash)}
        wanted.discard(None)
        priorities = [4 if i in wanted else 0 for i in range(ti.num_files())]
        if list(handle.get_file_priorities()) != priorities:
            handle.prioritize_files(priorities)
            torrent["piece_priorities"] = None  # file priorities reset piece priorities; re-apply them

    def _apply_piece_priorities(self, info_hash: str) -> None:
        """Prioritize pieces from each airing episode's live position onward."""
        torrent = self.torrents[info_hash]
        handle = torrent["handle"]
        ti = handle.torrent_file()
        if ti is None:
            return
        live = False
        priorities = [0] * ti.num_pieces()
        for key in self._episodes_for_hash(info_hash):
            info = self.episodes[key]
            layout = self._file_layout(info)
            if not layout:
                continue
            _t, _h, _i, size, offset, piece_length, first_piece, last_piece = layout
            ep = info["ep"]
            live_offset = float(ep.get("live_offset") or 0)
            live_duration = float(ep.get("live_duration") or 0)
            live_piece = None
            if live_offset > 0 and live_duration > 0:
                live_piece = (offset + int(size * min(1.0, live_offset / live_duration))) // piece_length
                live = True
            for piece, priority in live_piece_priorities(first_piece, last_piece, live_piece).items():
                priorities[piece] = max(priorities[piece], priority)
        if not live and torrent["piece_priorities"] is None:
            return  # file priorities already express "download the wanted files normally"
        if priorities == torrent["piece_priorities"]:
            return
        # Priority calls are applied asynchronously, so only remember them once
        # libtorrent reports them back (checked again on the next poll).
        if list(handle.get_piece_priorities()) == priorities:
            torrent["piece_priorities"] = priorities
        else:
            handle.prioritize_pieces(priorities)

    def _handle_alerts(self) -> int:
        """Process pending alerts; returns how many resume-data saves finished."""
        finished = 0
        for alert in self.session.pop_alerts():
            if isinstance(alert, lt.save_resume_data_alert):
                self._write_resume(alert)
                finished += 1
            elif isinstance(alert, lt.save_resume_data_failed_alert):
                finished += 1
            elif alert.category() & lt.alert.category_t.error_notification:
                logger.warning("libtorrent: %s", alert.message())
        return finished

    def poll(self) -> None:
        while True:
            try:
                key, payload, error = self._fetch_results.get_nowait()
            except queue.Empty:
                break
            info = self.episodes.get(key)
            if info is None:
                continue
            if error:
                info["state"] = "error"
                info["error"] = error
                continue
            try:
                atp = self._magnet_params(payload) if isinstance(payload, str) else self._torrent_file_params(payload)
                self._attach(key, atp)
            except Exception as exc:
                info["state"] = "error"
                info["error"] = f"Invalid torrent: {exc}"

        self._handle_alerts()

        now = time.time()
        for info_hash, torrent in self.torrents.items():
            handle = torrent["handle"]
            if not handle.is_valid():
                continue
            st = handle.status()
            # Anything that moves counts as progress: metadata arriving, wanted bytes, or a file check.
            marker = (bool(st.has_metadata), int(st.total_wanted_done))
            # Waiting in libtorrent's download queue is not the release's fault.
            queued = bool(getattr(st, "paused", False)) and bool(getattr(st, "auto_managed", False))
            if marker != torrent["progress_marker"] or queued or st.state == lt.torrent_status.states.checking_files:
                torrent["progress_marker"] = marker
                torrent["last_progress_at"] = now
            if not st.has_metadata:
                continue
            self._save_metadata(info_hash, torrent)
            ti = handle.torrent_file()
            files = _layout(ti)
            listing = [(i, files.file_path(i), files.file_size(i)) for i in range(files.num_files())]
            changed = False
            for key in self._episodes_for_hash(info_hash):
                info = self.episodes[key]
                if info["file_index"] is not None:
                    continue
                ep = info["ep"]
                index = select_episode_file(listing, ep.get("season"), ep.get("episode"))
                if index is None:
                    info["state"] = "error"
                    info["error"] = "Torrent has no video file"
                    continue
                info["file_index"] = index
                info["state"] = "downloading"
                changed = True
                logger.info("Episode %s -> %s", key, files.file_path(index))
            if changed:
                self._apply_file_priorities(info_hash)

        for info_hash, torrent in self.torrents.items():
            if torrent["handle"].is_valid() and torrent["handle"].torrent_file() is not None:
                self._apply_piece_priorities(info_hash)
        self._apply_queue_order()
        if self._focus:
            self._apply_focus(*self._focus)
        if now - self._last_resume_save >= RESUME_SAVE_INTERVAL:
            self._last_resume_save = now
            self.save_resume_data(only_modified=True)

    # ---------------- streaming ---------------- #

    def _file_layout(self, info: Dict):
        torrent = self.torrents.get(info.get("info_hash") or "")
        index = info.get("file_index")
        if not torrent or index is None:
            return None
        handle = torrent["handle"]
        ti = handle.torrent_file()
        if ti is None:
            return None
        files = _layout(ti)
        size = files.file_size(index)
        offset = files.file_offset(index)
        piece_length = ti.piece_length()
        first_piece = offset // piece_length
        last_piece = (offset + max(0, size - 1)) // piece_length
        return torrent, handle, index, size, offset, piece_length, first_piece, last_piece

    def focus_episode(self, ep: Dict, offset_seconds: float = 0.0, duration_seconds: float = 0.0) -> None:
        """Tell the streamer which episode is on screen so the pieces around the
        playhead (plus container header/trailer) are fetched first."""
        self._focus = (episode_key(ep), float(offset_seconds or 0), float(duration_seconds or 0))
        self._apply_focus(*self._focus)

    def clear_focus(self) -> None:
        self._focus = None

    def _apply_focus(self, key: str, offset_seconds: float, duration_seconds: float) -> None:
        info = self.episodes.get(key)
        layout = self._file_layout(info) if info else None
        if not layout:
            return
        _torrent, handle, _index, size, offset, piece_length, first_piece, last_piece = layout
        if handle.queue_position() > 0:
            handle.queue_position_top()  # the episode on screen must never wait in the queue
        fraction = min(max(offset_seconds / duration_seconds, 0.0), 1.0) if duration_seconds > 0 else 0.0
        start_piece = max(first_piece, min(last_piece, (offset + int(size * fraction)) // piece_length))
        targets = list(range(first_piece, min(last_piece, first_piece + HEAD_PIECES - 1) + 1))
        targets += list(range(max(first_piece, last_piece - TAIL_PIECES + 1), last_piece + 1))
        targets += list(range(start_piece, min(last_piece, start_piece + DEADLINE_WINDOW_PIECES) + 1))
        seen = set()
        for order, piece in enumerate(targets):
            if piece in seen or handle.have_piece(piece):
                continue
            seen.add(piece)
            handle.set_piece_deadline(piece, 500 + order * 150)

    def episode_status(self, ep: Dict, offset_seconds: float = 0.0, duration_seconds: float = 0.0) -> Dict[str, Any]:
        """Return a status dict: state, ready, progress, file_path, message, etc."""
        key = episode_key(ep)
        info = self.episodes.get(key)
        if info is None:
            return {"state": "idle", "ready": False, "progress": 0.0, "file_path": None, "message": "Not buffered"}
        if info["state"] == "error":
            return {"state": "error", "ready": False, "progress": 0.0, "file_path": None, "message": info["error"] or "Torrent error"}
        if info["state"] == "resolving" or not info.get("info_hash"):
            return {"state": "resolving", "ready": False, "progress": 0.0, "file_path": None, "message": "Fetching torrent..."}
        torrent = self.torrents.get(info["info_hash"])
        if torrent is None:
            return {"state": "idle", "ready": False, "progress": 0.0, "file_path": None, "message": "Not buffered"}
        status = torrent["handle"].status()
        base = {
            "download_rate": float(status.download_payload_rate),
            "peers": int(status.num_peers),
            "seeds": int(status.num_seeds),
            "stalled_seconds": max(0.0, time.time() - torrent.get("last_progress_at", time.time())),
        }
        if status.state == lt.torrent_status.states.checking_files:
            return dict(base, state="checking", ready=False, progress=float(status.progress), file_path=None, message="Checking existing download...")
        layout = self._file_layout(info)
        if not layout:
            return dict(base, state="metadata", ready=False, progress=0.0, file_path=None, message=f"Finding peers... ({base['peers']} connected)")
        _torrent, handle, index, size, offset, piece_length, first_piece, last_piece = layout
        file_path = self._file_path(torrent, index)
        downloaded = int(handle.file_progress(flags=1)[index]) if size else 0
        have = list(handle.status(lt.status_flags_t.query_pieces).pieces)
        readiness = compute_readiness(
            have, first_piece, last_piece, size, piece_length, offset, downloaded,
            float(offset_seconds or 0), float(duration_seconds or 0), base["download_rate"],
            min_buffer_seconds=self.min_buffer_seconds,
        )
        rate_kb = base["download_rate"] / 1024.0
        if readiness["complete"]:
            message = "Downloaded"
        else:
            message = f"Buffering {readiness['progress'] * 100:.0f}% - {rate_kb:.0f} KB/s from {base['peers']} peers"
        return dict(
            base,
            state="complete" if readiness["complete"] else "downloading",
            ready=bool(readiness["ready"]) and bool(file_path and os.path.exists(file_path)),
            complete=readiness["complete"],
            progress=readiness["progress"],
            buffered_seconds=readiness["buffered_seconds"],
            file_path=file_path,
            message=message,
        )

    def shutdown(self, resume_timeout: float = 5.0) -> None:
        """Save resume data, then detach all torrents, keeping downloaded files on disk."""
        try:
            outstanding = self.save_resume_data(only_modified=False)
            deadline = time.time() + resume_timeout
            while outstanding > 0 and time.time() < deadline:
                self.session.wait_for_alert(250)
                outstanding -= self._handle_alerts()
            for info_hash, torrent in list(self.torrents.items()):
                try:
                    self.session.remove_torrent(torrent["handle"])
                except Exception:
                    logger.debug("Failed to remove %s on shutdown", info_hash)
            self.torrents.clear()
            self.episodes.clear()
            logger.info("TorrentStreamer shutdown complete")
        except Exception:
            logger.exception("Error during TorrentStreamer shutdown")


if __name__ == "__main__":
    print("torrent_streamer.py loaded. Use TorrentStreamer in your application.")
