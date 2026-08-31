/* =========================================================================
   app.js — all the browser-side logic for Sid.

   Read this file top to bottom once. It is the whole client in ~180 lines,
   with no framework, so there is nothing hidden from you.
   ========================================================================= */

// ------------------------------------------------------------------ setup
// Grab the HTML elements we'll be touching. Doing this once at the top is
// faster and cleaner than searching the document every time.
const els = {
  messages: document.getElementById("messages"),
  form:     document.getElementById("composer"),
  input:    document.getElementById("input"),
  send:     document.getElementById("send-btn"),
  clear:    document.getElementById("clear-btn"),
  dot:      document.getElementById("status-dot"),
  mic:      document.getElementById("mic-btn"),
  speak:    document.getElementById("speak-btn"),
  quit:     document.getElementById("quit-btn"),
  brain:    document.getElementById("brain"),
  lang:     document.getElementById("lang"),
  google:   document.getElementById("google-btn"),
  googleLabel: document.getElementById("google-label"),
  orb:      document.getElementById("orb"),
  wake:     document.getElementById("wake-btn"),
  phone:    document.getElementById("phone-btn"),
  pairBox:  document.getElementById("pair-overlay"),
  pairQr:   document.getElementById("pair-qr"),
  pairUrl:  document.getElementById("pair-url"),
  pairNote: document.getElementById("pair-note"),
  pairSub:  document.getElementById("pair-sub"),
  pairBadge: document.getElementById("pair-badge"),
  pairMode: document.getElementById("pair-mode"),
  activity:  document.getElementById("activity-btn"),
  activityDot: document.getElementById("activity-dot"),
  actBox:    document.getElementById("activity-overlay"),
  tabTasks:  document.getElementById("tab-tasks"),
  tabAudit:  document.getElementById("tab-audit"),
  bg:        document.getElementById("bg-btn"),
  stateLabel: document.getElementById("state-label"),
};

// THE CONVERSATION. This array is the single source of truth: it is what
// gets drawn on screen AND what gets sent to the server. One variable,
// two jobs — so they can never disagree with each other.
let messages = [];

// Are we mid-reply? Used to stop you sending two messages at once.
let busy = false;

const STORAGE_KEY = "axon.messages";

// ------------------------------------------------------------ persistence
// localStorage is a tiny key-value store built into every browser (~5MB).
// It survives refreshes and reboots. It is NOT a real database — it lives
// only on this one device, so your phone and laptop have separate history.
// Real shared memory arrives in Phase 4 when we add Postgres.

function save() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(messages));
  } catch (e) {
    // Private-browsing mode can block this. Not worth crashing over.
    console.warn("could not save history", e);
  }
}

function load() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) messages = JSON.parse(raw);
  } catch (e) {
    messages = [];
  }

  // REPAIR anything the server would reject, before it can be sent.
  //
  // Saved history outlives the code that wrote it. A message shape that was
  // fine last week can be rejected today, and because it lives in
  // localStorage it gets re-sent on every single request - so one bad entry
  // breaks the app permanently, surviving reloads and restarts.
  //
  // Anything that persists user data needs a check like this on the way IN.
  const clean = messages.filter(
    (m) => m && (m.role === "user" || m.role === "assistant") &&
           typeof m.content === "string" && m.content.trim()
  );

  if (clean.length !== messages.length) {
    console.warn(`dropped ${messages.length - clean.length} unusable message(s)`);
    messages = clean;
    save();
  }
}

// -------------------------------------------------------------- rendering
function render() {
  els.messages.innerHTML = "";

  if (messages.length === 0) {
    showEmptyState();
    return;
  }

  for (const m of messages) addBubble(m.role, m.content);
  scrollToBottom();
}

function showEmptyState() {
  const box = document.createElement("div");
  box.className = "empty-state";

  const h = document.createElement("h2");
  h.textContent = "Sid online";

  const p = document.createElement("p");
  p.textContent = "Say “Hey Sid”, tap the orb, or just type.";

  box.append(h, p);
  els.messages.appendChild(box);
}

/**
 * Draw one message and hand back its text element, so the streaming code
 * can keep appending characters into it.
 */
function addBubble(role, text) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + role;

  const who = document.createElement("div");
  who.className = "who";
  who.textContent = role === "user" ? "You" : "Sid";

  const body = document.createElement("div");
  body.className = "body";
  // .textContent, never .innerHTML — textContent treats the string as plain
  // text. If we used innerHTML, a message containing a script tag would RUN.
  // That vulnerability is called XSS, and this one choice prevents it.
  body.textContent = text;

  // A column so tool steps can stack ABOVE the reply text, in the order they
  // actually happened. Seeing the work is most of what makes an agent feel
  // trustworthy instead of magic.
  const col = document.createElement("div");
  col.className = "col";
  col.appendChild(body);

  wrap.append(who, col);
  els.messages.appendChild(wrap);
  return body;
}

/**
 * Show "running get_time(...)" above the reply, and hand back the element so
 * it can be updated to a tick once the result arrives.
 */
function addToolStep(body, name, input) {
  const col = body.parentElement;

  let steps = col.querySelector(".steps");
  if (!steps) {
    steps = document.createElement("div");
    steps.className = "steps";
    col.insertBefore(steps, body);          // above the text, not below
  }

  const step = document.createElement("div");
  step.className = "step running";

  const args = Object.values(input || {}).join(", ");
  step.textContent = name + (args ? "(" + args + ")" : "");

  steps.appendChild(step);
  return step;
}

/**
 * Draw the Approve / Reject card for a dangerous tool.
 *
 * The summary shown is the REAL command, never a paraphrase. This card is
 * the last thing between the model and your machine, so anything that makes
 * it easier to approve without reading would be a bug, not a feature.
 */
function showApproval(body, event) {
  const col = body.parentElement;

  const card = document.createElement("div");
  card.className = "approval";
  card.id = "approval-" + event.id;

  const head = document.createElement("div");
  head.className = "approval-head";
  head.textContent = "Allow " + event.tool + "?";

  const detail = document.createElement("pre");
  detail.className = "approval-detail";
  detail.textContent = event.summary;      // textContent, never innerHTML

  const buttons = document.createElement("div");
  buttons.className = "approval-buttons";

  const decide = async (approved) => {
    buttons.querySelectorAll("button").forEach((b) => (b.disabled = true));
    await fetch("/api/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: event.id, approved }),
    }).catch(() => {});
  };

  const no = document.createElement("button");
  no.className = "approval-no";
  no.textContent = "No";
  no.addEventListener("click", () => decide(false));

  const yes = document.createElement("button");
  yes.className = "approval-yes";
  yes.textContent = "Run it";
  yes.addEventListener("click", () => decide(true));

  // "No" first, and it keeps keyboard focus. If you hammer Enter to get
  // through a dialog you weren't reading, the safe option is what fires.
  buttons.append(no, yes);
  card.append(head, detail, buttons);
  col.insertBefore(card, body);

  no.focus();
}

/**
 * Set what Sid appears to be doing.
 *
 * ONE function owns this, and it only sets a class on <body>. Nothing else
 * in the app touches a style. That means the orb, the HUD badge and any
 * future indicator all stay in agreement automatically - there is no way
 * for the orb to say "listening" while the label says "idle", because they
 * are reading the same single fact.
 */
const STATES = {
  idle:      "standby",
  listening: "listening",
  thinking:  "working",
  speaking:  "speaking",
};

function setState(state) {
  for (const name of Object.keys(STATES)) {
    document.body.classList.toggle("state-" + name, name === state);
  }
  els.stateLabel.textContent = STATES[state] || state;
}

/** Shrink the orb out of the way once a conversation exists. */
function refreshLayout() {
  document.body.classList.toggle("has-messages", messages.length > 0);
}

/**
 * Render the plan as levels, so the parallelism is visible.
 *
 * Steps on the same row run AT THE SAME TIME. That's the one thing the
 * picture has to communicate - a flat list would hide exactly what makes a
 * plan different from the old one-at-a-time loop.
 */
function showPlan(body, event) {
  const col = body.parentElement;

  const box = document.createElement("div");
  box.className = "plan";

  const head = document.createElement("div");
  head.className = "plan-head";
  const n = event.steps.length;
  head.textContent =
    `${n} step${n === 1 ? "" : "s"} in ${event.levels} ` +
    `wave${event.levels === 1 ? "" : "s"}`;
  box.appendChild(head);

  // Rebuild the levels client-side from `needs`, the same way the server
  // does: a step belongs to the wave after all of its dependencies.
  const done = new Set();
  let remaining = event.steps.slice();

  while (remaining.length) {
    const wave = remaining.filter((s) => s.needs.every((d) => done.has(d)));
    if (!wave.length) break;                 // cycle; server already rejects these

    const row = document.createElement("div");
    row.className = "plan-wave";

    for (const step of wave) {
      done.add(step.id);
      const chip = document.createElement("div");
      chip.className = "plan-step";
      chip.id = "step-" + step.id;
      chip.textContent = step.tool;
      row.appendChild(chip);
    }

    box.appendChild(row);
    remaining = remaining.filter((s) => !done.has(s.id));
  }

  col.insertBefore(box, body);
}

function scrollToBottom() {
  els.messages.scrollTop = els.messages.scrollHeight;
}

// ---------------------------------------------------------------- sending
async function send(text) {
  if (busy || !text.trim()) return;
  busy = true;
  els.send.disabled = true;
  setState("thinking");

  // 1. Show the user's message and record it.
  if (messages.length === 0) els.messages.innerHTML = "";
  messages.push({ role: "user", content: text });
  refreshLayout();
  addBubble("user", text);
  scrollToBottom();

  // 2. Make an empty bubble for Claude and give it a blinking cursor.
  const body = addBubble("assistant", "");
  body.classList.add("caret");
  let reply = "";
  let pendingStep = null;   // the tool step waiting for its result

  try {
    // 3. POST the WHOLE conversation. Remember: the API is stateless, so
    //    the history has to travel with every single request.
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages }),
    });

    if (!res.ok) {
      // "Server said 422" tells you nothing. FastAPI puts the actual reason
      // in the body, so read it - an error you can act on is worth the four
      // extra lines.
      let detail = "";
      try {
        const body = await res.json();
        detail = Array.isArray(body.detail)
          ? body.detail.map((d) => d.msg + " at " + (d.loc || []).join(".")).join("; ")
          : body.detail || "";
      } catch (err) { /* body wasn't JSON */ }
      throw new Error(`Server error ${res.status}${detail ? ": " + detail : ""}`);
    }

    // 4. READ THE STREAM.
    //    res.body is a ReadableStream of raw bytes. A "reader" lets us pull
    //    it one chunk at a time. TextDecoder turns those bytes into text.
    //
    //    (We can't use the simpler EventSource API here, because EventSource
    //     can only do GET requests and we need to POST the conversation.)
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      // Chunks arrive at arbitrary byte boundaries — one SSE message can be
      // split across two chunks. So we append to a buffer and only process
      // COMPLETE messages, which are the parts separated by a blank line.
      buffer += decoder.decode(value, { stream: true });

      const parts = buffer.split("\n\n");
      buffer = parts.pop();          // last piece may be incomplete — keep it

      for (const part of parts) {
        if (!part.startsWith("data: ")) continue;
        const event = JSON.parse(part.slice(6));   // drop the "data: " prefix

        if (event.type === "text") {
          reply += event.text;
          body.textContent = reply;
          scrollToBottom();

        } else if (event.type === "tool_call") {
          // The model asked for a tool. Show it immediately — the run may
          // take several seconds and silence looks like a hang.
          pendingStep = addToolStep(body, event.name, event.input);
          scrollToBottom();

        } else if (event.type === "tool_result") {
          // Same tool finished. Turn the spinner into a tick and attach the
          // result, so you can see exactly what the model was told.
          if (pendingStep) {
            pendingStep.className = "step done";
            pendingStep.title = event.output;
            pendingStep = null;
          }
          scrollToBottom();

        } else if (event.type === "plan") {
          // Draw the whole plan BEFORE anything runs. That moment - where a
          // plan exists and nothing has happened yet - is the entire point
          // of Phase 5. You can read it, and if it's wrong you know before
          // it acts rather than after.
          showPlan(body, event);
          scrollToBottom();

        } else if (event.type === "step_start") {
          const el = document.getElementById("step-" + event.id);
          if (el) el.className = "plan-step running";

        } else if (event.type === "step_done") {
          const el = document.getElementById("step-" + event.id);
          if (el) {
            el.className = "plan-step " + (event.ok ? "done" : "failed");
            el.title = event.output;
          }

        } else if (event.type === "notice") {
          const note = document.createElement("div");
          note.className = "step";
          note.textContent = event.text;
          body.parentElement.insertBefore(note, body);

        } else if (event.type === "approval_request") {
          // A dangerous tool is waiting on you. Everything is suspended
          // server-side until this resolves, so make it impossible to miss.
          showApproval(body, event);
          scrollToBottom();

        } else if (event.type === "approval_result") {
          const card = document.getElementById("approval-" + event.id);
          if (card) {
            card.className = "approval " + (event.granted ? "granted" : "denied");
            card.querySelector(".approval-buttons").remove();
            const verdict = document.createElement("div");
            verdict.className = "approval-verdict";
            verdict.textContent = event.granted ? "You approved this" : "You declined";
            card.appendChild(verdict);
          }

        } else if (event.type === "error") {
          throw new Error(event.message);

        } else if (event.type === "done") {
          // open DevTools (F12) -> Console to watch cost and step count
          console.log("tokens:", event.usage, "| steps:", event.steps);
        }
      }
    }

    // 5. Store the finished reply so the next turn has context.
    //    Only if there IS one. A tool-only turn produces no text, and saving
    //    an empty message used to poison the whole conversation.
    if (reply.trim()) {
      messages.push({ role: "assistant", content: reply });
      save();
    }

    // Read it out, if the speaker button is on. Deliberately AFTER the
    // stream finishes rather than per-chunk — speaking each fragment as it
    // arrives produces a stuttering robot.
    speak(reply);

  } catch (err) {
    body.parentElement.classList.add("error");
    body.textContent = "Failed: " + err.message;
    // Drop the user message we optimistically added, so a retry isn't
    // polluted by a turn that never actually completed.
    messages.pop();
  } finally {
    body.classList.remove("caret");
    busy = false;
    els.send.disabled = false;
    setState("idle");
    refreshLayout();
    els.input.focus();
  }
}

// ----------------------------------------------------------------- events
els.form.addEventListener("submit", (e) => {
  e.preventDefault();              // stop the browser reloading the page
  const text = els.input.value;
  els.input.value = "";
  els.input.style.height = "auto";

  // Offline? Queue it instead of throwing it away. On a phone this is the
  // difference between "Sid is broken in the lift" and "Sid sent it when I
  // came out". See outboxAdd in the Phase 10 section.
  if (!navigator.onLine && text.trim()) {
    // Clear the empty state first, exactly as send() does. Skipping this
    // left "Sid online — say Hey Sid" sitting above the queued message,
    // which reads as if nothing had been typed at all.
    if (messages.length === 0) els.messages.innerHTML = "";
    addBubble("user", text);
    outboxAdd(text);
    scrollToBottom();
    return;
  }

  send(text);
});

// Enter sends, Shift+Enter makes a new line — but only on desktop, where a
// physical keyboard exists. On phones the Enter key should behave normally.
els.input.addEventListener("keydown", (e) => {
  const isPhone = window.matchMedia("(pointer: coarse)").matches;
  if (e.key === "Enter" && !e.shiftKey && !isPhone) {
    e.preventDefault();
    els.form.requestSubmit();
  }
});

// Grow the textarea as you type, up to the max-height set in CSS.
els.input.addEventListener("input", () => {
  els.input.style.height = "auto";
  els.input.style.height = els.input.scrollHeight + "px";
});

els.clear.addEventListener("click", () => {
  if (messages.length && !confirm("Clear this conversation?")) return;
  messages = [];
  save();
  render();
});

els.quit.addEventListener("click", async () => {
  if (!confirm("Quit Sid? The background server will stop.")) return;
  try {
    await fetch("/api/quit", { method: "POST" });
  } catch (e) {
    /* the connection dying IS the success signal here */
  }
  document.body.innerHTML =
    "<div class=\"empty-state\"><h2>Sid stopped.</h2>" +
    "<p>Double-click the desktop shortcut to start it again.</p></div>";
  setTimeout(() => window.close(), 800);
});

// ----------------------------------------------------------- health check
// Ask the server if it's alive and has an API key, then colour the dot.
/**
 * Show whether Sid is reachable — and, for the local model, whether it's
 * still warming up.
 *
 * Warming takes ~45s the first time (see providers/ollama.py). Telling you
 * that is far better than a green dot and a question that takes a minute:
 * a wait you understand is a completely different experience from a wait
 * you don't.
 */
// Whether Sid's laptop answered the last time we asked. Separate from
// navigator.onLine: your phone can have perfect signal while the machine
// Sid actually runs on is asleep, and those need different wording.
let sidReachable = true;


function checkHealth() {
  fetch("/api/health")
    .then((r) => r.json())
    .then((d) => {
      if (!d.ready) {
        els.dot.className = "dot bad";
        els.dot.title = d.provider + " - " + d.detail;
        return;
      }

      if (d.warming) {
        els.dot.className = "dot warming";
        els.dot.title = "Warming up the local model (~45s, first run only)";
        setTimeout(checkHealth, 3000);       // keep checking until it's hot
        return;
      }

      els.dot.className = "dot ok";
      els.dot.title = d.provider + " - " + d.detail;
      sidReachable = true;
      updateOfflineBanner();
    })
    .catch(() => {
      els.dot.className = "dot bad";
      els.dot.title = "Server unreachable";
      // A tooltip is invisible on a phone - there is nothing to hover. So
      // say it where it can actually be read. Without this, the app looked
      // completely normal while Sid's laptop was switched off, and every
      // message silently went nowhere.
      sidReachable = false;
      updateOfflineBanner();
    });
}

/**
 * Fill the brain selector and let you switch without restarting.
 *
 * Worth having because the tradeoff is real and situational:
 *   gemini - ~2s per question, but it's a cloud service
 *   ollama - private and offline, ~55s on this laptop
 * Which one you want depends on what you're about to ask.
 */
function loadBrains() {
  fetch("/api/providers")
    .then((r) => r.json())
    .then((d) => {
      els.brain.innerHTML = "";
      for (const p of d.providers) {
        const option = document.createElement("option");
        option.value = p.name;
        option.textContent = p.name + (p.ready ? "" : " (not set up)");
        option.disabled = !p.ready;
        option.selected = p.active;
        els.brain.appendChild(option);
      }
    })
    .catch(() => { els.brain.style.display = "none"; });
}

els.brain.addEventListener("change", async () => {
  els.dot.className = "dot warming";
  await fetch("/api/provider", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider: els.brain.value }),
  });
  checkHealth();
});

/**
 * Show whether Google is linked, and let you link or unlink it.
 *
 * Connecting opens Google's own consent page — Sid never sees your password.
 * Note the server refuses this from anywhere but localhost: the flow has to
 * open a browser on the machine Sid runs on, so from your phone it would
 * pop a consent screen nobody is sitting in front of.
 */
function loadConnections() {
  fetch("/api/connections")
    .then((r) => r.json())
    .then((d) => {
      const g = d.google;
      els.google.dataset.connected = g.connected ? "1" : "0";
      els.googleLabel.textContent = g.connected
        ? (g.detail.split("@")[0] || "Google")
        : "Connect Google";
      els.google.title = g.connected
        ? g.detail + " - click to disconnect"
        : g.detail;
    })
    .catch(() => { els.google.style.display = "none"; });
}

els.google.addEventListener("click", async () => {
  const connected = els.google.dataset.connected === "1";

  if (connected) {
    const warning =
      "Disconnect Google?\n\n" +
      "This deletes the tokens Sid has saved. To fully revoke access, " +
      "also visit myaccount.google.com/permissions — deleting your copy " +
      "of a key is not the same as changing the lock.";
    if (!confirm(warning)) return;
    await fetch("/api/disconnect/google", { method: "POST" });
    loadConnections();
    return;
  }

  els.googleLabel.textContent = "Waiting...";
  try {
    const r = await fetch("/api/connect/google", { method: "POST" });
    if (!r.ok) throw new Error((await r.json()).detail);
  } catch (err) {
    alert("Could not connect: " + err.message);
  }
  loadConnections();
});

/**
 * Keep a connection open so the server can push to us.
 *
 * This is what stops "Hey Sid" opening a new window every time. The wake
 * word listener POSTs /api/wake, the server pushes down this connection,
 * and THIS window starts listening. No navigation, so no new window.
 *
 * EventSource (unlike the chat stream) reconnects by itself if the
 * connection drops - which it will, every time the server restarts. That
 * automatic retry is exactly why it's worth using here rather than fetch.
 */
function connectEvents() {
  let stream;
  try {
    stream = new EventSource("/api/events");
  } catch (err) {
    return;                       // no EventSource: wake just opens a window
  }

  stream.onmessage = (message) => {
    let event;
    try {
      event = JSON.parse(message.data);
    } catch (err) {
      return;
    }

    if (event.type === "wake") {
      window.focus();
      if (!busy) startListening();
    }

    // Phase 9: Sid speaking without being spoken to.
    if (event.type === "notification") {
      showNotification(event);
    }
  };

  // Errors are normal here - a server restart closes every stream. Say
  // nothing; the browser retries on its own after a few seconds.
  stream.onerror = () => {};
}

els.orb.addEventListener("click", () => {
  if (busy) return;
  voice.listening ? stopListening() : startListening();
});

/**
 * The wake word switch.
 *
 * This can't just be a variable in the page: the listener is a completely
 * separate process. So the server writes the setting to a file both
 * processes read, and the listener re-checks it about every two seconds.
 *
 * "Off" means the listener stops running audio through the recogniser at
 * all — not "hears you and ignores it". Worth the distinction: an
 * assistant that claims to be off should actually be off.
 */
function loadWakeSetting() {
  fetch("/api/settings")
    .then((r) => r.json())
    .then((s) => setWakeUI(s.wake_enabled))
    .catch(() => { els.wake.style.display = "none"; });
}

function setWakeUI(enabled) {
  els.wake.setAttribute("aria-pressed", String(enabled));
  els.wake.title = enabled
    ? 'Wake word ON - say "Hey Sid" to open. Click to disable.'
    : 'Wake word OFF - it will not listen. Click to enable.';
}

els.wake.addEventListener("click", async () => {
  const next = els.wake.getAttribute("aria-pressed") !== "true";
  setWakeUI(next);                        // respond instantly...
  try {
    const r = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: "wake_enabled", value: next }),
    });
    const s = await r.json();
    setWakeUI(s.wake_enabled);            // ...then correct if the server disagreed
  } catch (err) {
    setWakeUI(!next);                     // couldn't save it: put it back
  }
});

/**
 * Show a QR code for opening Sid on your phone.
 *
 * The server generates the image, because only the server knows the access
 * key. It refuses this request from anywhere but the machine Sid runs on -
 * the response literally contains the password, and pairing is something
 * you do at the screen in front of you.
 *
 * The QR arrives as a PNG data URI and goes into an <img src>, so server
 * output is never treated as markup. Building the SVG inline would work too
 * and would be one more place where injected content could execute.
 */
function showPairing(secure = true) {
  els.pairBox.hidden = false;
  els.pairQr.innerHTML = "";
  els.pairBadge.hidden = true;
  els.pairNote.textContent = "";
  els.pairMode.hidden = true;

  // Starting a tunnel takes several seconds. Saying so beats an empty box —
  // an unexplained pause always reads as "broken".
  els.pairSub.textContent = secure
    ? "Setting up a secure link..."
    : "Scan with your phone camera";
  els.pairUrl.textContent = secure ? "this takes a few seconds" : "generating...";

  fetch("/api/pairing?secure=" + (secure ? 1 : 0))
    .then(async (r) => {
      if (!r.ok) throw new Error((await r.json()).detail || r.status);
      return r.json();
    })
    .then((d) => {
      if (d.qr) {
        const img = document.createElement("img");
        img.src = d.qr;
        img.alt = "Pairing QR code";
        els.pairQr.appendChild(img);
      } else {
        els.pairQr.textContent = "Install segno for a QR: py -m pip install segno";
      }
      els.pairUrl.textContent = d.url;
      els.pairNote.textContent = d.note || "";
      els.pairSub.textContent = "Scan with your phone camera";

      els.pairBadge.hidden = false;
      els.pairBadge.className = "pair-badge " + (d.secure ? "secure" : "insecure");
      els.pairBadge.textContent = d.secure
        ? "HTTPS - voice works, any network"
        : "Wi-Fi only - no microphone";

      // Offer the other mode, so neither choice is a trap.
      els.pairMode.hidden = false;
      els.pairMode.textContent = d.secure
        ? "Use Wi-Fi only instead (stays in your house)"
        : "Try a secure link again";
      els.pairMode.onclick = () => showPairing(!d.secure);

      // A secure link was wanted and couldn't be made: show the LAN QR
      // anyway, and say why. A working Wi-Fi link beats no link.
      if (d.problem) {
        els.pairNote.textContent = `${d.problem}\n\n${d.note || ""}`;
      }
    })
    .catch((err) => {
      els.pairQr.textContent = "";
      els.pairUrl.textContent = String(err.message || err);
      els.pairNote.textContent =
        "Pairing can only be shown on the computer Sid is running on.";
    });
}

function hidePairing() { els.pairBox.hidden = true; }

els.phone.addEventListener("click", showPairing);

document.getElementById("push-btn").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const note = document.getElementById("push-note");
  btn.disabled = true;
  try {
    if (btn.textContent === "Turn off") {
      await disablePush();
    } else {
      const result = await enablePush();
      if (!result.ok) note.textContent = result.why;
    }
  } catch (err) {
    note.textContent = "Couldn't change that: " + err.message;
  } finally {
    btn.disabled = false;
    refreshPushUI();
  }
});
document.getElementById("pair-close").addEventListener("click", hidePairing);

// Click the dark area to dismiss, but not the panel itself.
els.pairBox.addEventListener("click", (e) => {
  if (e.target === els.pairBox) hidePairing();
});

// Escape closes it. Any overlay that traps you is a bad overlay.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !els.pairBox.hidden) hidePairing();
  if (e.key === "Escape" && !els.actBox.hidden) els.actBox.hidden = true;
});

document.getElementById("pair-copy").addEventListener("click", async () => {
  const btn = document.getElementById("pair-copy");
  try {
    await navigator.clipboard.writeText(els.pairUrl.textContent);
    btn.textContent = "Copied";
  } catch (err) {
    btn.textContent = "Copy failed - select it manually";
  }
  setTimeout(() => { btn.textContent = "Copy link"; }, 2000);
});

/* ==================================================================== */
/*  Background tasks and the audit log                                   */
/* ==================================================================== */

/**
 * Send this message to the background instead of waiting for it.
 *
 * The POST returns an id immediately and the connection closes. From that
 * point the work has nothing to do with this browser tab — close it, walk
 * away, check from your phone. That separation IS Phase 6.
 */
async function sendBackground(text) {
  if (!text.trim()) return;
  els.input.value = "";
  els.input.style.height = "auto";

  try {
    const r = await fetch("/api/task", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request: text }),
    });
    const job = await r.json();

    if (messages.length === 0) els.messages.innerHTML = "";
    const body = addBubble("assistant", "");
    body.textContent =
      `Running in the background (task ${job.id}). ` +
      `You can close this — check the Activity panel for the result.`;
    refreshLayout();
    scrollToBottom();
    pollTasks();
  } catch (err) {
    alert("Could not start the task: " + err.message);
  }
}

els.bg.addEventListener("click", () => sendBackground(els.input.value));

function fmtTime(iso) {
  try { return new Date(iso).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}); }
  catch (e) { return ""; }
}

async function renderTasks() {
  const d = await (await fetch("/api/tasks")).json();
  els.tabTasks.innerHTML = "";

  if (!d.tasks.length) {
    els.tabTasks.textContent = "No background tasks yet.";
    return;
  }

  for (const t of d.tasks) {
    const row = document.createElement("div");
    row.className = "row status-" + t.status;

    const head = document.createElement("div");
    head.className = "row-head";
    head.textContent = t.request.slice(0, 70);

    const meta = document.createElement("div");
    meta.className = "row-meta";
    meta.textContent = `${t.status} · ${fmtTime(t.created_at)}`;

    row.append(head, meta);

    if (t.answer || t.error) {
      const out = document.createElement("div");
      out.className = "row-out";
      out.textContent = (t.answer || t.error).slice(0, 240);
      row.appendChild(out);
    }

    // A parked job needs you. Make that the loudest thing on the row —
    // a job silently waiting forever is the failure mode to avoid.
    if (t.status === "needs_approval") {
      const btn = document.createElement("button");
      btn.className = "sheet-btn";
      btn.textContent = "Review and approve";
      btn.onclick = () => openTaskApproval(t.id);
      row.appendChild(btn);
    }

    els.tabTasks.appendChild(row);
  }
}

async function openTaskApproval(taskId) {
  const job = await (await fetch("/api/task/" + taskId)).json();
  const ask = [...job.events].reverse().find((e) => e.type === "approval_request");
  if (!ask) return renderTasks();

  const yes = confirm(
    `This background task wants to run:

${ask.summary}

Allow it?`
  );
  await fetch("/api/approve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: ask.id, approved: yes }),
  });
  setTimeout(renderTasks, 800);
}

/* ==========================================================================
 *  Phase 9 — schedules, and notifications you didn't ask for
 * ======================================================================= */

/**
 * Show a notification card.
 *
 * These arrive unprompted, which is why they DON'T go in the chat log: a
 * 7am briefing appearing under last night's conversation reads as a reply to
 * something you said, and it isn't.
 *
 * They stay until dismissed if they need you, and fade after 12 seconds if
 * they're just information. Anything demanding a decision must not be able
 * to time out while you're away from the laptop.
 */
function showNotification(event) {
  const host = document.getElementById("toast-host");
  if (!host) return;

  const card = document.createElement("div");
  card.className = "app-toast kind-" + (event.kind || "info");

  const title = document.createElement("div");
  title.className = "app-toast-title";
  title.textContent = event.title || "Sid";

  const body = document.createElement("div");
  body.className = "app-toast-body";
  body.textContent = event.body || "";

  const close = document.createElement("button");
  close.className = "app-toast-close";
  close.textContent = "×";
  close.onclick = () => card.remove();

  card.append(close, title, body);

  if (event.kind === "approval" && event.task_id) {
    const btn = document.createElement("button");
    btn.className = "sheet-btn";
    btn.textContent = "Review and approve";
    btn.onclick = () => { card.remove(); openTaskApproval(event.task_id); };
    card.appendChild(btn);
  }

  host.appendChild(card);

  // Only auto-dismiss things that need nothing from you.
  if (event.kind !== "approval") {
    setTimeout(() => card.remove(), 12000);
  }
}

/** Turn a stored trigger into words a person reads without decoding. */
function describeSchedule(t) {
  if (t.kind === "daily")    return `every day at ${t.spec}`;
  if (t.kind === "interval") {
    const n = Number(t.spec);
    return n % 60 === 0 && n >= 60
      ? `every ${n / 60} hour${n === 60 ? "" : "s"}`
      : `every ${n} minutes`;
  }
  if (t.kind === "once")     return "once, at " + fmtTime(t.spec);
  return `${t.kind} ${t.spec}`;
}

async function renderSchedules() {
  const box = document.getElementById("tab-schedules");
  const d = await (await fetch("/api/triggers")).json();
  box.innerHTML = "";

  if (!d.triggers.length) {
    box.textContent =
      'Nothing scheduled. Try: "every morning at 8, check my calendar and ' +
      'only tell me if something needs action".';
    return;
  }

  for (const t of d.triggers) {
    const row = document.createElement("div");
    row.className = "row" + (t.enabled ? "" : " status-off");

    const head = document.createElement("div");
    head.className = "row-head";
    head.textContent = t.name;

    const meta = document.createElement("div");
    meta.className = "row-meta";
    meta.textContent = describeSchedule(t) +
      (t.enabled && t.next_run
        ? ` · next ${new Date(t.next_run).toLocaleString([], {
            weekday: "short", hour: "2-digit", minute: "2-digit" })}`
        : " · paused");

    const what = document.createElement("div");
    what.className = "row-out";
    what.textContent = t.prompt;

    const actions = document.createElement("div");
    actions.className = "row-actions";

    const toggle = document.createElement("button");
    toggle.className = "mini-btn";
    toggle.textContent = t.enabled ? "Pause" : "Resume";
    toggle.onclick = async () => {
      await fetch(`/api/triggers/${t.id}/toggle?enabled=${!t.enabled}`,
                  { method: "POST" });
      renderSchedules();
    };

    const del = document.createElement("button");
    del.className = "mini-btn danger";
    del.textContent = "Delete";
    del.onclick = async () => {
      if (!confirm(`Delete "${t.name}"?`)) return;
      await fetch("/api/triggers/" + t.id, { method: "DELETE" });
      renderSchedules();
    };

    actions.append(toggle, del);
    row.append(head, meta, what, actions);
    box.appendChild(row);
  }
}

async function renderAudit() {
  const d = await (await fetch("/api/audit?limit=80")).json();
  els.tabAudit.innerHTML = "";

  const sum = document.createElement("div");
  sum.className = "row-meta";
  sum.textContent =
    `${d.summary.total} actions logged · ${d.summary.failed} failed · ` +
    `${d.summary.denied} denied`;
  els.tabAudit.appendChild(sum);

  for (const e of d.entries) {
    const row = document.createElement("div");
    row.className = "log-row tier-" + e.tier + (e.ok ? "" : " failed");
    row.textContent =
      `${fmtTime(e.at)}  ${e.tool}` +
      (e.approved ? `  [${e.approved}]` : "") +
      (e.ms != null ? `  ${e.ms}ms` : "");
    row.title = e.result || "";
    els.tabAudit.appendChild(row);
  }
}

/** Badge the button when a task needs you, so it's visible without opening. */
async function pollTasks() {
  try {
    const d = await (await fetch("/api/tasks?limit=10")).json();
    const busy = d.tasks.some((t) =>
      t.status === "needs_approval" || t.status === "running");
    els.activityDot.hidden = !busy;
    els.activityDot.className =
      "activity-dot" +
      (d.tasks.some((t) => t.status === "needs_approval") ? " needs" : "");
    if (!els.actBox.hidden) renderTasks();
  } catch (err) { /* server down; the health dot already says so */ }
}
setInterval(pollTasks, 5000);
pollTasks();

els.activity.addEventListener("click", async () => {
  els.actBox.hidden = false;
  const s = await (await fetch("/api/settings")).json();
  document.getElementById("dry-run-toggle").checked = !!s.dry_run;
  document.getElementById("confirm-act-toggle").checked = !!s.confirm_act;
  refreshPushUI();          // defined in the Phase 10 section below
  renderTasks();
  renderSchedules();
  renderAudit();
});

document.getElementById("activity-close")
  .addEventListener("click", () => { els.actBox.hidden = true; });
els.actBox.addEventListener("click", (e) => {
  if (e.target === els.actBox) els.actBox.hidden = true;
});

for (const [id, key] of [["dry-run-toggle", "dry_run"],
                         ["confirm-act-toggle", "confirm_act"]]) {
  document.getElementById(id).addEventListener("change", (e) => {
    fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, value: e.target.checked }),
    });
  });
}

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    const showing = tab.dataset.tab;
    els.tabTasks.hidden = showing !== "tasks";
    els.tabAudit.hidden = showing !== "audit";
    document.getElementById("tab-schedules").hidden = showing !== "schedules";
    document.getElementById("tab-traces").hidden = showing !== "traces";
    ({ tasks: renderTasks, schedules: renderSchedules,
       traces: renderTraces, audit: renderAudit }[showing])();
  });
}

loadWakeSetting();
connectEvents();
loadBrains();
loadConnections();
checkHealth();
setInterval(checkHealth, 15000);   // so the banner clears by itself

// -------------------------------------------------------- service worker
// Registering this is what makes Sid installable to a home screen.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(console.warn);
}

// ------------------------------------------------------------------ boot
initVoice();      // defined in voice.js
load();
render();
refreshLayout();
setState("idle");

// Opened by the wake-word listener? Then switch the mic on by ourselves, so
// you can talk straight through: "Hey Jarvis... play some music."
//
// The small delay is not superstition: Chrome needs the page painted and the
// mic permission resolved before .start() will succeed.
if (new URLSearchParams(location.search).get("listen") === "1") {
  setTimeout(startListening, 600);
}

/* =========================================================================
   PHASE 10 — the phone stops being a smaller laptop
   ========================================================================= */

/* ---------------------------------------------------- push notifications */

/**
 * Turn a base64url VAPID key into the Uint8Array `subscribe()` demands.
 *
 * This conversion is not optional decoration. `applicationServerKey` refuses
 * anything else, and the error it throws when you pass the string directly
 * says nothing useful about why.
 */
function urlBase64ToUint8Array(base64) {
  const padded = (base64 + "=".repeat((4 - (base64.length % 4)) % 4))
    .replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(padded);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

/**
 * Work out exactly why push can or can't work HERE.
 *
 * Push fails for about six different reasons and, left to itself, every one
 * of them looks identical from the laptop: nothing arrives. So this asks the
 * device directly and returns a reason a person can act on.
 *
 * The iOS case is the one that catches everyone: Safari has no PushManager
 * at all in a normal tab. The site must be added to the Home Screen first,
 * and only then does the API appear. Nothing about that is discoverable —
 * the button simply doesn't work and never says why.
 */
function pushDiagnosis() {
  const ua = navigator.userAgent;
  const isIOS = /iPad|iPhone|iPod/.test(ua) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const installed = window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;

  const d = {
    ua: ua.slice(0, 160),
    isIOS,
    installedToHomeScreen: installed,
    secureContext: window.isSecureContext,
    origin: location.origin,
    hasServiceWorker: "serviceWorker" in navigator,
    hasPushManager: "PushManager" in window,
    hasNotification: "Notification" in window,
    permission: ("Notification" in window) ? Notification.permission : "n/a",
  };

  if (!d.secureContext) {
    d.blocked = "not-https";
    d.fix = "Open Sid using the https QR link, not the Wi-Fi one.";
  } else if (isIOS && !installed) {
    d.blocked = "ios-not-installed";
    d.fix = "On iPhone: Share → Add to Home Screen, then open Sid from " +
            "that icon. Safari tabs cannot receive push at all.";
  } else if (!d.hasPushManager || !d.hasServiceWorker) {
    d.blocked = "no-push-api";
    d.fix = isIOS
      ? "This iOS version is too old for web push (needs 16.4+)."
      : "This browser has no push support. Try Chrome.";
  } else if (d.permission === "denied") {
    d.blocked = "permission-denied";
    d.fix = "Notifications are blocked for this site. Clear it in the " +
            "browser's site settings, then try again.";
  } else {
    d.blocked = null;
    d.fix = "";
  }
  return d;
}

/** Send the report to the laptop, so a human there can read it. */
async function reportPushDiagnosis() {
  const d = pushDiagnosis();
  try {
    await fetch("/api/push/diagnose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(d),
    });
  } catch (err) { /* diagnosing must never break the page */ }
  return d;
}

/**
 * Subscribe this device.
 *
 * Called from a BUTTON, never on load. Asking for notification permission
 * the instant a page opens is how you get permanently denied — browsers
 * remember a "no", and there is no second chance to explain yourself.
 */
async function enablePush() {
  const d = await reportPushDiagnosis();
  if (d.blocked) return { ok: false, why: d.fix };

  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    return { ok: false, why: "You didn't allow notifications. Tap again to retry." };
  }

  try {
    const info = await (await fetch("/api/push/key")).json();
    const registration = await navigator.serviceWorker.ready;

    // Reuse an existing subscription if there is one. Calling subscribe()
    // twice with different keys throws, and re-subscribing needlessly would
    // orphan the old endpoint on the server.
    let subscription = await registration.pushManager.getSubscription();

    // A subscription made under a DIFFERENT VAPID key (or a stale tunnel) is
    // useless — the push service will reject anything Sid signs now. Drop it
    // and make a fresh one rather than reporting a success that can't work.
    if (subscription) {
      const existing = new Uint8Array(subscription.options.applicationServerKey || []);
      const wanted = urlBase64ToUint8Array(info.key);
      const same = existing.length === wanted.length &&
        existing.every((b, i) => b === wanted[i]);
      if (!same) {
        await subscription.unsubscribe();
        subscription = null;
      }
    }

    if (!subscription) {
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,        // required; silent push is not allowed
        applicationServerKey: urlBase64ToUint8Array(info.key),
      });
    }

    const body = subscription.toJSON();
    body.label = (d.isIOS ? "iPhone" : "Android") + (d.installedToHomeScreen ? " (app)" : "");

    const res = await fetch("/api/push/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      return { ok: false, why: `Sid rejected the subscription (HTTP ${res.status}).` };
    }

    return { ok: true, stable: info.stable };
  } catch (err) {
    // Report the REAL error. "Something went wrong" is what made this take
    // three attempts to get nowhere.
    try {
      await fetch("/api/push/diagnose", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...d, subscribeError: String(err).slice(0, 300) }),
      });
    } catch (e) { /* ignore */ }
    return { ok: false, why: String(err).slice(0, 140) };
  }
}

async function disablePush() {
  const registration = await navigator.serviceWorker.ready;
  const subscription = await registration.pushManager.getSubscription();
  if (!subscription) return;
  await fetch("/api/push/unsubscribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ endpoint: subscription.endpoint }),
  });
  await subscription.unsubscribe();
}

/** Reflect the real state of things — never a guess. */
async function refreshPushUI() {
  const row = document.getElementById("push-row");
  const btn = document.getElementById("push-btn");
  const note = document.getElementById("push-note");
  if (!row) return;

  // Report to the laptop every time this panel opens, so the reason is
  // visible from the machine you can actually read logs on.
  const d = await reportPushDiagnosis();

  if (d.blocked) {
    row.hidden = false;
    btn.hidden = true;
    note.textContent = d.fix;
    return;
  }

  row.hidden = false;
  btn.hidden = false;

  const registration = await navigator.serviceWorker.ready;
  const subscription = await registration.pushManager.getSubscription();
  const info = await (await fetch("/api/push/key")).json();

  if (subscription) {
    btn.textContent = "Turn off";
    note.textContent = info.stable
      ? "On. You'll get results here even with Sid closed."
      : "On — until the tunnel restarts. Set AXON_PUBLIC_ORIGIN for a permanent address.";
  } else {
    btn.textContent = "Turn on";
    note.textContent = "Get scheduled results on this device, even with Sid closed.";
  }
}

/* --------------------------------------------------------- share target */

/**
 * Something was shared into Sid from another app.
 *
 * The server stashed the text and redirected here with a one-use token. We
 * drop it into the composer rather than sending it: you shared a link
 * because you want to ask something ABOUT it, and guessing what would be
 * wrong more often than right.
 */
async function collectShared() {
  const params = new URLSearchParams(location.search);
  const token = params.get("shared");
  if (!token) return;

  // Clean the URL immediately, so a refresh doesn't try to re-consume a
  // token the server has already thrown away.
  history.replaceState({}, "", "/");

  try {
    const { text } = await (await fetch("/api/shared/" + token)).json();
    if (!text) return;
    els.input.value = text;
    els.input.focus();
    refreshLayout();
  } catch (err) { /* nothing shared; nothing to do */ }
}

/* ------------------------------------------------------- offline outbox */

/**
 * A message you typed with no signal.
 *
 * Kept in localStorage, not memory: "no signal" and "the OS killed the tab"
 * happen together often enough that memory alone would lose exactly the
 * messages this exists to protect.
 */
const OUTBOX_KEY = "sid-outbox";

function outboxRead() {
  try { return JSON.parse(localStorage.getItem(OUTBOX_KEY) || "[]"); }
  catch (err) { return []; }
}

function outboxWrite(items) {
  try { localStorage.setItem(OUTBOX_KEY, JSON.stringify(items.slice(-10))); }
  catch (err) { /* storage full or blocked; the message is still on screen */ }
}

function outboxAdd(text) {
  outboxWrite([...outboxRead(), { text, at: Date.now() }]);
  updateOfflineBanner();
}

/** Back online: send what's waiting, oldest first. */
async function flushOutbox() {
  const items = outboxRead();
  if (!items.length || busy) return;

  // Clear FIRST, so a failure part-way through can't resend what already
  // went out. Losing one message beats sending it twice.
  outboxWrite([]);
  updateOfflineBanner();

  for (const item of items) {
    await send(item.text);
  }
}

function updateOfflineBanner() {
  const banner = document.getElementById("offline-banner");
  if (!banner) return;
  const queued = outboxRead().length;

  if (!navigator.onLine) {
    banner.hidden = false;
    banner.textContent = queued
      ? "No signal — " + queued + " message" + (queued > 1 ? "s" : "") +
        " will send when you're back"
      : "No signal on this device";
  } else if (!sidReachable) {
    // The important distinction. Sid is not a cloud service: it runs on the
    // laptop. Phone has signal, laptop is asleep -> nothing can happen, and
    // saying "offline" here would blame the wrong machine.
    banner.hidden = false;
    banner.textContent = "Sid isn't running on your laptop — start it there";
  } else if (queued) {
    banner.hidden = false;
    banner.textContent = "Back online — sending…";
  } else {
    banner.hidden = true;
  }
}

window.addEventListener("online", () => { updateOfflineBanner(); flushOutbox(); });
window.addEventListener("offline", updateOfflineBanner);

/* ------------------------------------------- shortcuts and notifications */

/** Long-press the app icon, or tap a notification. */
function applyLaunchParams() {
  const params = new URLSearchParams(location.search);

  if (params.get("focus") === "1") {
    els.input.focus();
  }
  if (params.get("panel")) {
    els.activity.click();
    const tab = document.querySelector('.tab[data-tab="' + params.get("panel") + '"]');
    if (tab) tab.click();
  }
  if (params.get("shared")) return;   // collectShared cleans that one up
  if (params.get("focus") || params.get("panel")) {
    history.replaceState({}, "", "/");
  }
}

// The service worker tells us when a notification was tapped on a window
// that was already open.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.addEventListener("message", (event) => {
    if (!event.data || event.data.type !== "notification-click") return;
    if (event.data.data && event.data.data.kind === "approval") {
      els.activity.click();
      renderTasks();
    }
  });
}

collectShared();
applyLaunchParams();
updateOfflineBanner();
flushOutbox();

/* =========================================================================
   PHASE 11 — traces: why Sid did what it did
   ========================================================================= */

/**
 * One turn, expandable.
 *
 * The Log tab answers "what did it do". This answers "why, and what did it
 * cost". Collapsed you see the shape — mode, time, steps; expanded you get
 * every step with its own timing, which is the number that actually tells
 * you where the seconds went.
 */
async function renderTraces() {
  const box = document.getElementById("tab-traces");
  const data = await (await fetch("/api/traces?limit=40")).json();
  box.innerHTML = "";

  const s = data.summary || {};
  const head = document.createElement("div");
  head.className = "row-meta";
  head.textContent = s.turns
    ? `${s.turns} turns · median ${(s.median_ms / 1000).toFixed(1)}s · ` +
      `slowest ${(s.slowest_ms / 1000).toFixed(1)}s · ${s.failed} failed`
    : "No turns recorded yet.";
  box.appendChild(head);

  if (!data.traces.length) return;

  for (const t of data.traces) {
    const row = document.createElement("div");
    row.className = "row trace-row" + (t.error ? " status-failed" : "");

    const headLine = document.createElement("div");
    headLine.className = "row-head";
    headLine.textContent = t.request.slice(0, 70) || "(empty)";

    const meta = document.createElement("div");
    meta.className = "row-meta";
    const tokens = (t.tokens_in || 0) + (t.tokens_out || 0);
    meta.textContent =
      [
        fmtTime(t.started_at),
        t.mode || "?",
        ((t.ms || 0) / 1000).toFixed(1) + "s",
        t.steps.length + " step" + (t.steps.length === 1 ? "" : "s"),
        tokens ? tokens + " tok" : null,
        t.model || null,
      ].filter(Boolean).join(" · ");

    row.append(headLine, meta);

    // The steps, each with its own time. A turn that took 9 seconds tells
    // you nothing; one step taking 8.4 of them tells you where to look.
    if (t.steps.length) {
      const steps = document.createElement("div");
      steps.className = "trace-steps";
      const slowest = Math.max(...t.steps.map((x) => x.ms || 0), 1);

      for (const step of t.steps) {
        const line = document.createElement("div");
        line.className = "trace-step" + (step.ok === false ? " failed" : "");

        const bar = document.createElement("span");
        bar.className = "trace-bar";
        // Width relative to the slowest step in THIS turn. Absolute scaling
        // would make every step in a fast turn invisible.
        bar.style.width = Math.max(2, ((step.ms || 0) / slowest) * 100) + "%";

        const label = document.createElement("span");
        label.className = "trace-label";
        label.textContent = step.tool + (step.ms != null ? `  ${step.ms}ms` : "");
        label.title = JSON.stringify(step.args || {}) +
          (step.output ? "\n\n" + step.output : "");

        line.append(bar, label);
        steps.appendChild(line);
      }
      row.appendChild(steps);
    }

    const out = document.createElement("div");
    out.className = "row-out";
    out.textContent = (t.error || t.answer || "").slice(0, 200);
    if (out.textContent) row.appendChild(out);

    box.appendChild(row);
  }
}
