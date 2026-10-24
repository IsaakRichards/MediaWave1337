"""
tools/load_channels.py

Populate missing magnet links in channel XML files from a Jackett/Prowlarr
indexer, and optionally start the libtorrent streamer to buffer the next
episodes of the current channel plus the channels directly above and below it.

The MediaWave1337 player does all of this by itself; this tool is for
pre-filling channel files or buffering headless.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indexer_client import IndexerClient  # noqa: E402
from torrent_channels import (  # noqa: E402
    DEFAULT_CATEGORIES,
    collect_buffer_episodes,
    list_channel_files,
    load_channels_dir,
    resolve_missing_magnets,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_CHANNELS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "channels")


def main() -> None:
    parser = argparse.ArgumentParser(description="Populate channel XMLs with magnet links and optionally start buffering via libtorrent")
    parser.add_argument("--base-url", required=True, help="Base URL for Jackett/Prowlarr (e.g. http://localhost:9117 or http://localhost:9696)")
    parser.add_argument("--api-key", required=False, help="API key for the indexer instance")
    parser.add_argument("--channels-dir", default=DEFAULT_CHANNELS_DIR, help="Directory containing channel XML files")
    parser.add_argument("--provider", choices=["jackett", "prowlarr"], default=None, help="Provider hint to IndexerClient")
    parser.add_argument("--stream", action="store_true", help="Start libtorrent streamer and buffer next episodes for current/adjacent channels")
    parser.add_argument("--save-root", default="data/videos", help="Where torrents are downloaded")
    parser.add_argument("--current-channel-file", help="Channel XML filename to treat as current (relative to channels-dir)")
    parser.add_argument("--current-episode-id", help="Episode id within the current channel to start buffering from")
    parser.add_argument("--per-channel", type=int, default=3, help="Episodes to buffer per channel")
    args = parser.parse_args()

    indexer = IndexerClient(args.base_url, api_key=args.api_key, provider_hint=args.provider)
    for path in list_channel_files(args.channels_dir):
        logger.info("Processing channel file: %s", path)
        found = resolve_missing_magnets(path, indexer, categories=DEFAULT_CATEGORIES)
        logger.info("Found %d new magnet(s) for %s", found, os.path.basename(path))

    if not args.stream:
        return

    from torrent_streamer import TorrentStreamer

    channels = load_channels_dir(args.channels_dir)
    current_index = 0
    if args.current_channel_file:
        wanted = os.path.join(args.channels_dir, args.current_channel_file)
        for i, ch in enumerate(channels):
            if os.path.abspath(ch["path"]) == os.path.abspath(wanted):
                current_index = i
    streamer = TorrentStreamer(save_root=args.save_root)
    buffer_eps = collect_buffer_episodes(channels, current_index, args.current_episode_id, per_channel_count=args.per_channel)
    logger.info("Buffering %d episodes across channels", len(buffer_eps))
    streamer.buffer_episodes(buffer_eps)
    logger.info("Buffering started. Press Ctrl+C to exit and shut down streamer.")
    try:
        while True:
            streamer.poll()
            for ep in buffer_eps:
                status = streamer.episode_status(ep)
                logger.info("%-28s %-12s %s", ep["id"], status["state"], status["message"])
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt, shutting down streamer")
    finally:
        streamer.shutdown()


if __name__ == "__main__":
    main()
