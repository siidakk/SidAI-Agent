"""
setup_wake_word.py — download the offline speech model the wake word needs.

    py setup_wake_word.py           get the small English model (~40 MB)
    py setup_wake_word.py --test    check it recognises your phrase

Run once. The model lives in models/ and nothing is sent anywhere after
that — detection is entirely offline.
"""

import io
import os
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

MODELS = ROOT / "models"
NAME = "vosk-model-small-en-us-0.15"
URL = f"https://alphacephei.com/vosk/models/{NAME}.zip"

GREEN, RED, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[90m", "\033[1m", "\033[0m"


def download() -> Path:
    target = MODELS / NAME
    if target.exists():
        size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
        print(f"{GREEN}Already installed{OFF} {DIM}({size/1e6:.0f} MB){OFF}")
        return target

    MODELS.mkdir(exist_ok=True)
    print(f"Downloading the speech model {DIM}(~40 MB, one time){OFF}...")

    start = time.time()
    with urllib.request.urlopen(URL, timeout=600) as response:
        data = response.read()
    print(f"  got {len(data)/1e6:.0f} MB in {time.time()-start:.0f}s")

    zipfile.ZipFile(io.BytesIO(data)).extractall(MODELS)
    print(f"{GREEN}Installed{OFF} -> models/{NAME}")
    return target


def test(phrase: str) -> None:
    """
    Speak the phrase with Windows' own text-to-speech and check it's heard.

    Using synthetic speech rather than asking you to talk means this can run
    unattended and gives the same answer every time. It is not a substitute
    for saying it out loud yourself - your voice, your mic and your room are
    what actually matter - but it proves the pipeline works end to end.
    """
    import json
    import subprocess
    import wave

    wav = MODELS / "_wake_test.wav"
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
        "16000,[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
        "[System.Speech.AudioFormat.AudioChannel]::Mono); "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f'$s.SetOutputToWaveFile("{wav}", $f); $s.Speak("{phrase}"); $s.Dispose()'
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True, timeout=120)

    if not wav.exists():
        print(f"{RED}Could not synthesise test audio.{OFF}")
        return

    import listener

    engine = listener.build_engine()
    handle = wave.open(str(wav), "rb")

    fired = False
    while True:
        chunk = handle.readframes(engine.frame_length)
        if not chunk:
            break
        if engine.process(chunk) > 0.5:
            fired = True
    handle.close()
    wav.unlink(missing_ok=True)

    if fired:
        print(f"{GREEN}Heard it.{OFF} '{phrase}' triggers the wake word.")
    else:
        print(f"{RED}Not detected.{OFF} Try a different phrase, or lower")
        print(f"{DIM}AXON_WAKE_THRESHOLD in .env{OFF}")


def main() -> None:
    download()

    from backend import config  # noqa: F401  (loads .env)

    phrase = os.getenv("AXON_WAKE_WORD", "hey sid")
    engine = os.getenv("AXON_WAKE_ENGINE", "vosk")

    print()
    print(f"{BOLD}Wake word{OFF}: \"{phrase}\"   {DIM}(engine: {engine}){OFF}")
    print()

    if "--test" in sys.argv:
        test(phrase)
        print()

    print(f"Try it:      {DIM}py listener.py --debug{OFF}")
    print(f"Run always:  {DIM}py install.py --listener{OFF}")
    print(f"Change it:   {DIM}AXON_WAKE_WORD in .env - any phrase, no training{OFF}")
    print()


if __name__ == "__main__":
    if os.name == "nt":
        os.system("")
    main()
