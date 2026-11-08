// How the page's sound sits with other apps' (Safari: navigator.audioSession).
//
// Left to itself ("auto"), Safari puts a page whose <audio> has ever played
// (the voice player plays a moment of silence on the first tap, to be
// allowed to speak later) in its "playback" mode: any sound from the page,
// even a Web Audio chime, stops the music or podcast that was on and takes
// the lock screen's player. "ambient" mixes with what's playing and leaves
// the player alone (and, like a game's sound, follows silent mode).
//
// So ccpipe is ambient by default and for its chimes (attention.ts). Claude's
// voice and dictation ask for "auto" as they start, which is how they always
// behaved: a reply in "playback", dictation in "play-and-record" (ambient
// can't record). Other browsers have no audioSession: these do nothing.

type Mode = "ambient" | "auto";

function session(): { type: string } | undefined {
  return (navigator as Navigator & { audioSession?: { type: string } }).audioSession;
}

function set(mode: Mode): void {
  try {
    const s = session();
    if (s && s.type !== mode) s.type = mode;
  } catch { /* not allowed here (a frame): leave it */ }
}

/** The chimes, and the page at rest: mix with other audio. */
export function alertAudio(): void { set("ambient"); }

/** Claude's voice or the microphone is about to start: Safari's choice. */
export function mediaAudio(): void { set("auto"); }

/** What it is now, for the server log. */
export function audioSessionType(): string {
  try { return session()?.type ?? "none"; } catch { return "none"; }
}
