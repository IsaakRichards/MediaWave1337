"""
torrent_channels.py

Channel playlist XML handling shared by tools/load_channels.py and the
MediaWave1337 player.

One XML file per channel, episodes in playback order:

    <channel name="Twilight Zone" number="3" default_duration="25m">
      <episode id="tz-s01e01" season="1" episode="1">
        <title>Twilight Zone S01E01</title>
        <query>The Twilight Zone 1959 S01E01</query>   (optional search text)
        <duration>25:10</duration>                     (optional, filled in automatically)
        <magnet></magnet>                              (filled in from the indexer)
        <min_seeders>1</min_seeders>
      </episode>
    </channel>
"""

from __future__ import annotations

import collections
import logging
import os
import re
import threading
import xml.etree.ElementTree as ET
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_EPISODE_DURATION = 30 * 60.0
DEFAULT_CATEGORIES = [5030, 2030]

_write_lock = threading.RLock()


def parse_duration(text: Optional[str]) -> float:
    """Parse '1500', '1500s', '25m', '25 min', '25:30', '1:02:03' into seconds (0 if unknown)."""
    if text is None:
        return 0.0
    value = str(text).strip().lower()
    if not value:
        return 0.0
    if ":" in value:
        try:
            parts = [float(p) for p in value.split(":")]
        except ValueError:
            return 0.0
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + part
        return max(0.0, seconds)
    match = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*(h|hr|hours?|m|min|mins|minutes?|s|sec|secs|seconds?)?", value)
    if not match:
        return 0.0
    number = float(match.group(1))
    unit = (match.group(2) or "s")[0]
    return max(0.0, number * {"h": 3600, "m": 60, "s": 1}[unit])


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _parse_tree(path: str) -> ET.ElementTree:
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    return ET.parse(path, parser=parser)


def _text(ep: ET.Element, tag: str) -> Optional[str]:
    el = ep.find(tag)
    if el is not None and el.text and el.text.strip():
        return el.text.strip()
    value = ep.get(tag)
    return value.strip() if value and value.strip() else None


def _int(value: Optional[str], fallback: int) -> int:
    try:
        return int(value) if value is not None else fallback
    except ValueError:
        return fallback


def _channel_name(root: ET.Element, path: str) -> str:
    return (root.get("name") or "").strip() or os.path.splitext(os.path.basename(path))[0].replace("_", " ").title()


def parse_channel_file(path: str) -> Dict:
    """Return {"name", "path", "number", "default_duration", "episodes": [...]}."""
    root = _parse_tree(path).getroot()
    name = _channel_name(root, path)
    default_duration = parse_duration(root.get("default_duration")) or DEFAULT_EPISODE_DURATION
    episodes: List[Dict] = []
    for idx, ep in enumerate(root.findall("episode")):
        ep_id = (ep.get("id") or "").strip() or f"{name}-{idx}"
        title = _text(ep, "title") or ep_id
        episodes.append({
            "channel": name,
            "channel_file": path,
            "id": ep_id,
            "key": f"{name}|{ep_id}",
            "season": ep.get("season"),
            "episode": ep.get("episode"),
            "title": title,
            "query": _text(ep, "query") or title,
            "magnet": _text(ep, "magnet"),
            "save_path": _text(ep, "save_path"),
            "duration": parse_duration(_text(ep, "duration")),
            "min_seeders": _int(_text(ep, "min_seeders"), 0),
            "priority": _int(_text(ep, "priority"), idx),
            "note": _text(ep, "note") or "",
            "_order_index": idx,
        })
    return {
        "name": name,
        "path": path,
        "number": _int(root.get("number"), 0),
        "default_duration": default_duration,
        "episodes": episodes,
    }


def parse_channel_to_episode_dicts(path: str) -> List[Dict]:
    return parse_channel_file(path)["episodes"]


def list_channel_files(channels_dir: str) -> List[str]:
    if not channels_dir or not os.path.isdir(channels_dir):
        return []
    return [
        os.path.join(channels_dir, fname)
        for fname in sorted(os.listdir(channels_dir), key=str.casefold)
        if fname.lower().endswith(".xml")
    ]


def load_channels_dir(channels_dir: str) -> List[Dict]:
    channels = []
    for path in list_channel_files(channels_dir):
        try:
            channels.append(parse_channel_file(path))
        except (ET.ParseError, OSError) as exc:
            logger.warning("Skipping unreadable channel file %s: %s", path, exc)
    return channels


def _write_tree(tree: ET.ElementTree, path: str) -> None:
    root = tree.getroot()
    for el in root.iter():
        # Normalize whitespace so ET.indent produces stable output.
        if len(el) and el.text is not None and not el.text.strip():
            el.text = None
        if el.tail is not None and not el.tail.strip():
            el.tail = None
    ET.indent(tree, space="  ")
    tmp_path = path + ".tmp"
    tree.write(tmp_path, encoding="utf-8", xml_declaration=True)
    with open(tmp_path, "a", encoding="utf-8") as fh:
        fh.write("\n")
    os.replace(tmp_path, path)


def update_episode_fields(path: str, episode_id: str, fields: Dict[str, str]) -> bool:
    """Set child elements (e.g. magnet, duration) of one episode and save the file."""
    with _write_lock:
        tree = _parse_tree(path)
        target = None
        name = _channel_name(tree.getroot(), path)
        for idx, ep in enumerate(tree.getroot().findall("episode")):
            if (ep.get("id") or f"{name}-{idx}") == episode_id:
                target = ep
                break
        if target is None:
            return False
        changed = False
        for tag, value in fields.items():
            el = target.find(tag)
            if el is None:
                el = ET.SubElement(target, tag)
            if (el.text or "") != value:
                el.text = value
                changed = True
        if changed:
            _write_tree(tree, path)
        return changed


def resolve_missing_magnets(path: str, indexer, categories: Optional[List[int]] = None, min_seeders_default: int = 0) -> int:
    """Look up every episode without a magnet and write results back. Returns count found."""
    from indexer_client import NoMagnetFound

    found = 0
    for ep in parse_channel_to_episode_dicts(path):
        if ep["magnet"]:
            continue
        try:
            logger.info("Searching indexer for: %s", ep["query"])
            link = indexer.get_best_magnet(
                ep["query"],
                categories=categories or DEFAULT_CATEGORIES,
                min_seeders=ep["min_seeders"] or min_seeders_default,
            )
        except NoMagnetFound as exc:
            logger.warning("%s", exc)
            continue
        except Exception:
            logger.exception("Error while searching for '%s'", ep["query"])
            continue
        update_episode_fields(path, ep["id"], {"magnet": link})
        found += 1
    return found


def collect_buffer_episodes(
    channels: List[Dict],
    current_index: int,
    current_episode_id: Optional[str] = None,
    per_channel_count: int = 3,
) -> List[Dict]:
    """Episodes to keep buffered: the next `per_channel_count` episodes of the
    current channel (from current_episode_id), then of the channels directly
    below and above it."""
    if not channels:
        return []
    current_index = max(0, min(current_index, len(channels) - 1))
    order = [current_index] + [i for i in (current_index + 1, current_index - 1) if 0 <= i < len(channels) and i != current_index]
    buffer_eps: List[Dict] = []
    for i in order:
        eps = channels[i]["episodes"]
        start = 0
        if i == current_index and current_episode_id:
            for j, e in enumerate(eps):
                if e["id"] == current_episode_id:
                    start = j
                    break
        buffer_eps.extend(eps[start:start + per_channel_count])
    return buffer_eps


class MagnetResolver:
    """Background indexer lookups. ``callback(ep, link_or_None, error)`` runs on
    the worker thread after the result has been written to the channel file.
    Urgent requests (episodes about to air) are served before the backlog."""

    def __init__(self, indexer, callback: Optional[Callable] = None, categories: Optional[List[int]] = None):
        self.indexer = indexer
        self.callback = callback
        self.categories = categories or DEFAULT_CATEGORIES
        self._urgent: "collections.deque[Dict]" = collections.deque()
        self._backlog: "collections.deque[Dict]" = collections.deque()
        self._pending: set = set()
        self._failed: set = set()
        self._stopped = False
        self._cond = threading.Condition()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def request(self, ep: Dict, urgent: bool = False, retry_failed: bool = False) -> bool:
        if ep.get("magnet"):
            return False
        key = ep["key"]
        with self._cond:
            if key in self._failed and not retry_failed:
                return False
            if key in self._pending:
                if urgent:
                    for queued in list(self._backlog):
                        if queued["key"] == key:
                            self._backlog.remove(queued)
                            self._urgent.append(queued)
                            self._cond.notify()
                return False
            self._failed.discard(key)
            self._pending.add(key)
            (self._urgent if urgent else self._backlog).append(dict(ep))
            self._cond.notify()
        return True

    def is_pending(self, ep: Dict) -> bool:
        with self._cond:
            return ep["key"] in self._pending

    def has_failed(self, ep: Dict) -> bool:
        with self._cond:
            return ep["key"] in self._failed

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()

    def _run(self) -> None:
        from indexer_client import NoMagnetFound

        while True:
            with self._cond:
                while not self._stopped and not self._urgent and not self._backlog:
                    self._cond.wait()
                if self._stopped:
                    return
                ep = self._urgent.popleft() if self._urgent else self._backlog.popleft()
            link, error = None, None
            try:
                link = self.indexer.get_best_magnet(ep["query"], categories=self.categories, min_seeders=ep.get("min_seeders") or 0)
                update_episode_fields(ep["channel_file"], ep["id"], {"magnet": link})
            except NoMagnetFound as exc:
                error = str(exc)
            except Exception as exc:
                logger.exception("Indexer lookup failed for %s", ep.get("query"))
                error = f"Indexer error: {exc}"
            with self._cond:
                self._pending.discard(ep["key"])
                if error:
                    self._failed.add(ep["key"])
                stopped = self._stopped
            if self.callback and not stopped:
                try:
                    self.callback(ep, link, error)
                except Exception:
                    logger.exception("MagnetResolver callback failed")
