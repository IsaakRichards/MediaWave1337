# Changelog

## MediaWave1337 (unreleased)

### Added

- Torrent channels: every XML file in `channels/` is a live channel in the player, scheduled in real time and persisted across restarts
- Background Jackett/Prowlarr lookups for episodes without a magnet, with urgent lookups for what is about to air
- Streaming playback with piece deadlines around the playhead, a readiness check, and buffering/stand-by slates
- Season-pack support (per-episode file selection by SxxEyy / NxNN)
- Buffer window of current + adjacent channels, with eviction and deletion of viewed episodes
- Torrent Channels and Advanced Torrent Settings sections in Advanced Config (download slots, nearby-channel range, lookahead, dead-release timeout, release attempts, pre-play buffer, speed limits, port, general-category fallback search); libtorrent status in diagnostics
- Real running times written back to channel XML
- Every torrent channel buffers its current episode (setting, on by default); downloads are queued by priority, with up to 8 at once
- Resume data for instant restarts; airing episodes download from the live point first; far channels pre-buffer their next episode near the end of the current one
- Release fallback: dead, invalid or unplayable releases are recorded as `<rejected>` and replaced by the next-best release; episodes with no working release are skipped from the lineup
- `order="shuffle"` channels: random lineup that keeps each show's episodes in order (`show` attribute or SxxEyy title grouping)
- Guided setup wizard for Windows and Linux (`setup.bat` / `setup.sh`): system check, package install with a live log, FFmpeg, indexer connection test, and shortcuts

### Fixed

- Re-tuning to the file that was just loaded (after a stand-by or rebuffer) never started playback
- A stand-by timed to a guessed slot length kept waiting after the real duration corrected the schedule
- libtorrent's default limit of 3 active downloads could queue the episode on screen and make queued releases look dead
- `tools/load_channels.py` could not import project modules and crashed on `--stream`
- Streamer used libtorrent APIs removed in 2.x and wrote files to paths it never checked
- Jackett/Prowlarr query parameters; `.torrent` download links and `min_seeders` are now honored

## v0.1.0-beta

Initial private beta.

### Included

- Live-TV-style local media playback and channel switching
- Multiple Guide themes, including Set Top Box styling
- MediaWave Vault browsing and resume support
- MediaWave Converter
- RadioWave, NetTV, and WeatherStar integrations
- Windows DPI, font rendering, image quality, and layout improvements
- Private-beta staging workflow with filtered platform packages

### Private Beta Notes

- Packages are unsigned.
- No automatic updater is included.
- Tester runtime settings, caches, logs, and media are excluded from packages.
