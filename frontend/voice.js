/* =========================================================================
   voice.js — talking to Sid, and Sid talking back.

   Two completely separate browser APIs, often confused:

     SpeechRecognition   your voice  -> text     ("speech to text")
     speechSynthesis     text -> spoken audio    ("text to speech")

   Both are BUILT INTO the browser. No library, no API key, no cost. This
   whole file is ~120 lines and gives Sid ears and a mouth.

   TWO THINGS TO KNOW BEFORE YOU TRUST IT
   --------------------------------------
   1. PRIVACY: in Chrome, SpeechRecognition is not done on your machine — the
      audio is sent to Google's servers and text comes back. That's fine for
      "play some music", less fine for dictating something private. Ollama
      keeping your text local doesn't help here; the audio took a different
      road. speechSynthesis (the voice output) IS local.

   2. SECURE CONTEXT: microphone access needs https:// or localhost. So voice
      works on your laptop at localhost:8321, but NOT on your phone at
      http://192.168.x.x:8321 — the browser silently refuses. Fixing that
      needs HTTPS, which is a Phase 10 problem.
   ========================================================================= */

const voice = {
  recognition: null,
  listening: false,
  speakReplies: false,
  blocked: null,      // why the mic can't be used here, if it can't
  primed: false,      // has iOS speech been unlocked by a real tap yet
};

// Chrome/Edge expose it prefixed; the standard name is there for the future.
const SpeechRecognition =
  window.SpeechRecognition || window.webkitSpeechRecognition;

// --------------------------------------------------------------- input
function setupRecognition() {
  if (!SpeechRecognition) return null;

  const rec = new SpeechRecognition();

  rec.lang = localStorage.getItem("sid.lang") || "en-IN";
  rec.continuous = false;      // stop after one utterance, don't run forever
  rec.interimResults = true;   // show words as they're recognised

  // Fires repeatedly: first with rough guesses, finally with the real result.
  rec.onresult = (event) => {
    let text = "";
    let isFinal = false;

    for (const result of event.results) {
      text += result[0].transcript;
      if (result.isFinal) isFinal = true;
    }

    // Show it in the box as you speak, so you can see it's hearing you.
    els.input.value = text;
    els.input.style.height = "auto";
    els.input.style.height = els.input.scrollHeight + "px";

    // Once the sentence is complete, send it. No pressing anything.
    if (isFinal && text.trim()) {
      stopListening();
      els.form.requestSubmit();
    }
  };

  rec.onerror = (event) => {
    stopListening();
    if (event.error === "not-allowed") {
      alert(
        "Microphone blocked.\n\nClick the padlock in the address bar and " +
        "allow the microphone. (On a phone over http:// this can't work — " +
        "browsers require https for the mic.)"
      );
    } else if (event.error === "no-speech") {
      // Extremely common and completely harmless. Say nothing.
    } else {
      console.warn("speech error:", event.error);
    }
  };

  rec.onend = () => stopListening();

  return rec;
}

function startListening() {
  if (!voice.recognition || voice.listening) return;

  // Never listen and talk at the same time — the mic would hear Sid's own
  // voice and transcribe it back as your next message.
  window.speechSynthesis.cancel();

  try {
    voice.recognition.start();
    voice.listening = true;
    els.mic.classList.add("listening");
    els.input.placeholder = "Listening...";
    setState("listening");
  } catch (err) {
    // .start() throws if it's already running. Nothing to do.
  }
}

function stopListening() {
  if (!voice.listening) return;
  voice.listening = false;
  els.mic.classList.remove("listening");
  els.input.placeholder = "Ask Sid anything";
  if (!busy) setState("idle");
  try {
    voice.recognition.stop();
  } catch (err) {
    /* already stopped */
  }
}

// -------------------------------------------------------------- output
/**
 * Read a reply aloud — but clean it up first.
 *
 * Raw replies contain URLs, brackets and markdown. Hearing
 * "h-t-t-p-s colon slash slash w-w-w dot youtube..." is unbearable, so we
 * strip anything that isn't meant to be spoken.
 */
function speak(text) {
  if (!voice.speakReplies || !text.trim()) return;

  const spoken = text
    .replace(/https?:\/\/\S+/g, "")     // drop URLs entirely
    .replace(/[*_`#>]/g, "")            // drop markdown punctuation
    .replace(/\([^)]*\)/g, "")          // drop parenthetical asides
    .replace(/\s+/g, " ")
    .trim();

  if (!spoken) return;

  const lang = localStorage.getItem("sid.lang") || "en-IN";

  const utterance = new SpeechSynthesisUtterance(spoken);
  utterance.lang = lang;
  // Hindi voices sound rushed and slurred at the English rate.
  utterance.rate = lang.startsWith("hi") ? 0.95 : 1.05;

  // Prefer a voice that actually matches the language. Without this the
  // browser often reads Hindi with an English voice, which is unintelligible.
  const match = window.speechSynthesis
    .getVoices()
    .find((v) => v.lang === lang) ||
    window.speechSynthesis
      .getVoices()
      .find((v) => v.lang.startsWith(lang.slice(0, 2)));
  if (match) utterance.voice = match;

  // Show that it's talking, and go back to idle when it stops - including
  // when it's cut off mid-sentence, which is what onerror covers.
  utterance.onstart = () => setState("speaking");
  utterance.onend = () => { if (!busy && !voice.listening) setState("idle"); };
  utterance.onerror = utterance.onend;

  window.speechSynthesis.cancel();     // cut off any previous reply
  window.speechSynthesis.speak(utterance);
}

// --------------------------------------------------------------- wiring
/**
 * Change the language the microphone listens for.
 *
 * IMPORTANT LIMITATION: SpeechRecognition listens for exactly ONE language.
 * There is no "detect automatically" and no "both". Set it to hi-IN and it
 * will hear Hindi well and English badly, and vice versa.
 *
 * In practice en-IN is the better default even for Hinglish — it transcribes
 * Hindi words phonetically in Roman script ("gaana bajao"), which the model
 * understands perfectly well. Switch to hi-IN when you want proper Devanagari
 * or you're speaking full sentences of Hindi.
 *
 * (The MODEL is genuinely bilingual — that's handled in the system prompt,
 * and typing works in either language regardless of this setting. This
 * dropdown only affects the microphone and the spoken replies.)
 */
function setLanguage(code) {
  localStorage.setItem("sid.lang", code);
  if (voice.recognition) voice.recognition.lang = code;

  // Restart if we're mid-listen, or the change won't take effect until the
  // next session.
  if (voice.listening) {
    stopListening();
    setTimeout(startListening, 150);
  }
}

function initVoice() {
  voice.recognition = setupRecognition();

  const savedLang = localStorage.getItem("sid.lang") || "en-IN";
  els.lang.value = savedLang;
  if (voice.recognition) voice.recognition.lang = savedLang;
  els.lang.addEventListener("change", () => setLanguage(els.lang.value));

  // ---- why the mic might not work here -----------------------------
  // Three separate reasons, and each needs a DIFFERENT message. The worst
  // outcome is what we had before: a button that silently does nothing.
  //
  // 1. Not a secure context  -> phone over http://. Fixable: use a tunnel.
  // 2. No SpeechRecognition  -> Firefox, and older iOS Safari. Not fixable.
  // 3. Permission denied     -> handled in rec.onerror.
  const iOS = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
              (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

  if (!window.isSecureContext) {
    // Template literals (backticks) so the line breaks are real characters
    // and there are no escape sequences to get mangled.
    voice.blocked = `The microphone needs a secure connection (https).

You're on http://, so the browser refuses — that's a browser rule, not an
Sid setting.

Fix: on your laptop, run

    py mobile.py --tunnel

and scan the new QR code. That gives you https, and the mic will work.`;
  } else if (!voice.recognition) {
    voice.blocked = iOS
      ? `This version of Safari can't do speech recognition.

Update iOS if you can. Typing still works — including Hindi — and spoken
replies still work.`
      : `This browser has no speech recognition.

Use Chrome or Edge.`;
  }

  if (voice.blocked) {
    els.mic.classList.add("unavailable");
  }

  els.mic.addEventListener("click", () => {
    // iOS refuses to speak unless speech was first triggered by a real tap.
    // So the first time you touch anything, we say an empty utterance to
    // unlock it. Without this, "read replies aloud" silently does nothing
    // on iPhone forever.
    if (iOS && !voice.primed) {
      window.speechSynthesis.speak(new SpeechSynthesisUtterance(""));
      voice.primed = true;
    }

    if (voice.blocked) {
      alert(voice.blocked);
      return;
    }
    voice.listening ? stopListening() : startListening();
  });

  // Remember the speak-aloud setting between sessions.
  voice.speakReplies = localStorage.getItem("sid.speak") === "1";
  els.speak.setAttribute("aria-pressed", String(voice.speakReplies));

  els.speak.addEventListener("click", () => {
    voice.speakReplies = !voice.speakReplies;
    localStorage.setItem("sid.speak", voice.speakReplies ? "1" : "0");
    els.speak.setAttribute("aria-pressed", String(voice.speakReplies));
    if (!voice.speakReplies) window.speechSynthesis.cancel();
  });
}
