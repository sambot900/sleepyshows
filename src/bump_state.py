"""Bump playback state enum.

Replaces the implicit boolean-flag state machine in MainWindow with
explicit, named states. Each state represents a distinct phase of bump
playback.
"""

from enum import Enum, auto


class BumpState(Enum):
    """Current phase of bump playback."""

    # No bump is playing. The player is either idle or playing an episode.
    IDLE = auto()

    # A bump video is playing (non-inclusive mode: video first, then script cards).
    PLAYING_VIDEO = auto()

    # A bump video is playing in inclusive mode (video with overlaid outro).
    PLAYING_VIDEO_INCLUSIVE = auto()

    # Bump script text cards are being displayed (with optional music).
    PLAYING_SCRIPT = auto()

    # The outro card is being displayed at the end of a bump script.
    SHOWING_OUTRO = auto()

    # Bump has finished; transitioning to episode playback.
    TRANSITIONING = auto()
