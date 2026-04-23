from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import threading
import time
import traceback
import uuid
from collections import Counter
from copy import deepcopy
from datetime import date, datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from moviebox_api.v1.constants import SubjectType
from moviebox_api.v2.core import Search as WebSearch
from moviebox_api.v2.download import DownloadableMovieFilesDetail as WebDownloadableMovieFilesDetail
from moviebox_api.v2.requests import Session as WebSession
from moviebox_api.v3.core import DownloadableFilesDetail, Homepage, ItemDetails, Search
from moviebox_api.v3.http_client import MovieBoxHttpClient
from moviebox_api.v3.urls import PLAY_INFO_PATH


ROOT_DIR = Path(__file__).resolve().parent
VIEWS_FILE = ROOT_DIR / "movie-views.json"
BUNDLED_HOME_FILE = ROOT_DIR / "moviebox-api" / "assets" / "recons2" / "homepage.json"
HOME_CACHE_TTL_SECONDS = 15 * 60
DETAIL_CACHE_TTL_SECONDS = 15 * 60
SEARCH_CACHE_TTL_SECONDS = 5 * 60
CATEGORY_CACHE_TTL_SECONDS = 5 * 60
STREAM_PROXY_TTL_SECONDS = 30 * 60
PLAYABLE_CACHE_TTL_SECONDS = DETAIL_CACHE_TTL_SECONDS
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
PAGE_MOVIE_LIMIT = 100
HOME_CATALOG_LIMIT = 48
SEARCH_PAGE_SIZE = 20
HOME_PAGE_FETCH_LIMIT = 5
PLAYABLE_BATCH_SIZE = 8
HOME_PAGE_REQUEST_TIMEOUT_SECONDS = 6
PLAYABLE_PROBE_TIMEOUT_SECONDS = 4
HOME_PLAYABLE_PROBE_LIMIT = 12
SEARCH_PLAYABLE_PROBE_LIMIT = 16
HOME_WARM_INITIAL_DELAY_SECONDS = 2
HOME_WARM_INTERVAL_SECONDS = 10 * 60
DIRECT_SOURCE_PREFIX = "moviebox direct"
SPECIAL_SERIES_CATEGORY_QUERIES = {
    "Korean Drama": ["korean drama", "kdrama"],
    "China Drama": ["china drama", "chinese drama", "cdrama"],
}

CATEGORY_BLURBS = {
    "Trending": "Fresh picks pulled from the latest Moviebox homepage feed.",
    "Action": "High-energy stories with momentum, spectacle, and sharp conflict.",
    "Comedy": "Lighter, funnier picks drawn from the live catalog.",
    "Drama": "Character-heavy titles with tension, emotion, and depth.",
}

TITLE_TAG_PATTERN = re.compile(r"\s*\[\s*hindi\s*\]\s*", re.IGNORECASE)
DIRECT_MEDIA_PATTERN = re.compile(
    r"(\.mp4|\.m3u8|\.webm|\.mkv|\.mpd|\.m4s|\.ts)(\?|$)|macdn\.aoneroom\.com|bcdn\.hakunaymatata\.com|sacdn\.hakunaymatata\.com|/resource/|/dash/",
    re.IGNORECASE,
)
DASH_BASE_URL_PATTERN = re.compile(r"<BaseURL>.*?</BaseURL>", re.IGNORECASE | re.DOTALL)
PERIOD_TAG_PATTERN = re.compile(r"(<Period\b[^>]*>)", re.IGNORECASE)


def resolve_server_port() -> int:
    raw_port = str(os.getenv("PORT", "")).strip()
    if raw_port.isdigit():
        return int(raw_port)
    return DEFAULT_PORT


def resolve_server_host() -> str:
    explicit_host = str(os.getenv("HOST", "")).strip()
    if explicit_host:
        return explicit_host
    if str(os.getenv("PORT", "")).strip():
        return "0.0.0.0"
    return DEFAULT_HOST


def background_home_warm_enabled() -> bool:
    raw_value = str(os.getenv("FLIXORA_DISABLE_BACKGROUND_WARM_CACHE", "")).strip().lower()
    return raw_value not in {"1", "true", "yes", "on"}


def load_view_counts() -> dict[str, int]:
    try:
        payload = json.loads(VIEWS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}

    if not isinstance(payload, dict):
        return {}

    counts: dict[str, int] = {}
    for key, value in payload.items():
        subject_id = str(key).strip()
        if not subject_id:
            continue
        try:
            counts[subject_id] = max(int(value), 0)
        except (TypeError, ValueError):
            continue
    return counts


_cache_lock = threading.Lock()
_cache: dict[str, Any] = {
    "home": {"timestamp": 0.0, "value": None},
    "detail": {},
    "playable": {},
    "search": {},
    "category": {},
    "stream_tokens": {},
    "views": load_view_counts(),
}


def split_csv(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(entry).strip() for entry in value if str(entry).strip()]
    if isinstance(value, str):
        return [entry.strip() for entry in value.split(",") if entry.strip()]
    return []


def clean_title(value: Any) -> str:
    title = str(value or "").strip()
    if not title:
        return "Untitled"

    cleaned = TITLE_TAG_PATTERN.sub(" ", title)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned or "Untitled"


def make_match_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean_title(value).lower())


def is_direct_media_url(url: Any) -> bool:
    return isinstance(url, str) and bool(DIRECT_MEDIA_PATTERN.search(url))


def parse_year(value: Any) -> int | None:
    if not value:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, date):
        return value.year
    if isinstance(value, str):
        if len(value) >= 4 and value[:4].isdigit():
            return int(value[:4])
        try:
            return datetime.fromisoformat(value).year
        except ValueError:
            return None
    return None


def to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def persist_view_counts() -> None:
    try:
        VIEWS_FILE.write_text(
            json.dumps(_cache["views"], ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        pass


def get_view_count(subject_id: Any) -> int:
    normalized_id = str(subject_id or "").strip()
    if not normalized_id:
        return 0
    with _cache_lock:
        return int(_cache["views"].get(normalized_id, 0))


def increment_view_count(subject_id: Any) -> int:
    normalized_id = str(subject_id or "").strip()
    if not normalized_id:
        return 0

    with _cache_lock:
        current_count = int(_cache["views"].get(normalized_id, 0)) + 1
        _cache["views"][normalized_id] = current_count
        persist_view_counts()
    return current_count


def first_url(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def is_series(subject_type: Any) -> bool:
    return str(subject_type) == str(int(SubjectType.TV_SERIES))


def is_movie_subject(subject: dict[str, Any]) -> bool:
    return isinstance(subject, dict) and not is_series(subject.get("subjectType"))


def normalize_subject(subject: dict[str, Any]) -> dict[str, Any]:
    genres = split_csv(subject.get("genre"))
    cover = subject.get("cover") or {}
    stills = subject.get("stills") or {}
    trailer = subject.get("trailer") or {}
    trailer_video = trailer.get("VideoAddress") or {}
    play_url = subject.get("playUrl") or {}
    detectors = subject.get("resourceDetectors") or []
    first_detector = detectors[0] if detectors else {}

    year = parse_year(subject.get("releaseDate"))
    rating = to_float(
        subject.get("imdbRatingValue")
        or subject.get("imdbRate")
        or subject.get("rate")
    )
    media_type = "series" if is_series(subject.get("subjectType")) else "movie"
    poster = first_url(cover.get("url"), stills.get("url"))
    backdrop = first_url(
        stills.get("url"),
        subject.get("preVideoCover", {}).get("url")
        if isinstance(subject.get("preVideoCover"), dict)
        else "",
        trailer.get("cover", {}).get("url") if isinstance(trailer.get("cover"), dict) else "",
        cover.get("url"),
    )
    trailer_url = first_url(
        trailer_video.get("url"),
        play_url.get("playUrl") if isinstance(play_url, dict) else "",
        subject.get("detailUrl"),
    )
    download_url = first_url(
        first_detector.get("downloadUrl"),
        first_detector.get("resourceLink"),
        trailer_url,
        subject.get("detailUrl"),
    )

    normalized = {
        "id": str(
            subject.get("subjectId")
            or subject.get("subject_id")
            or subject.get("id")
            or ""
        ),
        "title": clean_title(subject.get("title")),
        "poster": poster,
        "backdrop": backdrop or poster,
        "desc": subject.get("description") or subject.get("postTitle") or "No description available yet.",
        "rating": round(rating, 1),
        "year": year,
        "category": genres[0] if genres else "Trending",
        "genres": genres,
        "trailer": trailer_url,
        "downloadUrl": download_url,
        "detailUrl": subject.get("detailUrl") or "",
        "mediaType": media_type,
        "country": subject.get("countryName") or "",
        "language": split_csv(subject.get("language")),
        "hasResource": bool(subject.get("hasResource")),
        "seasonCount": int(subject.get("seNum") or 0),
        "resourceOptions": [],
        "subtitleOptions": [],
        "seriesItems": [],
        "viewCount": 0,
    }
    normalized["category"] = primary_category(normalized)
    return normalized


def infer_series_category(movie: dict[str, Any]) -> str | None:
    if movie.get("mediaType") != "series":
        return None

    country = str(movie.get("country") or "").strip().lower()
    if "korea" in country:
        return "Korean Drama"
    if "china" in country:
        return "China Drama"
    return None


def movie_category_tags(movie: dict[str, Any]) -> list[str]:
    tags: list[str] = []

    special_category = infer_series_category(movie)
    if special_category:
        tags.append(special_category)

    for genre in movie.get("genres", []):
        genre_name = str(genre or "").strip()
        if genre_name and genre_name not in tags:
            tags.append(genre_name)

    return tags


def primary_category(movie: dict[str, Any]) -> str:
    tags = movie_category_tags(movie)
    return tags[0] if tags else "Trending"


def movie_matches_category(movie: dict[str, Any], category_name: str) -> bool:
    target = str(category_name or "").strip().lower()
    if not target:
        return False
    return any(tag.lower() == target for tag in movie_category_tags(movie))


def dedupe_movies(movies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for movie in movies:
        movie_id = movie.get("id")
        if not movie_id or movie_id in seen:
            continue
        seen.add(movie_id)
        unique.append(movie)
    return unique


def dedupe_catalog_variants(movies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []

    for movie in movies:
        title_key = make_match_key(movie.get("title"))
        if not title_key:
            continue

        dedupe_key = "::".join(
            [
                str(movie.get("mediaType") or ""),
                title_key,
                str(movie.get("year") or ""),
            ]
        )
        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        unique.append(movie)

    return unique


def merge_catalogs(*catalogs: list[dict[str, Any]], limit: int = PAGE_MOVIE_LIMIT) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for catalog in catalogs:
        for movie in catalog:
            movie_id = str(movie.get("id") or "").strip()
            if not movie_id or movie_id in seen_ids:
                continue

            seen_ids.add(movie_id)
            merged.append(movie)
            if len(merged) >= limit:
                return merged[:limit]

    return merged[:limit]


def apply_view_count_to_movie(movie: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(movie, dict):
        return movie
    movie["viewCount"] = get_view_count(movie.get("id"))
    return movie


def apply_view_counts_to_movies(movies: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not isinstance(movies, list):
        return movies or []
    for movie in movies:
        apply_view_count_to_movie(movie)
    return movies


def apply_view_counts_to_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload

    for key in ("hero", "catalog", "items", "movies", "keepBrowsing", "related", "trending"):
        if key in payload:
            payload[key] = apply_view_counts_to_movies(payload.get(key))

    if "featured" in payload:
        payload["featured"] = apply_view_count_to_movie(payload.get("featured"))

    if "movie" in payload:
        payload["movie"] = apply_view_count_to_movie(payload.get("movie"))

    if isinstance(payload.get("sections"), list):
        for section in payload["sections"]:
            if isinstance(section, dict):
                section["movies"] = apply_view_counts_to_movies(section.get("movies"))

    return payload


def derive_categories(catalog: list[dict[str, Any]]) -> list[str]:
    counts: Counter[str] = Counter()
    for movie in catalog:
        for tag in movie_category_tags(movie):
            counts[tag] += 1

    preferred = ["Action", "Comedy", "Drama", "Korean Drama", "China Drama"]
    categories = [genre for genre in preferred if counts.get(genre)]
    for genre, _ in counts.most_common():
        if genre not in categories:
            categories.append(genre)
    return categories[:10]


def build_genre_sections(catalog: list[dict[str, Any]], categories: list[str]) -> list[dict[str, Any]]:
    sections = [
        {
            "slug": "trending",
            "title": "Trending Now",
            "description": "",
            "movies": sorted(catalog, key=lambda movie: movie.get("rating", 0), reverse=True)[:PAGE_MOVIE_LIMIT],
        }
    ]

    target_categories = [genre for genre in ["Action", "Comedy", "Drama"] if genre in categories]
    for genre in categories:
        if len(target_categories) >= 3:
            break
        if genre not in target_categories:
            target_categories.append(genre)

    for genre in target_categories[:3]:
        sections.append(
            {
                "slug": genre.lower().replace(" ", "-"),
                "title": genre,
                "description": "",
                "movies": [movie for movie in catalog if movie_matches_category(movie, genre)][:PAGE_MOVIE_LIMIT],
            }
        )

    return sections


def build_home_payload(
    catalog: list[dict[str, Any]],
    hero_ids: list[str] | None = None,
) -> dict[str, Any]:
    catalog = dedupe_movies(catalog)
    hero_movies: list[dict[str, Any]] = []

    if hero_ids:
        by_id = {
            str(movie.get("id") or ""): movie
            for movie in catalog
            if movie.get("id")
        }
        for movie_id in hero_ids:
            movie = by_id.get(movie_id)
            if movie is not None:
                hero_movies.append(movie)

    hero_movies = dedupe_movies(hero_movies)
    categories = derive_categories(catalog)
    sections = build_genre_sections(catalog, categories)

    return {
        "hero": hero_movies[:6] or catalog[:6],
        "catalog": catalog,
        "categories": categories,
        "sections": sections,
        "updatedAt": int(time.time()),
    }


def fallback_catalog_from_subjects(
    subjects: list[dict[str, Any]],
    limit: int = PAGE_MOVIE_LIMIT,
) -> list[dict[str, Any]]:
    normalized_movies = [
        normalize_subject(subject)
        for subject in subjects
        if isinstance(subject, dict) and is_movie_subject(subject)
    ]
    normalized_movies = dedupe_movies(normalized_movies)
    normalized_movies.sort(key=lambda movie: movie.get("rating", 0), reverse=True)
    return normalized_movies[:limit]


def load_bundled_home_payload() -> dict[str, Any] | None:
    try:
        raw_home = json.loads(BUNDLED_HOME_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None

    if not isinstance(raw_home, dict):
        return None

    hero_ids: list[str] = []
    candidate_subjects: list[dict[str, Any]] = []

    for item in raw_home.get("items", []):
        if not isinstance(item, dict):
            continue

        banner = item.get("banner") or {}
        for banner_item in banner.get("banners", []):
            if not isinstance(banner_item, dict):
                continue
            subject = banner_item.get("subject")
            if isinstance(subject, dict):
                subject_id = str(subject.get("subjectId") or "").strip()
                if subject_id:
                    hero_ids.append(subject_id)

        for subject in item.get("subjects", []):
            if isinstance(subject, dict):
                candidate_subjects.append(subject)

    if not candidate_subjects:
        return None

    catalog = fallback_catalog_from_subjects(
        candidate_subjects,
        limit=HOME_CATALOG_LIMIT,
    )
    return build_home_payload(catalog, hero_ids)


def stale(entry: dict[str, Any], ttl_seconds: int) -> bool:
    return time.time() - entry.get("timestamp", 0) > ttl_seconds


async def fetch_home_payload() -> dict[str, Any]:
    async with MovieBoxHttpClient(timeout=30) as client_session:
        raw_pages: list[dict[str, Any]] = []
        catalog_size = 0
        hero_ids: list[str] = []

        for page_number in range(1, HOME_PAGE_FETCH_LIMIT + 1):
            homepage = Homepage(client_session)
            homepage._page_number = page_number
            try:
                raw_home = await asyncio.wait_for(
                    homepage.get_content(),
                    timeout=HOME_PAGE_REQUEST_TIMEOUT_SECONDS,
                )
            except Exception:
                if raw_pages:
                    break
                bundled_payload = load_bundled_home_payload()
                if bundled_payload is not None:
                    return bundled_payload
                raise

            raw_pages.append(raw_home)

            if page_number == 1:
                for item in raw_home.get("items", []):
                    banner = item.get("banner") or {}
                    for banner_item in banner.get("banners", []):
                        subject = banner_item.get("subject")
                        if isinstance(subject, dict):
                            subject_id = str(subject.get("subjectId") or "").strip()
                            if subject_id:
                                hero_ids.append(subject_id)

            page_catalog_size = sum(
                len(item.get("subjects", []))
                for item in raw_home.get("items", [])
                if isinstance(item, dict)
            )
            catalog_size += page_catalog_size
            if catalog_size >= PAGE_MOVIE_LIMIT:
                break

        candidate_subjects: list[dict[str, Any]] = []
        for raw_home in raw_pages:
            for item in raw_home.get("items", []):
                for subject in item.get("subjects", []):
                    if isinstance(subject, dict):
                        candidate_subjects.append(subject)

        fallback_catalog = fallback_catalog_from_subjects(
            candidate_subjects,
            limit=HOME_CATALOG_LIMIT,
        )

        # Probe only a small slice of candidates for verified direct playback.
        # Render cold starts can otherwise spend tens of seconds waiting on
        # upstream Moviebox resource checks before the first page can render.
        direct_catalog = await filter_subjects_to_direct_movies(
            client_session,
            candidate_subjects[:HOME_PLAYABLE_PROBE_LIMIT],
            limit=HOME_PLAYABLE_PROBE_LIMIT,
            probe_timeout_seconds=PLAYABLE_PROBE_TIMEOUT_SECONDS,
        )
        merged_catalog = merge_catalogs(
            direct_catalog,
            fallback_catalog,
            limit=HOME_CATALOG_LIMIT,
        )

        return build_home_payload(merged_catalog, hero_ids)


def resolve_home(force_refresh: bool = False) -> dict[str, Any]:
    with _cache_lock:
        entry = _cache["home"]
        cached = entry.get("value")
        should_refresh = force_refresh or cached is None or stale(entry, HOME_CACHE_TTL_SECONDS)

    if not should_refresh:
        return apply_view_counts_to_payload(deepcopy(cached))

    try:
        fresh = asyncio.run(fetch_home_payload())
    except Exception:
        with _cache_lock:
            cached = _cache["home"].get("value")
        if cached is not None:
            return apply_view_counts_to_payload(deepcopy(cached))
        raise

    with _cache_lock:
        _cache["home"] = {"timestamp": time.time(), "value": fresh}
    return apply_view_counts_to_payload(deepcopy(fresh))


def background_home_warm_loop(stop_event: threading.Event) -> None:
    if HOME_WARM_INITIAL_DELAY_SECONDS > 0 and stop_event.wait(HOME_WARM_INITIAL_DELAY_SECONDS):
        return

    while not stop_event.is_set():
        try:
            payload = resolve_home(force_refresh=True)
            print(
                "[flixora] Warmed home cache "
                f"with {len(payload.get('catalog', []))} catalog items.",
                flush=True,
            )
        except Exception as exc:
            print(f"[flixora] Background home cache warm failed: {exc}", flush=True)

        if stop_event.wait(HOME_WARM_INTERVAL_SECONDS):
            return


def build_resource_options(detail: dict[str, Any]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for detector in detail.get("resourceDetectors") or []:
        qualities: list[dict[str, Any]] = []

        for entry in detector.get("resolutionList") or []:
            resolution = int(entry.get("resolution") or 0)
            play_url = entry.get("resourceLink") or detector.get("downloadUrl") or ""
            source_url = entry.get("sourceUrl") or detector.get("resourceLink") or play_url

            if not play_url and not source_url:
                continue

            qualities.append(
                {
                    "label": f"{resolution}P" if resolution else "Source",
                    "resolution": resolution,
                    "playUrl": play_url or source_url,
                    "downloadUrl": play_url or source_url,
                    "sourceUrl": source_url,
                }
            )

        if not qualities:
            fallback_url = detector.get("downloadUrl") or detector.get("resourceLink") or ""
            if fallback_url:
                qualities.append(
                    {
                        "label": "Source",
                        "resolution": 0,
                        "playUrl": fallback_url,
                        "downloadUrl": fallback_url,
                        "sourceUrl": detector.get("resourceLink") or fallback_url,
                    }
                )

        qualities.sort(key=lambda item: item.get("resolution", 0), reverse=True)
        resolutions = sorted(
            {
                int(item.get("resolution") or 0)
                for item in qualities
                if item.get("resolution")
            },
            reverse=True,
        )
        options.append(
            {
                "source": detector.get("source") or "Moviebox Source",
                "resourceLink": detector.get("resourceLink") or "",
                "downloadUrl": detector.get("downloadUrl") or "",
                "resolutions": resolutions,
                "qualities": qualities,
            }
        )
    return options


def build_downloadable_resource_options(downloadables: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(downloadables, dict):
        return []

    qualities: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for entry in downloadables.get("list") or []:
        play_url = first_url(entry.get("resourceLink"))
        if not play_url or play_url in seen_urls:
            continue

        seen_urls.add(play_url)
        resolution = int(entry.get("resolution") or 0)
        source_url = first_url(entry.get("sourceUrl"), play_url)
        qualities.append(
            {
                "label": f"{resolution}P" if resolution else "Source",
                "resolution": resolution,
                "playUrl": play_url,
                "downloadUrl": play_url,
                "sourceUrl": source_url,
            }
        )

    if not qualities:
        return []

    qualities.sort(key=lambda item: item.get("resolution", 0), reverse=True)
    resolutions = [
        item["resolution"]
        for item in qualities
        if item.get("resolution")
    ]
    best_quality = qualities[0]

    return [
        {
            "source": "Moviebox Direct",
            "resourceLink": best_quality.get("sourceUrl") or best_quality.get("playUrl") or "",
            "downloadUrl": best_quality.get("downloadUrl") or "",
            "resolutions": resolutions,
            "qualities": qualities,
        }
    ]


def build_named_downloadable_resource_options(
    downloadables: dict[str, Any] | None,
    source_name: str,
) -> list[dict[str, Any]]:
    options = build_downloadable_resource_options(downloadables)
    for option in options:
        option["source"] = source_name
    return options


def is_moviebox_direct_source(source: Any) -> bool:
    return isinstance(source, str) and source.strip().lower().startswith(
        DIRECT_SOURCE_PREFIX
    )


def is_direct_playable_quality(quality: dict[str, Any]) -> bool:
    if not isinstance(quality, dict):
        return False

    play_url = first_url(
        quality.get("playUrl"),
        quality.get("downloadUrl"),
        quality.get("sourceUrl"),
    )
    if not play_url:
        return False

    stream_type = str(quality.get("streamType") or "").strip().lower()
    if stream_type == "dash":
        return False

    return is_direct_media_url(play_url)


def is_playable_quality(quality: dict[str, Any]) -> bool:
    if not isinstance(quality, dict):
        return False

    play_url = first_url(
        quality.get("playUrl"),
        quality.get("downloadUrl"),
        quality.get("sourceUrl"),
    )
    stream_type = str(quality.get("streamType") or "").strip().lower()
    manifest_url = first_url(quality.get("manifestUrl"))

    if stream_type == "dash":
        return bool(play_url and manifest_url)

    return bool(play_url and is_direct_media_url(play_url))


def filter_direct_moviebox_options(
    options: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []

    for option in options or []:
        if not isinstance(option, dict):
            continue
        if not is_moviebox_direct_source(option.get("source")):
            continue

        qualities = [
            quality
            for quality in option.get("qualities") or []
            if is_direct_playable_quality(quality)
        ]
        if not qualities:
            continue

        resolutions = sorted(
            {
                int(quality.get("resolution") or 0)
                for quality in qualities
                if quality.get("resolution")
            },
            reverse=True,
        )
        best_quality = qualities[0]

        filtered.append(
            {
                **option,
                "downloadUrl": first_url(
                    best_quality.get("downloadUrl"),
                    best_quality.get("playUrl"),
                    option.get("downloadUrl"),
                ),
                "resourceLink": first_url(
                    best_quality.get("sourceUrl"),
                    best_quality.get("playUrl"),
                    option.get("resourceLink"),
                ),
                "resolutions": resolutions,
                "qualities": qualities,
            }
        )

    return filtered


def prioritize_playable_resource_options(
    options: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    prioritized: list[dict[str, Any]] = []

    for option in options or []:
        if not isinstance(option, dict):
            continue

        playable_qualities = [
            quality
            for quality in option.get("qualities") or []
            if is_playable_quality(quality)
        ]
        if not playable_qualities:
            continue

        playable_qualities.sort(
            key=lambda quality: (
                str(quality.get("streamType") or "").strip().lower() == "dash",
                int(quality.get("resolution") or 0),
            ),
            reverse=True,
        )
        best_quality = playable_qualities[0]
        resolutions = sorted(
            {
                int(quality.get("resolution") or 0)
                for quality in playable_qualities
                if quality.get("resolution")
            },
            reverse=True,
        )

        prioritized.append(
            {
                "source": option.get("source") or "Moviebox Source",
                "resourceLink": best_quality.get("sourceUrl") or best_quality.get("playUrl") or "",
                "downloadUrl": best_quality.get("downloadUrl") or "",
                "resolutions": resolutions,
                "qualities": playable_qualities,
            }
        )

    prioritized.sort(
        key=lambda option: (
            any(
                str(quality.get("streamType") or "").strip().lower() == "dash"
                for quality in option.get("qualities") or []
            ),
            is_moviebox_direct_source(option.get("source")),
            max((int(quality.get("resolution") or 0) for quality in option.get("qualities") or []), default=0),
        ),
        reverse=True,
    )
    return prioritized


def filter_playable_resource_options(
    options: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []

    for option in options or []:
        if not isinstance(option, dict):
            continue

        qualities = [
            quality
            for quality in option.get("qualities") or []
            if is_playable_quality(quality)
        ]
        if not qualities:
            continue

        resolutions = sorted(
            {
                int(quality.get("resolution") or 0)
                for quality in qualities
                if quality.get("resolution")
            },
            reverse=True,
        )
        best_quality = qualities[0]

        filtered.append(
            {
                **option,
                "downloadUrl": first_url(
                    best_quality.get("downloadUrl"),
                    best_quality.get("playUrl"),
                    option.get("downloadUrl"),
                ),
                "resourceLink": first_url(
                    best_quality.get("sourceUrl"),
                    best_quality.get("playUrl"),
                    option.get("resourceLink"),
                ),
                "resolutions": resolutions,
                "qualities": qualities,
            }
        )

    return filtered


def normalize_resolution_label(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        matches = re.findall(r"\d+", value)
        if matches:
            return max(int(entry) for entry in matches)
    return 0


def register_stream_proxy(target_url: str, cookie: str = "") -> str:
    token = uuid.uuid4().hex
    base_url = target_url.rsplit("/", 1)[0].rstrip("/") + "/"
    with _cache_lock:
        _cache["stream_tokens"][token] = {
            "timestamp": time.time(),
            "targetUrl": target_url,
            "baseUrl": base_url,
            "cookie": cookie,
        }
    return token


def resolve_stream_proxy(token: str) -> dict[str, str] | None:
    with _cache_lock:
        entry = _cache["stream_tokens"].get(token)
        if not entry:
            return None
        if stale(entry, STREAM_PROXY_TTL_SECONDS):
            del _cache["stream_tokens"][token]
            return None
        entry["timestamp"] = time.time()
        return dict(entry)


def build_dash_manifest_text(contents: str, proxy_base_url: str) -> str:
    base_url_tag = f"<BaseURL>{proxy_base_url}</BaseURL>"
    if DASH_BASE_URL_PATTERN.search(contents):
        return DASH_BASE_URL_PATTERN.sub(base_url_tag, contents)

    period_match = PERIOD_TAG_PATTERN.search(contents)
    if not period_match:
        return contents

    insert_at = period_match.end()
    return f"{contents[:insert_at]}{base_url_tag}{contents[insert_at:]}"


def build_playback_stream_options(
    play_info: dict[str, Any] | None,
    source_name: str,
) -> list[dict[str, Any]]:
    if not isinstance(play_info, dict):
        return []

    options: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for stream in play_info.get("streams") or []:
        play_url = first_url(stream.get("url"))
        if not play_url or play_url in seen_urls:
            continue

        seen_urls.add(play_url)
        stream_format = str(stream.get("format") or "stream").strip().lower()
        stream_type = "dash" if stream_format == "dash" or play_url.lower().endswith(".mpd") else "file"
        resolution = normalize_resolution_label(stream.get("resolutions"))
        token = register_stream_proxy(play_url, str(stream.get("signCookie") or "")) if stream_type == "dash" else ""

        options.append(
            {
                "source": source_name,
                "resourceLink": play_url,
                "downloadUrl": "",
                "resolutions": [resolution] if resolution else [],
                "qualities": [
                    {
                        "label": f"{resolution}P" if resolution else str(stream.get("format") or "Stream"),
                        "resolution": resolution,
                        "playUrl": play_url,
                        "downloadUrl": "",
                        "sourceUrl": play_url,
                        "streamType": stream_type,
                        "manifestUrl": f"/api/dash-manifest?token={token}" if token else "",
                    }
                ],
            }
        )

    return options


async def fetch_movie_playback_options(
    client_session: MovieBoxHttpClient,
    detail: dict[str, Any],
) -> list[dict[str, Any]]:
    if is_series(detail.get("subjectType")):
        return build_resource_options(detail)

    subject_sources: list[tuple[str, str]] = []
    seen_subject_ids: set[str] = set()

    def add_subject(subject_id: Any, label: str) -> None:
        normalized_id = str(subject_id or "").strip()
        if not normalized_id or normalized_id in seen_subject_ids:
            return
        seen_subject_ids.add(normalized_id)
        subject_sources.append((normalized_id, label))

    add_subject(detail.get("subjectId"), "Moviebox Direct")
    for dub in detail.get("dubs") or []:
        dub_label = str(dub.get("lanName") or dub.get("lanCode") or "").strip() or "Alt Audio"
        add_subject(dub.get("subjectId"), f"Moviebox Direct ({dub_label})")

    options: list[dict[str, Any]] = []
    for variant_subject_id, source_name in subject_sources:
        try:
            downloadables = await DownloadableFilesDetail(client_session).get_content(variant_subject_id)
        except Exception:
            downloadables = None

        options.extend(build_named_downloadable_resource_options(downloadables, source_name))

        try:
            play_info = await client_session.get_from_api(
                PLAY_INFO_PATH,
                params={"subjectId": variant_subject_id, "se": 0, "ep": 0},
                include_play_mode=True,
            )
        except Exception:
            play_info = None

        stream_source_name = source_name.replace("Direct", "Stream")
        options.extend(build_playback_stream_options(play_info, stream_source_name))

    options.extend(build_resource_options(detail))
    return options


def copy_cached_playable_movie(subject_id: str) -> dict[str, Any] | None | object:
    with _cache_lock:
        entry = _cache["playable"].get(subject_id)
        if entry and not stale(entry, PLAYABLE_CACHE_TTL_SECONDS):
            value = entry.get("value")
            if isinstance(value, dict):
                return deepcopy(value)
            return None
    return Ellipsis


def store_cached_playable_movie(
    subject_id: str,
    movie: dict[str, Any] | None,
) -> dict[str, Any] | None:
    with _cache_lock:
        _cache["playable"][subject_id] = {
            "timestamp": time.time(),
            "value": deepcopy(movie) if isinstance(movie, dict) else None,
        }
    return deepcopy(movie) if isinstance(movie, dict) else None


async def fetch_direct_playable_movie(
    client_session: MovieBoxHttpClient,
    subject: dict[str, Any],
) -> dict[str, Any] | None:
    subject_id = str(
        subject.get("subjectId")
        or subject.get("subject_id")
        or subject.get("id")
        or ""
    ).strip()
    if not subject_id:
        return None

    cached = copy_cached_playable_movie(subject_id)
    if cached is not Ellipsis:
        return cached

    movie = normalize_subject(subject)
    if movie.get("mediaType") != "movie" or not is_movie_subject(subject):
        return store_cached_playable_movie(subject_id, None)

    try:
        downloadables = await DownloadableFilesDetail(client_session).get_content(subject_id)
    except Exception:
        return store_cached_playable_movie(subject_id, None)

    resource_options = filter_direct_moviebox_options(
        build_downloadable_resource_options(downloadables)
    )
    if not resource_options:
        return store_cached_playable_movie(subject_id, None)

    movie["resourceOptions"] = resource_options
    movie["trailer"] = ""

    best_quality = resource_options[0]["qualities"][0]
    movie["downloadUrl"] = first_url(
        best_quality.get("downloadUrl"),
        best_quality.get("playUrl"),
        movie.get("downloadUrl"),
    )

    return store_cached_playable_movie(subject_id, movie)


async def filter_subjects_to_direct_movies(
    client_session: MovieBoxHttpClient,
    subjects: list[dict[str, Any]],
    limit: int = PAGE_MOVIE_LIMIT,
    probe_timeout_seconds: float | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for subject in subjects:
        if not isinstance(subject, dict):
            continue

        subject_id = str(
            subject.get("subjectId")
            or subject.get("subject_id")
            or subject.get("id")
            or ""
        ).strip()
        if not subject_id or subject_id in seen_ids:
            continue

        seen_ids.add(subject_id)
        candidates.append(subject)

    direct_movies: list[dict[str, Any]] = []

    for index in range(0, len(candidates), PLAYABLE_BATCH_SIZE):
        batch_ids = candidates[index:index + PLAYABLE_BATCH_SIZE]
        batch_results = await asyncio.gather(
            *(
                asyncio.wait_for(
                    fetch_direct_playable_movie(client_session, subject),
                    timeout=probe_timeout_seconds,
                )
                if probe_timeout_seconds
                else fetch_direct_playable_movie(client_session, subject)
                for subject in batch_ids
            ),
            return_exceptions=True,
        )

        for result in batch_results:
            if isinstance(result, dict):
                direct_movies.append(result)
                if len(direct_movies) >= limit:
                    return direct_movies[:limit]

    return direct_movies[:limit]


def select_best_subtitle_match(items: list[Any], movie: dict[str, Any]) -> Any | None:
    target_title_key = make_match_key(movie.get("title"))
    target_year = movie.get("year")
    target_is_series = movie.get("mediaType") == "series"
    ranked_matches: list[tuple[tuple[int, int, int], Any]] = []

    for item in items:
        item_is_series = is_series(getattr(item, "subjectType", None))
        if item_is_series != target_is_series:
            continue

        item_title_key = make_match_key(getattr(item, "title", ""))
        title_exact = int(item_title_key == target_title_key and bool(target_title_key))
        title_contains = int(
            bool(target_title_key)
            and bool(item_title_key)
            and (target_title_key in item_title_key or item_title_key in target_title_key)
        )
        item_year = parse_year(getattr(item, "releaseDate", None))
        year_exact = int(bool(target_year and item_year and target_year == item_year))

        if not title_exact and not (title_contains and year_exact):
            continue

        ranked_matches.append(((title_exact, year_exact, title_contains), item))

    if not ranked_matches:
        return None

    ranked_matches.sort(key=lambda entry: entry[0], reverse=True)
    return ranked_matches[0][1]


async def fetch_subtitle_options(movie: dict[str, Any]) -> list[dict[str, Any]]:
    if movie.get("mediaType") == "series":
        return []

    query = clean_title(movie.get("title"))
    if not query:
        return []

    session = WebSession(timeout=30)
    try:
        search_results = await WebSearch(
            session,
            query,
            subject_type=SubjectType.MOVIES,
        ).get_content_model()
        matched_item = select_best_subtitle_match(search_results.items, movie)
        if matched_item is None:
            return []

        files_detail = await WebDownloadableMovieFilesDetail(session, matched_item).get_content_model()
        options: list[dict[str, Any]] = []
        seen_languages: set[str] = set()

        for caption in files_detail.captions:
            language_code = str(caption.lan or "").strip().lower()
            language_label = str(caption.lanName or caption.lan or "").strip()
            url = str(caption.url)
            if not language_label or not url:
                continue

            dedupe_key = f"{language_code}:{language_label.lower()}"
            if dedupe_key in seen_languages:
                continue
            seen_languages.add(dedupe_key)

            options.append(
                {
                    "code": language_code or "und",
                    "label": language_label,
                    "url": url,
                    "ext": caption.ext,
                    "delay": int(caption.delay or 0),
                }
            )

        return options
    except Exception:
        return []
    finally:
        await session._client.aclose()


def normalize_webvtt_timestamp(match: re.Match[str]) -> str:
    return f"{match.group(1)}.{match.group(2)}"


def srt_to_webvtt(contents: str) -> str:
    normalized = contents.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if normalized.startswith("WEBVTT"):
        return normalized + ("\n" if not normalized.endswith("\n") else "")

    converted = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", normalize_webvtt_timestamp, normalized)
    converted = re.sub(r"(\d{2}:\d{2}),(\d{3})", normalize_webvtt_timestamp, converted)
    return f"WEBVTT\n\n{converted}\n"


def format_episode_code(season: int, episode: int) -> str:
    return f"S{season:02d}E{episode:02d}"


async def fetch_series_items(
    client_session: MovieBoxHttpClient,
    detail: dict[str, Any],
    movie: dict[str, Any],
) -> list[dict[str, Any]]:
    if movie.get("mediaType") != "series":
        return []

    subject_id = str(detail.get("subjectId") or movie.get("id") or "").strip()
    if not subject_id:
        return []

    details = DownloadableFilesDetail(client_session)
    episodes_map: dict[tuple[int, int], dict[str, Any]] = {}

    try:
        async for content in details.get_content_model_all(subject_id):
            for item in content.list:
                season_number = int(item.season or 0)
                episode_number = int(item.episode or 0)
                if season_number <= 0 or episode_number <= 0:
                    continue

                key = (season_number, episode_number)
                resource_link = str(item.resource_link)
                source_url = str(item.source_url)
                resolution = int(item.resolution or 0)
                existing = episodes_map.get(key)

                if existing is None or resolution > existing.get("resolution", 0):
                    title = str(item.title or "").strip()
                    meta_parts = [format_episode_code(season_number, episode_number)]
                    if resolution:
                        meta_parts.append(f"{resolution}p")

                    episodes_map[key] = {
                        "id": f"{movie['id']}-s{season_number}-e{episode_number}",
                        "title": title or format_episode_code(season_number, episode_number),
                        "episodeCode": format_episode_code(season_number, episode_number),
                        "season": season_number,
                        "episode": episode_number,
                        "year": movie.get("year"),
                        "poster": movie.get("poster"),
                        "backdrop": movie.get("backdrop"),
                        "downloadUrl": resource_link,
                        "playUrl": resource_link,
                        "sourceUrl": source_url or resource_link,
                        "resolution": resolution,
                        "meta": " | ".join(meta_parts),
                    }
    except Exception:
        return []

    return [
        episodes_map[key]
        for key in sorted(episodes_map.keys())
    ]


def select_related_movies(movie: dict[str, Any], catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    target_genres = set(movie.get("genres", []))
    related: list[tuple[int, float, dict[str, Any]]] = []
    for candidate in catalog:
        if candidate.get("id") == movie.get("id"):
            continue
        score = len(target_genres.intersection(candidate.get("genres", [])))
        if score == 0:
            continue
        related.append((score, candidate.get("rating", 0), candidate))

    related.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    return [entry[2] for entry in related[:PAGE_MOVIE_LIMIT]]


def neighbor_ids(movie_id: str, catalog: list[dict[str, Any]]) -> tuple[str, str]:
    ids = [movie.get("id") for movie in catalog if movie.get("id")]
    if not ids:
        return "", ""
    try:
        index = ids.index(movie_id)
    except ValueError:
        return ids[0], ids[0]

    return ids[(index - 1) % len(ids)], ids[(index + 1) % len(ids)]


async def fetch_detail_payload(subject_id: str, home: dict[str, Any]) -> dict[str, Any]:
    async with MovieBoxHttpClient(timeout=30) as client_session:
        detail = await ItemDetails(client_session, include_seasons=True).get_content(subject_id)
        movie = normalize_subject(detail)
        if movie.get("mediaType") != "movie" or not is_movie_subject(detail):
            raise LookupError("Only direct-playable full movies are available here.")

        playback_options = await fetch_movie_playback_options(client_session, detail)
        resource_options = prioritize_playable_resource_options(playback_options)
        if not resource_options:
            resource_options = filter_playable_resource_options(playback_options)
        if not resource_options:
            raise LookupError("No playable full-movie source is available for this title.")

        series_items = await fetch_series_items(client_session, detail, movie)

    movie["subtitleOptions"] = await fetch_subtitle_options(movie)
    movie["resourceOptions"] = resource_options
    movie["trailer"] = ""
    if resource_options:
        best_quality = resource_options[0]["qualities"][0]
        movie["downloadUrl"] = best_quality.get("downloadUrl") or movie.get("downloadUrl") or ""
    movie["dubOptions"] = [
        dub.get("lanName") or dub.get("lanCode") or ""
        for dub in detail.get("dubs") or []
        if dub.get("lanName") or dub.get("lanCode")
    ]
    movie["seriesItems"] = series_items
    movie["cast"] = [
        {
            "name": member.get("name") or "",
            "character": member.get("character") or "",
            "avatar": member.get("avatarUrl") or "",
        }
        for member in detail.get("staffList") or []
        if member.get("name")
    ][:8]
    movie["episodeCount"] = len(movie["seriesItems"])

    related = select_related_movies(movie, home.get("catalog", []))
    prev_id, next_id = neighbor_ids(movie["id"], home.get("catalog", []))

    return {
        "movie": movie,
        "related": related,
        "trending": home.get("sections", [{}])[0].get("movies", [])[:PAGE_MOVIE_LIMIT],
        "categories": home.get("categories", []),
        "prevId": prev_id,
        "nextId": next_id,
    }


def resolve_detail(subject_id: str) -> dict[str, Any]:
    with _cache_lock:
        entry = _cache["detail"].get(subject_id)
        if entry and not stale(entry, DETAIL_CACHE_TTL_SECONDS):
            return apply_view_counts_to_payload(deepcopy(entry["value"]))

    home = resolve_home()
    payload = asyncio.run(fetch_detail_payload(subject_id, home))

    with _cache_lock:
        _cache["detail"][subject_id] = {"timestamp": time.time(), "value": payload}

    return apply_view_counts_to_payload(deepcopy(payload))


def proxy_request_headers(cookie_header: str = "") -> dict[str, str]:
    headers: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


async def fetch_search_payload(query: str) -> dict[str, Any]:
    async with MovieBoxHttpClient(timeout=30) as client_session:
        search = Search(
            client_session,
            query,
            subject_type=SubjectType.MOVIES,
            per_page=SEARCH_PAGE_SIZE,
        )
        candidates: list[dict[str, Any]] = []

        async for content in search.get_content_model_all():
            page_items = [
                item.model_dump()
                for item in content.items
            ]
            candidates.extend(page_items)
            if len(dedupe_movies([normalize_subject(item) for item in candidates])) >= PAGE_MOVIE_LIMIT:
                break

        fallback_movies = fallback_catalog_from_subjects(
            candidates,
            limit=PAGE_MOVIE_LIMIT,
        )
        direct_movies = await filter_subjects_to_direct_movies(
            client_session,
            candidates[:SEARCH_PLAYABLE_PROBE_LIMIT],
            limit=SEARCH_PLAYABLE_PROBE_LIMIT,
            probe_timeout_seconds=PLAYABLE_PROBE_TIMEOUT_SECONDS,
        )
        merged_movies = merge_catalogs(
            direct_movies,
            fallback_movies,
            limit=PAGE_MOVIE_LIMIT,
        )

    return {
        "query": query,
        "items": merged_movies[:PAGE_MOVIE_LIMIT],
    }


async def fetch_special_series_category_titles(category_name: str) -> list[dict[str, Any]]:
    queries = SPECIAL_SERIES_CATEGORY_QUERIES.get(category_name, [])
    if not queries:
        return []

    async with MovieBoxHttpClient(timeout=30) as client_session:
        candidates: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for query in queries:
            search = Search(
                client_session,
                query,
                subject_type=SubjectType.TV_SERIES,
                per_page=SEARCH_PAGE_SIZE,
            )

            async for content in search.get_content_model_all():
                for item in content.items:
                    payload = item.model_dump()
                    subject_id = str(
                        payload.get("subjectId")
                        or payload.get("subject_id")
                        or payload.get("id")
                        or ""
                    ).strip()
                    if not subject_id or subject_id in seen_ids:
                        continue
                    seen_ids.add(subject_id)
                    candidates.append(payload)

                if len(candidates) >= PAGE_MOVIE_LIMIT * 3:
                    break

            if len(candidates) >= PAGE_MOVIE_LIMIT * 3:
                break

        direct_titles = await filter_subjects_to_direct_movies(
            client_session,
            candidates,
            limit=PAGE_MOVIE_LIMIT * 2,
        )
        if not direct_titles:
            direct_titles = fallback_catalog_from_subjects(
                candidates,
                limit=PAGE_MOVIE_LIMIT * 2,
            )
        matched_titles = [
            movie
            for movie in direct_titles
            if movie_matches_category(movie, category_name)
        ]
        matched_titles = dedupe_catalog_variants(matched_titles)
        matched_titles.sort(key=lambda movie: movie.get("rating", 0), reverse=True)
        return matched_titles[:PAGE_MOVIE_LIMIT]


def resolve_search(query: str) -> dict[str, Any]:
    key = query.strip().lower()
    with _cache_lock:
        entry = _cache["search"].get(key)
        if entry and not stale(entry, SEARCH_CACHE_TTL_SECONDS):
            return apply_view_counts_to_payload(deepcopy(entry["value"]))

    payload = asyncio.run(fetch_search_payload(query))
    with _cache_lock:
        _cache["search"][key] = {"timestamp": time.time(), "value": payload}
    return apply_view_counts_to_payload(deepcopy(payload))


def resolve_category(category: str) -> dict[str, Any]:
    category_name = (category or "Trending").strip() or "Trending"
    cache_key = category_name.lower()
    with _cache_lock:
        entry = _cache["category"].get(cache_key)
        if entry and not stale(entry, CATEGORY_CACHE_TTL_SECONDS):
            return apply_view_counts_to_payload(deepcopy(entry["value"]))

    home = resolve_home()
    catalog = home.get("catalog", [])

    if category_name.lower() == "trending":
        movies = sorted(catalog, key=lambda movie: movie.get("rating", 0), reverse=True)
    elif category_name in SPECIAL_SERIES_CATEGORY_QUERIES:
        movies = asyncio.run(fetch_special_series_category_titles(category_name))
    else:
        movies = [
            movie
            for movie in catalog
            if movie_matches_category(movie, category_name)
        ]
        movies = sorted(movies, key=lambda movie: movie.get("rating", 0), reverse=True)

    featured = movies[0] if movies else (catalog[0] if catalog else {})
    keep_browsing = [movie for movie in catalog if movie.get("id") != featured.get("id")]

    payload = {
        "requestedCategory": category_name,
        "description": "",
        "featured": featured,
        "movies": movies[:PAGE_MOVIE_LIMIT],
        "keepBrowsing": keep_browsing[:PAGE_MOVIE_LIMIT],
        "categories": home.get("categories", []),
    }
    with _cache_lock:
        _cache["category"][cache_key] = {"timestamp": time.time(), "value": payload}
    return apply_view_counts_to_payload(deepcopy(payload))


class FlixoraHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT_DIR), **kwargs)

    def cors_origin(self) -> str:
        allowed_origins = os.getenv("FLIXORA_ALLOWED_ORIGINS", "*").strip()
        request_origin = self.headers.get("Origin", "").strip()

        if allowed_origins == "*":
            return "*"

        if not request_origin:
            return ""

        allowed = set(split_csv(allowed_origins))
        if request_origin in allowed:
            return request_origin
        return ""

    def end_headers(self) -> None:
        cors_origin = self.cors_origin()
        if cors_origin:
            self.send_header("Access-Control-Allow-Origin", cors_origin)
            if cors_origin != "*":
                self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Accept, Content-Type, Range")
        self.send_header(
            "Access-Control-Expose-Headers",
            "Accept-Ranges, Content-Disposition, Content-Length, Content-Range, ETag, Last-Modified",
        )
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.handle_api(parsed)
            return
        super().do_GET()

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self.respond_json(
                {
                    "ok": True,
                    "service": "flixora",
                    "timestamp": int(time.time()),
                },
                include_body=False,
            )
            return
        super().do_HEAD()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/movie-view":
            params = parse_qs(parsed.query)
            movie_id = params.get("id", [""])[0].strip()
            if not movie_id:
                self.send_error(HTTPStatus.BAD_REQUEST, "Missing movie id")
                return
            self.respond_json({"id": movie_id, "viewCount": increment_view_count(movie_id)})
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown API endpoint")

    def handle_api(self, parsed) -> None:
        params = parse_qs(parsed.query)

        try:
            if parsed.path == "/api/health":
                payload = {
                    "ok": True,
                    "service": "flixora",
                    "timestamp": int(time.time()),
                }
            elif parsed.path == "/api/home":
                payload = resolve_home(force_refresh=params.get("refresh", ["0"])[0] == "1")
            elif parsed.path == "/api/category":
                payload = resolve_category(params.get("category", ["Trending"])[0])
            elif parsed.path == "/api/movie":
                movie_id = params.get("id", [""])[0].strip()
                if not movie_id:
                    self.send_error(HTTPStatus.BAD_REQUEST, "Missing movie id")
                    return
                payload = resolve_detail(movie_id)
            elif parsed.path == "/api/dash-manifest":
                token = params.get("token", [""])[0].strip()
                if not token:
                    self.send_error(HTTPStatus.BAD_REQUEST, "Missing stream token")
                    return
                self.proxy_dash_manifest(token)
                return
            elif parsed.path.startswith("/api/dash/"):
                path_bits = parsed.path[len("/api/dash/"):].split("/", 1)
                if len(path_bits) != 2 or not path_bits[0] or not path_bits[1]:
                    self.send_error(HTTPStatus.BAD_REQUEST, "Invalid DASH asset path")
                    return
                self.proxy_dash_asset(path_bits[0], unquote(path_bits[1]))
                return
            elif parsed.path == "/api/media":
                target_url = params.get("url", [""])[0].strip()
                if not target_url:
                    self.send_error(HTTPStatus.BAD_REQUEST, "Missing media url")
                    return
                self.proxy_media(
                    target_url=target_url,
                    as_attachment=params.get("download", ["0"])[0] == "1",
                    filename=params.get("filename", ["media.mp4"])[0].strip() or "media.mp4",
                )
                return
            elif parsed.path == "/api/subtitle":
                target_url = params.get("url", [""])[0].strip()
                if not target_url:
                    self.send_error(HTTPStatus.BAD_REQUEST, "Missing subtitle url")
                    return
                self.proxy_subtitle(
                    target_url=target_url,
                    language=params.get("lang", ["und"])[0].strip() or "und",
                    label=params.get("label", ["Caption"])[0].strip() or "Caption",
                )
                return
            elif parsed.path == "/api/search":
                query = params.get("q", [""])[0].strip()
                if not query:
                    payload = {"query": "", "items": []}
                else:
                    payload = resolve_search(query)
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown API endpoint")
                return
        except Exception as exc:
            if isinstance(exc, LookupError):
                self.respond_json(
                    {"error": str(exc), "path": parsed.path},
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            print(
                f"[flixora] API error on {parsed.path}: {exc}",
                flush=True,
            )
            print(traceback.format_exc(), flush=True)
            self.respond_json(
                {"error": str(exc), "path": parsed.path},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        self.respond_json(payload)

    def proxy_media(
        self,
        target_url: str,
        as_attachment: bool = False,
        filename: str = "media.mp4",
        cookie_header: str = "",
    ) -> None:
        if not is_direct_media_url(target_url):
            self.send_error(HTTPStatus.BAD_REQUEST, "Only direct media urls can be proxied")
            return

        request_headers = proxy_request_headers(cookie_header)
        range_header = self.headers.get("Range")
        if range_header:
            request_headers["Range"] = range_header

        try:
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                with client.stream("GET", target_url, headers=request_headers) as response:
                    status_code = response.status_code
                    if status_code >= 400:
                        self.send_error(status_code, "Upstream media request failed")
                        return

                    self.send_response(status_code)
                    self.send_header("Content-Type", response.headers.get("Content-Type", "application/octet-stream"))

                    for header_name in ("Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified"):
                        header_value = response.headers.get(header_name)
                        if header_value:
                            self.send_header(header_name, header_value)

                    if as_attachment:
                        safe_name = filename.replace('"', "").replace("\r", "").replace("\n", "")
                        self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')

                    self.end_headers()

                    for chunk in response.iter_bytes():
                        if chunk:
                            self.wfile.write(chunk)
        except (httpx.HTTPError, OSError):
            self.send_error(HTTPStatus.BAD_GATEWAY, "Unable to proxy media from upstream source")

    def proxy_dash_manifest(self, token: str) -> None:
        proxy = resolve_stream_proxy(token)
        if proxy is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown or expired stream token")
            return

        try:
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                response = client.get(
                    proxy["targetUrl"],
                    headers=proxy_request_headers(proxy.get("cookie", "")),
                )
                response.raise_for_status()
        except (httpx.HTTPError, OSError):
            self.send_error(HTTPStatus.BAD_GATEWAY, "Unable to fetch DASH manifest from upstream source")
            return

        rewritten = build_dash_manifest_text(
            response.text,
            f"/api/dash/{token}/",
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/dash+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(rewritten)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(rewritten)

    def proxy_dash_asset(self, token: str, asset_path: str) -> None:
        proxy = resolve_stream_proxy(token)
        if proxy is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown or expired stream token")
            return

        target_url = urljoin(proxy["baseUrl"], asset_path)
        self.proxy_media(
            target_url=target_url,
            filename=Path(asset_path).name or "segment.bin",
            cookie_header=proxy.get("cookie", ""),
        )

    def proxy_subtitle(self, target_url: str, language: str = "und", label: str = "Caption") -> None:
        request_headers: dict[str, str] = {
            **proxy_request_headers(),
            "Accept": "text/plain, */*",
            "Origin": "https://videodownloader.site/",
            "Referer": "https://videodownloader.site/",
        }

        try:
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                response = client.get(target_url, headers=request_headers)
                response.raise_for_status()
        except (httpx.HTTPError, OSError):
            self.send_error(HTTPStatus.BAD_GATEWAY, "Unable to proxy subtitle from upstream source")
            return

        encoding = response.encoding or "utf-8"
        subtitle_text = response.content.decode(encoding, errors="replace")
        webvtt_text = srt_to_webvtt(subtitle_text)
        safe_label = label.replace('"', "").replace("\r", "").replace("\n", "")
        safe_language = re.sub(r"[^a-z0-9-]", "", language.lower()) or "und"
        safe_filename = f"{safe_label or 'caption'}-{safe_language}.vtt"

        body = webvtt_text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/vtt; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'inline; filename="{safe_filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def respond_json(
        self,
        payload: dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
        include_body: bool = True,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if include_body:
            self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve FeemX with Moviebox-backed API endpoints.")
    parser.add_argument("--host", default=resolve_server_host())
    parser.add_argument("--port", default=resolve_server_port(), type=int)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), FlixoraHandler)
    stop_event = threading.Event()
    warm_thread: threading.Thread | None = None
    if background_home_warm_enabled():
        warm_thread = threading.Thread(
            target=background_home_warm_loop,
            args=(stop_event,),
            name="flixora-home-warmer",
            daemon=True,
        )
        warm_thread.start()
        print(
            "[flixora] Background home cache warmer enabled "
            f"(initial delay: {HOME_WARM_INITIAL_DELAY_SECONDS}s, "
            f"interval: {HOME_WARM_INTERVAL_SECONDS}s).",
            flush=True,
        )

    print(f"Serving FeemX at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        if warm_thread and warm_thread.is_alive():
            warm_thread.join(timeout=1)
        server.server_close()


if __name__ == "__main__":
    main()
