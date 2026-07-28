"""
Update tools/load_channels.py to use TorrentStreamer when --stream is passed.
It will populate missing magnets via IndexerClient as before, then initialize
TorrentStreamer and buffer the next N episodes for the current, previous, and
next channels.
"""

from __future__ import annotations

import argparse
import os
import logging
import xml.etree.ElementTree as ET
from xml.dom import minidom
from typing import List, Dict, Optional
from indexer_client import IndexerClient, NoMagnetFound

# New import
from torrent_streamer import TorrentStreamer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_CHANNELS_DIR = "channels"


def _pretty_xml(elem: ET.Element) -> str:
    rough = ET.tostring(elem, encoding="utf-8")
    reparsed = minidom.parseString(rough)
    return reparsed.toprettyxml(indent="  ")


def load_channel_file(path: str) -> ET.Element:
    tree = ET.parse(path)
    return tree.getroot()


def find_episode_elements(root: ET.Element) -> List[ET.Element]:
    return list(root.findall("episode"))


def get_child_text(ep: ET.Element, tag: str) -> Optional[str]:
    el = ep.find(tag)
    if el is None:
        return None
    if el.text is None:
        return None
    txt = el.text.strip()
    return txt if txt != "" else None


def set_child_text(ep: ET.Element, tag: str, text: str) -> None:
    el = ep.find(tag)
    if el is None:
        el = ET.SubElement(ep, tag)
    el.text = text


def parse_channel_to_episode_dicts(path: str) -> List[Dict]:
    """Return ordered list of episode dicts for the channel XML at path."""
    root = load_channel_file(path)
    channel_name = root.get("name") or os.path.splitext(os.path.basename(path))[0]
    episodes: List[Dict] = []
    for idx, ep in enumerate(find_episode_elements(root)):
        ep_dict = {}
        ep_dict["channel"] = channel_name
        ep_dict["id"] = ep.get("id") or f"{channel_name}-{idx}"
        ep_dict["season"] = ep.get("season")
        ep_dict["episode"] = ep.get("episode")

        def text_or_attr(tag, fallback=None):
            el = ep.find(tag)
            if el is not None and el.text and el.text.strip():
                return el.text.strip()
            return ep.get(tag) or fallback

        ep_dict["title"] = text_or_attr("title", fallback=ep_dict["id"])
        ep_dict["magnet"] = text_or_attr("magnet", fallback=None)
        ep_dict["save_path"] = text_or_attr("save_path", fallback=f"data/videos/{channel_name}/{ep_dict['id']}.mp4")
        ep_dict["min_seeders"] = int(text_or_attr("min_seeders", fallback="0"))
        ep_dict["priority"] = int(text_or_attr("priority", fallback=str(idx)))
        ep_dict["note"] = text_or_attr("note", fallback="")
        ep_dict["_order_index"] = idx
        episodes.append(ep_dict)
    return episodes


def process_channel_file(path: str, indexer: IndexerClient, categories: Optional[List[int]] = None, min_seeders_default: int = 0) -> None:
    logger.info("Processing channel file: %s", path)
    root = load_channel_file(path)
    episodes = find_episode_elements(root)
    modified = False

    for idx, ep in enumerate(episodes):
        title = get_child_text(ep, "title")
        if not title:
            logger.warning("Skipping episode with missing title in %s (element index %d)", path, idx)
            continue

        magnet = get_child_text(ep, "magnet")
        if magnet:
            logger.info("Episode '%s' already has magnet; skipping", title)
            continue

        min_seeders_text = get_child_text(ep, "min_seeders")
        try:
            min_seeders = int(min_seeders_text) if min_seeders_text is not None else min_seeders_default
        except ValueError:
            min_seeders = min_seeders_default

        try:
            logger.info("Searching indexer for title: %s", title)
            best_magnet = indexer.get_best_magnet(title, categories=categories)
            set_child_text(ep, "magnet", best_magnet)
            modified = True
            logger.info("Wrote magnet for '%s' into %s", title, path)
        except NoMagnetFound:
            logger.warning("No magnet found for title: %s", title)
        except Exception as exc:
            logger.exception("Error while searching for '%s': %s", title, exc)

    if modified:
        pretty = _pretty_xml(root)
        backup_path = path + ".bak"
        try:
            if os.path.exists(path):
                os.replace(path, backup_path)
        except Exception:
            logger.exception("Failed to create backup of %s", path)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(pretty)
            logger.info("Updated channel file written: %s", path)
        except Exception:
            logger.exception("Failed to write updated XML to %s", path)
    else:
        logger.info("No changes for %s", path)


def process_all_channels(channels_dir: str, indexer: IndexerClient, categories: Optional[List[int]] = None) -> List[str]:
    """Process all channels and return sorted list of channel file paths."""
    if not os.path.isdir(channels_dir):
        logger.error("Channels directory not found: %s", channels_dir)
        return []
    files = []
    for fname in sorted(os.listdir(channels_dir)):
        if not fname.lower().endswith(".xml"):
            continue
        path = os.path.join(channels_dir, fname)
        process_channel_file(path, indexer, categories=categories)
        files.append(path)
    return files


def collect_buffer_episodes(channels_files: List[str], current_channel_file: Optional[str], current_episode_id: Optional[str], per_channel_count: int = 3) -> List[Dict]:
    """From the list of channel XML file paths, determine the episodes to buffer.
    We take the current channel, plus the one above and below (by order in channels_files),
    and for each channel pick the next `per_channel_count` episodes starting from
    the current_episode_id if provided, otherwise from the first episode.
    Returns a flat list of episode dicts (with magnet and save_path present where possible).
    """
    if not channels_files:
        return []
    # Resolve current index
    if current_channel_file and current_channel_file in channels_files:
        idx = channels_files.index(current_channel_file)
    else:
        idx = 0
        current_channel_file = channels_files[0]

    indices = [i for i in (idx - 1, idx, idx + 1) if 0 <= i < len(channels_files)]
    buffer_eps: List[Dict] = []

    for i in indices:
        ch_file = channels_files[i]
        eps = parse_channel_to_episode_dicts(ch_file)
        # find start index
        start = 0
        if ch_file == current_channel_file and current_episode_id:
            for j, e in enumerate(eps):
                if e["id"] == current_episode_id:
                    start = j
                    break
        selected = eps[start:start + per_channel_count]
        buffer_eps.extend(selected)
    return buffer_eps


def main() -> None:
    parser = argparse.ArgumentParser(description="Populate channel XMLs with magnet links and optionally start buffering via libtorrent")
    parser.add_argument("--base-url", required=True, help="Base URL for Jackett/Prowlarr (e.g. http://localhost:9117)")
    parser.add_argument("--api-key", required=False, help="API key for the indexer instance")
    parser.add_argument("--channels-dir", default=DEFAULT_CHANNELS_DIR, help="Directory containing channel XML files")
    parser.add_argument("--provider", choices=["jackett", "prowlarr"], default=None, help="Provider hint to IndexerClient")
    parser.add_argument("--stream", action="store_true", help="Start libtorrent streamer and buffer next episodes for current/adjacent channels")
    parser.add_argument("--current-channel-file", help="Channel XML filename to treat as current (relative to channels-dir)")
    parser.add_argument("--current-episode-id", help="Episode id within the current channel to start buffering from")
    args = parser.parse_args()

    indexer = IndexerClient(args.base_url, api_key=args.api_key, provider_hint=args.provider)
    categories = [5030, 2030]

    channel_files = process_all_channels(args.channels_dir, indexer, categories=categories)

    if args.stream:
        # Determine full path to current channel file
        cur_file = None
        if args.current_channel_file:
            candidate = os.path.join(args.channels_dir, args.current_channel_file)
            if candidate in channel_files:
                cur_file = candidate
        # Initialize streamer
        streamer = TorrentStreamer(save_root="data/videos")
        buffer_eps = collect_buffer_episodes(channel_files, cur_file, args.current_episode_id, per_channel_count=3)
        logger.info("Buffering %d episodes across channels", len(buffer_eps))
        streamer.buffer_episodes(buffer_eps)
        logger.info("Buffering started. Press Ctrl+C to exit and shut down streamer.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt, shutting down streamer")
        finally:
            streamer.shutdown()


if __name__ == "__main__":
    main()
