"""
indexer_client.py

Provides IndexerClient for querying a local Jackett or Prowlarr instance
and selecting the best magnet link for CRT-targeted playback (SD/480i/4:3).

Requirements implemented:
- Query Jackett or Prowlarr REST API using requests.
- Restrict category searches to SD TV (5030) and SD Movies (2030).
- Title regex scoring prioritizes keywords: 480i, 480p, DVDRip, SDTV, 4:3
  and penalizes 1080p, 4k, BluRay.
- Returns a single magnet link that balances seeder counts and title score.
- Clean exceptions if no magnet link is found.

Usage example:
    client = IndexerClient('http://localhost:9117', api_key='ABC')
    magnet = client.get_best_magnet('Twilight Zone S01E01')

Note: This module tries the common Jackett endpoint first (/api/v2.0/indexers/all/results)
and falls back to a Prowlarr-like search endpoint if needed. The Jackett API returns
result objects with keys like 'Title', 'MagnetUri', 'Seeders'. Prowlarr may return
different keys; the implementation attempts to be forgiving when parsing.
"""

from typing import List, Dict, Optional, Any
import requests
import math
import re
import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Categories restricted to SD TV and SD Movies as requested
_DEFAULT_CATEGORIES = [5030, 2030]


class NoMagnetFound(Exception):
    """Raised when a suitable magnet link cannot be found for the query."""


class IndexerClient:
    """Client for querying a local Jackett or Prowlarr instance and selecting
    the best magnet link for CRT-friendly releases.

    Parameters
    - base_url: Base URL of the Jackett/Prowlarr instance, e.g. http://localhost:9117
    - api_key: API key for the instance (Jackett commonly uses 'apikey' param).
    - timeout: HTTP request timeout in seconds.
    - provider_hint: Optional hint: 'jackett' or 'prowlarr'. If None, Jackett is tried first.
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: int = 10,
        provider_hint: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.provider_hint = provider_hint

    def _try_jackett(self, query: str, categories: List[int]) -> List[Dict[str, Any]]:
        """Call Jackett API (/api/v2.0/indexers/all/results).
        Returns a list of raw result dicts (may be empty).
        """
        url = f"{self.base_url}/api/v2.0/indexers/all/results"
        params = {"Query": query, "Limit": 100}
        # Jackett accepts category as 'Category' (comma separated) or 'Category[]'
        params["Category"] = ",".join(str(c) for c in categories)
        headers = {}
        if self.api_key:
            # Jackett typically expects apikey in query params
            params["apikey"] = self.api_key
        try:
            r = requests.get(url, params=params, headers=headers, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
            # Jackett commonly wraps results in 'Results'
            if isinstance(data, dict) and "Results" in data:
                return data["Results"] or []
            # Sometimes Jackett may return a list directly
            if isinstance(data, list):
                return data
            # Fall back to attempting to interpret top-level as iterable
            return []
        except requests.RequestException as exc:
            logger.debug("Jackett request failed: %s", exc)
            return []

    def _try_prowlarr(self, query: str, categories: List[int]) -> List[Dict[str, Any]]:
        """Attempt a Prowlarr-like search. Prowlarr's API surface can differ; try a
        reasonable endpoint and parameter set.
        """
        # Prowlarr common search endpoint (attempt): /api/v1/search
        url = f"{self.base_url}/api/v1/search"
        params = {"Query": query, "Limit": 100}
        params["categories"] = ",".join(str(c) for c in categories)
        headers = {}
        if self.api_key:
            # Prowlarr tends to accept X-Api-Key header
            headers["X-Api-Key"] = self.api_key
        try:
            r = requests.get(url, params=params, headers=headers, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
            # Prowlarr might return results in a 'Records' or 'Results' key
            if isinstance(data, dict):
                for key in ("Records", "Results", "results"):
                    if key in data and isinstance(data[key], list):
                        return data[key]
                # Maybe it's a list already
                return []
            if isinstance(data, list):
                return data
            return []
        except requests.RequestException as exc:
            logger.debug("Prowlarr request failed: %s", exc)
            return []

    def _fetch_candidates(self, title: str, categories: Optional[List[int]] = None) -> List[Dict[str, Any]]:
        """Fetch candidate release dicts from the indexer(s). Tries Jackett first,
        then Prowlarr. Returns a list (possibly empty).
        """
        cats = categories if categories is not None else _DEFAULT_CATEGORIES
        # Provider hint can short-circuit
        tried = []
        if self.provider_hint == "prowlarr":
            tried.extend([self._try_prowlarr(title, cats), self._try_jackett(title, cats)])
        else:
            # default: jackett first
            tried.extend([self._try_jackett(title, cats), self._try_prowlarr(title, cats)])

        # Flatten while preserving order preference
        candidates: List[Dict[str, Any]] = []
        for res in tried:
            if res:
                # Expecting list-like
                candidates.extend(res)
        return candidates

    @staticmethod
    def _extract_field(obj: Dict[str, Any], candidates: List[str]) -> Optional[Any]:
        """Try multiple possible keys in result object and return the first found."""
        for key in candidates:
            if key in obj:
                return obj[key]
            # case-insensitive
            lowmap = {k.lower(): v for k, v in obj.items()}
            if key.lower() in lowmap:
                return lowmap[key.lower()]
        return None

    @staticmethod
    def _get_magnet_from_result(result: Dict[str, Any]) -> Optional[str]:
        """Extract a magnet link from a result object if present."""
        possible_keys = ["MagnetUri", "MagnetLink", "Magnet", "magnetUri", "magnet", "Link", "link", "DownloadUrl", "Uri"]
        for k in possible_keys:
            val = IndexerClient._extract_field(result, [k])
            if isinstance(val, str) and val.startswith("magnet:"):
                return val
        # Some result objects contain nested 'Links' or 'Additional' structures
        # Try to search for any string that starts with magnet:
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
    def _get_seeders_from_result(result: Dict[str, Any]) -> int:
        """Extract seeder count from common keys, fallback to 0."""
        for k in ("Seeders", "seeders", "Seeds", "seeds", "peers", "Peers"):
            val = IndexerClient._extract_field(result, [k])
            if val is None:
                continue
            try:
                return int(val)
            except (ValueError, TypeError):
                continue
        return 0

    @staticmethod
    def _title_score(title: str) -> float:
        """Return a score for the title favoring CRT-friendly releases.

        Positive signals: '480i','480p','DVDRip','SDTV','4:3'
        Negative signals: '1080p','4k','BluRay'
        """
        if not title:
            return 0.0
        s = title.lower()
        score = 0.0

        # Positive keywords with weights
        positives = {
            r"\b480i\b": 4.0,
            r"\b480p\b": 3.5,
            r"\bdvdrip\b": 3.0,
            r"\bsdtv\b": 3.0,
            r"4[:x_]3": 4.0,  # matches 4:3, 4x3, 4_3
        }
        for pattern, w in positives.items():
            if re.search(pattern, s, flags=re.IGNORECASE):
                score += w

        # Negative keywords with weights (penalties)
        negatives = {
            r"\b1080p\b": -4.0,
            r"\b4k\b": -4.0,
            r"\bbluray\b": -3.5,
            r"\bbb(r)?rip\b": -2.0,
        }
        for pattern, w in negatives.items():
            if re.search(pattern, s, flags=re.IGNORECASE):
                score += w

        # Slight bonus for exact 'SD' mentions
        if re.search(r"\bsd\b", s):
            score += 1.0

        return score

    def _score_candidate(self, result: Dict[str, Any]) -> float:
        """Compute combined score balancing title quality and seeders.

        Formula (heuristic): combined = title_score * 10 + log(1 + seeders)
        Higher title score strongly favors CRT-friendly releases while seeders
        break ties and favor more available torrents.
        """
        title = self._extract_field(result, ["Title", "title", "name"]) or ""
        title_score = self._title_score(str(title))
        seeders = self._get_seeders_from_result(result)
        # Use log to avoid seeders dominating title quality
        combined = title_score * 10.0 + math.log(1 + seeders)
        logger.debug("Candidate: title=%s, title_score=%.2f, seeders=%d, combined=%.2f", title, title_score, seeders, combined)
        return combined

    def get_best_magnet(self, title: str, categories: Optional[List[int]] = None) -> str:
        """Return the single magnet link that best balances seeder counts and CRT-friendly titles.

        Raises NoMagnetFound if no suitable magnet is found.
        """
        candidates = self._fetch_candidates(title, categories=categories)
        if not candidates:
            raise NoMagnetFound(f"No results returned from indexer for query: {title}")

        scored: List[tuple] = []
        for c in candidates:
            magnet = self._get_magnet_from_result(c)
            if not magnet:
                continue
            score = self._score_candidate(c)
            scored.append((score, magnet, c))

        if not scored:
            raise NoMagnetFound(f"No magnet links found for query: {title}")

        # Pick highest score; in case of ties, higher seeders should already impact score
        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_magnet, best_obj = scored[0]
        logger.info("Selected magnet (score=%.2f): %s", best_score, best_magnet)
        return best_magnet


if __name__ == "__main__":
    # Simple local test harness (will not run during imports)
    import sys
    if len(sys.argv) < 2:
        print("Usage: python indexer_client.py \"search query\"")
        sys.exit(2)
    q = " ".join(sys.argv[1:])
    client = IndexerClient("http://localhost:9117", api_key=None)
    try:
        magnet = client.get_best_magnet(q)
        print(magnet)
    except NoMagnetFound as e:
        print("No magnet found:", e)
    except Exception as e:
        print("Error:", e)
