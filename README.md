# MediaWave1337

> **Turn torrent-sourced SD video releases into a real cable TV experience for CRT displays.**

<img width="288" height="180" alt="Kapture 2026-06-14 at 21 13 51" src="https://github.com/user-attachments/assets/9cc72a69-b647-43a5-8338-ebb50590794f" />

MediaWave1337 is a fork of MediaWave2000 repurposed to stream videos via BitTorrent for playback on CRT-style displays (SD / 480i / 4:3). Alongside (or instead of) scanned local folders, MediaWave1337 builds channels from XML playlists, finds releases on a local indexer (Jackett or Prowlarr), prefers CRT-friendly SD releases, and streams them with libtorrent using file-level and piece-level prioritization so playback can start before a download finishes.

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

- **Torrent channels in the player**: each XML file in `channels/` becomes a live channel in the MediaWave guide, next to your local catalog channels and companion channels
- **Real-time schedule**: episodes air back to back in file order, on the wall clock, and keep their place across restarts
- **Indexer integration**: episodes without a magnet are looked up on Jackett or Prowlarr in the background (the episode about to air first) and the link is written back to the XML
- **CRT-first release selection**: scoring prefers `480i`, `480p`, `DVDRip`, `SDTV`, `4:3`, and penalizes 720p/1080p/4K, BluRay, x265/HEVC; `min_seeders` is enforced
- **Streaming playback**: the file under the playhead, plus the container header and trailer, are fetched first, so an episode can start before it finishes downloading
- **Season packs**: one torrent can serve many episodes; only the matching `SxxEyy` / `1x01` file is downloaded
- **Buffering**: the current channel and the channels directly above and below it keep their next 3 episodes (configurable) downloading
- **Viewed / evicted episodes are deleted** to limit disk use (turn on "Keep downloaded episodes" to keep them)
- **Stand-by slates** while an episode is buffering, has no release yet, or fails to play, instead of a black screen

---

## Quick start

### Guided setup (Windows and Linux)

- **Windows:** double-click `setup.bat`. If Python is missing it opens the download page; install it with "Add python.exe to PATH" ticked, then run `setup.bat` again.
- **Linux:** run `./setup.sh`. If Python 3.9+ or tkinter is missing, it shows the install command for your distro and offers to run it.

The setup wizard then walks you through:

1. **System check**: Python version, virtual-environment support, FFmpeg and disk space, with an "Install it" button for anything fixable (for example `python3-venv` on Debian/Ubuntu).
2. **Python packages**: installs PySide6, requests and libtorrent into a private `venv/` folder (or your user account), with a live log. If libtorrent has no wheel for your Python on Linux, "Use system libtorrent" installs your distro's package instead.
3. **FFmpeg**: one click, using winget on Windows or your package manager on Linux.
4. **Indexer**: enter your Jackett or Prowlarr URL and API key, and use **Test connection** to check them.
5. **Finish**: creates a launcher (`MediaWave1337.bat` / `MediaWave1337.sh`), plus desktop and Start Menu / applications-menu shortcuts if you want them, and starts the app.

You can rerun the wizard at any time; it skips work that is already done.

### Manual setup

1. `pip install -r requirements.txt` (Python 3.9+). `libtorrent` is the PyPI wheel name; on Debian/Ubuntu `sudo apt install python3-libtorrent` also works.
2. Run [Jackett](https://github.com/Jackett/Jackett) (port 9117) or [Prowlarr](https://github.com/Prowlarr/Prowlarr) (port 9696) locally and copy its API key.
3. Start the player with `python channelsurfer2000.py`.
4. Open **Advanced Config → Companion Channels → Torrent Channels** and set the indexer URL and API key. The indexer type is detected from the port, or you can pick it.
5. Press **Watch TV**. A local catalog is optional: torrent channels work on their own.

When running from source, channels are read from `channels/` and downloads go to `torrent_downloads/` in the repo. Packaged builds use `channels/` and `torrent_downloads/` in the app data folder, seeded with the sample channel on first run. Both folders can be changed in the settings.

---

## Repository layout

- `channelsurfer2000.py`: the player (MediaWave2000 plus the `TorrentChannel` integration)
- `torrent_channels.py`: channel XML parsing/writing, buffer-window selection, background magnet lookups
- `indexer_client.py`: `IndexerClient` for Jackett/Prowlarr searches with CRT scoring
- `torrent_streamer.py`: libtorrent session, file selection, streaming deadlines, readiness checks, eviction
- `channels/`: channel XML files (one per channel), e.g. `channels/twilight_zone.xml`
- `setup.bat`, `setup.sh`, `setup_wizard.py`, `setup_core.py`: guided installer (tkinter, standard library only)
- `tools/load_channels.py`: headless CLI to pre-fill magnets and optionally buffer without the player
- `tests/`: pytest suite (the libtorrent tests seed a real local torrent and are skipped if libtorrent is missing)

---

## Channels and playlist format

Place one XML file per channel in the `channels/` directory. Episodes air in the order listed.

```xml
<channel name="Twilight Zone" number="3" default_duration="25m">
  <episode id="tz-s01e01" season="1" episode="1">
    <title>Twilight Zone S01E01</title>
    <query>The Twilight Zone 1959 S01E01</query>
    <duration>25:10</duration>
    <magnet></magnet>
    <min_seeders>1</min_seeders>
    <note>Prefer 480i/4:3 releases</note>
  </episode>
</channel>
```

| Field | | Meaning |
|---|---|---|
| `name` (channel attr) | optional | Channel name shown in the guide (defaults to the file name) |
| `number` (channel attr) | optional | Preferred channel number (My Catalog overrides it) |
| `default_duration` (channel attr) | optional | Slot length for episodes whose duration is not known yet (default 30m) |
| `id`, `season`, `episode` (attrs) | recommended | `id` must be unique in the file; season/episode pick the right file out of season packs |
| `title` | required | Shown in the guide; also the search text unless `query` is set |
| `query` | optional | Indexer search text |
| `duration` | optional | `1500`, `25m`, `25:10` or `1:02:03`; filled in automatically once the real file is known |
| `magnet` | optional | Magnet URI, http(s) `.torrent` URL, or local `.torrent` path; filled in from the indexer when empty |
| `min_seeders` | optional | Releases with fewer seeders are ignored |
| `save_path` | optional | Download directory for this episode, relative to the download folder |

The player rewrites files when it fills in `magnet` and `duration`. Comments inside `<channel>` are preserved.

---

## How playback works

- Each torrent channel has a schedule anchor, stored in `torrent_state.json`. The first time a channel is seen it starts at episode 1, and from then on it runs like a real station whether or not you are watching.
- When you tune in, the streamer prioritizes the pieces at the current position, plus the first and last pieces of the file (container header and index). Playback starts once about 45 seconds are buffered ahead and the download rate can keep up, or immediately if the file is complete.
- Until then a stand-by slate shows the progress, for example "Buffering 12% - 640 KB/s from 9 peers".
- If playback stalls, the slate returns ("Signal lost - rebuffering...") and retries with back-off. After repeated failures the episode is only retried once fully downloaded, and a file that is complete but unplayable sits out its slot.
- The real running time is read from the file (ffprobe or the player) and saved to the XML, so the guide becomes exact after the first airing.
- Episodes outside the buffer window are removed from the session, and deleted unless "Keep downloaded episodes" is on.

Limitations: poorly seeded releases may never start (the slate shows the peer count). Seeking far ahead within an episode only works once those pieces are downloaded.

---

## Settings (Advanced Config → Torrent Channels)

| Setting | Default |
|---|---|
| Channels folder | `channels/` in the data folder |
| Download folder | `torrent_downloads/` in the data folder |
| Indexer URL / API key / type | `http://localhost:9117`, empty, Auto |
| Episodes to buffer (per channel) | 3 |
| Keep downloaded episodes | off |
| Enable torrent channels | on |

The libtorrent listen port (`torrent_listen_port`, default 6881) can be changed in `app_settings.json`.

---

## CLI (headless)

Populate magnets only:

```bash
python tools/load_channels.py --base-url http://localhost:9117 --api-key YOUR_KEY
```

Populate magnets and buffer the current and adjacent channels, printing progress:

```bash
python tools/load_channels.py \
  --base-url http://localhost:9696 --api-key YOUR_KEY \
  --stream --current-channel-file twilight_zone.xml --current-episode-id tz-s01e02
```

---

## Testing and development

```bash
pip install -r requirements.txt
pytest
```

The streamer tests create a small season-pack torrent, seed it from a second local libtorrent session, and check that exactly one episode is downloaded, its bytes match, and "mark viewed" deletes it. They are skipped when libtorrent is not installed.

---

## Roadmap

- Monitoring panel for buffering progress and torrent health
- Save libtorrent resume data so restarts skip re-checking files that are already on disk
- Per-channel indexer categories and release-group preferences
- Linux/Windows builds of the bundled ffmpeg tools (`bin/` currently holds macOS arm64 binaries; source runs use the system ffmpeg/ffprobe)

---

## Credits & original content

This fork is based on MediaWave2000. Much of the original README content, screenshots, and ideas are preserved here with a new focus on torrent-sourced streaming for CRT-targeted playback.

**NO copyrighted media is included with MediaWave1337. Users must supply their own legally obtained media files.**

*Tune in. Sit back. There's always something on.*
