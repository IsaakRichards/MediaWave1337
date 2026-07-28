import pytest
from indexer_client import IndexerClient


def test_title_score_prefers_480i_and_4_3():
    c = IndexerClient("http://example.local")
    t1 = "SomeShow S01E01 480i 4:3 DVDRip"
    t2 = "SomeShow S01E01 1080p BluRay"
    s1 = c._title_score(t1)
    s2 = c._title_score(t2)
    assert s1 > s2


def test_title_score_handles_empty():
    c = IndexerClient("http://example.local")
    assert c._title_score("") == 0.0
