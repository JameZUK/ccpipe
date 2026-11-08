// What the page's sound is doing, for the server log: Safari's audio
// session (navigator.audioSession: active, inactive, interrupted), the
// page going behind other apps and back. The iPad is where the trouble
// is, and the log is the only view of it.
//
// ccpipe doesn't set the session's type. "ambient" for the chimes (so they
// mix with music) made the iPad pause the music at random (user,
// 2026-11-08, c54c41b): while a page sets a type Safari never lets go of
// the page's audio (WebKit's MediaSessionManagerInterface::
// maybeDeactivateAudioSession), and each switch back to "auto" for
// Claude's voice re-applied "playback" to audio that was already running.
// Left at "auto", Safari treats Web Audio alone as ambient anyway; what
// ccpipe can do is keep its audio short (attention.ts runs the chime's
// context only while it plays).

/** An iPad or iPhone (an iPad's Safari says it's a Mac, but with touch). */
export const APPLE_TOUCH = typeof navigator !== "undefined"
  && (/iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1));

type Session = EventTarget & { type?: string; state?: string };

function session(): Session | undefined {
  try {
    return (navigator as Navigator & { audioSession?: Session }).audioSession;
  } catch {
    return undefined;
  }
}

/** "auto/active", or "none" where there's no audioSession (not Safari). */
export function audioSessionType(): string {
  try {
    const s = session();
    return s ? `${s.type ?? "?"}/${s.state ?? "?"}` : "none";
  } catch {
    return "none";
  }
}

/** Report changes to `log` while a session page is open. Returns an undo. */
export function watchAudio(log: (msg: string) => void): () => void {
  const s = session();
  const say = (msg: string) => { try { log(msg); } catch { /* socket gone */ } };
  const onState = () => say(`audio session ${audioSessionType()}`);
  const onVisible = () => say(`page ${document.hidden ? "hidden" : "shown"} (audio session ${audioSessionType()})`);
  s?.addEventListener("statechange", onState);
  document.addEventListener("visibilitychange", onVisible);
  const standalone = matchMedia("(display-mode: standalone)").matches
    || (navigator as Navigator & { standalone?: boolean }).standalone === true;
  const ua = navigator.userAgent;
  const version = ua.match(/Version\/[\d.]+/)?.[0] ?? ua.match(/(Firefox|Chrome)\/[\d.]+/)?.[0] ?? "?";
  say(`page open: ${standalone ? "home-screen app" : "browser tab"}, ${APPLE_TOUCH ? "iPad/iPhone" : "other"}, `
    + `${version}, audio session ${audioSessionType()}`);
  return () => {
    s?.removeEventListener("statechange", onState);
    document.removeEventListener("visibilitychange", onVisible);
  };
}
