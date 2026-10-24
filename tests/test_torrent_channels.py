import time

import pytest

from torrent_channels import (
    MagnetResolver,
    collect_buffer_episodes,
    load_channels_dir,
    parse_channel_file,
    parse_duration,
    update_episode_fields,
)

SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<channel name="Twilight Zone" number="5" default_duration="25m">
  <!-- keep me -->
  <episode id="tz-s01e01" season="1" episode="1">
    <title>Twilight Zone S01E01</title>
    <magnet></magnet>
    <min_seeders>2</min_seeders>
  </episode>
  <episode id="tz-s01e02" season="1" episode="2">
    <title>Twilight Zone S01E02</title>
    <query>The Twilight Zone 1959 S01E02</query>
    <duration>24:30</duration>
    <magnet>magnet:?xt=urn:btih:ABC</magnet>
  </episode>
</channel>
"""


@pytest.mark.parametrize("text,expected", [
    ("1500", 1500), ("25m", 1500), ("25 min", 1500), ("24:30", 1470), ("1:02:03", 3723),
    ("1.5h", 5400), ("", 0), (None, 0), ("garbage", 0),
])
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


def test_parse_channel_file(tmp_path):
    path = tmp_path / "tz.xml"
    path.write_text(SAMPLE)
    ch = parse_channel_file(str(path))
    assert ch["name"] == "Twilight Zone" and ch["number"] == 5 and ch["default_duration"] == 1500
    e1, e2 = ch["episodes"]
    assert e1["magnet"] is None and e1["min_seeders"] == 2 and e1["query"] == "Twilight Zone S01E01"
    assert e2["query"] == "The Twilight Zone 1959 S01E02" and e2["duration"] == 1470
    assert e1["key"] == "Twilight Zone|tz-s01e01"


def test_update_episode_fields_round_trip_keeps_comments(tmp_path):
    path = tmp_path / "tz.xml"
    path.write_text(SAMPLE)
    assert update_episode_fields(str(path), "tz-s01e01", {"magnet": "magnet:?xt=urn:btih:NEW", "duration": "25:00"})
    assert not update_episode_fields(str(path), "tz-s01e01", {"magnet": "magnet:?xt=urn:btih:NEW"})
    text = path.read_text()
    assert "keep me" in text
    e1 = parse_channel_file(str(path))["episodes"][0]
    assert e1["magnet"] == "magnet:?xt=urn:btih:NEW" and e1["duration"] == 1500
    # Re-writing does not keep growing whitespace.
    update_episode_fields(str(path), "tz-s01e02", {"duration": "24:31"})
    update_episode_fields(str(path), "tz-s01e02", {"duration": "24:30"})
    assert "\n\n" not in path.read_text()


def test_collect_buffer_episodes(tmp_path):
    for name in ("a", "b", "c"):
        (tmp_path / f"{name}.xml").write_text(
            f'<channel name="{name}">' + "".join(f'<episode id="{name}{i}"><title>{name}{i}</title></episode>' for i in range(5)) + "</channel>"
        )
    channels = load_channels_dir(str(tmp_path))
    eps = collect_buffer_episodes(channels, 1, "b2", per_channel_count=2)
    assert [e["id"] for e in eps] == ["b2", "b3", "c0", "c1", "a0", "a1"]


def test_magnet_resolver_writes_back(tmp_path):
    path = tmp_path / "tz.xml"
    path.write_text(SAMPLE)

    class FakeIndexer:
        def get_best_magnet(self, query, categories=None, min_seeders=0):
            assert min_seeders == 2
            return "magnet:?xt=urn:btih:FOUND"

    results = []
    resolver = MagnetResolver(FakeIndexer(), callback=lambda ep, link, err: results.append((ep["id"], link, err)))
    ep = parse_channel_file(str(path))["episodes"][0]
    assert resolver.request(ep)
    assert not resolver.request(ep)  # already pending or done
    deadline = time.time() + 5
    while not results and time.time() < deadline:
        time.sleep(0.05)
    resolver.stop()
    assert results == [("tz-s01e01", "magnet:?xt=urn:btih:FOUND", None)]
    assert parse_channel_file(str(path))["episodes"][0]["magnet"] == "magnet:?xt=urn:btih:FOUND"


def test_magnet_resolver_serves_urgent_first(tmp_path):
    path = tmp_path / "ch.xml"
    path.write_text('<channel name="c">' + "".join(f'<episode id="e{i}"><title>e{i}</title></episode>' for i in range(4)) + "</channel>")
    eps = parse_channel_file(str(path))["episodes"]
    import threading
    gate = threading.Event()
    order = []

    class SlowIndexer:
        def get_best_magnet(self, query, categories=None, min_seeders=0):
            gate.wait(5)
            order.append(query)
            return f"magnet:?xt=urn:btih:{query}"

    resolver = MagnetResolver(SlowIndexer())
    for ep in eps:
        resolver.request(ep)
    time.sleep(0.1)  # worker is now blocked on e0
    resolver.request(eps[3], urgent=True)
    gate.set()
    deadline = time.time() + 5
    while len(order) < 4 and time.time() < deadline:
        time.sleep(0.05)
    resolver.stop()
    assert order == ["e0", "e3", "e1", "e2"]
