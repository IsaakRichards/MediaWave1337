# MediaWave1337

> **Turn torrent-sourced SD video releases into a real cable TV experience for CRT displays.**

<img width="288" height="180" alt="Kapture 2026-06-14 at 21 13 51" src="https://github.com/user-attachments/assets/9cc72a69-b647-43a5-8338-ebb50590794f" />

MediaWave1337 is a fork of MediaWave2000 repurposed to stream videos via BitTorrent for playback on CRT-style displays (SD / 480i / 4:3). Instead of scanning local video directories, MediaWave1337 obtains releases from local indexers (Jackett or Prowlarr), prefers CRT-friendly SD releases, and streams them using python-libtorrent with file-level piece prioritization for smoother linear playback.

Put your channels (playlists) as XML files under `channels/`, let the loader populate magnet links from your local indexer, and let the streamer buffer and prioritize files so playback on low-resolution CRTs starts earlier and feels like real TV.

---

## Screenshots

<img width="1440" height="900" alt="Screenshot 2026-06-20 at 2 11 32 PM" src="https://github.com/user-attachments/assets/37fc6849-83fe-4485-a5a9-d55a97720a40" />
<img width="1440" height="900" alt="Screenshot 2026-06-20 at 2 11 48 PM" src="https://github.com/user-attachments/assets/c3f3966b-ebc0-43c9-8472-e309c4a53f03" />
<img width="1440" height="900" alt="Screenshot 2026-06-20 at 2 12 09 PM" src="https://github.com/user-attachments/assets/0eacfe00-a23a-4682-97ba-2f0cb8cddf8f" />
<img width="1440" height="900" alt="Screenshot 2026-06-20 at 2 12 28 PM" src="https://github.com/user-attachments/assets/f5c1cd64-f09b-4b48-8ff8-6a24868bf067" />
<img width="1440" height="900" alt="Screenshot 2026-06-14 at 8 51 00 PM" src="https://github.com/user-attachments/assets/97149040-1a1e-4152-bc0e-461fc858d5f1" />

---

## What is this?

MediaWave1337 transforms curated video playback into a faux-cable experience targeted at CRT TVs. It keeps the channel/playlist model from the original MediaWave2000 but replaces local-folder scanning with indexed BitTorrent streaming so you can source releases from indexers while keeping playback order, scheduling, and ad breaks.

If you liked FieldStation42 or MediaWave2000's concept, but want a setup that can acquire and stream SD releases automatically (favoring older 480i/4:3 masters), this fork is built for that workflow.

---

## Features (high level)

- Channel playlists: one XML file per channel under `channels/` (playback order preserved)
- Indexer integration: query Jackett or Prowlarr REST APIs to find magnet links
- CRT-first release selection: automatic scoring that prefers `480i`, `480p`, `DVDRip`, `SDTV`, and `4:3`, and penalizes HD/BluRay tags
- Category restriction: searches limited to SD TV (5030) and SD Movies (2030) by default
- libtorrent streaming with file-priority: prioritize the intended file inside a torrent and enable sequential download for smoother linear playback
- Buffering: automatic buffer for the current channel plus the previous and next channels (next 3 episodes per channel by default)
- Mark-as-viewed flow: delete viewed files and remove torrents so episodes can be re-downloaded later
- Backwards-compatible save paths so the rest of the player logic can still read files from expected locations

See `FEATURES.md` in the original project for additional UI features that can still be integrated (themes, logos, vault, commercials).

---

## Repository layout

- indexer_client.py — IndexerClient for Jackett/Prowlarr searches with CRT scoring
- torrent_streamer.py — libtorrent integration, buffering, file-priority, mark-as-viewed
- channels/ — channel XML files (one per channel). Example: `channels/twilight_zone.xml`
- tools/load_channels.py — CLI: populate magnets from indexer and optionally start streamer
- tests/ — pytest tests (libtorrent mocked for CI)
- requirements.txt — runtime and test dependencies

---

## Channels and playlist format

Place one XML file per channel in the `channels/` directory. Each file contains ordered `<episode>` elements; the loader preserves element order. Use a minimal schema so files remain human-editable.

Minimal episode element:

```xml
<episode id="tz-s01e01" season="1" episode="1">
  <title>Twilight Zone S01E01</title>
  <save_path>data/videos/twilight_zone/s01e01.mp4</save_path>
  <magnet></magnet>
  <min_seeders>1</min_seeders>
  <priority>1</priority>
  <note>Prefer 480i/4:3 releases</note>
</episode>
```

- `title` (required): Query string used by IndexerClient
- `save_path` (recommended): where downloads are written (repo-relative paths suggested)
- `magnet` (optional): pre-fill to skip indexer lookups
- `min_seeders` (optional): read by loader but currently not enforced
- `priority` / `note` (optional): metadata for ordering and human notes

Example sample channel included: `channels/twilight_zone.xml`.

---

## Indexer behavior and scoring

- Default providers: Jackett (tried first) and Prowlarr (fallback). API key handling matches common conventions (Jackett: `apikey` query param, Prowlarr: `X-Api-Key` header).
- Category restriction: searches limited to SD TV (5030) and SD Movies (2030) to bias results toward CRT-appropriate releases.
- Title scoring: positive weight for `480i`, `480p`, `DVDRip`, `SDTV`, `4:3` patterns; penalties for `1080p`, `4k`, `BluRay` and similar markers. The final candidate score = title_score * 10 + log(...)

---

## Streaming and buffering

- Use python-libtorrent to add magnet links and download into the configured save root (default `data/videos/`).
- After adding a magnet the streamer runs a background metadata worker that:
  - Waits for torrent metadata,
  - Attempts to locate the desired file inside the torrent (matching `save_path` suffix or falling back to single-file torrents),
  - Applies high file priority to that file and attempts to enable sequential download (if supported), to improve start-of-playback latency.
- Buffering policy: the loader buffers the current channel, the one above it, and the one below it (by filename ordering) and fetches the next 3 episodes per channel. The streamer keeps only the current buffer set in active downloads to limit disk use.

Limitations: piece-priority + sequential download improves linear playback but seeking arbitrarily still depends on pieces already downloaded; perfect random-access streaming would need more advanced support.

---

## Mark-as-viewed and re-download

- Call `TorrentStreamer.mark_episode_viewed(ep)` with an episode dict (`magnet` and `save_path`) to delete the file and remove the torrent from the session.
- The channel XML keeps the `<magnet>` so a subsequent buffering run will re-download the episode when it enters the buffer window.

---

## CLI examples

Populate magnets only (no streaming):

```bash
python tools/load_channels.py --base-url http://localhost:9117 --api-key YOUR_KEY
```

Populate magnets and start streaming/buffering:

```bash
python tools/load_channels.py \
  --base-url http://localhost:9117 \
  --api-key YOUR_KEY \
  --stream \
  --current-channel-file twilight_zone.xml \
  --current-episode-id tz-s01e02
```

Mark an episode as viewed (example within Python):

```python
from torrent_streamer import TorrentStreamer

ts = TorrentStreamer(save_root="data/videos")
ep = {"magnet": "magnet:?xt=urn:btih:EXAMPLE", "save_path": "twilight_zone/s01e01.mp4"}
ts.mark_episode_viewed(ep)
```

---

## Testing and development

- Install development requirements and run tests:

```bash
pip install -r requirements.txt
pytest
```

Unit tests mock libtorrent so CI can run without a platform-specific libtorrent wheel.

---

## Installing python-libtorrent

python-libtorrent can be platform-dependent. Common advice:

- Debian/Ubuntu:
  ```bash
  sudo apt-get install -y python3-libtorrent
  ```
  or install `libtorrent-rasterbar-dev` and build the Python bindings if needed.

- macOS:
  Use Homebrew to install libtorrent and then install a compatible Python wheel or build python-libtorrent from source.

- Windows:
  Look for prebuilt wheels for your Python version or run the streamer on a Linux host/container if wheel support is limited.

Tell me your OS and Python version and I can give step-by-step instructions.

---

## Roadmap and next steps

- Integrate an external player (mpv) that notifies the streamer to mark episodes viewed automatically on playback end
- Improve file-matching heuristics inside torrents (basename-fuzzy matching and regex rules)
- Add a small HTTP control API or CLI for remote control and to mark episodes viewed from external players
- Add a monitoring UI to display buffering progress and torrent health
- Persist streamer state across restarts

---

## Credits & original content

This fork is based on MediaWave2000. Much of the original README content, screenshots, and ideas are preserved here with a new focus on torrent-sourced streaming for CRT-targeted playback.

**NO copyrighted media is included with MediaWave1337. Users must supply their own legally obtained media files.**

*Tune in. Sit back. There's always something on.*
