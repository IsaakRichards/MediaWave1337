# Changelog

## MediaWave1337 (unreleased)

### Added

- Torrent channels: every XML file in `channels/` is a live channel in the player, scheduled in real time and persisted across restarts
- Background Jackett/Prowlarr lookups for episodes without a magnet, with urgent lookups for what is about to air
- Streaming playback with piece deadlines around the playhead, a readiness check, and buffering/stand-by slates
- Season-pack support (per-episode file selection by SxxEyy / NxNN)
- Buffer window of current + adjacent channels, with eviction and deletion of viewed episodes
- Torrent Channels section in Advanced Config; libtorrent status in diagnostics
- Real running times written back to channel XML
- Guided setup wizard for Windows and Linux (`setup.bat` / `setup.sh`): system check, package install with a live log, FFmpeg, indexer connection test, and shortcuts

### Fixed

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
