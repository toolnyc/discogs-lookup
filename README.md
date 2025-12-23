# Discogs Style Enrichment Script

A tool that automatically tags your music files with style/genre information from Discogs. Perfect for DJs and producers who want better organization in their library.

## What This Does

1. **Scans your music folders** (including all subfolders)
2. **Parses filenames** to extract artist/album/title when ID3 tags are missing
3. **Searches Discogs** using the full filename and lets Discogs figure out what's what
4. **Writes tags from Discogs** including artist, title, album, style, and release ID
5. **Handles compilations** by matching track-level artist credits

**Supported formats:** MP3, AIFF

---

## Quick Start

```bash
# Test first (no changes made)
./run.sh "/path/to/your/music" --dry-run

# Run for real
./run.sh "/path/to/your/music"

# Limit to random N files (for testing - shuffles each run)
./run.sh "/path/to/your/music" --dry-run --limit 15
```

**First time running?** The script will automatically create a virtual environment and install dependencies.

---

## Command Line Options

| Option | Description |
|--------|-------------|
| `--dry-run` | Show what would be done without writing tags |
| `--limit N` or `-n N` | Process N random files (shuffled each run for testing) |
| `--force` or `-f` | Re-process files that already have a STYLE tag |
| `--no-title-cleanup` | Skip the Bandcamp title cleanup step |
| `--verbose` or `-v` | Show detailed debug output |
| `--interactive` or `-i` | Interactive mode for manual entry when no match found |
| `--review` | Process files from `manual_review.log` interactively |
| `--config FILE` | Use a YAML config file |
| `--token TOKEN` | Discogs personal access token |
| `--no-album-search` | Disable album-first search, process all files individually |

---

## Album-First Search (New)

When pointing at a Bandcamp folder structure, the script now uses **album-first search**:

```
Bandcamp/
  Legowelt - Teac Life/          → Album folder: searches for release first
    01 Track One.aiff
    02 Track Two.aiff
  Compilation Name EP/           → VA compilation: searches by album name
    ArtistA - Track1.aiff
    ArtistB - Track2.aiff
  Selects/                       → Non-album folder: per-file search
    random-track.aiff
```

**How it works:**

1. **Discover folders** in the root directory
2. **Parse folder name** → `"Artist - Album"` or just `"Album"` for compilations
3. **Search Discogs** for the release by album name
4. **Match tracklist** → fuzzy match files to release tracks
5. **Apply styles** → if ≥50% tracks match, apply release styles to all
6. **Fallback** → unmatched files use per-file search

**Benefits:**
- Fewer API calls (1 per album vs 1 per track)
- Better VA/compilation handling
- Consistent styles across album tracks
- Album names are often more searchable than individual tracks

**Special folders:**
- `Selects` folder is recognized as non-album (curated individual tracks)
- Processed with per-file search instead of album-first

---

## How It Works

### Filename Parsing

The script uses **track number prefixes** (01, 02, etc.) to intelligently parse filenames:

| Filename | Search Query | Track Parts |
|----------|-------------|-------------|
| `DJ Deep - CH001 - VAINCRE - 01 Fluorescent.aiff` | `dj deep vaincre` | `Fluorescent` |
| `Efdemin - Decay - 06 Subatomic.aiff` | `efdemin decay` | `Subatomic` |
| `Artist - Album - 05 Track Artist - Title.aiff` | `artist album` | `Track Artist`, `Title` |

**Parsing rules:**
- Parts **before** the track number = artist/album (used for search)
- Parts **from** the track number onward = track info (used for validation)
- Catalog numbers (e.g., `CH001`, `SV68`) are filtered from search
- Duplicate artist names are skipped

### Discogs Matching

The script searches Discogs with the normalized artist + album query, then scores results:

- **Release artist matches** → +100 points
- **Track title matches** → +100 points
- **Track artist matches** (for compilations) → +100 points
- **Release title matches** → +50 points
- **Partial matches (70%+ similarity)** → proportional points

A match needs at least **100 points**. After scoring, a **validation check** ensures most filename parts appear in the result (allows 1 unmatched part for formatting differences like "1740" vs "Seventeen Four Zero").

**Smart features:**
- Normalizes search queries: lowercase, strips accents (Ácido → acido)
- Filters catalog numbers from search (keeps artist names like `JSPRV35`)
- Handles word-swapped titles via fuzzy matching

### Compilation Support

For compilation tracks like `Energy Rush - The Trip.aiff`:
- Searches Discogs for "Energy Rush - The Trip"
- Finds "The Trip - UK Business" where "Energy Rush" is a track
- Correctly tags: Artist = "The Trip", Title = "Energy Rush", Album = "UK Business"

---

## What Gets Written

| Tag | Source |
|-----|--------|
| `ARTIST` | From Discogs release artist |
| `TITLE` | From matched Discogs track |
| `ALBUM` | From Discogs release title |
| `STYLE` | Styles from Discogs (e.g., "Deep House; Techno") |
| `DISCOGS_RELEASE_ID` | Release ID for faster future lookups |

---

## Common Scenarios

### Test on a small batch first

```bash
./run.sh "/path/to/music" --dry-run --limit 20
```

### Re-process files with existing STYLE tags

```bash
./run.sh "/path/to/music" --force
```

### See detailed matching info

```bash
./run.sh "/path/to/music" --dry-run --verbose
```

### Process multiple folders

```bash
./run.sh "/path/to/folder1" "/path/to/folder2"
```

### Run overnight, review failures in the morning

```bash
# Step 1: Run autonomously (can run overnight)
./run.sh "/path/to/music"

# Step 2: Review failures interactively
./run.sh --review
```

The script saves failed files to `manual_review.log`. Use `--review` to process those files interactively the next day.

---

## Using Results in DJ Software

### Mp3tag

Add a STYLE column:
1. Right-click column headers → "Customize columns..."
2. Click "New" → Name: `Style`, Value: `%STYLE%`

### Rekordbox / Traktor

The STYLE tag is stored as a custom TXXX frame. You may need to copy it to GENRE:
1. In Mp3tag, select files
2. Actions → Format value → Field: `GENRE`, Format: `%STYLE%`

---

## Troubleshooting

### "Permission denied" when running ./run.sh

```bash
chmod +x run.sh
```

### "No Discogs results found"

- The track may not be on Discogs
- Try adding the Discogs release ID manually in Mp3tag (`DISCOGS_RELEASE_ID` tag)
- Run again with `--force`

### The script seems slow

Discogs limits requests to 60/minute. The script waits 1 second between requests. A large library will take time—run it overnight.

---

## Configuration File

Copy `config.example.yaml` to `config.yaml`:

```yaml
min_match_score: 100           # Minimum score to accept (default: 100)
title_cleanup_threshold: 0.90  # Similarity for Bandcamp cleanup
style_separator: "; "          # Separator between styles
request_delay: 1.0             # Seconds between API calls

# Album-first search options
album_search_first: true       # Enable album-first search strategy
album_match_threshold: 0.5     # Min % of tracks that must match release (50%)
track_match_threshold: 0.7     # Min similarity for track matching (70%)
non_album_folders:             # Folders to process per-file instead of album-first
  - selects
```

---

## Files

| File | Description |
|------|-------------|
| `run.sh` | Launcher script (handles venv setup) |
| `discogs_enrich.py` | Main Python script |
| `config.example.yaml` | Example configuration |
| `CLAUDE.md` | Documentation for Claude Code |
| `.venv/` | Virtual environment (created automatically) |

---

## Credits

Built with:
- [python3-discogs-client](https://github.com/joalla/discogs_client) for Discogs API
- [mutagen](https://mutagen.readthedocs.io/) for ID3 tag reading/writing
