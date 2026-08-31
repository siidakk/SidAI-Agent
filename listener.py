"""
listener.py — always-on wake word. Say it and Sid opens.

    py listener.py --debug     watch live scores in a console
    py install.py --listener   run it automatically at every Windows startup

It's a .py (not .pyw) so --debug can actually print. The startup shortcut
names pythonw.exe directly, so it still runs with no console window.

HOW WAKE WORDS ACTUALLY WORK
----------------------------
This is NOT speech recognition. A recogniser is huge, slow, and would have to
stream every sound in your room somewhere. A wake word detector is a tiny
neural network — around 1 MB — trained to answer exactly one question, about
twelve times a second:

    "did the last ~1.4 seconds of audio contain this specific phrase?"

It outputs a score from 0 to 1. Above the threshold, we wake up. It cannot
understand anything else, which is precisely why it's safe to leave running:
**nothing is recorded and nothing leaves your machine.** Audio flows through
a small rolling buffer and is discarded frame by frame.

That two-stage design — cheap always-on detector, expensive recogniser only
after it fires — is how every Alexa and Siri on earth works.

...EXCEPT for the default engine here, which does the opposite. Worth
reading why, because it is a nice lesson in re-framing a problem.

THREE ENGINES, and why the default is the odd one out
-----------------------------------------------------
The obvious approach needs a model trained on one exact phrase. No "Sid"
model exists, and getting one means an hour of Colab training or a Picovoice
account that demands a company email. Both are walls.

So the default sidesteps it: run a small speech RECOGNISER, but restrict it
to a grammar of four allowed outputs. It barely has to think, and any phrase
works with no training at all.

  vosk          DEFAULT. Offline, no account, no training. Any phrase you
                like - just set AXON_WAKE_WORD="hey sid". 71 MB model,
                147 MB RAM, 30x faster than real time.

  openwakeword  A true 1 MB wake-word net. Lighter in principle, but only
                six built-in phrases; a custom one needs ~1h Colab training.

  porcupine     Best accuracy of the three. Needs a Picovoice AccessKey,
                and their signup asks for a company email.

Set AXON_WAKE_ENGINE in .env to pick.
"""

import argparse
import os
import queue
import struct
import sys
import re
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backend import config, settings  # noqa: E402

MODELS_DIR = ROOT / "models"

# Both engines want 16 kHz mono 16-bit audio. openWakeWord wants 1280-sample
# frames (80ms); Porcupine tells us its own frame length at runtime.
SAMPLE_RATE = 16000
OWW_FRAME = 1280

THRESHOLD = float(os.getenv("AXON_WAKE_THRESHOLD", "0.5"))
ENGINE = os.getenv("AXON_WAKE_ENGINE", "vosk").lower()
WAKE_WORD = os.getenv("AXON_WAKE_WORD", "hey sid")
PICOVOICE_KEY = os.getenv("PICOVOICE_ACCESS_KEY", "")

# Ignore further detections for this long after waking. Without it, one
# "Hey Sid" fires several times as the phrase passes through the buffer.
COOLDOWN_SECONDS = 4.0


# ==========================================================================
#  Engines. Each exposes: frame_length, and process(audio) -> score 0..1
# ==========================================================================
class OpenWakeWordEngine:
    """Free, offline, no account. Custom phrases need Colab training."""

    frame_length = OWW_FRAME

    def __init__(self, wake_word: str):
        from openwakeword.model import Model

        # A custom model is just a path. Accept a bare name, a filename in
        # models/, or a full path - so dropping a trained .onnx in models/
        # and setting AXON_WAKE_WORD=hey_sid.onnx just works.
        candidate = MODELS_DIR / wake_word
        if candidate.exists():
            target, self.key = str(candidate), Path(wake_word).stem
        elif Path(wake_word).exists():
            target, self.key = wake_word, Path(wake_word).stem
        else:
            target = self.key = wake_word

        try:
            self.model = Model(wakeword_models=[target], inference_framework="onnx")
        except Exception as exc:
            # openWakeWord's own message ("could not find pretrained model")
            # doesn't tell you that custom phrases need training, or where to
            # do it. This is the error you hit the moment you want your own
            # wake word, so it's worth answering properly.
            raise RuntimeError(
                f"""No wake word model called '{wake_word}'.

Built in: alexa, hey_jarvis, hey_mycroft, hey_rhasspy

For a custom phrase like "hey sid" you have to train one:
  - free, ~1 hour, no account : see models/README.md
  - or switch to Porcupine    : AXON_WAKE_ENGINE=porcupine
    (about a minute, but needs a free Picovoice key)

({exc})"""
            ) from exc

        # openWakeWord keys its results by model name, which for a custom
        # file is the filename stem. Take whatever it actually registered
        # rather than assuming - one less thing to get subtly wrong.
        if self.key not in self.model.models:
            self.key = list(self.model.models)[0]

    def process(self, raw: bytes) -> float:
        return self.model.predict(np.frombuffer(raw, dtype=np.int16)).get(self.key, 0.0)


class PorcupineEngine:
    """Picovoice. Custom phrases in a minute, but needs a free AccessKey."""

    def __init__(self, keyword_path: str, access_key: str):
        import pvporcupine

        path = MODELS_DIR / keyword_path
        if not path.exists():
            path = Path(keyword_path)
        if not path.exists():
            raise FileNotFoundError(
                f"Keyword file not found: {keyword_path}. Generate one at "
                f"console.picovoice.ai and save it in {MODELS_DIR}"
            )

        self.porcupine = pvporcupine.create(
            access_key=access_key, keyword_paths=[str(path)]
        )
        self.frame_length = self.porcupine.frame_length

    def process(self, raw: bytes) -> float:
        # Porcupine returns an index (-1 for nothing), not a score. We map it
        # to 1.0 / 0.0 so the calling loop treats every engine identically.
        pcm = struct.unpack_from('h' * self.frame_length, raw)
        return 1.0 if self.porcupine.process(pcm) >= 0 else 0.0


def _near_misses(name: str) -> set[str]:
    """
    Ways a recogniser commonly mishears the name.

    Kept deliberately short. Every extra spelling is another chance to fire
    on someone's ordinary sentence, and the cost of a false trigger is much
    higher than the cost of saying the wake word twice.
    """
    variants = {name}
    if name == "sid":
        variants |= {"said", "sid", "syd", "sit"}
    return variants


class VoskEngine:
    """
    A small speech recogniser, restricted to a handful of allowed words.

    WHY THIS IS THE DEFAULT
    -----------------------
    The other two engines need a model trained on one exact phrase. There is
    no "Sid" model in existence, and getting one means either an hour of
    Colab training or a Picovoice account that wants a company email.

    This takes the opposite approach: run a real (if small) recogniser, but
    hand it a GRAMMAR - a list of the only things it is allowed to output.
    With a grammar of four entries it barely has to think, and any phrase
    works with zero training. Change AXON_WAKE_WORD to "hey computer" and
    that is the whole job.

    Measured on this laptop: real-time factor 0.03 (thirty times faster than
    the audio arrives), 147 MB RAM - actually lighter than openWakeWord.

    The tradeoff: this IS speech recognition, so it is doing more work than a
    1 MB purpose-built net. Still entirely offline - the model sits in
    models/ and nothing is sent anywhere.
    """

    # 250ms chunks. Small enough to feel instant, big enough that we aren't
    # calling the recogniser sixty times a second.
    frame_length = 4000

    def __init__(self, phrase: str, model_dir: Path):
        import json as _json
        from vosk import KaldiRecognizer, Model as VoskModel, SetLogLevel

        if not model_dir.exists():
            raise RuntimeError(
                f"""Vosk model missing: {model_dir}

Download it (about 40 MB, one time) with:
    py setup_wake_word.py"""
            )

        SetLogLevel(-1)                       # vosk is extremely chatty
        self._json = _json
        self.phrase = phrase.lower().strip()

        # WHY THIS IS SO MUCH STRICTER THAN IT WAS
        #
        # The first version accepted "hey sid", "hey said" AND bare "sid",
        # and tested them with `phrase in text` — a plain substring match.
        # Every part of that was wrong in a way that made Sid interrupt
        # ordinary conversation:
        #
        #   * "said" is one of the commonest words in English.
        #   * bare "sid" is a SUBSTRING of "inside", "beside", "outside",
        #     "considered" — all of which fired the wake word.
        #   * a grammar-restricted recogniser is forced to map whatever it
        #     hears onto the nearest phrase it knows, so unrelated speech
        #     lands on "sid" rather than being rejected.
        #
        # A wake word that fires while you are talking to someone else is
        # worse than one that occasionally misses: you disable it, and then
        # it does nothing at all.
        #
        # So: the "hey" is now REQUIRED — two syllables of context is what
        # separates a summons from a passing word — and matching is on whole
        # words, never substrings.
        self.accept = {self.phrase}
        if self.phrase.startswith("hey "):
            name = self.phrase[4:]
            # Keep only near-misses that still carry the "hey".
            self.accept |= {f"hey {v}" for v in _near_misses(name)}

        # `[unk]` lets Vosk say "that was none of these" instead of being
        # forced to pick one. Without it, silence and coughs get mapped onto
        # the wake phrase.
        grammar = _json.dumps(sorted(self.accept) + ["[unk]"])
        self.rec = KaldiRecognizer(VoskModel(str(model_dir)), 16000, grammar)

        # Compiled once: whole-word matching for each accepted phrase.
        self._patterns = [
            re.compile(r"(?<!\w)" + re.escape(a) + r"(?!\w)") for a in self.accept
        ]

    def process(self, raw: bytes) -> float:
        if self.rec.AcceptWaveform(raw):
            text = self._json.loads(self.rec.Result()).get("text", "")
        else:
            # Partial results let us fire mid-sentence instead of waiting for
            # a pause, which is the difference between snappy and sluggish.
            text = self._json.loads(self.rec.PartialResult()).get("partial", "")

        if not text:
            return 0.0

        # Whole-word match, not `in`. "beside" contains "sid"; that one
        # character of laziness was most of the false triggers.
        if any(p.search(text) for p in self._patterns):
            self.rec.Reset()                  # don't re-fire on the same words
            return 1.0
        return 0.0


VOSK_MODEL_DIR = MODELS_DIR / "vosk-model-small-en-us-0.15"


def build_engine():
    if ENGINE == "vosk":
        return VoskEngine(WAKE_WORD, VOSK_MODEL_DIR)


    if ENGINE == "porcupine":
        if not PICOVOICE_KEY:
            raise RuntimeError(
                "AXON_WAKE_ENGINE=porcupine needs PICOVOICE_ACCESS_KEY in .env. "
                "Get one free at console.picovoice.ai"
            )
        return PorcupineEngine(WAKE_WORD, PICOVOICE_KEY)

    if ENGINE != "openwakeword":
        raise RuntimeError(
            f"Unknown AXON_WAKE_ENGINE '{ENGINE}'. Use vosk, openwakeword or porcupine."
        )
    return OpenWakeWordEngine(WAKE_WORD)


# ==========================================================================
def on_wake(debug: bool = False) -> None:
    """
    Woken up. Start Sid (if needed) and open it ready to listen.

    ?listen=1 tells the page to switch the microphone on by itself, so you
    can talk straight through: "Hey Sid... play some music."
    """
    if debug:
        print("  -> waking Sid", flush=True)

    import importlib.util

    spec = importlib.util.spec_from_file_location("sid_launcher", ROOT / "Axon.pyw")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)

    # 1. Make sure the server is up.
    if not launcher.server_responds():
        if not launcher.port_is_open():
            launcher.start_server()
        for _ in range(60):
            if launcher.server_responds():
                break
            time.sleep(0.5)

    # 2. Is a window ACTUALLY open?
    #
    #    We used to ask the server, which counts open /api/events connections.
    #    That was wrong: closing a browser window does not immediately close
    #    its connection - the server only notices when it next tries to write
    #    and the socket fails. So after closing the window the server still
    #    reported subscribers, the listener assumed a window existed, focused
    #    nothing, and returned. Saying "Hey Sid" appeared to do nothing.
    #
    #    So ask the operating system instead. A window either exists or it
    #    doesn't; there is no stale-connection ambiguity in that answer.
    #
    #    Lesson worth keeping: when a cheap check and an authoritative check
    #    disagree, the bug is usually that you trusted the cheap one.
    if sid_window_is_open():
        if debug:
            print("  window is open - telling it to listen", flush=True)
        notify_open_window()
        focus_existing_window()
        return

    # 3. Nothing open, so make one - and only now do we navigate.
    if debug:
        print("  no window open, starting one", flush=True)
    global _opened_at
    from backend.tools.web import open_app_window

    _opened_at = time.time()          # see sid_window_is_open
    open_app_window(f"http://127.0.0.1:{config.PORT}/?listen=1")


def ensure_server_running(debug: bool = False) -> bool:
    """Start the background server if it isn't already up. Opens no window."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("sid_launcher", ROOT / "Axon.pyw")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)

    if launcher.server_responds():
        if debug:
            print("  server already running")
        return True

    if debug:
        print("  starting server in the background...")
    if not launcher.port_is_open():
        launcher.start_server()

    for _ in range(60):
        if launcher.server_responds():
            if debug:
                print("  server ready")
            return True
        time.sleep(0.5)

    if debug:
        print("  server did not come up")
    return False


# When we last asked a window to open. A Chrome window takes a couple of
# seconds to exist and be titled, and during that gap it is invisible to any
# check you can write.
_opened_at = 0.0
WINDOW_APPEAR_GRACE = 12.0


def sid_window_is_open() -> bool:
    """
    Is there a Sid window on screen?

    THE THIRD ANSWER TO THIS QUESTION, and the first one that holds.

    1. Ask the server how many /api/events subscribers it has. Stale: closing
       a window doesn't close its socket until the server next tries to write.

    2. Ask Windows for a window titled 'Sid'. Correct as far as it goes - and
       it still missed, because of what it does NOT cover: **a window that
       has been asked to open but has not appeared yet.** Chrome takes one to
       three seconds. Say "Hey Sid" twice in that gap and the second check
       truthfully answers "no window", so a second one opens. That is how two
       Sid windows ended up stacked.

    So the check is now "a window exists, OR we asked for one recently".
    Time is part of the state, whether or not you model it.

    (The enumeration itself moved to backend/windows.py and walks every
    top-level window rather than one-per-process. That is the more correct
    question to ask, though it was not what caused the duplicates - measured,
    not assumed: the per-process check reported Sid correctly even with other
    Chrome windows open.)
    """
    from backend import windows

    if time.time() - _opened_at < WINDOW_APPEAR_GRACE:
        return True
    return windows.window_exists("Sid", exact=True)


def notify_open_window() -> None:
    """Push a wake event so the open page turns its microphone on."""
    import urllib.request

    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{config.PORT}/api/wake", data=b"", method="POST"
        )
        urllib.request.urlopen(request, timeout=5).close()
    except Exception:
        pass


def focus_existing_window() -> None:
    """
    Bring the Sid window to the front so you can see it heard you.

    Windows deliberately makes stealing focus hard - an app that yanks your
    attention mid-typing is obnoxious - so this may be declined and the
    taskbar button flashes instead. That is the polite fallback, not a bug.
    """
    from backend import windows

    for hwnd in windows.find_windows("Sid", exact=True):
        windows.focus_window_handle(hwnd)
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true",
                        help="print live detection scores")
    parser.add_argument("--threshold", type=float, default=THRESHOLD)
    args = parser.parse_args()

    try:
        engine = build_engine()
    except Exception as exc:
        _log(f"engine failed to start: {exc}")
        print(f"Wake word engine failed to start:\n  {exc}")
        return

    if args.debug:
        print(f"Engine: {ENGINE}   phrase: {WAKE_WORD}   threshold: {args.threshold}")

    # Bring the server up now, quietly, WITHOUT opening a window.
    #
    # The listener starts at boot, so the server is already warm by the time
    # you first say the wake word - no waiting for uvicorn to start. And
    # nothing appears on screen until you actually ask for something, which
    # is what you want from something that's always on.
    ensure_server_running(args.debug)

    # Logged AFTER the model has loaded, so the log distinguishes "started"
    # from "actually able to hear you" — which is the exact gap that made a
    # broken listener look fine.
    _log("engine ready, listening")

    if args.debug:
        print("Say it out loud. Ctrl+C to stop.\n")

    # The audio callback runs on a separate high-priority thread. Do NOT do
    # slow work in it - a late callback means dropped audio, crackling, and
    # missed detections. It only drops frames into a queue; the real work
    # happens on the main thread below.
    frames: queue.Queue = queue.Queue()

    def callback(indata, _frames, _time, status):
        frames.put(bytes(indata))

    last_wake = 0.0

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=engine.frame_length,
        dtype="int16",
        channels=1,
        callback=callback,
    ):
        # How often to re-check the on/off switch. Every 8 frames is about
        # two seconds - responsive enough that flipping the toggle feels
        # instant, rare enough that we're not opening a file 12 times a
        # second for the lifetime of the process.
        CHECK_EVERY = 8
        frame_count = 0
        armed = settings.get("wake_enabled", True)
        if not armed and args.debug:
            print("  wake word is OFF - not listening", flush=True)

        while True:
            raw = frames.get()

            frame_count += 1
            if frame_count % CHECK_EVERY == 0:
                was_armed = armed
                armed = settings.get("wake_enabled", True)
                if armed != was_armed:
                    _log(f"wake word turned {'ON' if armed else 'OFF'}")
                    if args.debug:
                        print(f"  wake word {'ON' if armed else 'OFF'}", flush=True)

            # OFF means we genuinely stop analysing the audio, not "hear it
            # and ignore it". Frames are still pulled off the queue and
            # discarded - if we stopped reading, the queue would grow until
            # the process ran out of memory.
            if not armed:
                continue

            score = engine.process(raw)

            if args.debug and score > 0.05:
                print(f"  {score:5.3f} {'#' * int(score * 40)}", flush=True)

            if score > args.threshold and time.time() - last_wake > COOLDOWN_SECONDS:
                last_wake = time.time()
                if args.debug:
                    print(f"\n  DETECTED ({score:.3f})", flush=True)
                try:
                    on_wake(args.debug)
                except Exception as exc:
                    if args.debug:
                        print(f"  wake failed: {exc}", flush=True)

                # THE COOLDOWN HAS TO BE RESTARTED HERE, not only above.
                #
                # on_wake can take several seconds — it may start the server
                # and wait for a window to appear. Meanwhile the microphone
                # keeps filling the queue and the clock keeps running, so by
                # the time we got back the 4-second cooldown had already
                # expired and every buffered frame fired again. That is what
                # stacked up duplicate Sid windows.
                last_wake = time.time()

                # Throw away everything recorded WHILE we were waking. It is
                # stale by definition, and most of it is the tail of the
                # phrase that just woke us.
                dropped = 0
                try:
                    while True:
                        frames.get_nowait()
                        dropped += 1
                except queue.Empty:
                    pass
                if hasattr(engine, "rec"):
                    engine.rec.Reset()
                if args.debug and dropped:
                    print(f"  dropped {dropped} stale frames", flush=True)


def _log(message: str) -> None:
    """
    Append a line to logs/listener.log.

    WHY THIS EXISTS
    ---------------
    The startup shortcut runs this with pythonw and output discarded, so
    anything printed goes nowhere. That is how a listener ended up ALIVE BUT
    DEAF: it had failed before loading the speech model, sat there using
    40 MB instead of 115 MB, and left no trace anywhere. The only symptom
    was "it stopped working".

    Any process that runs invisibly in the background needs somewhere to say
    what happened to it. Otherwise the first sign of failure is silence, and
    silence is impossible to debug.
    """
    try:
        log_dir = ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(log_dir / "listener.log", "a", encoding="utf-8") as handle:
            handle.write(f"{stamp}  {message}\n")
    except Exception:
        pass          # logging must never be the thing that breaks it


if __name__ == "__main__":
    try:
        _log(f"starting (engine={ENGINE}, phrase={WAKE_WORD!r})")
        main()
    except KeyboardInterrupt:
        _log("stopped by user")
    except Exception as exc:
        import traceback
        _log(f"CRASHED: {type(exc).__name__}: {exc}")
        _log(traceback.format_exc())
        raise
