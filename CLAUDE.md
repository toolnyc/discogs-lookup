# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a music library enrichment tool that adds Discogs style/genre metadata to audio files (MP3, AIFF). It walks a directory tree, matches tracks against the Discogs database using fuzzy matching, and writes STYLE tags to the files.

## Running the Script

```bash
# Via the launcher script (handles venv and dependencies automatically)
./run.sh "/path/to/music"

# Common options
./run.sh "/path/to/music" --dry-run          # Preview without writing
./run.sh "/path/to/music" --no-title-cleanup # Skip Bandcamp title cleanup
./run.sh "/path/to/music" --force            # Re-process files with existing STYLE
./run.sh "/path/to/music" --verbose          # Debug output
./run.sh "/path/to/music" --config config.yaml
```

First run automatically creates `.venv/` and installs dependencies (python3-discogs-client, mutagen, pyyaml).

## Architecture

Single-file Python script (`discogs_enrich.py`) with these key components:

- **Configuration**: `Config` dataclass merges CLI args, YAML config, and env vars. Token priority: CLI > env var > config file > default.

- **Tag I/O**: Uses mutagen for ID3 tags. Reads TPE1 (artist), TIT2 (title), TALB (album), TDRC/TYER (year). Writes TXXX frames for STYLE and DISCOGS_RELEASE_ID.

- **Title Cleanup**: Detects Bandcamp-style "Artist – Title" patterns and removes the artist prefix when similarity exceeds threshold (default 0.90).

- **Discogs Matching**: Searches by artist+title (or title-only for VA compilations). Scores results based on artist match (+100), track match (+100), album match (+25), year match (+20). Requires minimum 130 points by default.

- **Caching**: In-memory cache keyed by normalized (artist, title, album) prevents duplicate API lookups within a run.

- **Rate Limiting**: 1-second delay between API calls to respect Discogs limits.

## Key Data Structures

- `TrackTags`: Tags read from file (artist, title, album, year, existing style/release_id)
- `MatchResult`: Discogs match with release_id, score, styles, genres
- `ProcessingResult`: Outcome for a single file (cleaned title, match info, skip reason)
- `Stats`: Aggregate counts for final summary

## Environment

- `DISCOGS_TOKEN`: Personal access token for Discogs API (optional, has default)
