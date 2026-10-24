from unittest import mock

import pytest

from indexer_client import IndexerClient, NoMagnetFound


def test_title_score_prefers_480i_and_4_3():
    c = IndexerClient("http://example.local")
    t1 = "SomeShow S01E01 480i 4:3 DVDRip"
    t2 = "SomeShow S01E01 1080p BluRay"
    assert c._title_score(t1) > c._title_score(t2)
    assert c._title_score("Show S01E01 BDRip") < 0


def test_title_score_handles_empty():
    c = IndexerClient("http://example.local")
    assert c._title_score("") == 0.0


def test_provider_guessed_from_port():
    assert IndexerClient("http://localhost:9696").provider_hint == "prowlarr"
    assert IndexerClient("http://localhost:9117").provider_hint == "jackett"
    assert IndexerClient("http://indexer.lan").provider_hint is None


def _response(payload, status=200):
    r = mock.MagicMock()
    r.json.return_value = payload
    r.raise_for_status.side_effect = None if status < 400 else __import__("requests").HTTPError(str(status))
    return r


def test_jackett_query_params_and_best_pick():
    results = {"Results": [
        {"Title": "Show S01E01 1080p BluRay x265", "Seeders": 500, "MagnetUri": "magnet:?xt=urn:btih:HD"},
        {"Title": "Show S01E01 DVDRip XviD", "Seeders": 12, "MagnetUri": None, "Link": "http://jackett/dl/1.torrent"},
        {"Title": "Show S01E01 SDTV", "Seeders": 0, "MagnetUri": "magnet:?xt=urn:btih:DEAD"},
    ]}
    with mock.patch("indexer_client.requests.get", return_value=_response(results)) as get:
        c = IndexerClient("http://localhost:9117", api_key="KEY")
        assert c.get_best_magnet("Show S01E01", min_seeders=1) == "http://jackett/dl/1.torrent"
    params = get.call_args.kwargs["params"]
    assert ("Category[]", "5030") in params and ("Category[]", "2030") in params
    assert ("apikey", "KEY") in params


def test_prowlarr_filters_usenet_and_uses_header():
    results = [
        {"title": "Show S01E01 480p", "seeders": 3, "protocol": "usenet", "downloadUrl": "http://nzb"},
        {"title": "Show S01E01 480p", "seeders": 3, "protocol": "torrent", "magnetUrl": "magnet:?xt=urn:btih:OK"},
    ]
    with mock.patch("indexer_client.requests.get", return_value=_response(results)) as get:
        c = IndexerClient("http://localhost:9696", api_key="KEY")
        assert c.get_best_magnet("Show S01E01") == "magnet:?xt=urn:btih:OK"
    assert get.call_args.kwargs["headers"] == {"X-Api-Key": "KEY"}
    assert ("categories", "5030") in get.call_args.kwargs["params"]


def test_min_seeders_filters_everything_raises():
    results = {"Results": [{"Title": "Show 480p", "Seeders": 0, "MagnetUri": "magnet:?xt=urn:btih:A"}]}
    with mock.patch("indexer_client.requests.get", return_value=_response(results)):
        with pytest.raises(NoMagnetFound):
            IndexerClient("http://localhost:9117").get_best_magnet("Show", min_seeders=2)


def test_exclude_skips_rejected_links_and_runs_out():
    results = {"Results": [
        {"Title": "Show S01E01 DVDRip", "Seeders": 10, "MagnetUri": "magnet:?xt=urn:btih:BEST"},
        {"Title": "Show S01E01 SDTV", "Seeders": 10, "MagnetUri": "magnet:?xt=urn:btih:NEXT"},
    ]}
    with mock.patch("indexer_client.requests.get", return_value=_response(results)):
        c = IndexerClient("http://localhost:9117")
        assert c.get_best_magnet("Show") == "magnet:?xt=urn:btih:BEST"
        assert c.get_best_magnet("Show", exclude=["magnet:?xt=urn:btih:BEST"]) == "magnet:?xt=urn:btih:NEXT"
        with pytest.raises(NoMagnetFound, match="untried"):
            c.get_best_magnet("Show", exclude=["magnet:?xt=urn:btih:BEST", "magnet:?xt=urn:btih:NEXT"])


def test_unreachable_indexer_raises_unavailable():
    import requests
    from indexer_client import IndexerUnavailable
    with mock.patch("indexer_client.requests.get", side_effect=requests.ConnectionError("refused")):
        with pytest.raises(IndexerUnavailable):
            IndexerClient("http://localhost:9117").get_best_magnet("Show")


def test_general_categories_fallback_only_when_sd_finds_nothing():
    from indexer_client import GENERAL_CATEGORIES

    def fake_get(url, params=None, timeout=None, **kw):
        cats = {v for k, v in params if k == "Category[]"}
        if cats == {"5030", "2030"}:
            return _response({"Results": []})
        return _response({"Results": [
            {"Title": "Show S01E01 720p", "Seeders": 90, "MagnetUri": "magnet:?xt=urn:btih:HD"},
            {"Title": "Show S01E01 DVDRip", "Seeders": 3, "MagnetUri": "magnet:?xt=urn:btih:SD"},
        ]})

    with mock.patch("indexer_client.requests.get", side_effect=fake_get) as get:
        c = IndexerClient("http://localhost:9117")
        with pytest.raises(NoMagnetFound):
            c.get_best_magnet("Show S01E01")
        assert c.get_best_magnet("Show S01E01", fallback_categories=GENERAL_CATEGORIES) == "magnet:?xt=urn:btih:SD"
        searched = [{v for k, v in call.kwargs["params"] if k == "Category[]"} for call in get.call_args_list]
        assert searched[-2:] == [{"5030", "2030"}, {"5000", "2000"}]
