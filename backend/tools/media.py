"""
tools/media.py — volume and playback control.

This file exists because of an honest gap. Sid could *start* a song but not
pause it or turn it down, which made it feel like a demo rather than an
assistant. The roadmap was all about orchestration — memory, planning,
approvals — and quietly forgot that an assistant has to be able to work the
machine it lives on.

TWO DIFFERENT MECHANISMS, and it's worth knowing why:

1. MEDIA KEYS (playback)
   Windows defines virtual key codes for the play/pause and track buttons
   found on keyboards. We synthesise those key presses with `keybd_event`.
   Whatever app currently owns media focus receives them — Chrome playing
   YouTube, Spotify, VLC, anything. We are not talking to YouTube at all;
   we're pressing the same button your keyboard would.

   That's why this works on apps we know nothing about. It's also why it's
   slightly imprecise: "pause" goes to whatever Windows thinks is playing.

2. THE AUDIO API (volume)
   Volume needs more than a key press, because "set it to 40%" is an
   absolute instruction and the volume key only nudges by a step. So for
   volume we talk to the Windows Core Audio API through pycaw, which lets us
   read the current level and set an exact one.

   If pycaw isn't installed we fall back to tapping the volume keys — less
   precise, but it still works. **A tool that degrades is better than a tool
   that disappears.**
"""

import ctypes
import time

from . import tool

# Virtual key codes. These are Windows constants, not our invention.
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT = 0xB0
VK_MEDIA_PREV = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3

KEYEVENTF_KEYUP = 0x0002

# What the model may pass to control_media, and which key each maps to.
ACTIONS = {
    "playpause": VK_MEDIA_PLAY_PAUSE,
    "pause": VK_MEDIA_PLAY_PAUSE,     # same key toggles; both names accepted
    "play": VK_MEDIA_PLAY_PAUSE,
    "next": VK_MEDIA_NEXT,
    "previous": VK_MEDIA_PREV,
    "stop": VK_MEDIA_STOP,
    "mute": VK_VOLUME_MUTE,
    "unmute": VK_VOLUME_MUTE,         # mute is a toggle too
}


def _tap(key_code: int, times: int = 1) -> None:
    """Press and release a key, as if the keyboard had done it."""
    for _ in range(times):
        ctypes.windll.user32.keybd_event(key_code, 0, 0, 0)              # down
        ctypes.windll.user32.keybd_event(key_code, 0, KEYEVENTF_KEYUP, 0)  # up
        time.sleep(0.01)   # Windows drops keys sent faster than this


def _endpoint_volume():
    """
    Get the Windows master volume control, or None if unavailable.

    pycaw renamed things between versions, so we try the modern property
    first and fall back to the older private attribute. Pinning to one shape
    would break the day the package updates.
    """
    try:
        from pycaw.utils import AudioUtilities

        speakers = AudioUtilities.GetSpeakers()

        if hasattr(speakers, "EndpointVolume"):     # newer pycaw
            return speakers.EndpointVolume

        from ctypes import POINTER, cast            # older pycaw
        from comtypes import CLSCTX_ALL
        from pycaw.api.endpointvolume import IAudioEndpointVolume

        raw = getattr(speakers, "_dev", speakers)
        iface = raw.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return cast(iface, POINTER(IAudioEndpointVolume))
    except Exception:
        return None


@tool(tier="act")
def set_volume(level: int) -> str:
    """Set the computer's speaker volume to an exact percentage.

    Use this for "turn it up", "make it quieter", "volume to 50", "too loud".
    For a relative change, read the current level from the reply and set a
    new one - going up or down by about 20 is a normal step.

    Args:
        level: Volume from 0 (silent) to 100 (maximum)
    """
    try:
        level = int(level)
    except (TypeError, ValueError):
        return f"'{level}' isn't a number. Give a volume from 0 to 100."

    # Clamp rather than reject. The model saying 150 clearly means "loud",
    # and refusing would just waste a whole extra round trip.
    level = max(0, min(100, level))

    volume = _endpoint_volume()

    if volume is None:
        # Fallback: each volume key tap moves Windows by ~2%, so 50 taps
        # covers the full range. Crude, but it works with no dependencies.
        _tap(VK_VOLUME_DOWN, 50)
        _tap(VK_VOLUME_UP, round(level / 2))
        return f"Volume set to about {level}%."

    try:
        if volume.GetMute():
            volume.SetMute(0, None)          # setting a level implies unmute
        volume.SetMasterVolumeLevelScalar(level / 100.0, None)
    except Exception as exc:
        return f"Could not change the volume: {exc}"

    if level == 0:
        return "Volume set to 0 - silent."
    return f"Volume set to {level}%."


@tool(tier="read")
def get_volume() -> str:
    """Check the computer's current speaker volume and whether it is muted.

    Call this before a relative change like "turn it down a bit", so you know
    what you are changing from.
    """
    volume = _endpoint_volume()
    if volume is None:
        return "Can't read the volume on this system - you can still set it."

    try:
        level = round(volume.GetMasterVolumeLevelScalar() * 100)
        muted = bool(volume.GetMute())
    except Exception as exc:
        return f"Could not read the volume: {exc}"

    return f"Volume is {level}%" + (" (muted)." if muted else ".")


@tool(tier="act")
def control_media(action: str) -> str:
    """Control whatever is currently playing - YouTube, Spotify, VLC, anything.

    This presses the media keys your keyboard has, so it works with any app
    that is playing sound, without Sid needing to know which one.

    Args:
        action: One of "playpause", "pause", "play", "next", "previous",
                "stop", "mute", "unmute"
    """
    key = ACTIONS.get(str(action).lower().strip().replace(" ", ""))

    if key is None:
        return (
            f"'{action}' isn't something I can do. "
            f"Try: {', '.join(sorted(set(ACTIONS)))}"
        )

    _tap(key)

    # Honest wording. "pause" and "play" are the same physical key, so we
    # genuinely don't know which way it toggled - saying "paused" would be a
    # guess presented as a fact.
    friendly = {
        "playpause": "Toggled play/pause.",
        "pause": "Sent play/pause - if it was playing, it's paused now.",
        "play": "Sent play/pause - if it was paused, it's playing now.",
        "next": "Skipped to the next track.",
        "previous": "Went back to the previous track.",
        "stop": "Stopped playback.",
        "mute": "Toggled mute.",
        "unmute": "Toggled mute.",
    }
    return friendly.get(str(action).lower().strip(), "Done.")
