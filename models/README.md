# models/

Offline models live here. Nothing in this folder is committed to git.

## What's here now

`vosk-model-small-en-us-0.15/` — 71 MB English speech model, used by the
default wake word engine. Get it with:

    py setup_wake_word.py

## Changing the wake phrase

With the default (vosk) engine there is nothing to train. Edit `.env`:

    AXON_WAKE_WORD=hey sid

Any phrase works — "computer", "hey buddy", "okay sid". Check it:

    py setup_wake_word.py --test

## The other two engines (optional)

You only need these if the default isn't accurate enough for you.

### openWakeWord (.onnx) — free, ~1 hour

A true purpose-built wake-word net, ~1 MB. Six phrases ship built in
(`hey_jarvis`, `alexa`, `hey_mycroft`, `hey_rhasspy`). A custom one means
training:

https://colab.research.google.com/github/dscripka/openWakeWord/blob/main/notebooks/automatic_model_training.ipynb

Type `hey sid` as the phrase, run all cells, download the `.onnx`, save it
here, then:

    AXON_WAKE_ENGINE=openwakeword
    AXON_WAKE_WORD=hey_sid.onnx

### Porcupine (.ppn) — best accuracy, needs an account

console.picovoice.ai generates a custom phrase in about a minute. **Their
signup asks for a company email**, which is what ruled it out here.

    AXON_WAKE_ENGINE=porcupine
    AXON_WAKE_WORD=Hey-Sid_en_windows_v3_0_0.ppn
    PICOVOICE_ACCESS_KEY=your-key

`.ppn` files are platform-specific — download the **Windows** one. A Linux
file fails to load and the error doesn't say why.
