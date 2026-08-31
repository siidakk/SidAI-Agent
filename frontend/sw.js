/* =========================================================================
   sw.js — the Service Worker.

   WHAT IS A SERVICE WORKER?
   A small JavaScript file the browser runs in the BACKGROUND, separate from
   your page. It sits between the page and the network like a proxy: every
   request the page makes passes through it first, and it can answer from a
   cache instead of the internet.

   Two reasons we need one:
     1. A site is only "installable" to a phone home screen if it has a
        manifest AND a service worker with a fetch handler. This file is
        literally the price of admission.
     2. It makes the app open instantly, and show something useful even with
        no signal.

   As of Phase 10 it also receives PUSH NOTIFICATIONS. That is the one job
   only a service worker can do: your page is closed, your phone is in your
   pocket, and this file still runs. It is the difference between an app you
   have to remember to check and one that tells you.
   ========================================================================= */

// Bump this string whenever you change the cached files.
//
// Renaming the app to Sid is exactly such a change, and forgetting this
// line meant the browser kept serving the OLD index.html - the page title
// still said Axon while everything server-side said Sid. If a frontend
// change 'didn't apply', check this constant before anything else. Because the name
// changes, a brand-new cache is created and the old one gets deleted in the
// "activate" step below. This is how you avoid the classic PWA bug where
// users are stuck on a stale version forever.
const CACHE = "sid-v12";  // bumped: says when the laptop is unreachable

// The minimum set of files needed to show the interface.
const SHELL = [
  "/",
  "/style.css",
  "/app.js",
  "/voice.js",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
];

// ---- install: runs once when the worker is first registered -------------
self.addEventListener("install", (event) => {
  // waitUntil says "don't consider me installed until this promise finishes".
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL))
  );
  // Don't sit around waiting for old tabs to close — take over immediately.
  self.skipWaiting();
});

// ---- activate: clean up caches from previous versions -------------------
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names.filter((n) => n !== CACHE).map((n) => caches.delete(n))
      )
    )
  );
  self.clients.claim();
});

// ---- fetch: intercept every network request the page makes --------------
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // NEVER cache API calls. A cached chat reply would be worse than useless —
  // you would ask a new question and get yesterday's answer. Let these go
  // straight to the network, untouched.
  if (url.pathname.startsWith("/api/")) return;

  // Only handle simple GETs for our own origin.
  if (event.request.method !== "GET" || url.origin !== self.location.origin) {
    return;
  }

  // Strategy: "network first, fall back to cache".
  // Try the internet so you always get the freshest file; if the network is
  // down, serve the last copy we saved. (The opposite strategy, cache-first,
  // is faster but makes development maddening — you edit a file and the
  // browser keeps showing the old one.)
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();   // a response body can only be read once
        caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});


/* =========================================================================
   PUSH — Phase 10
   ========================================================================= */

/* A push message arrived. The phone woke this worker up specifically to
   handle it; the page may not exist at all.

   The payload is encrypted end-to-end: Google's push service carried it but
   could not read it. Decryption happens transparently in `event.data`. */
self.addEventListener("push", (event) => {
  let data = { title: "Sid", body: "", kind: "info" };
  try {
    if (event.data) data = { ...data, ...event.data.json() };
  } catch (err) {
    // A push with no JSON body still deserves to be shown, rather than
    // silently dropped because we couldn't parse something.
    if (event.data) data.body = event.data.text();
  }

  // requireInteraction: an approval must NOT vanish on its own. You might be
  // looking at your phone ten minutes later, and a decision that quietly
  // timed out while you weren't watching is the failure this avoids.
  event.waitUntil(
    self.registration.showNotification(data.title || "Sid", {
      body: data.body || "",
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-192.png",
      tag: data.task_id || data.kind || "sid",
      requireInteraction: data.kind === "approval",
      vibrate: data.kind === "approval" ? [120, 60, 120] : [80],
      data: data,
    })
  );
});

/* Tapping the notification should land you on the RIGHT screen, and reuse a
   window you already have open rather than stacking up new ones. */
self.addEventListener("notificationclick", (event) => {
  event.notification.close();

  const target = event.notification.data && event.notification.data.kind === "approval"
    ? "/?panel=tasks"
    : "/";

  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if ("focus" in client) {
          // Already open: focus it and tell the page where to go. Opening a
          // second window instead is the bug that made "Hey Sid" spawn a new
          // window every time — same mistake, different door.
          client.postMessage({ type: "notification-click", data: event.notification.data });
          return client.focus();
        }
      }
      return clients.openWindow(target);
    })
  );
});
