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
