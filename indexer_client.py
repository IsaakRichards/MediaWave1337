"""
indexer_client.py

Provides IndexerClient for querying a local Jackett or Prowlarr instance
and selecting the best release for CRT-targeted playback (SD/480i/4:3).

- Queries Jackett (/api/v2.0/indexers/all/results) or Prowlarr (/api/v1/search).
- Restricts category searches to SD TV (5030) and SD Movies (2030) by default.
- Title regex scoring prioritizes 480i, 480p, DVDRip, SDTV, 4:3 and penalizes
  720p/1080p/2160p/4k, BluRay/BDRip, x265/HEVC.
- Returns a single link that balances seeder counts and title score. The link
  is a magnet URI when the indexer provides one, otherwise the indexer's
  http(s) .torrent download URL (torrent_streamer handles both).
- Raises NoMagnetFound if nothing usable is found.

Usage example:
    client = IndexerClient('http://localhost:9117', api_key='ABC')
    link = client.get_best_magnet('Twilight Zone S01E01')
"""

from typing import List, Dict, Optional, Any
from urllib.parse import urlparse
import requests
import math
import re
import logging

logger = logging.getLogger(__name__)

# Categories restricted to SD TV and SD Movies
_DEFAULT_CATEGORIES = [5030, 2030]
# All TV and all Movies: optional second search when the SD categories find nothing.
GENERAL_CATEGORIES = [5000, 2000]

_POSITIVE_PATTERNS = {
    r"\b480i\b": 4.0,
    r"\b480p\b": 3.5,
    r"\b576[ip]\b": 3.0,
    r"\bdvd(rip|5|9|r)?\b": 3.0,
    r"\bsdtv\b": 3.0,
    r"\b(xvid|divx)\b": 1.0,
    r"4[:x_]3": 4.0,  # matches 4:3, 4x3, 4_3
}
_NEGATIVE_PATTERNS = {
    r"\b720p\b": -2.5,
    r"\b1080[pi]\b": -4.0,
    r"\b(2160p|4k|uhd)\b": -5.0,
    r"\bblu-?ray\b": -3.5,
    r"\b(bdrip|brrip|bdremux)\b": -2.0,
    r"\b(x265|hevc|h\.?265)\b": -1.5,
    r"\b(16[:x]9|widescreen)\b": -2.0,
}


class NoMagnetFound(Exception):
    """Raised when a suitable magnet link cannot be found for the query."""


class IndexerUnavailable(Exception):
    """Raised when no indexer answered at all (as opposed to answering with no results)."""


class IndexerClient:
    """Client for querying a local Jackett or Prowlarr instance and selecting
    the best release for CRT-friendly playback.

    Parameters
    - base_url: Base URL of the Jackett/Prowlarr instance, e.g. http://localhost:9117
    - api_key: API key for the instance.
    - timeout: HTTP request timeout in seconds.
    - provider_hint: 'jackett' or 'prowlarr'. If None it is guessed from the
      port (9117 = Jackett, 9696 = Prowlarr), otherwise Jackett is tried first
      and Prowlarr only if the Jackett endpoint is unreachable.
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: int = 20,
        provider_hint: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.provider_hint = provider_hint or self._guess_provider(self.base_url)

    @staticmethod
    def _guess_provider(base_url: str) -> Optional[str]:
        try:
            port = urlparse(base_url).port
        except ValueError:
            port = None
        if port == 9696:
            return "prowlarr"
        if port == 9117:
            return "jackett"
        return None

    def _try_jackett(self, query: str, categories: List[int]) -> Optional[List[Dict[str, Any]]]:
        """Search Jackett. Returns None if the endpoint failed, else a list."""
        url = f"{self.base_url}/api/v2.0/indexers/all/results"
        params: List[tuple] = [("Query", query)]
        params += [("Category[]", str(c)) for c in categories]
        if self.api_key:
            params.append(("apikey", self.api_key))
        try:
            r = requests.get(url, params=params, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            logger.debug("Jackett request failed: %s", exc)
            return None
        if isinstance(data, dict):
            return data.get("Results") or []
        return data if isinstance(data, list) else []

    def _try_prowlarr(self, query: str, categories: List[int]) -> Optional[List[Dict[str, Any]]]:
        """Search Prowlarr. Returns None if the endpoint failed, else a list."""
        url = f"{self.base_url}/api/v1/search"
        params: List[tuple] = [("query", query), ("type", "search"), ("limit", "100")]
        params += [("categories", str(c)) for c in categories]
        headers = {"X-Api-Key": self.api_key} if self.api_key else {}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            logger.debug("Prowlarr request failed: %s", exc)
            return None
        if isinstance(data, dict):
            for key in ("Records", "Results", "results", "records"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
            else:
                return []
        if not isinstance(data, list):
            return []
        # Prowlarr can also return usenet results; keep torrents only.
        return [r for r in data if str(r.get("protocol", "torrent")).lower() == "torrent"]

    def _fetch_candidates(self, title: str, categories: Optional[List[int]] = None) -> List[Dict[str, Any]]:
        """Fetch candidate release dicts from the configured indexer."""
        cats = categories if categories is not None else _DEFAULT_CATEGORIES
        if self.provider_hint == "prowlarr":
            order = [self._try_prowlarr, self._try_jackett]
        else:
            order = [self._try_jackett, self._try_prowlarr]
        for attempt in order:
            results = attempt(title, cats)
            if results is not None:
                return results
        logger.warning("No indexer answered at %s", self.base_url)
        raise IndexerUnavailable(f"No indexer answered at {self.base_url}")

    @staticmethod
    def _extract_field(obj: Dict[str, Any], candidates: List[str]) -> Optional[Any]:
        """Try multiple possible keys (case-insensitive) and return the first found."""
        lowmap = {k.lower(): v for k, v in obj.items()}
        for key in candidates:
            if key in obj:
                return obj[key]
            if key.lower() in lowmap:
                return lowmap[key.lower()]
        return None

    @staticmethod
    def _get_magnet_from_result(result: Dict[str, Any]) -> Optional[str]:
        """Extract a magnet link from a result object if present."""
        for k in ("MagnetUri", "magnetUrl", "MagnetLink", "Magnet", "guid", "Link", "downloadUrl", "Uri"):
            val = IndexerClient._extract_field(result, [k])
            if isinstance(val, str) and val.startswith("magnet:"):
                return val

        def walk(o: Any) -> Optional[str]:
            if isinstance(o, str) and o.startswith("magnet:"):
                return o
            if isinstance(o, dict):
                for v in o.values():
                    found = walk(v)
                    if found:
                        return found
            if isinstance(o, list):
                for it in o:
                    found = walk(it)
                    if found:
                        return found
            return None

        return walk(result)

    @staticmethod
    def _get_link_from_result(result: Dict[str, Any]) -> Optional[str]:
        """Magnet if available, otherwise an http(s) .torrent download link."""
        magnet = IndexerClient._get_magnet_from_result(result)
        if magnet:
            return magnet
        for k in ("Link", "downloadUrl", "DownloadUrl"):
            val = IndexerClient._extract_field(result, [k])
            if isinstance(val, str) and val.lower().startswith(("http://", "https://")):
                return val
        return None

    @staticmethod
    def _get_seeders_from_result(result: Dict[str, Any]) -> int:
        """Extract seeder count from common keys, fallback to 0."""
        val = IndexerClient._extract_field(result, ["Seeders", "seeds"])
        try:
            return max(0, int(val))
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _title_score(title: str) -> float:
        """Return a score for the title favoring CRT-friendly releases."""
        if not title:
            return 0.0
        s = title.lower()
        score = 0.0
        for pattern, w in _POSITIVE_PATTERNS.items():
            if re.search(pattern, s):
                score += w
        for pattern, w in _NEGATIVE_PATTERNS.items():
            if re.search(pattern, s):
                score += w
        if re.search(r"\bsd\b", s):
            score += 1.0
        return score

    def _score_candidate(self, result: Dict[str, Any]) -> float:
        """combined = title_score * 10 + log(1 + seeders)

        Title quality dominates; seeders break ties and favor healthy torrents.
        """
        title = self._extract_field(result, ["Title", "name"]) or ""
        title_score = self._title_score(str(title))
        seeders = self._get_seeders_from_result(result)
        combined = title_score * 10.0 + math.log(1 + seeders)
        logger.debug("Candidate: title=%s, title_score=%.2f, seeders=%d, combined=%.2f", title, title_score, seeders, combined)
        return combined

    def rank_releases(self, candidates: List[Dict[str, Any]], min_seeders: int = 0) -> List[Dict[str, Any]]:
        """Return usable candidates as dicts sorted best-first."""
        ranked = []
        for c in candidates:
            link = self._get_link_from_result(c)
            if not link:
                continue
            seeders = self._get_seeders_from_result(c)
            if seeders < max(0, min_seeders):
                continue
            ranked.append({
                "title": str(self._extract_field(c, ["Title", "name"]) or ""),
                "link": link,
                "seeders": seeders,
                "score": self._score_candidate(c),
            })
        ranked.sort(key=lambda r: r["score"], reverse=True)
        return ranked

    def get_best_release(
        self,
        title: str,
        categories: Optional[List[int]] = None,
        min_seeders: int = 0,
        exclude: Optional[List[str]] = None,
        fallback_categories: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Best-scoring release whose link is not in ``exclude`` (links that already failed).
        If nothing usable is found and ``fallback_categories`` is given, search those too
        (title scoring still prefers SD releases there).

        Raises NoMagnetFound, or IndexerUnavailable if the indexer did not answer."""
        try:
            return self._best_release(title, categories, min_seeders, exclude)
        except NoMagnetFound:
            if not fallback_categories:
                raise
            logger.info("Nothing in the SD categories for %s; searching %s", title, fallback_categories)
            return self._best_release(title, fallback_categories, min_seeders, exclude)

    def _best_release(self, title, categories, min_seeders, exclude) -> Dict[str, Any]:
        candidates = self._fetch_candidates(title, categories=categories)
        if not candidates:
            raise NoMagnetFound(f"No results returned from indexer for query: {title}")
        excluded = set(exclude or [])
        ranked = [r for r in self.rank_releases(candidates, min_seeders=min_seeders) if r["link"] not in excluded]
        if not ranked:
            if excluded:
                raise NoMagnetFound(f"No untried releases left for: {title}")
            raise NoMagnetFound(f"No downloadable releases with at least {min_seeders} seeders for: {title}")
        best = ranked[0]
        logger.info("Selected release (score=%.2f, seeders=%d): %s", best["score"], best["seeders"], best["title"])
        return best

    def get_best_magnet(
        self,
        title: str,
        categories: Optional[List[int]] = None,
        min_seeders: int = 0,
        exclude: Optional[List[str]] = None,
        fallback_categories: Optional[List[int]] = None,
    ) -> str:
        """Return the single link (magnet or .torrent URL) that best balances
        seeder counts and CRT-friendly titles, skipping links in ``exclude``.
        Raises NoMagnetFound or IndexerUnavailable."""
        return self.get_best_release(
            title, categories=categories, min_seeders=min_seeders, exclude=exclude, fallback_categories=fallback_categories,
        )["link"]


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("Usage: python indexer_client.py \"search query\"")
        sys.exit(2)
    q = " ".join(sys.argv[1:])
    client = IndexerClient("http://localhost:9117", api_key=None)
    try:
        print(client.get_best_magnet(q))
    except (NoMagnetFound, IndexerUnavailable) as e:
        print("No magnet found:", e)
