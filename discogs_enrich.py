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
    skipped: bool = False
    skip_reason: SkipReason | None = None
    error: str | None = None


@dataclass
class Stats:
    """Processing statistics."""
    files_scanned: int = 0
    files_processed: int = 0
    titles_cleaned: int = 0
    styles_written: int = 0
    used_existing_id: int = 0
    files_skipped: int = 0
    skip_reasons: dict = field(default_factory=dict)
    errors: int = 0


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
    try:
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
        try:
            tags = ID3(file_path)
        except ID3NoHeaderError:
            # Create new ID3 header
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
            is_substring = (norm_release_artist in norm_part or norm_part in norm_release_artist)

            if ratio >= 0.85 or (is_substring and len(norm_release_artist) >= 3):
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
    full_query: str,
    logger: logging.Logger
) -> list:
    """Search Discogs using the full filename. Let Discogs figure out artist/title."""
    try:
        # Strip accents for better API search compatibility
        search_query = strip_accents(full_query)
        logger.debug(f"  Searching Discogs for: {search_query}")
        results = client.search(search_query, type='release')
        return list(results.page(1))
    except Exception as e:
        logger.error(f"Discogs search error: {e}")
        return []


def is_catalog_number(s: str) -> bool:
    """Check if string looks like a catalog number (e.g., GYST009, SV68)."""
    # Catalog numbers are typically short alphanumeric codes
    if len(s) > 15 or len(s) < 3:
        return False
    # Must have both letters and numbers, or be all caps with numbers
    has_letter = any(c.isalpha() for c in s)
    has_digit = any(c.isdigit() for c in s)
    return has_letter and has_digit and len(s.split()) == 1


def search_discogs_with_fallbacks(
    client: discogs_client.Client,
    filename_stem: str,
    logger: logging.Logger,
    request_delay: float
) -> list:
    """
    Search Discogs with limited fallback strategies.
    Only tries combinations likely to yield good results.
    """
    # Strategy 1: Full filename as-is
    results = search_discogs(client, filename_stem, logger)
    if results:
        return results

    # Parse filename parts for alternative searches
    parts = [p.strip() for p in filename_stem.split(" - ") if p.strip()]

    # Filter out catalog numbers and very short/generic parts
    meaningful_parts = [p for p in parts if len(p) > 2 and not is_catalog_number(p)]

    # Only try ONE fallback: artist + title (first + last meaningful parts)
    # This is the most likely to find the right result without being too vague
    if len(meaningful_parts) >= 2:
        time.sleep(request_delay)
        query = f"{meaningful_parts[0]} {meaningful_parts[-1]}"
        logger.debug(f"  Fallback search (artist+title): {query}")
        results = search_discogs(client, query, logger)
        if results:
            return results

    # No more fallbacks - if artist+title didn't work, single parts are too vague
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
    full_filename: str | None = None
) -> MatchResult | None:
    """Search Discogs and return best matching release."""

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
            return None
        logger.debug(f"Cache hit: release {cached.release_id}")
        return cached

    # Search Discogs with filename - but limit to first 2-3 meaningful parts
    raw_query = full_filename or f"{tags.artist} - {tags.title}"

    # Strip track numbers and limit parts for cleaner search
    raw_parts = [p.strip() for p in raw_query.split(" - ") if p.strip()]
    clean_parts = [strip_track_number(p) for p in raw_parts if not is_catalog_number(p)]

    # Limit to first 2 parts for search (artist + album/title) - more is too noisy
    search_parts = clean_parts[:2] if len(clean_parts) > 2 else clean_parts
    search_query = " - ".join(search_parts)

    if search_query != raw_query:
        logger.debug(f"  Shortened query: {raw_query} -> {search_query}")

    results = search_discogs_with_fallbacks(client, search_query, logger, request_delay)

    if not results:
        cache[cache_key] = None
        logger.info(f"  No Discogs results for: {search_query} (tried multiple strategies)")
        return None

    # Use cleaned parts for scoring
    filename_parts = clean_parts

    # For single-part filenames (no " - " separator), we can't reliably score
    # because we don't know what's artist vs title. Use a lower effective threshold.
    is_unparsed_filename = len(filename_parts) == 1 and " - " not in search_query
    effective_min_score = min_score // 2 if is_unparsed_filename else min_score

    if is_unparsed_filename:
        logger.debug(f"  Unparsed filename, using lower score threshold: {effective_min_score}")

    # Score each result and collect candidates
    candidates = []

    for release in results:
        try:
            score, matched_track = score_release(release, filename_parts, logger)

            if score >= effective_min_score:
                # Extract styles/genres
                styles = list(release.styles) if release.styles else []
                genres = list(release.genres) if release.genres else []

                artists = release.artists
                artist_name = artists[0].name if artists else "Unknown"
                # Clean up artist name
                artist_name = re.sub(r'\s*\(\d+\)\s*$', '', artist_name)

                candidates.append(MatchResult(
                    release_id=release.id,
                    release_title=release.title or "",
                    artist_name=artist_name,
                    score=score,
                    styles=styles,
                    genres=genres,
                    matched_track=matched_track
                ))

            # Small delay between processing results that require API calls
            time.sleep(request_delay * 0.1)

        except Exception as e:
            logger.debug(f"Error scoring release: {e}")
            continue

    # Sort by score descending and try each until one passes validation
    candidates.sort(key=lambda m: m.score, reverse=True)

    for match in candidates:
        if validate_match_against_filename(match, filename_parts, search_query, logger):
            cache[cache_key] = match
            logger.info(f"  Matched: {match.artist_name} - {match.release_title} (score: {match.score})")
            return match

    # No valid matches found
    cache[cache_key] = None
    if candidates:
        logger.info(f"  All {len(candidates)} candidates failed validation")
    return None


def validate_match_against_filename(
    match: MatchResult,
    filename_parts: list[str],
    full_filename: str,
    logger: logging.Logger
) -> bool:
    """
    Validate that a match makes sense for the given filename.
    Checks that the matched artist or track title appears in the filename.
    Returns False if the match seems wrong.
    """
    norm_filename = normalize_for_comparison(full_filename)

    # Check if artist appears in filename
    norm_artist = normalize_for_comparison(match.artist_name)
    # Skip "Various" artist check - it won't appear in filename
    if norm_artist and norm_artist not in ("various", "various artists", "va"):
        artist_in_filename = fuzzy_ratio(norm_artist, norm_filename) >= 0.3
        # Also check individual parts
        best_artist_part_match = 0.0
        for part in filename_parts:
            norm_part = normalize_for_comparison(part)
            ratio = fuzzy_ratio(norm_part, norm_artist)
            best_artist_part_match = max(best_artist_part_match, ratio)

        if best_artist_part_match < 0.7 and not any(
            norm_artist in normalize_for_comparison(p) or
            normalize_for_comparison(p) in norm_artist
            for p in filename_parts
        ):
            # Artist doesn't match any filename part well
            # Check if at least the track matches
            if match.matched_track:
                norm_track = normalize_for_comparison(match.matched_track)
                best_track_match = max(
                    fuzzy_ratio(norm_track, normalize_for_comparison(p))
                    for p in filename_parts
                ) if filename_parts else 0

                if best_track_match < 0.7:
                    logger.info(f"  Rejected: artist '{match.artist_name}' (best={best_artist_part_match:.2f}) "
                               f"and track '{match.matched_track}' (best={best_track_match:.2f}) not in filename")
                    return False
            else:
                logger.info(f"  Rejected: artist '{match.artist_name}' not found in filename (best={best_artist_part_match:.2f})")
                return False

    # Overall sanity check: combine match info and compare to filename
    match_combined = f"{match.artist_name} {match.release_title} {match.matched_track or ''}"
    norm_combined = normalize_for_comparison(match_combined)
    overall_similarity = fuzzy_ratio(norm_combined, norm_filename)

    if overall_similarity < 0.25:
        logger.info(f"  Rejected: overall similarity too low ({overall_similarity:.2f}): {match_combined}")
        return False

    return True


# =============================================================================
# Main Processing
# =============================================================================

def process_file(
    file_path: Path,
    client: discogs_client.Client,
    config: Config,
    cache: dict,
    logger: logging.Logger
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
    used_existing_id = False

    if tags.discogs_release_id:
        logger.debug(f"  Using existing DISCOGS_RELEASE_ID: {tags.discogs_release_id}")
        match = fetch_release_by_id(client, tags.discogs_release_id, logger)
        used_existing_id = True
        time.sleep(config.request_delay)
    else:
        # Always search Discogs using filename - we can match even without proper tags
        match = find_best_match(
            client, tags, config.min_match_score, cache, config.request_delay, logger,
            full_filename=file_path.stem
        )
        time.sleep(config.request_delay)

    if not match:
        result.skipped = True
        result.skip_reason = SkipReason.NO_SEARCH_RESULTS
        return result

    result.discogs_matched = True
    result.match = match

    # Extract style string (prefer styles over genres)
    styles = match.styles if match.styles else match.genres
    if not styles:
        result.skipped = True
        result.skip_reason = SkipReason.NO_STYLE_DATA
        return result

    style_str = config.style_separator.join(styles)

    # Write tags
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

    # Collect all files first
    all_files = list(find_audio_files(config.root_folders, logger))

    # Shuffle when using limit so we test different files each run
    if config.limit:
        random.shuffle(all_files)
        logger.info(f"Shuffled {len(all_files)} files, processing first {config.limit}")

    for file_path in all_files:
        # Check limit
        if config.limit and stats.files_scanned >= config.limit:
            logger.info(f"Reached limit of {config.limit} files")
            break

        stats.files_scanned += 1

        logger.info(f"Processing: {file_path}")

        try:
            result = process_file(file_path, client, config, cache, logger)

            if result.error:
                stats.errors += 1
            elif result.skipped:
                stats.files_skipped += 1
                reason = result.skip_reason.value if result.skip_reason else "Unknown"
                stats.skip_reasons[reason] = stats.skip_reasons.get(reason, 0) + 1
                logger.info(f"  Skipped: {reason}")
            else:
                stats.files_processed += 1
                if result.title_cleaned:
                    stats.titles_cleaned += 1
                if result.discogs_matched:
                    stats.styles_written += 1
                if result.match and result.match.score == 999:
                    stats.used_existing_id += 1

        except KeyboardInterrupt:
            logger.info("\nInterrupted by user")
            break
        except Exception as e:
            stats.errors += 1
            logger.error(f"  Error: {e}")

    return stats


def print_summary(stats: Stats, config: Config, logger: logging.Logger):
    """Print processing summary."""
    dry_run_note = " (DRY RUN)" if config.dry_run else ""

    summary = f"""
{'=' * 50}
Discogs Enrichment Complete{dry_run_note}
{'=' * 50}
Files scanned:      {stats.files_scanned:>6}
Files processed:    {stats.files_processed:>6}
  - Titles cleaned: {stats.titles_cleaned:>6}
  - Styles written: {stats.styles_written:>6}
  - Used cached ID: {stats.used_existing_id:>6}
Files skipped:      {stats.files_skipped:>6}"""

    for reason, count in sorted(stats.skip_reasons.items()):
        summary += f"\n  - {reason}: {count}"

    summary += f"""
Errors:             {stats.errors:>6}
{'=' * 50}"""

    logger.info(summary)


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
        nargs="+",
        help="Root folder(s) to scan for audio files"
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

    return parser.parse_args()


# =============================================================================
# Entry Point
# =============================================================================

def main() -> int:
    """Main entry point."""
    args = parse_args()
    config = load_config(args)

    if not config.root_folders:
        print("Error: No root folders specified", file=sys.stderr)
        return 1

    logger = setup_logging(config)

    if config.dry_run:
        logger.info("DRY RUN MODE - no changes will be written")

    # Create Discogs client
    client = discogs_client.Client(USER_AGENT, user_token=config.discogs_token)

    logger.info(f"Scanning: {', '.join(config.root_folders)}")
    logger.info(f"Supported formats: {', '.join(SUPPORTED_EXTENSIONS)}")

    # Process library
    stats = process_library(config, client, logger)

    # Print summary
    print_summary(stats, config, logger)

    return 0 if stats.errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
