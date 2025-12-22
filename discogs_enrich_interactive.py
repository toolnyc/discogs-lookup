#!/usr/bin/env python3
"""
Interactive Discogs Style Enrichment Script

Adds audio preview and manual style entry when no Discogs match is found.
"""

from __future__ import annotations

import os
import subprocess
import platform
import sys
import threading
import time
from pathlib import Path

# Suppress pygame welcome message
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'

# Try to import audio playback
# We try multiple backends in order of preference
AUDIO_AVAILABLE = False
AUDIO_BACKEND = None

# Try pygame first (best cross-platform support)
try:
    import pygame
    pygame.mixer.init()
    AUDIO_AVAILABLE = True
    AUDIO_BACKEND = "pygame"
except Exception:
    pass

# Fall back to simpleaudio (macOS/Windows/Linux, but WAV only)
if not AUDIO_AVAILABLE:
    try:
        import simpleaudio
        AUDIO_AVAILABLE = True
        AUDIO_BACKEND = "simpleaudio"
    except ImportError:
        pass

# Fall back to afplay on macOS (built-in, no dependencies)
if not AUDIO_AVAILABLE:
    if platform.system() == "Darwin":
        AUDIO_AVAILABLE = True
        AUDIO_BACKEND = "afplay"

if not AUDIO_AVAILABLE:
    print("Note: Audio preview unavailable. On macOS this should work automatically.")

# Try to import rich for nice terminal output
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich import print as rprint
    RICH_AVAILABLE = True
    console = Console()
except ImportError:
    RICH_AVAILABLE = False
    print("Note: Install rich for better UI: pip install rich")


# =============================================================================
# Audio Player
# =============================================================================

class AudioPlayer:
    """Simple audio player with play/pause/stop. Supports multiple backends."""

    def __init__(self):
        self.current_file: Path | None = None
        self.is_playing = False
        self.position = 0  # seconds
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None  # For afplay backend

    def load(self, file_path: Path) -> bool:
        """Load an audio file."""
        if not AUDIO_AVAILABLE:
            return False
        try:
            self.stop()  # Stop any current playback
            with self._lock:
                self.current_file = file_path
                self.position = 0
                self.is_playing = False

                if AUDIO_BACKEND == "pygame":
                    pygame.mixer.music.load(str(file_path))

            return True
        except Exception as e:
            print(f"Could not load audio: {e}")
            return False

    def play(self):
        """Start or resume playback."""
        if not AUDIO_AVAILABLE or not self.current_file:
            return
        with self._lock:
            if not self.is_playing:
                if AUDIO_BACKEND == "pygame":
                    pygame.mixer.music.play(start=self.position)
                elif AUDIO_BACKEND == "afplay":
                    # afplay is macOS's built-in audio player
                    self._process = subprocess.Popen(
                        ["afplay", str(self.current_file)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                self.is_playing = True

    def pause(self):
        """Pause playback."""
        if not AUDIO_AVAILABLE:
            return
        with self._lock:
            if self.is_playing:
                if AUDIO_BACKEND == "pygame":
                    self.position = pygame.mixer.music.get_pos() / 1000
                    pygame.mixer.music.pause()
                elif AUDIO_BACKEND == "afplay":
                    # afplay doesn't support pause, so we stop it
                    if self._process:
                        self._process.terminate()
                        self._process = None
                self.is_playing = False

    def stop(self):
        """Stop playback."""
        if not AUDIO_AVAILABLE:
            return
        with self._lock:
            if AUDIO_BACKEND == "pygame":
                pygame.mixer.music.stop()
            elif AUDIO_BACKEND == "afplay":
                if self._process:
                    self._process.terminate()
                    self._process = None
            self.is_playing = False
            self.position = 0

    def toggle(self):
        """Toggle play/pause."""
        if self.is_playing:
            self.pause()
        else:
            self.play()

    def seek(self, seconds: float):
        """Seek to position (limited support depending on backend)."""
        if not AUDIO_AVAILABLE:
            return
        with self._lock:
            self.position = max(0, seconds)
            if AUDIO_BACKEND == "pygame" and self.is_playing:
                pygame.mixer.music.play(start=self.position)
            # afplay doesn't support seeking

    def get_status(self) -> str:
        """Get current status string."""
        if not self.current_file:
            return "No track loaded"
        status = "▶ Playing" if self.is_playing else "⏸ Paused"
        backend_note = f" [{AUDIO_BACKEND}]" if AUDIO_BACKEND else ""
        return f"{status} - {self.current_file.name}{backend_note}"


# Global player instance
player = AudioPlayer()


# =============================================================================
# Interactive UI
# =============================================================================

def display_track_info(file_path: Path, tags: dict, match_info: dict | None = None):
    """Display current track information."""
    if RICH_AVAILABLE:
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Field", style="cyan")
        table.add_column("Value")

        table.add_row("File", file_path.name)
        table.add_row("Artist", tags.get("artist", "Unknown"))
        table.add_row("Title", tags.get("title", "Unknown"))
        table.add_row("Album", tags.get("album", "-"))

        if match_info:
            table.add_row("", "")
            table.add_row("Best Match", f"{match_info.get('artist', '?')} - {match_info.get('release', '?')}")
            table.add_row("Score", f"{match_info.get('score', 0)} (threshold: {match_info.get('threshold', 100)})")
            if match_info.get("styles"):
                table.add_row("Styles", ", ".join(match_info["styles"]))

        console.print(Panel(table, title="[bold]Track Info[/bold]", border_style="blue"))
    else:
        print(f"\n{'='*50}")
        print(f"File:   {file_path.name}")
        print(f"Artist: {tags.get('artist', 'Unknown')}")
        print(f"Title:  {tags.get('title', 'Unknown')}")
        print(f"Album:  {tags.get('album', '-')}")
        if match_info:
            print(f"\nBest Match: {match_info.get('artist', '?')} - {match_info.get('release', '?')}")
            print(f"Score: {match_info.get('score', 0)}")
        print(f"{'='*50}")


def display_search_results(results: list[dict]):
    """Display Discogs search results for user selection."""
    if not results:
        print("No results found.")
        return

    if RICH_AVAILABLE:
        table = Table(title="Discogs Results", show_lines=True)
        table.add_column("#", style="cyan", width=3)
        table.add_column("Artist", style="green")
        table.add_column("Release")
        table.add_column("Styles", style="yellow")
        table.add_column("Score", justify="right")

        for i, r in enumerate(results[:10], 1):
            styles = ", ".join(r.get("styles", [])[:3])
            table.add_row(
                str(i),
                r.get("artist", "?")[:30],
                r.get("release", "?")[:40],
                styles[:30],
                str(r.get("score", "-"))
            )

        console.print(table)
    else:
        print("\nDiscogs Results:")
        for i, r in enumerate(results[:10], 1):
            styles = ", ".join(r.get("styles", [])[:3])
            print(f"  {i}. {r.get('artist', '?')} - {r.get('release', '?')}")
            print(f"     Styles: {styles} (score: {r.get('score', '-')})")


def show_controls():
    """Show available controls."""
    if RICH_AVAILABLE:
        controls = """
[bold cyan]Controls:[/bold cyan]
  [green]p[/green] = play/pause    [green]s[/green] = stop    [green]<[/green]/[green]>[/green] = seek ±10s
  [green]1-9[/green] = select result    [green]m[/green] = enter style manually
  [green]n[/green] = skip (no style)    [green]q[/green] = quit
"""
        console.print(Panel(controls, border_style="dim"))
    else:
        print("\nControls: p=play/pause, s=stop, </>=seek, 1-9=select, m=manual, n=skip, q=quit")


def get_manual_style() -> str | None:
    """Prompt user to enter style manually."""
    if RICH_AVAILABLE:
        console.print("\n[yellow]Enter style(s) separated by semicolons:[/yellow]")
        console.print("[dim]Examples: House; Deep House  or  Techno; Minimal[/dim]")
        style = Prompt.ask("Style")
    else:
        print("\nEnter style(s) separated by semicolons:")
        print("Examples: House; Deep House  or  Techno; Minimal")
        style = input("Style: ").strip()

    return style if style else None


def interactive_review(
    file_path: Path,
    tags: dict,
    search_results: list[dict],
    best_match: dict | None = None
) -> tuple[str | None, int | None]:
    """
    Interactive review for a track with no confident match.

    Returns: (style_string, discogs_release_id) or (None, None) to skip
    """
    # Load audio for preview
    player.load(file_path)

    # Display info
    print("\n" + "="*60)
    display_track_info(file_path, tags, best_match)

    if search_results:
        display_search_results(search_results)

    show_controls()
    print(f"\n{player.get_status()}")

    while True:
        try:
            if RICH_AVAILABLE:
                choice = Prompt.ask("\n[bold]Action[/bold]", default="p").lower().strip()
            else:
                choice = input("\nAction [p]: ").lower().strip() or "p"

            # Audio controls
            if choice == "p":
                player.toggle()
                status = "▶ Playing" if player.is_playing else "⏸ Paused"
                print(f"  {status}")

            elif choice == "s":
                player.stop()
                print("  ⏹ Stopped")

            elif choice == "<":
                player.seek(player.position - 10)
                print(f"  ⏪ Seek -10s")

            elif choice == ">":
                player.seek(player.position + 10)
                print(f"  ⏩ Seek +10s")

            # Selection
            elif choice.isdigit() and 1 <= int(choice) <= len(search_results):
                idx = int(choice) - 1
                selected = search_results[idx]
                styles = "; ".join(selected.get("styles", []))
                player.stop()

                if RICH_AVAILABLE:
                    console.print(f"\n[green]Selected:[/green] {selected['artist']} - {selected['release']}")
                    console.print(f"[green]Styles:[/green] {styles}")
                else:
                    print(f"\nSelected: {selected['artist']} - {selected['release']}")
                    print(f"Styles: {styles}")

                return styles, selected.get("release_id")

            # Manual entry
            elif choice == "m":
                style = get_manual_style()
                if style:
                    player.stop()
                    return style, None
                print("  (cancelled)")

            # Skip
            elif choice == "n":
                player.stop()
                return None, None

            # Quit
            elif choice == "q":
                player.stop()
                raise KeyboardInterrupt("User quit")

            # Replay results
            elif choice == "r":
                display_search_results(search_results)

            else:
                print("  Unknown command. Use: p, s, <, >, 1-9, m, n, q")

        except EOFError:
            player.stop()
            return None, None


# =============================================================================
# Demo / Test
# =============================================================================

def demo():
    """Demo the interactive UI with a fake track."""
    print("\n" + "="*60)
    print("INTERACTIVE MODE DEMO")
    print("="*60)

    # Fake data for demo
    fake_tags = {
        "artist": "Unknown Artist",
        "title": "Deep Track",
        "album": "Various Artists"
    }

    fake_results = [
        {
            "artist": "DJ Deep",
            "release": "Deep Thoughts EP",
            "styles": ["Deep House", "House"],
            "score": 85,
            "release_id": 12345
        },
        {
            "artist": "Deep Dish",
            "release": "Deep In The Night",
            "styles": ["Progressive House", "House"],
            "score": 72,
            "release_id": 23456
        },
        {
            "artist": "The Deep",
            "release": "Underground Sessions",
            "styles": ["Techno", "Minimal"],
            "score": 65,
            "release_id": 34567
        },
    ]

    fake_best = {
        "artist": "DJ Deep",
        "release": "Deep Thoughts EP",
        "score": 85,
        "threshold": 100,
        "styles": ["Deep House", "House"]
    }

    # Find a real audio file to test with, or use a fake path
    test_file = Path("/tmp/fake_track.mp3")

    print("\nThis is a demo of the interactive review mode.")
    print("In real usage, this appears when a track has no confident Discogs match.")

    if not AUDIO_AVAILABLE:
        print("\n⚠️  Audio preview requires pygame: pip install pygame")

    if not RICH_AVAILABLE:
        print("\n⚠️  Better UI requires rich: pip install rich")

    # Run interactive review
    style, release_id = interactive_review(test_file, fake_tags, fake_results, fake_best)

    if style:
        print(f"\n✓ Would write style: {style}")
        if release_id:
            print(f"✓ From Discogs release: {release_id}")
    else:
        print("\n✗ Skipped (no style applied)")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--demo":
        demo()
    else:
        print("Usage: python discogs_enrich_interactive.py --demo")
        print("\nThis module provides interactive review functions to be integrated")
        print("with the main discogs_enrich.py script.")
        print("\nRun with --demo to see the interactive UI in action.")
