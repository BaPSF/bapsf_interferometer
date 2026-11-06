// The page's one WebSocket (docs/ARCHITECTURE.md D18). It never reconnects: once closed, for any reason, the page stays
// as it is and asks for a reload, which opens a fresh socket and refills the history. Timers, clock and document come in
// through `env`, so tests/test_interf_web.py can drive it in node with fake ones.
(function (root) {
  "use strict";

  const HIDDEN_PAUSE_MS = 15 * 60 * 1000;  // a tab hidden or minimized this long gives its viewer slot back
  // The server sends a status frame every second; this long without one is a dead link. Only checked while visible:
  // a hidden tab's frames can be held back.
  const FRAME_TIMEOUT_MS = 60 * 1000;
  const DISCONNECTED = "Disconnected - reload the page to reconnect";
  const PAUSED = "Paused - reload to resume";

  // env: url, WebSocket, document (hidden, addEventListener, removeEventListener), timers (setTimeout, clearTimeout,
  // setInterval, clearInterval; window in the page), now() in ms, onShot(ArrayBuffer), onStatus(object),
  // onClosed(note, reason): reason is the server's close reason, "" if none. Returns nothing: only the session closes it.
  function openSession(env) {
    const socket = new env.WebSocket(env.url);
    socket.binaryType = "arraybuffer";
    let note = null, lastFrame = env.now(), hiddenTimer = null;

    const close = text => {
      if (note === null) note = text;
      socket.close();
    };
    const watchdog = env.timers.setInterval(() => {
      if (!env.document.hidden && env.now() - lastFrame >= FRAME_TIMEOUT_MS) close(DISCONNECTED);
    }, 1000);
    // No mouse or keyboard counts: a visible tab never times out, however long it is watched.
    const onVisibility = () => {
      env.timers.clearTimeout(hiddenTimer);
      if (env.document.hidden) hiddenTimer = env.timers.setTimeout(() => close(PAUSED), HIDDEN_PAUSE_MS);
      else lastFrame = env.now();
    };
    env.document.addEventListener("visibilitychange", onVisibility);
    onVisibility();

    socket.onmessage = event => {
      lastFrame = env.now();
      if (typeof event.data === "string") env.onStatus(JSON.parse(event.data));
      else env.onShot(event.data);
    };
    socket.onclose = event => {
      env.timers.clearInterval(watchdog);
      env.timers.clearTimeout(hiddenTimer);
      env.document.removeEventListener("visibilitychange", onVisibility);
      env.onClosed(note ?? DISCONNECTED, event.reason);
    };
  }

  const api = {openSession, HIDDEN_PAUSE_MS, FRAME_TIMEOUT_MS, DISCONNECTED, PAUSED};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.InterfSession = api;
})(this);
