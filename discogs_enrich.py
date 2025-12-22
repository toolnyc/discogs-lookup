#!/usr/bin/env python3
"""
Discogs Style Enrichment Script

Walks a music library, cleans up Bandcamp-style titles, and enriches
files with style/genre metadata from Discogs.

Supported formats: MP3, AIFF
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from pathlib import Path
from typing import Iterator

import discogs_client
from mutagen.aiff import AIFF
from mutagen.id3 import ID3, TXXX, TIT2, TPE1, TALB
from mutagen.id3._util import ID3NoHeaderError

# =============================================================================
# Configuration
# =============================================================================

DEFAULT_TOKEN = "dpyrTxeaVUXrfTuGXKauTJTwmaullUZRvqfZDKJP"
SUPPORTED_EXTENSIONS = {".mp3", ".aiff", ".aif"}
USER_AGENT = "DiscogsEnrich/1.0"


class SkipReason(Enum):
    MISSING_ARTIST = "Missing ARTIST tag"
    MISSING_TITLE = "Missing TITLE tag"
    NO_SEARCH_RESULTS = "No Discogs results found"
    SCORE_BELOW_THRESHOLD = "Best match score below threshold"
    NO_STYLE_DATA = "Release has no style/genre data"
    ALREADY_HAS_STYLE = "File already has STYLE tag"
    FILE_READ_ERROR = "Could not read file tags"
    API_ERROR = "Discogs API error"


@dataclass
class Config:
    """Runtime configuration."""
    root_folders: list[str]
    discogs_token: str = DEFAULT_TOKEN
    dry_run: bool = False
    enable_title_cleanup: bool = True
    title_cleanup_threshold: float = 0.90
    min_match_score: int = 100
    style_separator: str = "; "
    request_delay: float = 1.0
    skip_existing_style: bool = True
    log_level: str = "INFO"
    log_file: str | None = None
    limit: int | None = None
    interactive: bool = False
    review_mode: bool = False  # Process files from manual_review.log


@dataclass
class TrackTags:
    """Tags read from an audio file."""
    artist: str | None = None
    title: str | None = None
    album: str | None = None
    year: int | None = None
    discogs_release_id: int | None = None
    style: str | None = None


@dataclass
class MatchResult:
    """Result of Discogs matching."""
    release_id: int
    release_title: str
    artist_name: str
    score: int
    styles: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    matched_track: str | None = None  # Track title that matched from Discogs


@dataclass
class ProcessingResult:
    """Outcome of processing a single file."""
    file_path: str
    title_cleaned: bool = False
    old_title: str | None = None
    new_title: str | None = None
    discogs_matched: bool = False
    match: MatchResult | None = None
    manual_style: bool = False  # Style was entered manually in interactive mode
    skipped: bool = False
    skip_reason: SkipReason | None = None
    error: str | None = None
    pending_review: any = None  # PendingReview data if saved for later review


@dataclass
class PendingReview:
    """File pending manual review."""
    file_path: Path
    tags: any  # TrackTags - use any to avoid forward reference issues
    candidates: list
    parsed_artist: str | None = None
    parsed_title: str | None = None
    parsed_album: str | None = None


@dataclass
class Stats:
    """Processing statistics."""
    files_scanned: int = 0
    files_processed: int = 0
    titles_cleaned: int = 0
    styles_written: int = 0
    manual_styles: int = 0  # Styles entered manually in interactive mode
    used_existing_id: int = 0
    files_skipped: int = 0
    skip_reasons: dict = field(default_factory=dict)
    errors: int = 0
    # Track file paths for summary
    successful_files: list = field(default_factory=list)  # (path, style) tuples
    failed_files: list = field(default_factory=list)  # (path, reason) tuples


# =============================================================================
# String Utilities
# =============================================================================

def strip_accents(s: str) -> str:
    """Remove accents from characters (e.g., Á -> A, ü -> u)."""
    return ''.join(
        c for c in unicodedata.normalize('NFD', s)
        if unicodedata.category(c) != 'Mn'
    )


def normalize_for_comparison(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    s = s.lower()
    s = re.sub(r'[^\w\s]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def fuzzy_ratio(a: str, b: str) -> float:
    """Return similarity ratio between 0.0 and 1.0."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def strip_track_number(title: str) -> str:
    """Remove leading track number from title (e.g., '01 Mind Control' -> 'Mind Control')."""
    # Only match zero-padded track numbers (01-29) to avoid stripping titles like "2 Full Minds"
    # Bandcamp uses zero-padded numbers, so this is safer
    match = re.match(r'^(0[1-9]|1[0-9]|2[0-9])[.\-]?\s+', title)
    if match:
        return title[match.end():]
    return title


def parse_filename(file_path: Path) -> tuple[str | None, str | None, str | None]:
    """
    Parse artist, album, title from filename.
    Supports: "Artist - Title" and "Artist - Album - Title"
    Returns: (artist, album, title)
    """
    stem = file_path.stem  # filename without extension
    parts = [p.strip() for p in stem.split(" - ")]

    if len(parts) == 2:
        return parts[0], None, strip_track_number(parts[1])  # Artist - Title
    elif len(parts) == 3:
        return parts[0], parts[1], strip_track_number(parts[2])  # Artist - Album - Title
    else:
        return None, None, None  # Can't parse


# =============================================================================
# Title Cleanup
# =============================================================================

DASH_PATTERN = re.compile(r'\s*[-–—]\s*')


def cleanup_title(artist: str, title: str, threshold: float = 0.90) -> str | None:
    """
    If title matches "Artist – Title" pattern and left part matches artist,
    return the cleaned title. Returns None if no cleanup needed.
    """
    if not artist or not title:
        return None

    # Find all dash-like split positions
    splits = list(DASH_PATTERN.finditer(title))
    if not splits:
        return None

    norm_artist = normalize_for_comparison(artist)
    best_split = None
    best_similarity = 0.0

    for match in splits:
        left = title[:match.start()].strip()
        right = title[match.end():].strip()

        if not left or not right:
            continue

        similarity = fuzzy_ratio(normalize_for_comparison(left), norm_artist)
        if similarity > best_similarity:
            best_similarity = similarity
            best_split = right

    if best_similarity >= threshold and best_split:
        return best_split

    return None


# =============================================================================
# Tag I/O
# =============================================================================

def read_tags(file_path: Path, logger: logging.Logger) -> TrackTags | None:
    """Read relevant ID3 tags from file."""
    is_aiff = file_path.suffix.lower() in ('.aiff', '.aif')

    try:
        if is_aiff:
            audio = AIFF(file_path)
            tags = audio.tags
            if tags is None:
                return TrackTags()
        else:
            tags = ID3(file_path)
    except ID3NoHeaderError:
        # File has no ID3 tags
        return TrackTags()
    except Exception as e:
        logger.error(f"Could not read tags from {file_path}: {e}")
        return None

    result = TrackTags()

    # Artist (TPE1)
    if "TPE1" in tags:
        result.artist = str(tags["TPE1"])

    # Title (TIT2)
    if "TIT2" in tags:
        result.title = str(tags["TIT2"])

    # Album (TALB)
    if "TALB" in tags:
        result.album = str(tags["TALB"])

    # Year - try TDRC (v2.4) then TYER (v2.3)
    for year_frame in ["TDRC", "TYER"]:
        if year_frame in tags:
            try:
                year_str = str(tags[year_frame])[:4]
                result.year = int(year_str)
                break
            except (ValueError, IndexError):
                pass

    # DISCOGS_RELEASE_ID (TXXX)
    for key in tags:
        if key.startswith("TXXX:"):
            frame = tags[key]
            if frame.desc == "DISCOGS_RELEASE_ID":
                try:
                    result.discogs_release_id = int(frame.text[0])
                except (ValueError, IndexError):
                    pass
            elif frame.desc == "STYLE":
                result.style = frame.text[0] if frame.text else None

    return result


def write_tags(
    file_path: Path,
    style: str | None,
    new_title: str | None,
    discogs_release_id: int | None,
    dry_run: bool,
    logger: logging.Logger,
    new_artist: str | None = None,
    new_album: str | None = None,
) -> bool:
    """Write updated tags to file. Returns True if successful."""
    try:
        is_aiff = file_path.suffix.lower() in ('.aiff', '.aif')

        if is_aiff:
            # AIFF files need special handling via mutagen.aiff.AIFF
            try:
                audio = AIFF(file_path)
                # Add ID3 tag if it doesn't exist
                if audio.tags is None:
                    audio.add_tags()
                tags = audio.tags
            except Exception as e:
                logger.error(f"Failed to open AIFF file {file_path}: {e}")
                return False
        else:
            # MP3 files use ID3 directly
            try:
                tags = ID3(file_path)
            except ID3NoHeaderError:
                tags = ID3()

        if new_artist is not None:
            tags["TPE1"] = TPE1(encoding=3, text=new_artist)
            logger.info(f"  Artist written: {new_artist}")

        if new_album is not None:
            tags["TALB"] = TALB(encoding=3, text=new_album)
            logger.info(f"  Album written: {new_album}")

        if new_title is not None:
            tags["TIT2"] = TIT2(encoding=3, text=new_title)
            logger.info(f"  Title updated: {new_title}")

        if style is not None:
            tags["TXXX:STYLE"] = TXXX(encoding=3, desc="STYLE", text=style)
            logger.info(f"  STYLE written: {style}")

        if discogs_release_id is not None:
            tags["TXXX:DISCOGS_RELEASE_ID"] = TXXX(
                encoding=3, desc="DISCOGS_RELEASE_ID", text=str(discogs_release_id)
            )
            logger.debug(f"  DISCOGS_RELEASE_ID written: {discogs_release_id}")

        if not dry_run:
            if is_aiff:
                audio.save()
            else:
                tags.save(file_path)

        return True

    except Exception as e:
        logger.error(f"Failed to write tags to {file_path}: {e}")
        return False


# =============================================================================
# File Discovery
# =============================================================================

def find_audio_files(root_folders: list[str], logger: logging.Logger) -> Iterator[Path]:
    """Recursively yield all supported audio files under the given folders."""
    for folder in root_folders:
        root_path = Path(folder)
        if not root_path.exists():
            logger.warning(f"Folder does not exist: {folder}")
            continue
        if not root_path.is_dir():
            logger.warning(f"Not a directory: {folder}")
            continue

        for file_path in root_path.rglob("*"):
            if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_EXTENSIONS:
                yield file_path


# =============================================================================
# Discogs Matching
# =============================================================================

def is_various_artists(artist: str | None) -> bool:
    """Check if artist indicates a VA compilation."""
    if not artist:
        return False
    norm = normalize_for_comparison(artist)
    return norm in {"various", "various artists", "va"}


def score_release(
    release,
    filename_parts: list[str],
    logger: logging.Logger
) -> tuple[int, str | None]:
    """
    Score a release by checking if filename parts appear in the release.
    Returns (score, matched_track_title).
    """
    score = 0
    matched_parts = set()
    matched_track = None
    best_partial_ratio = 0.0  # Track partial matches for fallback scoring

    # Get release artist (clean up disambiguation)
    try:
        release_artists = release.artists
        release_artist = release_artists[0].name if release_artists else ""
        release_artist = re.sub(r'\s*\(\d+\)\s*$', '', release_artist)
    except Exception:
        release_artist = ""

    norm_release_artist = normalize_for_comparison(release_artist)
    release_title = getattr(release, 'title', '') or ""
    norm_release_title = normalize_for_comparison(release_title)

    # Check if release artist matches any filename part
    for part in filename_parts:
        norm_part = normalize_for_comparison(part)
        if norm_part and norm_release_artist:
            ratio = fuzzy_ratio(norm_part, norm_release_artist)
            best_partial_ratio = max(best_partial_ratio, ratio)

            # Check for substring containment (e.g., "Hell" in "DJ Hell")
            # Must be a complete word match, not partial (to avoid "atom" matching "atomix")
            is_substring = False
            if norm_release_artist in norm_part:
                # Check if it's a complete word (surrounded by spaces or at boundaries)
                idx = norm_part.find(norm_release_artist)
                before_ok = idx == 0 or norm_part[idx-1] == ' '
                after_ok = idx + len(norm_release_artist) == len(norm_part) or norm_part[idx + len(norm_release_artist)] == ' '
                if before_ok and after_ok:
                    is_substring = True
            elif norm_part in norm_release_artist:
                idx = norm_release_artist.find(norm_part)
                before_ok = idx == 0 or norm_release_artist[idx-1] == ' '
                after_ok = idx + len(norm_part) == len(norm_release_artist) or norm_release_artist[idx + len(norm_part)] == ' '
                if before_ok and after_ok:
                    is_substring = True

            if ratio >= 0.85 or is_substring:
                score += 100
                matched_parts.add(part)
                break
            elif ratio >= 0.6:
                # Partial match - give some points
                score += int(50 * ratio)

    # Check if release title matches any filename part
    for part in filename_parts:
        if part in matched_parts:
            continue
        norm_part = normalize_for_comparison(part)
        if norm_part and norm_release_title:
            ratio = fuzzy_ratio(norm_part, norm_release_title)
            best_partial_ratio = max(best_partial_ratio, ratio)
            if ratio >= 0.85:
                score += 50  # Release title match is worth less than track match
                matched_parts.add(part)
            elif ratio >= 0.6:
                score += int(25 * ratio)

    # Check tracklist for matches
    try:
        tracklist = release.tracklist
        for track in tracklist:
            track_title = getattr(track, 'title', '')
            if track_title:
                norm_track = normalize_for_comparison(track_title)
                # Check if track title matches any filename part
                for part in filename_parts:
                    if part in matched_parts:
                        continue
                    norm_part = normalize_for_comparison(part)
                    if norm_part:
                        ratio = fuzzy_ratio(norm_part, norm_track)
                        best_partial_ratio = max(best_partial_ratio, ratio)
                        if ratio >= 0.85:
                            score += 100
                            matched_parts.add(part)
                            matched_track = track_title
                            break
                        elif ratio >= 0.6:
                            partial_score = int(50 * ratio)
                            score += partial_score
                            if not matched_track:
                                matched_track = track_title

            # Check track artists (remixers, etc.)
            track_artists = getattr(track, 'artists', None)
            if track_artists:
                for track_artist in track_artists:
                    artist_name = getattr(track_artist, 'name', '')
                    clean_artist = re.sub(r'\s*\(\d+\)\s*$', '', artist_name)
                    if clean_artist:
                        norm_track_artist = normalize_for_comparison(clean_artist)
                        for part in filename_parts:
                            if part in matched_parts:
                                continue
                            norm_part = normalize_for_comparison(part)
                            if norm_part:
                                ratio = fuzzy_ratio(norm_part, norm_track_artist)
                                best_partial_ratio = max(best_partial_ratio, ratio)
                                if ratio >= 0.85:
                                    score += 100
                                    matched_parts.add(part)
                                    if not matched_track:
                                        matched_track = track_title
                                    break
                                elif ratio >= 0.6:
                                    partial_score = int(50 * ratio)
                                    score += partial_score
                                    if not matched_track:
                                        matched_track = track_title

            # Also check extraartists (remixers often listed here)
            extra_artists = getattr(track, 'extraartists', None)
            if extra_artists:
                for extra_artist in extra_artists:
                    artist_name = getattr(extra_artist, 'name', '')
                    clean_artist = re.sub(r'\s*\(\d+\)\s*$', '', artist_name)
                    if clean_artist:
                        norm_extra_artist = normalize_for_comparison(clean_artist)
                        for part in filename_parts:
                            if part in matched_parts:
                                continue
                            norm_part = normalize_for_comparison(part)
                            if norm_part:
                                ratio = fuzzy_ratio(norm_part, norm_extra_artist)
                                best_partial_ratio = max(best_partial_ratio, ratio)
                                if ratio >= 0.80:
                                    score += 75
                                    matched_parts.add(part)
                                    if not matched_track:
                                        matched_track = track_title
                                    break

    except Exception as e:
        logger.debug(f"Could not check tracklist: {e}")

    # Check release-level extra artists (producers, remixers)
    try:
        extra_artists = getattr(release, 'extraartists', None)
        if extra_artists:
            for extra_artist in extra_artists:
                artist_name = getattr(extra_artist, 'name', '')
                clean_artist = re.sub(r'\s*\(\d+\)\s*$', '', artist_name)
                if clean_artist:
                    norm_extra_artist = normalize_for_comparison(clean_artist)
                    for part in filename_parts:
                        if part in matched_parts:
                            continue
                        norm_part = normalize_for_comparison(part)
                        if norm_part:
                            ratio = fuzzy_ratio(norm_part, norm_extra_artist)
                            if ratio >= 0.80:
                                score += 50
                                matched_parts.add(part)
                                break
    except Exception as e:
        logger.debug(f"Could not check release extra artists: {e}")

    return score, matched_track


def search_discogs(
    client: discogs_client.Client,
    query: str,
    logger: logging.Logger
) -> list:
    """Search Discogs. Query should already be normalized (lowercase, no accents)."""
    try:
        results = client.search(query, type='release')
        return list(results.page(1))
    except Exception as e:
        logger.error(f"Discogs search error: {e}")
        return []


def is_catalog_number(s: str) -> bool:
    """Check if string looks like a catalog number (e.g., GYST009, SV68, CH001)."""
    # Catalog numbers are short alphanumeric codes: 2-4 letters + 2-4 numbers
    # e.g., CH001, SV68, DOLLY15, but NOT JSPRV35 (too many letters)
    if ' ' in s:
        return False
    # Pattern: 2-4 letters followed by 2-4 numbers
    if re.match(r'^[A-Za-z]{2,4}\d{2,4}$', s):
        return True
    # Or: 2-4 numbers followed by 2-4 letters (003EP)
    if re.match(r'^\d{2,4}[A-Za-z]{2,4}$', s):
        return True
    return False


def has_track_number_prefix(s: str) -> bool:
    """Check if string starts with a track number like '01 ', '05 ', '12 '."""
    return bool(re.match(r'^0?[1-9]\d?\s+', s))


def parse_filename_for_search(filename_stem: str) -> dict:
    """
    Parse filename into search parts and track parts using track number as delimiter.

    Examples:
    - "DJ Deep - CH001 - DJ DEEP - VAINCRE - 01 Fluorescent"
      → search: ["DJ Deep", "VAINCRE"], track: ["Fluorescent"]
    - "dolly - dolly 15YRS - 05 Juri Heidemann - Post Pre"
      → search: ["dolly", "dolly 15YRS"], track: ["Juri Heidemann", "Post Pre"]
    - "Efdemin - Decay - 06 Subatomic"
      → search: ["Efdemin", "Decay"], track: ["Subatomic"]

    Returns dict with:
      - search_parts: parts to use for Discogs search (artist/album, catalog numbers filtered)
      - track_parts: parts that identify the track (for validation)
      - all_parts: all parts with track numbers stripped (for scoring)
    """
    parts = [p.strip() for p in filename_stem.split(" - ") if p.strip()]

    # Find first part with leading track number (01-99)
    track_number_idx = -1
    for i, part in enumerate(parts):
        if has_track_number_prefix(part):
            track_number_idx = i
            break

    if track_number_idx == -1:
        # No track number found - use first 2 parts for search
        # First part is always artist, second is usually album/title
        # Skip catalog numbers (except position 0) and duplicates
        search_parts = []
        seen_normalized = set()
        for i, p in enumerate(parts[:4]):  # Check first 4 parts max
            p_norm = p.lower().strip()
            if i == 0:
                search_parts.append(p)
                seen_normalized.add(p_norm)
            elif not is_catalog_number(p) and p_norm not in seen_normalized:
                search_parts.append(p)
                seen_normalized.add(p_norm)
            if len(search_parts) >= 2:
                break
        if not search_parts and parts:
            search_parts = parts[:1]
        return {
            'search_parts': search_parts,
            'track_parts': parts[1:] if len(parts) > 1 else [],
            'all_parts': [strip_track_number(p) for p in parts]
        }

    # Parts before track number = search material (artist/album)
    # Take first 2 meaningful parts: artist (always first) + album (skip catalogs and duplicates)
    before_track = parts[:track_number_idx]
    search_parts = []
    seen_normalized = set()

    for i, p in enumerate(before_track):
        p_norm = p.lower().strip()
        if i == 0:
            # First part is always artist - keep it
            search_parts.append(p)
            seen_normalized.add(p_norm)
        elif not is_catalog_number(p) and p_norm not in seen_normalized and len(search_parts) < 2:
            # Non-catalog, non-duplicate part, and we need more search terms
            search_parts.append(p)
            seen_normalized.add(p_norm)

    # If we only got catalog numbers after artist, just use artist
    if not search_parts and before_track:
        search_parts = [before_track[0]]

    # Parts from track number onward = track info (strip the numbers)
    track_parts = [strip_track_number(p) for p in parts[track_number_idx:]]

    return {
        'search_parts': search_parts,
        'track_parts': track_parts,
        'all_parts': [strip_track_number(p) for p in parts]
    }


def search_discogs_with_fallbacks(
    client: discogs_client.Client,
    query: str,
    logger: logging.Logger,
    request_delay: float
) -> list:
    """
    Search Discogs. Query should already be normalized (lowercase, no accents).
    Format: "artist title" - already built from first two filename parts.
    """
    # Primary search with the normalized artist+title query
    results = search_discogs(client, query, logger)
    if results:
        return results

    # Fallback: try just the first word (artist) if query has multiple words
    # This helps when title is misspelled or obscure
    words = query.split()
    if len(words) >= 2:
        time.sleep(request_delay)
        # Try artist + first word of title
        fallback_query = f"{words[0]} {words[1]}"
        if fallback_query != query:
            logger.debug(f"  Fallback search: {fallback_query}")
            results = search_discogs(client, fallback_query, logger)
            if results:
                return results

    return []


def fetch_release_by_id(
    client: discogs_client.Client,
    release_id: int,
    logger: logging.Logger
) -> MatchResult | None:
    """Fetch a release directly by ID."""
    try:
        release = client.release(release_id)

        # Access data to trigger fetch
        styles = list(release.styles) if release.styles else []
        genres = list(release.genres) if release.genres else []

        artists = release.artists
        artist_name = artists[0].name if artists else "Unknown"

        return MatchResult(
            release_id=release_id,
            release_title=release.title or "",
            artist_name=artist_name,
            score=999,  # Direct ID lookup, no scoring needed
            styles=styles,
            genres=genres
        )
    except Exception as e:
        logger.error(f"Failed to fetch release {release_id}: {e}")
        return None


def find_best_match(
    client: discogs_client.Client,
    tags: TrackTags,
    min_score: int,
    cache: dict,
    request_delay: float,
    logger: logging.Logger,
    full_filename: str | None = None,
    return_candidates: bool = False
) -> MatchResult | None | tuple[MatchResult | None, list[MatchResult]]:
    """
    Search Discogs and return best matching release.

    If return_candidates=True, returns (best_match, all_candidates) tuple
    where all_candidates includes even low-scoring results for manual review.
    """

    # Build cache key - use filename for raw filename searches to avoid collisions
    if full_filename and not tags.artist and not tags.title:
        cache_key = ("__raw__", normalize_for_comparison(full_filename), "")
    else:
        cache_key = (
            normalize_for_comparison(tags.artist or ""),
            normalize_for_comparison(tags.title or ""),
            normalize_for_comparison(tags.album or "")
        )

    if cache_key in cache:
        cached = cache[cache_key]
        if cached is None:
            logger.debug("Cache hit: no match")
            return (None, []) if return_candidates else None
        logger.debug(f"Cache hit: release {cached.release_id}")
        return (cached, [cached]) if return_candidates else cached

    # Parse filename to extract search parts (artist/album) and track parts
    raw_query = full_filename or f"{tags.artist} - {tags.title}"
    parsed = parse_filename_for_search(raw_query)

    # Build search query from artist/album parts (before track number)
    # Join with space and normalize
    search_query = " ".join(parsed['search_parts'])
    search_query = strip_accents(search_query).lower()

    if not search_query.strip():
        logger.warning(f"  Could not extract search terms from: {raw_query}")
        return (None, []) if return_candidates else None

    logger.info(f"  Searching: {search_query}")
    if parsed['track_parts']:
        logger.debug(f"  Track parts: {parsed['track_parts']}")

    results = search_discogs_with_fallbacks(client, search_query, logger, request_delay)

    if not results:
        cache[cache_key] = None
        logger.info(f"  No Discogs results for: {search_query}")
        return (None, []) if return_candidates else None

    # Use all parts for scoring
    filename_parts = parsed['all_parts']

    # For single-part filenames, use lower threshold
    is_unparsed_filename = len(filename_parts) == 1
    effective_min_score = min_score // 2 if is_unparsed_filename else min_score

    if is_unparsed_filename:
        logger.debug(f"  Single-part filename, using lower score threshold: {effective_min_score}")

    # Score each result and collect candidates
    # When return_candidates=True, collect ALL results for manual review
    candidates = []
    all_candidates = []

    for release in results:
        try:
            score, matched_track = score_release(release, filename_parts, logger)

            # Extract styles/genres
            styles = list(release.styles) if release.styles else []
            genres = list(release.genres) if release.genres else []

            artists = release.artists
            artist_name = artists[0].name if artists else "Unknown"
            # Clean up artist name
            artist_name = re.sub(r'\s*\(\d+\)\s*$', '', artist_name)

            match_result = MatchResult(
                release_id=release.id,
                release_title=release.title or "",
                artist_name=artist_name,
                score=score,
                styles=styles,
                genres=genres,
                matched_track=matched_track
            )

            # Always collect for all_candidates (for interactive mode)
            if return_candidates and score > 0:
                all_candidates.append(match_result)

            # Only add to candidates if above threshold
            if score >= effective_min_score:
                candidates.append(match_result)

            # Small delay between processing results that require API calls
            time.sleep(request_delay * 0.1)

        except Exception as e:
            logger.debug(f"Error scoring release: {e}")
            continue

    # Sort by score descending and try each until one passes validation
    candidates.sort(key=lambda m: m.score, reverse=True)
    all_candidates.sort(key=lambda m: m.score, reverse=True)

    for match in candidates:
        if validate_match_against_filename(match, filename_parts, search_query, logger):
            cache[cache_key] = match
            logger.info(f"  Matched: {match.artist_name} - {match.release_title} (score: {match.score})")
            return (match, all_candidates) if return_candidates else match

    # No valid matches found
    cache[cache_key] = None
    if candidates:
        logger.info(f"  All {len(candidates)} candidates failed validation")

    return (None, all_candidates) if return_candidates else None


def validate_match_against_filename(
    match: MatchResult,
    filename_parts: list[str],
    full_filename: str,
    logger: logging.Logger
) -> bool:
    """
    Validate that a match makes sense for the given filename.
    Each significant filename part should appear SOMEWHERE in the match
    (artist, release title, or track title). We don't assume which part is which.
    """
    # Build a combined string of all match info to check against
    match_texts = []
    norm_artist = normalize_for_comparison(match.artist_name)
    if norm_artist and norm_artist not in ("various", "various artists", "va"):
        match_texts.append(norm_artist)
    match_texts.append(normalize_for_comparison(match.release_title))
    if match.matched_track:
        match_texts.append(normalize_for_comparison(match.matched_track))

    match_combined = " ".join(match_texts)
    logger.debug(f"  Validating against: {match_texts}")

    # Check each filename part - each should appear somewhere in the match
    unmatched_parts = []
    for part in filename_parts:
        norm_part = normalize_for_comparison(part)
        if len(norm_part) < 3:  # Skip very short parts
            continue

        # Check if this part appears in any match text
        part_matched = False
        best_ratio = 0.0

        for match_text in match_texts:
            # Check substring (must be complete word, not partial like "atom" in "atomix")
            if norm_part in match_text:
                idx = match_text.find(norm_part)
                before_ok = idx == 0 or match_text[idx-1] == ' '
                after_ok = idx + len(norm_part) == len(match_text) or match_text[idx + len(norm_part)] == ' '
                if before_ok and after_ok:
                    part_matched = True
                    break
            elif match_text in norm_part:
                idx = norm_part.find(match_text)
                before_ok = idx == 0 or norm_part[idx-1] == ' '
                after_ok = idx + len(match_text) == len(norm_part) or norm_part[idx + len(match_text)] == ' '
                if before_ok and after_ok:
                    part_matched = True
                    break

            # Check fuzzy match (0.7 threshold for word reordering cases)
            ratio = fuzzy_ratio(norm_part, match_text)
            best_ratio = max(best_ratio, ratio)
            if ratio >= 0.7:
                part_matched = True
                break

        if not part_matched:
            unmatched_parts.append((part, best_ratio))

    # Allow 1 unmatched part if we have multiple parts (album name may be formatted differently)
    # e.g., "Seventeen Four Zero" on filename vs "1740" on Discogs
    total_parts = len([p for p in filename_parts if len(normalize_for_comparison(p)) >= 3])
    max_unmatched = 1 if total_parts >= 2 else 0

    if len(unmatched_parts) > max_unmatched:
        parts_str = ", ".join(f"'{p}' ({r:.2f})" for p, r in unmatched_parts)
        logger.info(f"  Rejected: filename parts not found in match: {parts_str}")
        return False

    if unmatched_parts:
        parts_str = ", ".join(f"'{p}'" for p, _ in unmatched_parts)
        logger.debug(f"  Allowing unmatched part (1 of {total_parts}): {parts_str}")

    return True


# =============================================================================
# Main Processing
# =============================================================================

def process_file(
    file_path: Path,
    client: discogs_client.Client,
    config: Config,
    cache: dict,
    logger: logging.Logger,
    collect_for_review: bool = False
) -> ProcessingResult:
    """Process a single audio file."""
    result = ProcessingResult(file_path=str(file_path))

    # Read tags
    tags = read_tags(file_path, logger)
    if tags is None:
        result.skipped = True
        result.skip_reason = SkipReason.FILE_READ_ERROR
        return result

    # Parse filename to fill in missing tags
    parsed_artist = None
    parsed_album = None
    parsed_title = None
    use_raw_filename_search = False

    if not tags.artist or not tags.title:
        fn_artist, fn_album, fn_title = parse_filename(file_path)
        if fn_artist and fn_title:
            if not tags.artist:
                tags.artist = fn_artist
                parsed_artist = fn_artist
                logger.info(f"  Parsed artist from filename: {fn_artist}")
            if not tags.title:
                tags.title = fn_title
                parsed_title = fn_title
                logger.info(f"  Parsed title from filename: {fn_title}")
            if fn_album and not tags.album:
                tags.album = fn_album
                parsed_album = fn_album
                logger.info(f"  Parsed album from filename: {fn_album}")
        elif not tags.title:
            # Can't parse filename but still have the raw filename - use it for search
            use_raw_filename_search = True
            logger.info(f"  Using raw filename for Discogs search: {file_path.stem}")

    # Strip track number from title (whether from tag or filename)
    if tags.title:
        stripped_title = strip_track_number(tags.title)
        if stripped_title != tags.title:
            logger.info(f"  Stripped track number: \"{tags.title}\" -> \"{stripped_title}\"")
            if parsed_title is None:
                # Title came from existing tag, mark it for write-back
                parsed_title = stripped_title
            tags.title = stripped_title

    # Skip if already has style (optional)
    if config.skip_existing_style and tags.style:
        result.skipped = True
        result.skip_reason = SkipReason.ALREADY_HAS_STYLE
        return result

    # Title cleanup
    new_title = None
    if config.enable_title_cleanup and tags.artist and tags.title:
        cleaned = cleanup_title(tags.artist, tags.title, config.title_cleanup_threshold)
        if cleaned:
            result.title_cleaned = True
            result.old_title = tags.title
            result.new_title = cleaned
            new_title = cleaned
            logger.info(f"  Title cleanup: \"{tags.title}\" -> \"{cleaned}\"")

    # Discogs lookup
    match = None
    candidates = []
    used_existing_id = False
    manual_style = None  # For interactive mode manual entry

    if tags.discogs_release_id:
        logger.debug(f"  Using existing DISCOGS_RELEASE_ID: {tags.discogs_release_id}")
        match = fetch_release_by_id(client, tags.discogs_release_id, logger)
        used_existing_id = True
        time.sleep(config.request_delay)
    else:
        # Always search Discogs using filename - we can match even without proper tags
        # Get all candidates if we might need them for review
        if config.interactive or collect_for_review:
            match, candidates = find_best_match(
                client, tags, config.min_match_score, cache, config.request_delay, logger,
                full_filename=file_path.stem,
                return_candidates=True
            )
        else:
            match = find_best_match(
                client, tags, config.min_match_score, cache, config.request_delay, logger,
                full_filename=file_path.stem
            )
        time.sleep(config.request_delay)

    # If no confident match and collect_for_review is enabled, save for later
    if not match and collect_for_review:
        result.pending_review = PendingReview(
            file_path=file_path,
            tags=tags,
            candidates=candidates,
            parsed_artist=parsed_artist,
            parsed_title=parsed_title,
            parsed_album=parsed_album
        )
        result.skipped = True
        result.skip_reason = SkipReason.NO_SEARCH_RESULTS
        return result

    if not match and not manual_style:
        result.skipped = True
        result.skip_reason = SkipReason.NO_SEARCH_RESULTS
        return result

    result.discogs_matched = True if match else False
    result.match = match

    # Extract style string (prefer styles over genres, or use manual entry)
    if manual_style:
        styles = manual_style.split("; ") if "; " in manual_style else [manual_style]
    elif match:
        styles = match.styles if match.styles else match.genres
    else:
        styles = []

    if not styles:
        result.skipped = True
        result.skip_reason = SkipReason.NO_STYLE_DATA
        return result

    style_str = config.style_separator.join(styles)

    # Write tags
    # Determine what metadata to write based on match quality
    if match:
        discogs_id_to_write = None if used_existing_id else match.release_id

        # Use Discogs artist/track when we have a good match, otherwise fall back to parsed values
        if match.score >= 100:
            # Trust Discogs for artist and track title
            artist_to_write = match.artist_name
            title_to_write = match.matched_track or parsed_title or tags.title
            album_to_write = match.release_title if match.release_title else parsed_album
        elif use_raw_filename_search:
            # We matched using raw filename search - use Discogs data since we have no parsed tags
            artist_to_write = match.artist_name
            title_to_write = match.matched_track
            album_to_write = match.release_title
            logger.info(f"  Using Discogs metadata (raw filename match)")
        else:
            # Fall back to parsed values
            artist_to_write = parsed_artist
            title_to_write = new_title if new_title else parsed_title
            album_to_write = parsed_album
    else:
        # Manual style entry without Discogs match - only write style, keep existing metadata
        discogs_id_to_write = None
        artist_to_write = parsed_artist
        title_to_write = new_title if new_title else parsed_title
        album_to_write = parsed_album

    success = write_tags(
        file_path,
        style=style_str,
        new_title=title_to_write,
        discogs_release_id=discogs_id_to_write,
        dry_run=config.dry_run,
        logger=logger,
        new_artist=artist_to_write,
        new_album=album_to_write,
    )

    if not success:
        result.error = "Failed to write tags"

    return result


def process_library(
    config: Config,
    client: discogs_client.Client,
    logger: logging.Logger
) -> Stats:
    """Process all files in configured folders."""
    stats = Stats()
    cache: dict = {}
    pending_review: list[PendingReview] = []

    # Collect all files first
    all_files = list(find_audio_files(config.root_folders, logger))

    # Shuffle when using limit so we test different files each run
    if config.limit:
        random.shuffle(all_files)
        logger.info(f"Shuffled {len(all_files)} files, processing first {config.limit}")

    # Phase 1: Autonomous processing
    logger.info("Phase 1: Autonomous matching...")
    for file_path in all_files:
        # Check limit
        if config.limit and stats.files_scanned >= config.limit:
            logger.info(f"Reached limit of {config.limit} files")
            break

        stats.files_scanned += 1

        logger.info(f"Processing: {file_path}")

        try:
            result = process_file(file_path, client, config, cache, logger,
                                  collect_for_review=config.interactive)

            if result.error:
                stats.errors += 1
                stats.failed_files.append((str(file_path), f"Error: {result.error}"))
            elif result.skipped:
                # Check if this should be saved for manual review
                if (config.interactive and
                    result.skip_reason == SkipReason.NO_SEARCH_RESULTS and
                    hasattr(result, 'pending_review') and result.pending_review):
                    pending_review.append(result.pending_review)
                    logger.info(f"  Saved for manual review")
                else:
                    stats.files_skipped += 1
                    reason = result.skip_reason.value if result.skip_reason else "Unknown"
                    stats.skip_reasons[reason] = stats.skip_reasons.get(reason, 0) + 1
                    logger.info(f"  Skipped: {reason}")
                    # Track failures that need manual review
                    if result.skip_reason in (SkipReason.NO_SEARCH_RESULTS, SkipReason.NO_MATCHING_RELEASE):
                        stats.failed_files.append((str(file_path), reason))
            else:
                stats.files_processed += 1
                if result.title_cleaned:
                    stats.titles_cleaned += 1
                if result.discogs_matched or result.manual_style:
                    stats.styles_written += 1
                    # Track successful writes with style info
                    style = result.match.styles if result.match else "Manual"
                    stats.successful_files.append((str(file_path), style))
                if result.manual_style:
                    stats.manual_styles += 1
                if result.match and result.match.score == 999:
                    stats.used_existing_id += 1

        except KeyboardInterrupt:
            logger.info("\nInterrupted by user")
            break
        except Exception as e:
            stats.errors += 1
            stats.failed_files.append((str(file_path), f"Error: {e}"))
            logger.error(f"  Error: {e}")

    # Phase 2: Manual review (if any pending and interactive mode)
    if pending_review and config.interactive:
        logger.info(f"\n{'=' * 50}")
        logger.info(f"Phase 2: Manual review ({len(pending_review)} files)")
        logger.info(f"{'=' * 50}")
        logger.info("Press Enter when ready to begin manual review (or Ctrl+C to skip)...")

        try:
            input()
            stats = process_pending_reviews(pending_review, client, config, cache, logger, stats)
        except KeyboardInterrupt:
            logger.info(f"\nSkipped manual review. {len(pending_review)} files remain untagged.")
            stats.files_skipped += len(pending_review)
            stats.skip_reasons["Skipped manual review"] = len(pending_review)

    return stats


def process_pending_reviews(
    pending_review: list[PendingReview],
    client: discogs_client.Client,
    config: Config,
    cache: dict,
    logger: logging.Logger,
    stats: Stats
) -> Stats:
    """Process files that need manual review."""
    from discogs_enrich_interactive import interactive_review

    for i, pending in enumerate(pending_review, 1):
        logger.info(f"\n[{i}/{len(pending_review)}] {pending.file_path.name}")

        tags_dict = {
            "artist": pending.tags.artist or pending.parsed_artist or "Unknown",
            "title": pending.tags.title or pending.parsed_title or "Unknown",
            "album": pending.tags.album or pending.parsed_album or "-"
        }

        candidates_for_ui = [
            {
                "artist": c.artist_name,
                "release": c.release_title,
                "styles": c.styles if c.styles else c.genres,
                "score": c.score,
                "release_id": c.release_id
            }
            for c in pending.candidates[:10]
        ]

        best_match_info = None
        if pending.candidates:
            best = pending.candidates[0]
            best_match_info = {
                "artist": best.artist_name,
                "release": best.release_title,
                "score": best.score,
                "threshold": config.min_match_score,
                "styles": best.styles if best.styles else best.genres
            }

        try:
            user_style, user_release_id = interactive_review(
                pending.file_path, tags_dict, candidates_for_ui, best_match_info
            )

            if user_style:
                # Write the selected style
                match = None
                if user_release_id:
                    for c in pending.candidates:
                        if c.release_id == user_release_id:
                            match = c
                            break

                if match:
                    artist_to_write = match.artist_name
                    title_to_write = match.matched_track or pending.parsed_title
                    album_to_write = match.release_title
                    discogs_id = match.release_id
                else:
                    artist_to_write = pending.parsed_artist
                    title_to_write = pending.parsed_title
                    album_to_write = pending.parsed_album
                    discogs_id = None

                success = write_tags(
                    pending.file_path,
                    style=user_style,
                    new_title=title_to_write,
                    discogs_release_id=discogs_id,
                    dry_run=config.dry_run,
                    logger=logger,
                    new_artist=artist_to_write,
                    new_album=album_to_write,
                )

                if success:
                    stats.files_processed += 1
                    stats.styles_written += 1
                    if not user_release_id:
                        stats.manual_styles += 1
                    logger.info(f"  Style written: {user_style}")
                else:
                    stats.errors += 1
            else:
                stats.files_skipped += 1
                stats.skip_reasons["User skipped"] = stats.skip_reasons.get("User skipped", 0) + 1
                logger.info(f"  Skipped by user")

        except KeyboardInterrupt:
            remaining = len(pending_review) - i
            logger.info(f"\nManual review interrupted. {remaining} files remain.")
            stats.files_skipped += remaining
            stats.skip_reasons["Review interrupted"] = remaining
            break

    return stats


def print_summary(stats: Stats, config: Config, logger: logging.Logger):
    """Print processing summary and write failures to log file."""
    dry_run_note = " (DRY RUN)" if config.dry_run else ""

    summary = f"""
{'=' * 50}
Discogs Enrichment Complete{dry_run_note}
{'=' * 50}
Files scanned:      {stats.files_scanned:>6}
Files processed:    {stats.files_processed:>6}
  - Titles cleaned: {stats.titles_cleaned:>6}
  - Styles written: {stats.styles_written:>6}
  - Manual styles:  {stats.manual_styles:>6}
  - Used cached ID: {stats.used_existing_id:>6}
Files skipped:      {stats.files_skipped:>6}"""

    for reason, count in sorted(stats.skip_reasons.items()):
        summary += f"\n  - {reason}: {count}"

    summary += f"""
Errors:             {stats.errors:>6}
{'=' * 50}"""

    logger.info(summary)

    # Print successful writes
    if stats.successful_files:
        logger.info(f"\n{'=' * 50}")
        logger.info(f"SUCCESSFUL STYLE WRITES ({len(stats.successful_files)} files)")
        logger.info(f"{'=' * 50}")
        for file_path, style in stats.successful_files:
            filename = Path(file_path).name
            logger.info(f"  {filename}")
            logger.info(f"    -> {style}")

    # Print and save failures
    if stats.failed_files:
        logger.info(f"\n{'=' * 50}")
        logger.info(f"FILES NEEDING MANUAL REVIEW ({len(stats.failed_files)} files)")
        logger.info(f"{'=' * 50}")
        for file_path, reason in stats.failed_files:
            filename = Path(file_path).name
            logger.info(f"  {filename}")
            logger.info(f"    Reason: {reason}")

        # Write failures to log file for later review
        log_file = Path("manual_review.log")
        if not config.dry_run:
            with open(log_file, "w") as f:
                f.write(f"# Discogs Enrichment - Files Needing Manual Review\n")
                f.write(f"# Generated: {Path(__file__).name}\n")
                f.write(f"# Run with: ./run.sh --review\n\n")
                for file_path, reason in stats.failed_files:
                    f.write(f"{file_path}\n")
            logger.info(f"\nFailed files saved to: {log_file}")
            logger.info(f"Run './run.sh --review' to process these interactively")


# =============================================================================
# Configuration Loading
# =============================================================================

def load_config_file(path: str) -> dict:
    """Load YAML config file."""
    try:
        import yaml
        with open(path, 'r') as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        print("Warning: PyYAML not installed, cannot load config file", file=sys.stderr)
        return {}
    except Exception as e:
        print(f"Warning: Could not load config file {path}: {e}", file=sys.stderr)
        return {}


def load_config(args: argparse.Namespace) -> Config:
    """Merge CLI args, config file, and env vars into Config."""

    # Start with defaults
    config_dict = {
        "root_folders": [],
        "discogs_token": DEFAULT_TOKEN,
        "dry_run": False,
        "enable_title_cleanup": True,
        "title_cleanup_threshold": 0.90,
        "min_match_score": 100,
        "style_separator": "; ",
        "request_delay": 1.0,
        "skip_existing_style": True,
        "log_level": "INFO",
        "log_file": None,
        "limit": None,
        "interactive": False,
        "review_mode": False,
    }

    # Load config file if specified
    if args.config:
        file_config = load_config_file(args.config)
        for key, value in file_config.items():
            if key in config_dict and value is not None:
                config_dict[key] = value

    # Environment variables
    env_token = os.environ.get("DISCOGS_TOKEN")
    if env_token:
        config_dict["discogs_token"] = env_token

    # CLI args override everything
    if args.root_folders:
        config_dict["root_folders"] = args.root_folders
    if args.token:
        config_dict["discogs_token"] = args.token
    if args.dry_run:
        config_dict["dry_run"] = True
    if args.no_title_cleanup:
        config_dict["enable_title_cleanup"] = False
    if args.verbose:
        config_dict["log_level"] = "DEBUG"
    if args.force:
        config_dict["skip_existing_style"] = False
    if args.limit:
        config_dict["limit"] = args.limit
    if args.interactive:
        config_dict["interactive"] = True
    if args.review:
        config_dict["review_mode"] = True
        config_dict["interactive"] = True  # Review mode implies interactive

    return Config(**config_dict)


def setup_logging(config: Config) -> logging.Logger:
    """Configure logging."""
    logger = logging.getLogger("discogs_enrich")
    logger.setLevel(getattr(logging, config.log_level.upper()))

    formatter = logging.Formatter("%(levelname)s: %(message)s")

    # Console handler
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    # File handler (optional)
    if config.log_file:
        file_handler = logging.FileHandler(config.log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Enrich music library with Discogs style metadata",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s /path/to/music
  %(prog)s /path/to/music --dry-run
  %(prog)s /path/to/music --no-title-cleanup
  %(prog)s /path/to/music --force  # Re-process files with existing STYLE
"""
    )

    parser.add_argument(
        "root_folders",
        nargs="*",
        help="Root folder(s) to scan for audio files"
    )
    parser.add_argument(
        "--review",
        action="store_true",
        help="Process files from manual_review.log interactively"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be done without writing tags"
    )
    parser.add_argument(
        "--no-title-cleanup",
        action="store_true",
        help="Skip the title cleanup step"
    )
    parser.add_argument(
        "--token",
        help="Discogs personal access token (or set DISCOGS_TOKEN env var)"
    )
    parser.add_argument(
        "--config",
        help="Path to YAML config file"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="Process files even if they already have a STYLE tag"
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        help="Only process first N files (useful for testing)"
    )
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="Interactive mode: preview audio and manually enter styles when no match found"
    )

    return parser.parse_args()


# =============================================================================
# Entry Point
# =============================================================================

def main() -> int:
    """Main entry point."""
    args = parse_args()
    config = load_config(args)

    # Handle review mode: read files from manual_review.log
    if config.review_mode:
        log_file = Path("manual_review.log")
        if not log_file.exists():
            print(f"Error: {log_file} not found. Run the script normally first.", file=sys.stderr)
            return 1

        # Read file paths from log, skipping comments and empty lines
        review_files = []
        with open(log_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    review_files.append(line)

        if not review_files:
            print("No files to review in manual_review.log", file=sys.stderr)
            return 0

        # Use parent directories as root folders
        config.root_folders = list(set(str(Path(f).parent) for f in review_files))
        config.skip_existing_style = False  # Force re-process these files

    if not config.root_folders and not config.review_mode:
        print("Error: No root folders specified", file=sys.stderr)
        return 1

    logger = setup_logging(config)

    if config.dry_run:
        logger.info("DRY RUN MODE - no changes will be written")

    # Create Discogs client
    client = discogs_client.Client(USER_AGENT, user_token=config.discogs_token)

    if config.review_mode:
        logger.info(f"REVIEW MODE - processing {len(review_files)} files from manual_review.log")
        # Process only the files from the log
        stats = process_review_files(review_files, config, client, logger)
    else:
        logger.info(f"Scanning: {', '.join(config.root_folders)}")
        logger.info(f"Supported formats: {', '.join(SUPPORTED_EXTENSIONS)}")
        # Process library
        stats = process_library(config, client, logger)

    # Print summary
    print_summary(stats, config, logger)

    return 0 if stats.errors == 0 else 1


def process_review_files(
    file_paths: list[str],
    config: Config,
    client: discogs_client.Client,
    logger: logging.Logger
) -> Stats:
    """Process specific files from manual_review.log interactively."""
    stats = Stats()
    cache: dict = {}

    for i, file_path_str in enumerate(file_paths, 1):
        file_path = Path(file_path_str)

        if not file_path.exists():
            logger.warning(f"[{i}/{len(file_paths)}] File not found: {file_path}")
            stats.errors += 1
            continue

        stats.files_scanned += 1
        logger.info(f"\n[{i}/{len(file_paths)}] {file_path.name}")

        try:
            result = process_file(file_path, client, config, cache, logger,
                                  collect_for_review=False)

            if result.error:
                stats.errors += 1
                stats.failed_files.append((str(file_path), f"Error: {result.error}"))
            elif result.skipped:
                stats.files_skipped += 1
                reason = result.skip_reason.value if result.skip_reason else "Unknown"
                stats.skip_reasons[reason] = stats.skip_reasons.get(reason, 0) + 1
                logger.info(f"  Skipped: {reason}")
                if result.skip_reason in (SkipReason.NO_SEARCH_RESULTS, SkipReason.NO_MATCHING_RELEASE):
                    stats.failed_files.append((str(file_path), reason))
            else:
                stats.files_processed += 1
                if result.title_cleaned:
                    stats.titles_cleaned += 1
                if result.discogs_matched or result.manual_style:
                    stats.styles_written += 1
                    style = result.match.styles if result.match else "Manual"
                    stats.successful_files.append((str(file_path), style))
                if result.manual_style:
                    stats.manual_styles += 1

        except KeyboardInterrupt:
            logger.info("\nInterrupted by user")
            break
        except Exception as e:
            stats.errors += 1
            stats.failed_files.append((str(file_path), f"Error: {e}"))
            logger.error(f"  Error: {e}")

    return stats


if __name__ == "__main__":
    sys.exit(main())
