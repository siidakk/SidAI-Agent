"""
voice_session.py — a whole conversation with no window open.

THE GAP THIS FILLS
------------------
Pressing the hotkey used to publish a "wake" event, and that event's only
job was to tell an **already-open Sid web page** to switch its microphone
on. With no page open, the glow lit up and absolutely nothing else
happened - which is exactly what it looked like.

The speech recognition lived in the browser, so no browser meant no ears.

This moves the whole turn out of the browser:

    record  →  transcribe  →  ask Sid  →  speak the answer

All of it local except the model call. Nothing opens, nothing steals focus.
You press a key, say a thing, and it happens.

WHY VOSK AND SAPI
-----------------
Both were already on the machine and both are free and offline:

  * **vosk** is already here for the wake word. The wake word hands it a
    *grammar* - a list of the only four phrases it may output. Leave the
    grammar out and the same model does general transcription.
  * **Windows SAPI** (`System.Speech`) ships with Windows. No install, no
    key, no network, and it works with Sid's window closed.

Neither is as good as a cloud service. Both are good enough, cost nothing,
and keep your voice on your own machine - which for an always-listening
assistant matters more than the last few percent of accuracy.

KNOWING WHEN YOU HAVE STOPPED TALKING
-------------------------------------
There is no button to press, so it has to work that out. It waits for you
to start (so a moment of hesitation doesn't count as an empty request),
then ends the turn after about a second of quiet.

Tuned on the generous side deliberately: cutting someone off mid-sentence
is far more annoying than waiting an extra half second.
"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backend import config  # noqa: E402

SAMPLE_RATE = 16000
BLOCK = 2000                  # ~8 frames a second

# Silence detection. RMS on int16 audio, so roughly 0-32768.
#
# Measured on this machine: a quiet room sits at ~4, with occasional
# transients to ~290 (a keystroke, a fan). Ordinary speech is thousands.
# 700 clears the transients with room to spare and is still far below
# anything a person says.
SPEECH_LEVEL = 700            # above this counts as talking

# ...and it has to stay above it for two blocks running. One loud click
# was enough to start a recording otherwise, which then transcribed to
# nothing and looked like a failure rather than a stray noise.
BLOCKS_TO_START = 2
SILENCE_TO_END = 1.1          # seconds of quiet that ends the turn
WAIT_FOR_SPEECH = 4.0         # give up if nothing is said at all
MAX_TURN = 18.0               # hard ceiling, whatever happens

_model = None

# The currently-speaking process, and a counter that says which turn is the
# live one. Both exist so a turn can be interrupted - see cancel().
_tts = None
_tts_lock = __import__("threading").Lock()
_generation = 0


def _get_model():
    """Load the speech model once; it takes a couple of seconds."""
    global _model
    if _model is None:
        from vosk import Model, SetLogLevel
        SetLogLevel(-1)
        _model = Model(str(ROOT / "models" / "vosk-model-small-en-us-0.15"))
    return _model


def record_until_silence(on_level=None) -> bytes:
    """
    Record from the default microphone until the talking stops.

    Returns raw 16-bit mono PCM, or b"" if nothing was said.
    """
    import sounddevice as sd

    frames: list[bytes] = []
    chunks: queue.Queue = queue.Queue()

    def callback(indata, _frames, _time, _status):
        chunks.put(bytes(indata))

    started = False
    loud_run = 0
    quiet_for = 0.0
    began = time.time()

    with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=BLOCK,
                           dtype="int16", channels=1, callback=callback):
        while True:
            try:
                block = chunks.get(timeout=0.5)
            except queue.Empty:
                if time.time() - began > MAX_TURN:
                    break
                continue

            samples = np.frombuffer(block, dtype=np.int16).astype(np.float32)
            level = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
            if on_level:
                on_level(level)

            seconds = len(samples) / SAMPLE_RATE

            if level > SPEECH_LEVEL:
                loud_run += 1
                if loud_run >= BLOCKS_TO_START:
                    started = True
                quiet_for = 0.0
                frames.append(block)
            elif started:
                # Keep the quiet tail: trailing silence helps the recogniser
                # decide a word has actually finished.
                frames.append(block)
                quiet_for += seconds
                if quiet_for >= SILENCE_TO_END:
                    break
            else:
                # Nothing said yet. Don't record the room, and forget any
                # single stray thump that didn't turn into speech.
                loud_run = 0
                frames.clear()
                if time.time() - began > WAIT_FOR_SPEECH:
                    break

            if time.time() - began > MAX_TURN:
                break

    return b"".join(frames) if started else b""


def record_while(is_held, on_level=None) -> bytes:
    """
    Record for exactly as long as the key is held down.

    PUSH-TO-TALK, rather than guessing when you stopped.

    Silence detection is a heuristic and it is always wrong sometimes: it
    cuts you off when you pause to think, and it keeps recording when a fan
    kicks in. Holding a key removes the guess entirely - the recording
    starts when you press and ends when you let go, which is the one thing
    the machine can know for certain.

    `is_held` is called each block and returns True while the key is down.
    """
    import sounddevice as sd

    frames: list[bytes] = []
    chunks: queue.Queue = queue.Queue()

    def callback(indata, _frames, _time, _status):
        chunks.put(bytes(indata))

    began = time.time()
    with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=BLOCK,
                           dtype="int16", channels=1, callback=callback):
        while True:
            if not is_held():
                break
            if time.time() - began > MAX_TURN:
                break
            try:
                block = chunks.get(timeout=0.15)
            except queue.Empty:
                continue
            frames.append(block)
            if on_level is not None:
                samples = np.frombuffer(block, dtype=np.int16).astype(np.float32)
                on_level(float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0)

        # Drain whatever the callback captured between the key coming up and
        # the stream closing - that tail usually holds the last word.
        while True:
            try:
                frames.append(chunks.get_nowait())
            except queue.Empty:
                break

    # Too short to be speech. A tap of the key is not a request.
    if len(frames) < 3:
        return b""
    return b"".join(frames)


def run_turn_held(is_held, on_state=None) -> dict:
    """
    One push-to-talk exchange.

    `on_state` gets single words - "listening", "thinking", "working",
    "speaking" - because that is all a caption on a screen edge should ever
    say. Anything longer is a sentence you have to stop and read.
    """
    def state(name):
        if on_state:
            try:
                on_state(name)
            except Exception:
                pass

    mine = generation()

    def superseded() -> bool:
        """Has a newer press taken over while this turn was working?"""
        return generation() != mine

    state("listening")
    try:
        audio = record_while(is_held)
    except Exception as exc:
        state("error")
        return {"ok": False, "why": f"microphone: {exc}"}

    if superseded():
        return {"ok": False, "why": "interrupted"}

    if not audio:
        state("error")
        return {"ok": False, "why": "held too briefly"}

    state("thinking")
    heard = transcribe(audio)
    if not heard:
        state("error")
        speak("Sorry, I didn't catch that.")
        return {"ok": False, "why": "no words made out"}

    # SHOW WHAT IT HEARD, briefly.
    #
    # The whole hands-free path was reported as useless, and the reason was
    # not that Sid did the wrong thing - it was that there was no way to
    # SEE it had misheard. "open chrome" became "open my grown that", Sid
    # did something odd, and the agent looked stupid rather than deaf.
    #
    # One glance at the words it captured turns a baffling result into an
    # obvious one, and tells you whether to blame the ears or the brain.
    state(f'"{heard[:46]}"')

    if superseded():
        return {"ok": False, "heard": heard, "why": "interrupted"}

    state("working")
    try:
        answer = ask_sid(heard)
    except Exception as exc:
        state("error")
        return {"ok": False, "heard": heard, "why": f"Sid: {exc}"}

    # A model call cannot be taken back, so the answer may arrive after you
    # have already started asking something else. Checking here is what
    # stops Sid talking over your new question with a stale one.
    if superseded():
        return {"ok": False, "heard": heard, "answer": answer,
                "why": "interrupted"}

    state("speaking")
    speak(answer)
    return {"ok": True, "heard": heard, "answer": answer}


def _to_wav(pcm: bytes) -> bytes:
    """Wrap raw 16-bit mono PCM in a WAV header."""
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return buffer.getvalue()


def transcribe_local(audio: bytes) -> str:
    """Vosk. Offline, instant, and only as good as its 40 MB model."""
    if not audio:
        return ""
    from vosk import KaldiRecognizer

    # No grammar here, unlike the wake word - this has to handle any
    # sentence, not four fixed phrases.
    rec = KaldiRecognizer(_get_model(), SAMPLE_RATE)
    rec.SetWords(False)

    step = 4000
    for i in range(0, len(audio), step):
        rec.AcceptWaveform(audio[i:i + step])
    return json.loads(rec.FinalResult()).get("text", "").strip()


def transcribe_cloud(audio: bytes) -> str:
    """
    Gemini. Slower by about two seconds, and right.

    WHY THIS HAD TO CHANGE
    ----------------------
    Vosk was the only recogniser here, and on real microphone audio it was
    producing this:

        "play low fade by ... on youtube"  ->  "laidlaw fade by better noise on you tube"
        "what did I ask you to remember"   ->  "what what did it at and you to remember"
        "open chrome"                      ->  "open my grown that"

    Sid then did its best with the garbage, which is why the whole
    hands-free path felt useless while the app - which uses the browser's
    cloud recogniser - felt perfect. **It was never the agent. It was deaf.**

    Tested on clean synthesised speech vosk is fine, so the model is not
    hopeless in the abstract; it falls apart on a real room, a real mic and
    an Indian-English accent, none of which its American training data
    covers. Gemini got all three test phrases exactly right, Hinglish
    included.

    Two seconds for an instruction that actually works beats instant
    nonsense. Fast and wrong is not a trade, it is just wrong.
    """
    import base64
    import urllib.request

    from backend import config

    if not config.GEMINI_API_KEY:
        raise RuntimeError("no Gemini key")

    payload = {"contents": [{"role": "user", "parts": [
        {"text": "Transcribe this audio exactly, in Roman script. "
                 "The speaker is an Indian English speaker. ASSUME ENGLISH. "
                 "Only write a Hindi word where one was genuinely spoken and "
                 "no English word was - do not 'correct' accented English "
                 "into Hindi, which makes Sid answer in the wrong language. "
                 "Reply with ONLY the words spoken - no commentary, no "
                 "punctuation notes, nothing else."},
        {"inline_data": {"mime_type": "audio/wav",
                         "data": base64.b64encode(_to_wav(audio)).decode()}},
    ]}]}

    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config.GEMINI_MODEL}:generateContent",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "x-goog-api-key": config.GEMINI_API_KEY})

    data = json.loads(urllib.request.urlopen(req, timeout=60).read())
    text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    # It sometimes wraps the answer in quotes despite being told not to.
    return text.strip().strip('"').strip()


def transcribe(audio: bytes) -> str:
    """
    Cloud first, local as the safety net.

    Gemini is far better on real speech, but it needs the network and a
    quota that can run out. When it cannot answer, vosk still can - badly,
    but badly beats not at all, and the failure is then visible in the
    answer rather than silent.
    """
    if not audio:
        return ""
    try:
        heard = transcribe_cloud(audio)
        if heard:
            return heard
    except Exception:
        pass
    return transcribe_local(audio)


# The SAME conversation the app uses.
#
# This was "voice", and that one word made the two halves of Sid into two
# different assistants. The app had 349 messages of history; the key had
# its own 64. Ask something by voice, then open the app, and it had no idea
# what you had just said - because as far as the server was concerned, a
# different person was talking.
#
# One conversation means one memory, one thread, one assistant that happens
# to have two doors.
APP_CONVERSATION = "default"


def ask_sid(text: str, conversation: str = APP_CONVERSATION) -> str:
    """Send it to Sid and collect the reply off the stream."""
    body = {"messages": [{"role": "user", "content": text}],
            "conversation": conversation}
    req = urllib.request.Request(
        f"http://127.0.0.1:{config.PORT}/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})

    _announce("user", text)

    reply: list[str] = []
    with urllib.request.urlopen(req, timeout=240) as stream:
        for raw in stream:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except Exception:
                continue
            if event.get("type") == "text":
                reply.append(event["text"])

    answer = "".join(reply).strip()
    _announce("assistant", answer)
    return answer


def _announce(role: str, text: str) -> None:
    """
    Tell any open Sid window what just happened by voice.

    Without this, a turn spoken into the hotkey never appeared on screen,
    so the two doors into Sid still *looked* like different assistants even
    once they shared a conversation.
    """
    if not text:
        return
    try:
        body = json.dumps({"role": role, "text": text}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{config.PORT}/api/voice-turn",
            data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).close()
    except Exception:
        pass          # a window not being open is the normal case


def speak(text: str) -> None:
    """
    Say it out loud, through Windows' own voice.

    The text goes via a FILE rather than inline in the command. Speaking
    back whatever Sid just said means arbitrary text - quotes, apostrophes,
    newlines, `$` - and inlining that into a shell command is both fragile
    and a genuine injection risk when the words can come from a web page
    Sid just read.
    """
    if not text.strip():
        return

    # Trim: spoken aloud, a long answer is unbearable. The full text is on
    # screen anyway if a window is open.
    spoken = text.strip()
    if len(spoken) > 600:
        spoken = spoken[:600].rsplit(".", 1)[0] + "."

    global _tts

    handle, path = tempfile.mkstemp(suffix=".txt", text=True)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(spoken)

        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$s.Rate = 1; "
            f"$s.Speak([IO.File]::ReadAllText('{path}', "
            "[Text.Encoding]::UTF8))")

        # Popen, not run(). run() blocks until the sentence finishes, and
        # there is then no handle to kill - so pressing the key mid-answer
        # could do nothing but wait politely for Sid to stop talking.
        # Interrupting something is only possible if you kept hold of it.
        with _tts_lock:
            _tts = subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        proc = _tts
        try:
            proc.wait(timeout=120)
        except Exception:
            pass
    except Exception:
        pass
    finally:
        with _tts_lock:
            _tts = None
        try:
            os.unlink(path)
        except Exception:
            pass


def stop_speaking() -> bool:
    """Cut Sid off mid-sentence. Returns whether anything was talking."""
    global _tts
    with _tts_lock:
        proc = _tts
        _tts = None
    if proc is None or proc.poll() is not None:
        return False
    try:
        proc.kill()
        return True
    except Exception:
        return False


def cancel() -> bool:
    """
    Abandon whatever is in flight so a new request can start immediately.

    Bumps a generation counter as well as killing the speech. The old turn
    may still be waiting on a model call it cannot take back; the counter
    is how it learns, when that finally returns, that nobody is waiting for
    the answer any more and it should quietly stop rather than speak over
    whatever is happening now.
    """
    global _generation
    with _tts_lock:
        _generation += 1
    return stop_speaking()


def generation() -> int:
    return _generation


def run_turn(on_state=None) -> dict:
    """
    One complete spoken exchange.

    `on_state` is called with "listening" / "thinking" / "speaking" /
    "error" so the glow can follow along.
    """
    def state(name):
        if on_state:
            try:
                on_state(name)
            except Exception:
                pass

    state("listening")
    try:
        audio = record_until_silence()
    except Exception as exc:
        state("error")
        return {"ok": False, "why": f"microphone: {exc}"}

    # SAY SOMETHING WHEN IT FAILS.
    #
    # A failed turn used to be complete silence, which is indistinguishable
    # from the feature being broken - and that is exactly how it was
    # reported: "the glow comes but it doesn't do anything".
    if not audio:
        state("error")
        speak("I didn't hear anything.")
        return {"ok": False, "why": "nothing was said"}

    heard = transcribe(audio)
    if not heard:
        state("error")
        speak("Sorry, I didn't catch that.")
        return {"ok": False, "why": "could not make out any words"}

    state("thinking")
    try:
        answer = ask_sid(heard)
    except Exception as exc:
        state("error")
        return {"ok": False, "heard": heard, "why": f"Sid: {exc}"}

    state("speaking")
    speak(answer)
    return {"ok": True, "heard": heard, "answer": answer}


if __name__ == "__main__":
    # py voice_session.py   - one turn, out loud, no window
    print("Speak now...", flush=True)
    result = run_turn(lambda s: print(f"  [{s}]", flush=True))
    print(json.dumps(result, ensure_ascii=False, indent=2))
