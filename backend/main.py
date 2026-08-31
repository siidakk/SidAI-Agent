"""
main.py  —  the web server. This is the front door of Sid.

WHAT A WEB SERVER ACTUALLY IS
-----------------------------
A program that sits and listens on a port (here: 8000). When someone sends it
an HTTP request ("GET /" or "POST /api/chat"), it runs a function and sends
back a response. That's genuinely all it is.

We use FastAPI, which lets us say "when this URL is requested, run this
function" by writing a decorator (@app.get / @app.post) above the function.
"""

import asyncio
import json
import os
import secrets
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (approvals, audit, auth, config, events, jobs, llm, memory,
               notify, push, settings, tools, traces, triggers, tunnel)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs once when the server starts, and once when it stops.

    We use it to PRIME the local model. Reading the tool schemas for the first
    time costs ~46 seconds on this laptop, and without this that bill lands on
    your first question. Doing it here means it happens while you're still
    looking at the UI.

    create_task, not await: the server must start accepting requests
    immediately. Blocking startup for a minute would just move the wait.
    """
    # Make sure an access key exists before we accept a single request.
    auth.ensure_key()

    # Create the tables if they don't exist yet.
    memory.init()
    audit.init()
    jobs.init()
    triggers.init()
    push.init()
    traces.init()

    # Start the scheduler. This is what lets Sid act without being
    # asked - see triggers.py.
    triggers.start_scheduler()

    if config.WARMUP and config.PROVIDER == "ollama":
        from .providers import ollama
        asyncio.create_task(ollama.warmup(tools.schemas()))

    yield          # <- the server runs for as long as this is suspended here

    # Shutting down: take our child process with us. A tunnel left running
    # after Sid exits would keep a public URL pointing at a dead port - and
    # would quietly hold an ngrok session slot.
    events.begin_shutdown()
    triggers.stop_scheduler()
    tunnel.stop()


app = FastAPI(title="Sid", version="0.3.0", lifespan=lifespan)

# THE LOCK. Registered before any route is declared, so nothing can be added
# later that accidentally sits outside it. Requests from this machine pass
# straight through; anything from the network needs the key.
app.middleware("http")(auth.middleware)


# ==========================================================================
#  REQUEST SHAPES
# ==========================================================================
# These classes describe what a valid request looks like. FastAPI checks
# incoming JSON against them automatically and returns a clear 422 error if
# the browser sends something malformed — so our code below can assume the
# data is already valid. This is called "validation at the boundary".

class Message(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")  # only these two allowed

    # NOT min_length=1, and that was a real bug.
    #
    # An assistant turn can legitimately have no text: when the model only
    # asks for a tool and says nothing, `reply` is "". The browser saved that
    # empty message into its history, and from then on EVERY request was
    # rejected with a 422 - permanently, because the bad message lived in
    # localStorage and got re-sent every time. One empty string bricked the
    # conversation until storage was cleared.
    #
    # The lesson: validate what would actually break something. "Assistant
    # said nothing" breaks nothing. Being strict for its own sake turned a
    # non-event into a dead end.
    content: str = Field(max_length=100_000)


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1)

    # Which conversation this belongs to. The browser generates one and keeps
    # it, so the server-side log is grouped the same way the user sees it.
    # Defaults to "default" so a client that doesn't send one still works.
    conversation: str = Field(default="default", max_length=64)


# ==========================================================================
#  ROUTES
# ==========================================================================

@app.get("/api/health")
async def health():
    """
    A "is the server alive, and is the AI reachable?" endpoint. Every real
    service has one. Visit /api/health in a browser to check.

    It asks the ACTIVE PROVIDER whether it's ready, so the answer differs
    depending on whether you're running Ollama, Gemini or Claude.
    """
    status = await llm.check()
    return {"ok": True, **status}


class ProviderChoice(BaseModel):
    provider: str


@app.get("/api/providers")
async def list_providers():
    """Every brain Sid can use, and whether each is currently usable."""
    from . import providers as registry

    out = []
    for name, module in registry.REGISTRY.items():
        try:
            status = await module.check()
        except Exception as exc:
            status = {"ready": False, "detail": str(exc)[:100]}
        out.append({"name": name, "active": name == config.PROVIDER, **status})
    return {"providers": out}


@app.post("/api/provider")
async def set_provider(choice: ProviderChoice):
    """
    Switch brains without restarting.

    Speed and privacy genuinely trade off here — the local model keeps
    everything on your laptop but takes ~55s a question on this hardware,
    while the cloud model answers in ~2s. Which one you want depends on what
    you're asking, so it should be one click, not a file edit and a restart.

    This works because config.PROVIDER is just a module-level variable and
    llm.py re-reads it on every request. Nothing caches it.
    """
    from . import providers as registry

    if choice.provider not in registry.REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider. Options: {', '.join(registry.REGISTRY)}",
        )

    config.PROVIDER = choice.provider

    # Write it down. Without this the choice lives only in memory, and a
    # switch to the slow local model looks like an unexplained slowdown with
    # .env still claiming something else.
    config.persist("AXON_PROVIDER", choice.provider)

    # Moving to the local model? Start warming it immediately, in the
    # background, so the first question isn't the 60-second one.
    if choice.provider == "ollama" and config.WARMUP:
        from .providers import ollama
        if not ollama.warm:
            asyncio.create_task(ollama.warmup(tools.schemas()))

    return await llm.check()


class ApprovalDecision(BaseModel):
    id: str
    approved: bool


@app.post("/api/approve")
async def approve(decision: ApprovalDecision):
    """
    Answer a pending approval.

    The chat request is currently suspended inside `approvals.wait_for`,
    awaiting an asyncio.Event. Setting it wakes that request up and the tool
    either runs or doesn't. This is a SEPARATE request from the chat stream -
    the stream is busy being a stream, so the answer has to come back on its
    own connection.
    """
    if not approvals.resolve(decision.id, decision.approved):
        raise HTTPException(
            status_code=404,
            detail="That approval expired or was already answered.",
        )
    return {"ok": True, "approved": decision.approved}


class SettingChange(BaseModel):
    key: str
    value: bool


@app.get("/api/pairing")
async def pairing(request: Request, secure: int = 1):
    """
    A QR code for pairing your phone, generated on demand.

    LOCALHOST ONLY. This response contains the access key in plain text -
    it IS the password. The auth middleware already gates every request, so
    anyone reaching this endpoint has the key anyway, but requiring you to
    be sitting at the machine matches how pairing actually works: you scan
    a code on the screen in front of you.
    """
    want_secure = bool(secure)

    if request.client.host not in auth.LOCAL_HOSTS:
        raise HTTPException(
            status_code=403,
            detail="The pairing code can only be shown on the computer Sid runs on.",
        )

    import socket

    # Ask the routing table which interface would be used to reach the
    # internet. More reliable than gethostbyname(), which often returns
    # 127.0.0.1 or a stale VPN address. Nothing is actually sent - UDP is
    # connectionless.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        lan_ip = sock.getsockname()[0]
    except Exception:
        lan_ip = "127.0.0.1"
    finally:
        sock.close()

    key = auth.ensure_key()

    # Prefer HTTPS. It's the difference between a working microphone on your
    # phone and a button that silently does nothing, so it's worth the few
    # seconds of setup - see NOTES/mobile.md.
    #
    # `secure=0` skips it for a fast Wi-Fi-only link that keeps all traffic
    # inside your house.
    base = f"http://{lan_ip}:{config.PORT}"
    secure = False
    problem = None

    if want_secure:
        result = await tunnel.start()
        if result.get("url"):
            base, secure = result["url"], True
        else:
            problem = result.get("error")

    url = f"{base}/?key={key}"

    try:
        import segno
        # A PNG data URI rather than inline SVG: it drops straight into an
        # <img src>, so the browser never has to treat server output as
        # markup. One less place where injected content could execute.
        qr = segno.make(url, error="m").png_data_uri(scale=6, border=3,
                                                    dark="#04070e", light="#ffffff")
    except ImportError:
        qr = None

    return {
        "url": url,
        "qr": qr,
        "lan_ip": lan_ip,
        "port": config.PORT,
        # The phone will be on http://, which browsers refuse to give a
        # microphone. Say so here rather than letting them discover a dead
        # mic button - see NOTES/mobile.md.
        "secure": secure,
        "note": (
            "Works from anywhere, and the microphone works because it's https."
            if secure else
            "Wi-Fi only, and the microphone will NOT work over http."
        ),
        # Only set when a secure link was wanted but couldn't be made. The UI
        # shows the LAN QR anyway - a working Wi-Fi link beats no link.
        "problem": problem,
    }


@app.get("/api/settings")
async def get_settings():
    """Runtime switches, e.g. whether the wake word is armed."""
    return settings.all()


@app.post("/api/settings")
async def set_setting(change: SettingChange):
    """
    Flip a switch.

    The listener is a SEPARATE process, so this writes to a file both
    processes read rather than setting a variable here — a variable in this
    process would be invisible to it.
    """
    if change.key not in settings.DEFAULTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown setting. Valid: {', '.join(settings.DEFAULTS)}",
        )
    return settings.set(change.key, change.value)


class TaskRequest(BaseModel):
    request: str = Field(min_length=1, max_length=8000)


@app.post("/api/task")
async def create_task(req: TaskRequest):
    """
    Start work in the background and return immediately.

    This is the whole point of Phase 6: the request that STARTS the work is
    no longer the request that WAITS for it. Close the laptop, check from
    your phone.
    """
    job_id = jobs.start(req.request)
    return {"id": job_id, "status": "queued"}


@app.get("/api/tasks")
async def list_tasks(limit: int = 25):
    return {"tasks": jobs.recent(limit)}


@app.get("/api/task/{job_id}")
async def get_task(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No task with that id.")
    return job


@app.post("/api/task/{job_id}/cancel")
async def cancel_task(job_id: str):
    return {"cancelled": jobs.cancel(job_id)}


class TriggerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: str = Field(pattern="^(daily|interval|once)$")
    spec: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=4000)
    notify: bool = True


@app.get("/api/triggers")
async def list_triggers():
    """Everything Sid is set to do on its own."""
    return {"triggers": triggers.all_triggers()}


@app.post("/api/triggers")
async def add_trigger(req: TriggerRequest):
    try:
        return triggers.create(req.name, req.kind, req.spec, req.prompt, req.notify)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/triggers/{trigger_id}")
async def remove_trigger(trigger_id: str):
    if not triggers.delete(trigger_id):
        raise HTTPException(status_code=404, detail="No trigger with that id.")
    return {"deleted": trigger_id}


@app.post("/api/triggers/{trigger_id}/toggle")
async def toggle_trigger(trigger_id: str, enabled: bool = True):
    if not triggers.set_enabled(trigger_id, enabled):
        raise HTTPException(status_code=404, detail="No trigger with that id.")
    return triggers.get(trigger_id)


@app.get("/api/audit")
async def audit_log(limit: int = 100, tool: str | None = None,
                    task_id: str | None = None):
    """
    Everything Sid has actually done. Append-only - there is no delete here.

    A log you can edit isn't evidence, and this is the only thing that can
    answer "what did it do while I was asleep?"
    """
    return {
        "summary": audit.summary(),
        "entries": audit.recent(limit, tool=tool, task_id=task_id),
    }


@app.get("/api/memory")
async def memory_list():
    """Everything Sid remembers. Being able to see it is what makes it trustable."""
    return {"stats": memory.count(), "facts": memory.all_facts(limit=200)}


@app.delete("/api/memory/{fact_id}")
async def memory_forget(fact_id: int):
    if not memory.forget(fact_id):
        raise HTTPException(status_code=404, detail="No memory with that id.")
    return {"forgotten": fact_id}


@app.get("/api/history/{conversation}")
async def conversation_history(conversation: str, limit: int = 50):
    """Server-side history, so any device can pick up where another left off."""
    return {
        "conversation": conversation,
        "messages": memory.history(conversation, limit),
    }


@app.get("/api/events")
async def event_stream():
    """
    A connection the page keeps open so the server can push to it.

    Used by the wake word: saying "Hey Sid" tells an already-open window to
    start listening, instead of opening another one.
    """
    async def stream():
        queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        events.SUBSCRIBERS.add(queue)
        try:
            # SSE messages end with a BLANK line — that is the delimiter.
            # See NOTES/phase-1.md §7; miss one newline and nothing arrives.
            END = "\n\n"

            yield 'data: {"type": "connected"}' + END
            while not events.SHUTTING_DOWN:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=10)
                    yield f"data: {json.dumps(event)}" + END
                except asyncio.TimeoutError:
                    # A comment line every 10 seconds, doing two jobs.
                    #
                    # It stops the browser or a proxy closing an idle
                    # connection — and it's also how a CLOSED window gets
                    # detected. Writing to a socket whose browser has gone
                    # raises, which runs the `finally` below and drops the
                    # subscriber. With no periodic write, a closed window
                    # lingers in the count indefinitely, which is exactly
                    # what made "Hey Sid" stop working after closing it.
                    yield ": keepalive" + END
        finally:
            # Runs when the page closes or navigates away. Without it,
            # SUBSCRIBERS grows forever with dead queues.
            events.SUBSCRIBERS.discard(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/wake")
async def wake():
    """
    Tell any open window to start listening. Called by listener.py.

    Returns how many pages heard it, so the listener knows whether it can
    reuse an existing window or has to open one.
    """
    delivered = events.publish({"type": "wake"})
    return {"ok": True, "delivered": delivered}


@app.get("/api/connections")
async def connections():
    """Which outside accounts Sid is linked to. Never returns any token."""
    from . import google_auth
    return {"google": google_auth.status()}


@app.post("/api/connect/google")
async def connect_google(request: Request):
    """
    Start the Google consent flow.

    LOCALHOST ONLY, and not for the same reason as /api/quit. This flow opens
    a browser window and starts a temporary listener on this machine — from
    your phone it would pop a consent screen on your laptop that nobody is
    sitting at. It cannot work remotely, so we refuse clearly rather than
    appearing to hang.
    """
    if request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(
            status_code=403,
            detail="Connecting an account has to be done on the computer Sid runs on.",
        )

    from . import google_auth

    # run_local_server BLOCKS until you click Allow. Running it directly would
    # freeze the whole server — including the page you're clicking on. So it
    # goes to a worker thread and the endpoint returns immediately.
    loop = asyncio.get_running_loop()
    try:
        email = await loop.run_in_executor(None, google_auth.connect)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:300])

    return {"connected": True, "email": email}


@app.post("/api/disconnect/google")
async def disconnect_google(request: Request):
    """Forget the stored tokens. Does NOT revoke access at Google's end."""
    if request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="Localhost only.")

    from . import google_auth
    removed = google_auth.disconnect()
    return {
        "disconnected": removed,
        "note": "Tokens deleted locally. To fully revoke, visit "
                "myaccount.google.com/permissions",
    }


@app.post("/api/quit")
async def quit_sid(request: Request):
    """
    Stop the background server.

    Sid now runs hidden (no console window), so without this the only way to
    stop it is Task Manager. An app you can't quit isn't an app.

    ⚠️ WHY THE LOCALHOST CHECK MATTERS
    We bind to 0.0.0.0 so your phone can reach Sid — which also means anyone
    else on the same Wi-Fi can. Without this check, a stranger in a cafe could
    POST to /api/quit and kill your assistant. So: only requests originating
    from this machine may shut it down.

    This is the first taste of Phase 7's whole problem. The moment something
    is reachable, "who is asking?" becomes a question you must answer.
    """
    if request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(
            status_code=403,
            detail="Sid can only be shut down from the computer it runs on.",
        )

    def shutdown():
        # Give the HTTP response ~0.4s to actually reach the browser. Exiting
        # immediately would drop the connection and look like a crash.
        import time
        time.sleep(0.4)
        os._exit(0)

    threading.Thread(target=shutdown, daemon=True).start()
    return {"stopping": True}


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """
    The main endpoint. Takes the conversation, streams back Claude's reply.

    WHY STREAM INSTEAD OF JUST RETURNING TEXT?
    A full reply can take 20 seconds. If we waited for all of it, the user
    would stare at nothing and assume the app is broken. Streaming shows
    words as they're generated, which *feels* about 10x faster even though
    the total time is identical.

    HOW WE STREAM: Server-Sent Events (SSE)
    A normal response is "here is the body, goodbye". SSE keeps the
    connection open and pushes lines in this exact format:

        data: {"type":"text","text":"Hel"}\n\n
        data: {"type":"text","text":"lo"}\n\n
        data: {"type":"done","usage":{...}}\n\n

    Each message is the literal word `data: `, then JSON, then TWO newlines.
    The two newlines are the delimiter — miss one and nothing works.
    """
    # .model_dump() turns our Pydantic objects back into plain dicts,
    # because that is what the Anthropic SDK expects.
    messages = [m.model_dump() for m in req.messages]

    # Log the user's message server-side. This is what makes history follow
    # you between devices — the browser's localStorage becomes a cache of
    # what THIS machine has seen, rather than the only copy in existence.
    if messages and messages[-1]["role"] == "user":
        memory.log_message(req.conversation, "user", messages[-1]["content"])

    async def event_stream():
        reply_parts: list[str] = []

        # Phase 11: record the whole turn. The trace WATCHES the same events
        # the browser gets rather than being told separately what happened,
        # so it cannot drift out of step with what actually ran.
        trace = traces.Trace(
            messages[-1]["content"] if messages else "", req.conversation)

        try:
            async for event in llm.stream_reply(messages):
                if event["type"] == "text":
                    reply_parts.append(event["text"])
                trace.observe(event)
                yield f"data: {json.dumps(event)}\n\n"

            # Log the reply only once it completed. Doing this in `finally`
            # would also record half-written replies from a request the user
            # cancelled, which is worse than not recording them at all.
            reply = "".join(reply_parts).strip()
            if reply:
                memory.log_message(req.conversation, "assistant", reply)
        except Exception as exc:
            # If Claude errors (bad key, no credit, rate limit) we must still
            # tell the browser something, or it will hang forever waiting.
            # We send an error EVENT rather than an HTTP error code, because
            # the response already started — the status code is long gone.
            trace.observe({"type": "error", "message": str(exc)})
            payload = {"type": "error", "message": str(exc)}
            yield f"data: {json.dumps(payload)}\n\n"
        finally:
            # In `finally` here, unlike the message log above: a turn the user
            # cancelled halfway is exactly the kind you want a trace of.
            trace.save()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",     # never cache a live stream
            "X-Accel-Buffering": "no",       # tell proxies to not hold chunks
        },
    )


# ==========================================================================
#  SERVING THE FRONTEND
# ==========================================================================
# The same Python process that answers /api/chat also hands out the HTML,
# CSS and JS. One server, one port, one command to start. No CORS problems,
# because the page and the API share an origin.
#
# NOTE: this mount must be declared LAST. FastAPI matches routes top to
# bottom, and mounting "/" catches everything — declared earlier, it would
# swallow /api/chat before that route was ever considered.

@app.get("/sw.js")
async def service_worker():
    """
    The service worker gets its own route because browsers refuse to let a
    worker control the whole site unless it is served from the site's root
    with a JavaScript content type. A detail that costs people hours.
    """
    path = config.FRONTEND_DIR / "sw.js"
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/javascript")


# ==========================================================================
#  PHASE 11 — traces
# ==========================================================================

@app.get("/api/traces")
async def list_traces(limit: int = 50):
    """Recent turns, newest first, with the headline numbers."""
    return {"traces": traces.recent(min(limit, 200)), "summary": traces.summary()}


@app.get("/api/traces/{trace_id}")
async def get_trace(trace_id: str):
    trace = traces.get(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="No trace with that id.")
    return trace


# ==========================================================================
#  PHASE 10 — push notifications, and sharing into Sid
# ==========================================================================

class PushSubscription(BaseModel):
    endpoint: str = Field(min_length=8, max_length=1000)
    keys: dict = {}
    label: str = Field(default="", max_length=80)


@app.get("/api/push/key")
async def push_key():
    """
    The VAPID public key, plus whether push can work right now.

    `origin` matters: a subscription is bound to the URL that created it, and
    Sid's phone URL changes with every tunnel. The page needs to know which
    origin it is subscribing under so it can notice when that changes.
    """
    return {
        "key": push.public_key(),
        "origin": push.current_origin(),
        "stable": bool(config.PUBLIC_ORIGIN),
        "devices": len(push.all_subscriptions()),
    }


@app.post("/api/push/subscribe")
async def push_subscribe(sub: PushSubscription, request: Request):
    """Remember a phone so Sid can reach it with the app closed."""
    # The origin the browser actually used, not the one we hope it used.
    # Trusting our own guess here would store a subscription under an origin
    # that never created it, and it would fail silently forever.
    origin = (request.headers.get("origin")
              or push.current_origin()
              or str(request.base_url).rstrip("/"))
    push.save(sub.model_dump(), origin.rstrip("/"), sub.label)
    return {"ok": True, "devices": len(push.all_subscriptions())}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(sub: PushSubscription):
    return {"removed": push.remove(sub.endpoint)}


# What the last phone reported about itself. In memory, one slot: this is a
# debugging aid, not a feature.
#
# WHY IT EXISTS: push failed three times on a real phone and the laptop had
# no way to see why. The phone is the only thing that knows whether it has a
# PushManager, whether it is installed to the home screen, or whether
# permission was denied - and none of that reaches the server on its own.
# Guessing across a device boundary wastes far more time than asking.
_PUSH_DIAGNOSIS: dict = {}


@app.post("/api/push/diagnose")
async def push_diagnose(report: dict):
    _PUSH_DIAGNOSIS.clear()
    _PUSH_DIAGNOSIS.update(report)
    _PUSH_DIAGNOSIS["received_at"] = __import__("datetime").datetime.now().isoformat()
    return {"ok": True}


@app.get("/api/push/diagnose")
async def push_diagnosis():
    return _PUSH_DIAGNOSIS or {"empty": "no device has reported yet"}


@app.post("/api/push/test")
async def push_test():
    """Send a real push, so you can prove it works before relying on it."""
    return push.send("Sid", "Push is working. You'll get scheduled results here.",
                     kind="info")


@app.post("/share")
async def share_target(request: Request):
    """
    Android's share sheet posts here.

    THE FLOW: you're in any app, you hit Share, you pick Sid. Android POSTs
    the shared text/URL here as a form. We stash it and REDIRECT to the page
    with a marker, because a POST response can't become the app's UI - the
    browser would show a bare response body, not Sid.

    Redirecting to a GET is the standard fix (POST/redirect/GET), and it also
    means reloading the page doesn't re-share the same thing.
    """
    from fastapi.responses import RedirectResponse

    form = await request.form()
    parts = [str(form.get(k) or "").strip() for k in ("title", "text", "url")]
    shared = "\n".join(p for p in parts if p)[:4000]

    if not shared:
        return RedirectResponse("/", status_code=303)

    token = secrets.token_urlsafe(8)
    _SHARED[token] = shared
    # Keep the stash small. These are consumed within seconds.
    while len(_SHARED) > 20:
        _SHARED.pop(next(iter(_SHARED)))
    return RedirectResponse(f"/?shared={token}", status_code=303)


# Shared text waiting to be picked up, keyed by a one-use token. In memory
# rather than a table: it lives for the two seconds between the POST and the
# page loading, and losing it on restart costs nothing.
_SHARED: dict[str, str] = {}


@app.get("/api/shared/{token}")
async def get_shared(token: str):
    """Hand the shared text to the page, once."""
    return {"text": _SHARED.pop(token, "")}


@app.get("/manifest.webmanifest")
async def manifest():
    """
    Explicit for the same reason: Windows has no registered MIME type for
    .webmanifest, so the static handler would label it octet-stream and the
    browser would silently ignore it — meaning no install prompt, and hours
    of wondering why.
    """
    path = config.FRONTEND_DIR / "manifest.webmanifest"
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/manifest+json")


class FreshStaticFiles(StaticFiles):
    """
    Static files that never go stale.

    THE BUG THIS FIXES
    ------------------
    Browsers cache CSS and JS hard. Rewriting style.css and reloading showed
    the OLD stylesheet - the page had 68 rules loaded while the server was
    serving a file with the orb in it. No service worker involved; just the
    plain HTTP cache. Every frontend change would have needed a manual
    hard-refresh, and "did my change apply?" is a miserable thing to have to
    wonder on every edit.

    `no-cache` does NOT mean "don't cache". It means "cache it, but always
    ask before reusing it". The browser sends the ETag, and an unchanged
    file comes back as a 304 with no body - so this costs one tiny
    round trip and never serves something stale.

    (`no-store` would be the "really don't cache" option, and would be
    wasteful here: it re-downloads the file every single time.)
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app.mount(
    "/",
    FreshStaticFiles(directory=config.FRONTEND_DIR, html=True),
    name="frontend",
)
