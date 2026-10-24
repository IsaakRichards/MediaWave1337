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
        def get_best_magnet(self, query, categories=None, min_seeders=0, exclude=()):
            assert min_seeders == 2
            return "magnet:?xt=urn:btih:FOUND"

    results = []
    resolver = MagnetResolver(FakeIndexer(), callback=lambda ep, link, err, kind: results.append((ep["id"], link, err, kind)))
    ep = parse_channel_file(str(path))["episodes"][0]
    assert resolver.request(ep)
    assert not resolver.request(ep)  # already pending or done
    deadline = time.time() + 5
    while not results and time.time() < deadline:
        time.sleep(0.05)
    resolver.stop()
    assert results == [("tz-s01e01", "magnet:?xt=urn:btih:FOUND", None, "found")]
    assert parse_channel_file(str(path))["episodes"][0]["magnet"] == "magnet:?xt=urn:btih:FOUND"


def test_magnet_resolver_serves_urgent_first(tmp_path):
    path = tmp_path / "ch.xml"
    path.write_text('<channel name="c">' + "".join(f'<episode id="e{i}"><title>e{i}</title></episode>' for i in range(4)) + "</channel>")
    eps = parse_channel_file(str(path))["episodes"]
    import threading
    gate = threading.Event()
    order = []

    class SlowIndexer:
        def get_best_magnet(self, query, categories=None, min_seeders=0, exclude=()):
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


from torrent_channels import show_key, shuffle_keeping_show_order


@pytest.mark.parametrize("title,expected", [
    ("Twilight Zone S01E01", "show:twilight zone"),
    ("The Twilight Zone (1959) S02E10 DVDRip", "show:the twilight zone"),
    ("Twilight.Zone.1x03", "show:twilight zone"),
    ("Star Trek - Season 1 Episode 2", "show:star trek"),
    ("Night of the Living Dead (1968)", "single:m1"),
])
def test_show_key(title, expected):
    assert show_key({"id": "m1", "title": title}) == expected


def test_show_key_prefers_explicit_show():
    assert show_key({"id": "x", "title": "Pilot", "show": "Batman "}) == "show:batman"


MIXED = """<channel name="Saturday" order="shuffle">
""" + "".join(
    f'<episode id="tz{i}" season="1" episode="{i}"><title>Twilight Zone S01E0{i}</title></episode>' for i in range(1, 7)
) + "".join(
    f'<episode id="ol{i}" season="1" episode="{i}"><title>Outer Limits S01E0{i}</title></episode>' for i in range(1, 5)
) + """
<episode id="b1" show="Batman"><title>Batman: Pilot</title></episode>
<episode id="b2" show="Batman"><title>Batman: Part Two</title></episode>
<episode id="m1"><title>Them! (1954)</title></episode>
<episode id="m2"><title>Tarantula (1955)</title></episode>
</channel>"""


def test_shuffle_keeps_each_show_in_order_and_is_stable(tmp_path):
    path = tmp_path / "sat.xml"
    path.write_text(MIXED)
    ids = [e["id"] for e in parse_channel_file(str(path))["episodes"]]
    assert sorted(ids) == sorted(["tz1", "tz2", "tz3", "tz4", "tz5", "tz6", "ol1", "ol2", "ol3", "ol4", "b1", "b2", "m1", "m2"])
    for prefix, count in (("tz", 6), ("ol", 4), ("b", 2)):
        assert [i for i in ids if i.startswith(prefix)] == [f"{prefix}{n}" for n in range(1, count + 1)]
    again = [e["id"] for e in parse_channel_file(str(path))["episodes"]]
    assert ids == again  # same result every load, so the schedule stays put across restarts
    assert ids != sorted(ids, key=lambda i: MIXED.index(f'id="{i}"'))  # actually shuffled


def test_seed_changes_mix_and_listed_is_default(tmp_path):
    path = tmp_path / "sat.xml"
    path.write_text(MIXED)
    first = [e["id"] for e in parse_channel_file(str(path))["episodes"]]
    seeds = set()
    for seed in ("a", "b", "c", "d"):
        path.write_text(MIXED.replace('order="shuffle"', f'order="shuffle" seed="{seed}"'))
        seeds.add(tuple(e["id"] for e in parse_channel_file(str(path))["episodes"]))
    assert len(seeds | {tuple(first)}) > 1
    path.write_text(MIXED.replace(' order="shuffle"', ""))
    ch = parse_channel_file(str(path))
    assert ch["order"] == "listed" and ch["episodes"][0]["id"] == "tz1" and ch["episodes"][-1]["id"] == "m2"


def test_shuffle_spreads_a_long_series():
    eps = [{"id": f"s{i}", "title": f"Show S01E{i:02d}"} for i in range(1, 41)]
    eps += [{"id": f"m{i}", "title": f"Movie {i}"} for i in range(10)]
    order = [e["id"] for e in shuffle_keeping_show_order(eps, "x")]
    movie_positions = [n for n, i in enumerate(order) if i.startswith("m")]
    # Movies are spread over the lineup, not bunched at one end.
    assert movie_positions[0] < 20 and movie_positions[-1] > 30


from torrent_channels import reject_release


def test_reject_release_records_link_and_clears_magnet(tmp_path):
    path = tmp_path / "tz.xml"
    path.write_text(SAMPLE)
    assert reject_release(str(path), "tz-s01e02", "magnet:?xt=urn:btih:ABC")
    reject_release(str(path), "tz-s01e02", "magnet:?xt=urn:btih:ABC")  # recorded once only
    e2 = parse_channel_file(str(path))["episodes"][1]
    assert e2["magnet"] is None and e2["rejected"] == ["magnet:?xt=urn:btih:ABC"]
    assert "keep me" in path.read_text()


def _wait_for(results, n=1):
    deadline = time.time() + 5
    while len(results) < n and time.time() < deadline:
        time.sleep(0.05)


def test_resolver_excludes_rejected_and_reports_kinds(tmp_path):
    from indexer_client import IndexerUnavailable, NoMagnetFound
    path = tmp_path / "tz.xml"
    path.write_text(SAMPLE)
    calls = []

    class Indexer:
        mode = "ok"

        def get_best_magnet(self, query, categories=None, min_seeders=0, exclude=()):
            calls.append(list(exclude))
            if self.mode == "down":
                raise IndexerUnavailable("down")
            if self.mode == "none":
                raise NoMagnetFound("nothing")
            return "magnet:?xt=urn:btih:SECOND"

    indexer = Indexer()
    results = []
    resolver = MagnetResolver(indexer, callback=lambda ep, link, err, kind: results.append(kind))
    ep = dict(parse_channel_file(str(path))["episodes"][0], rejected=["magnet:?xt=urn:btih:FIRST"])
    resolver.request(ep)
    _wait_for(results)
    assert results == ["found"] and calls[-1] == ["magnet:?xt=urn:btih:FIRST"]
    assert not resolver.request(ep)  # already resolved: no duplicate search
    assert len(calls) == 1

    indexer.mode = "down"
    ep["key"] = "other"
    resolver.request(ep)
    _wait_for(results, 2)
    assert results[-1] == "unavailable" and not resolver.has_failed(ep)
    assert not resolver.request(ep)  # backing off after an outage
    assert resolver.request(ep, retry_failed=True)
    _wait_for(results, 3)

    indexer.mode = "none"
    ep["key"] = "third"
    resolver.request(ep)
    _wait_for(results, 4)
    resolver.stop()
    assert results[-1] == "not-found" and resolver.has_failed(ep)


def test_resolver_passes_fallback_categories(tmp_path):
    path = tmp_path / "tz.xml"
    path.write_text(SAMPLE)
    seen = []

    class Indexer:
        def get_best_magnet(self, query, categories=None, min_seeders=0, exclude=(), fallback_categories=None):
            seen.append(fallback_categories)
            return "magnet:?xt=urn:btih:X"

    results = []
    resolver = MagnetResolver(Indexer(), callback=lambda *a: results.append(a), fallback_categories=[5000, 2000])
    resolver.request(parse_channel_file(str(path))["episodes"][0])
    _wait_for(results)
    resolver.stop()
    assert seen == [[5000, 2000]]
