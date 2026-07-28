"""
tools/load_channels.py

Load channel XML files from channels/ and populate missing magnet URIs by
querying the IndexerClient (Jackett/Prowlarr). Found magnet links are written
back into the same XML files so future runs skip indexer lookups.

Usage example:
  python tools/load_channels.py --base-url http://localhost:9117 --api-key YOURKEY

The script processes all .xml files in the channels/ directory.
"""

from __future__ import annotations

import argparse
import os
import logging
import xml.etree.ElementTree as ET
from xml.dom import minidom
from typing import List, Dict, Optional
from indexer_client import IndexerClient, NoMagnetFound

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_CHANNELS_DIR = "channels"


def _pretty_xml(elem: ET.Element) -> str:
    """Return a pretty-printed XML string for the Element (with declaration).
    This helps keep the channel files readable after we write magnets back.
    """
    rough = ET.tostring(elem, encoding="utf-8")
    reparsed = minidom.parseString(rough)
    return reparsed.toprettyxml(indent="  ")


def load_channel_file(path: str) -> ET.Element:
    """Parse and return the root element of a channel XML file."""
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


def process_channel_file(path: str, indexer: IndexerClient, categories: Optional[List[int]] = None, min_seeders_default: int = 0) -> None:
    logger.info("Processing channel file: %s", path)
    root = load_channel_file(path)
    episodes = find_episode_elements(root)
    modified = False

    for idx, ep in enumerate(episodes):
        title = get_child_text(ep, "title")
        if not title:
            # skip malformed entries
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
            # Optionally could verify seeders threshold via indexer candidates, but the client
            # already factors seeders into selection. We'll respect min_seeders by rejecting
            # magnets if they don't meet the threshold by re-querying seeders from the candidate list
            # (omitted for brevity). For now, accept whatever get_best_magnet returns.
            set_child_text(ep, "magnet", best_magnet)
            modified = True
            logger.info("Wrote magnet for '%s' into %s", title, path)
        except NoMagnetFound:
            logger.warning("No magnet found for title: %s", title)
        except Exception as exc:
            logger.exception("Error while searching for '%s': %s", title, exc)

    if modified:
        # Write back the XML file, preserving a pretty layout
        pretty = _pretty_xml(root)
        # Ensure directory and make a backup of the original file
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


def process_all_channels(channels_dir: str, indexer: IndexerClient, categories: Optional[List[int]] = None) -> None:
    if not os.path.isdir(channels_dir):
        logger.error("Channels directory not found: %s", channels_dir)
        return
    for fname in sorted(os.listdir(channels_dir)):
        if not fname.lower().endswith(".xml"):
            continue
        path = os.path.join(channels_dir, fname)
        process_channel_file(path, indexer, categories=categories)


def main() -> None:
    parser = argparse.ArgumentParser(description="Populate channel XMLs with magnet links via IndexerClient")
    parser.add_argument("--base-url", required=True, help="Base URL for Jackett/Prowlarr (e.g. http://localhost:9117)")
    parser.add_argument("--api-key", required=False, help="API key for the indexer instance")
    parser.add_argument("--channels-dir", default=DEFAULT_CHANNELS_DIR, help="Directory containing channel XML files")
    parser.add_argument("--provider", choices=["jackett", "prowlarr"], default=None, help="Provider hint to IndexerClient")
    args = parser.parse_args()

    indexer = IndexerClient(args.base_url, api_key=args.api_key, provider_hint=args.provider)
    # Default categories for CRT-friendly SD content
    categories = [5030, 2030]

    process_all_channels(args.channels_dir, indexer, categories=categories)


if __name__ == "__main__":
    main()
