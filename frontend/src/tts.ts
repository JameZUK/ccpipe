// TTS player: collects per-utterance audio chunks streamed from the
// backend and plays them sequentially via a single persistent
// HTMLAudioElement. The element's audio is routed through a Web Audio
// AnalyserNode so the UI can draw a realtime scope while playback runs
// (so the user can tell "yes, sound is actually leaving the speaker"
// vs "my system is muted").
//
// Per-utterance lifecycle:
//   ws → onStart()       buffer reset
//   ws → onChunk(bytes)  buffered
//   ws → onEnd()         blob assembled, queued, audio.play() if idle
//   audio "playing"      → onPlaybackStart fires
//   audio "ended"        → next queued utterance plays, or onPlaybackEnd
//
// Browser autoplay quirks: createMediaElementSource binds the element to
// Web Audio permanently and the element goes silent unless the graph is
// connected to ctx.destination — or while the AudioContext is suspended.
//
// Safari (iPad, iPhone) is the strict one: it plays sound a page starts by
// itself only on an element a tap has already played, and it resumes an
// AudioContext only inside a tap (and suspends it again when the app goes
// to the background). The element and context used to be made lazily, on
// the first reply, outside any tap, so on the iPad replies played
// "sometimes" (2026-11-03). Now the first tap or key press unlocks both,
// and every tap after that resumes the context if Safari suspended it.
// Audio the browser refuses isn't dropped: it waits, and the next tap
// plays it (onBlockedChange lets the page say so). The visualiser's
// analyser is only made inside a tap; until then audio plays without it.
//
// On an iPad or iPhone it is never made. There Safari started the context
// suspended even inside the tap, played the routed element for 0.0 s
// (the analyser's loudest sample: 0) and then marked the context
// "interrupted" — the player's own reports from the user's iPad,
// 2026-11-03. The bare element plays fine; the visualiser stays hidden.
const APPLE_TOUCH = typeof navigator !== "undefined"
  && (/iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1));

type Queued = { blob: Blob; at: number };

// Audio refused for longer than this is dropped rather than played late.
const KEEP_BLOCKED_MS = 3 * 60_000;
// Events that count as the user's own action for autoplay.
const GESTURES = ["pointerup", "touchend", "click", "keydown"] as const;


import { loadTtsMuted, saveTtsMuted } from "./display-prefs";
import * as wakeLock from "./wake-lock";
import { silence } from "./silence";

export class TtsPlayer {
  // Per-utterance chunk buffer (between onStart and onEnd).
  private current: Uint8Array[] = [];
  // Completed blobs waiting to play.
  private queue: Queued[] = [];
  // A tap has played the element (Safari's condition for playing later).
  private unlocked = false;
  // Playing the unlocking silence: its events aren't a reply playing.
  private priming = false;
  // Between deciding to play and play() (waiting on the context).
  private starting = false;
  // The browser refused to play; the head of the queue waits for a tap.
  private blocked = false;
  // A play() not yet settled. One tap fires pointerup, touchend and click;
  // without this a second of them could start another play over the
  // first, and Safari aborts the first.
  private playPending = false;
  // Numbers each play(); the outcome of one superseded by a newer play
  // (its src replaced) is ignored.
  private playSeq = 0;

  private audio: HTMLAudioElement | null = null;
  private audioCtx: AudioContext | null = null;
  private analyser: AnalyserNode | null = null;
  private playingUrl: string | null = null;
  // Mute state is keyed per session (tmux session name) so muting one
  // conversation doesn't silence another tab. Falls back to the global
  // pref for sessions you haven't muted explicitly.
  private readonly session: string | undefined;
  private muted: boolean;
  private mimeType = "audio/mpeg";  // Kokoro default
  // Set when the user mutes mid-utterance. Subsequent chunks and the
  // closing onEnd for the in-flight utterance are discarded so that
  // unmuting mid-stream doesn't queue a truncated blob that plays
  // starting from whichever chunk arrived after unmute.
  private discardCurrentUtterance = false;

  // Bound handlers stored as fields so dispose() can remove them. Closures
  // attached via addEventListener anonymously stay reachable from the
  // audio element until GC, which transitively keeps `this` (the player)
  // alive longer than necessary.
  private readonly onAudioPlaying = () => { if (!this.priming) this.handlePlaybackStart(); };
  private readonly onAudioPause = () => { if (!this.priming) this.handlePlaybackEnd(); };
  private readonly onAudioEnded = () => {
    if (this.priming) return;
    this.handlePlaybackEnd();
    this.pumpQueue();
  };
  private readonly onAudioError = () => {
    if (this.priming) return;
    const e = this.audio?.error;
    this.debug(`media error ${e?.code ?? "?"} ${e?.message ?? ""}`);
    this.handlePlaybackEnd();
    this.pumpQueue();
  };

  // Fired when actual playback (sound leaving the speakers) starts and
  // ends. Consumers use these to drive UI affordances like a visualiser.
  onPlaybackStart: (() => void) | null = null;
  onPlaybackEnd: (() => void) | null = null;
  // Fired when the user toggles mute. The shell wires this to a
  // ``tts_mute`` WS message so the server can skip the Kokoro round-
  // trip while nobody's listening.
  onMutedChange: ((muted: boolean) => void) | null = null;
  /** Audio is waiting for a tap (true), or the wait is over (false). */
  onBlockedChange: ((blocked: boolean) => void) | null = null;
  /** What the player did, a line at a time, for the server log: the only
   * view of playback on a device we can't see (an iPad). */
  onDebug: ((msg: string) => void) | null = null;
  private debug(msg: string): void { try { this.onDebug?.(msg); } catch {} }
  // While a clip plays: when it started and the loudest sample the
  // analyser saw (0 = the graph passed silence).
  private playStartedAt = 0;
  private peak = -1;
  private peakTimer: number | null = null;

  // Taps on the Settings sound check are its own (it tests playback).
  private readonly onGesture = (e: Event) => {
    const t = e.target as Element | null;
    if (t && typeof t.closest === "function" && t.closest("[data-tts-own]")) return;
    this.unlock();
  };

  constructor(session?: string) {
    this.session = session;
    this.muted = loadTtsMuted(session);
    for (const g of GESTURES) {
      document.addEventListener(g, this.onGesture, { capture: true, passive: true });
    }
  }

  /** Web Audio node tapping the audio output. Valid only while audio is wired. */
  getAnalyser(): AnalyserNode | null {
    return this.analyser;
  }

  get isMuted(): boolean { return this.muted; }

  setMuted(muted: boolean): void {
    if (this.muted === muted) return;
    this.muted = muted;
    saveTtsMuted(muted, this.session);
    if (muted) {
      this.queue = [];
      this.setBlocked(false);
      this.current = [];
      // Any in-flight utterance must NOT be played back if the user
      // unmutes again before it ends — otherwise we'd assemble a blob
      // from only the chunks received after unmute, which starts
      // playing mid-word. Mark the rest of this utterance discarded.
      this.discardCurrentUtterance = true;
      if (this.audio && !this.audio.paused) {
        try { this.audio.pause(); } catch {}
        this.handlePlaybackEnd();
      }
    }
    try { this.onMutedChange?.(muted); } catch {}
  }

  onStart(): void {
    this.current = [];
    // A new utterance starts fresh — clear the discard flag so it can
    // be heard even if the previous utterance was muted partway.
    this.discardCurrentUtterance = false;
  }

  onChunk(data: Uint8Array): void {
    if (this.muted || this.discardCurrentUtterance) return;
    // WebSocket binaryType="arraybuffer" hands each frame a fresh
    // ArrayBuffer (per the WS spec — there's no reused pool to defend
    // against), so we can store the subarray view directly without
    // copying. `data` here is buf.subarray(1) from ws.ts which shares
    // the message's buffer.
    this.current.push(data);
  }

  onEnd(): void {
    if (this.muted || this.discardCurrentUtterance || this.current.length === 0) {
      this.current = [];
      return;
    }
    // Blob accepts Uint8Array elements; the previous code slice()-copied
    // every buffer for no reason. Hand the views straight through. The
    // cast widens TS's "ArrayBufferLike could be SharedArrayBuffer" hint
    // — WS frames are always plain ArrayBuffer.
    const blob = new Blob(this.current as BlobPart[], { type: this.mimeType });
    this.current = [];
    this.queue.push({ blob, at: Date.now() });
    this.pumpQueue();
  }

  private pumpQueue(): void {
    if (this.muted || this.blocked || this.starting || this.priming || this.playPending) return;
    if (this.audio && !this.audio.paused) return;
    if (this.queue.length === 0) return;
    this.playBlob(this.queue.shift()!);
  }

  private setBlocked(blocked: boolean): void {
    if (this.blocked === blocked) return;
    this.blocked = blocked;
    try { this.onBlockedChange?.(blocked); } catch (e) { console.warn(e); }
  }

  /** Wait for a tap to play `item` (the browser refused to). */
  private blockUntilTap(item: Queued, why: string): void {
    this.debug(`waiting for a tap: ${why}`);
    this.queue.unshift(item);
    this.unlocked = false;
    this.setBlocked(true);
  }

  /** On every tap or key press: wake a context Safari suspended, and the
   * first time (or after a refusal) unlock the element — by playing what
   * was waiting, or a moment of silence. Runs inside the gesture, which
   * is what makes it count. Public for the unmute tap, which happens
   * while still muted when the document listener sees it. */
  unlock(): void {
    if (this.muted) return;
    this.ensureWebAudio();
    this.ensureGraph();
    const ctx = this.audioCtx;
    if (ctx && ctx.state !== "running") {
      this.debug(`tap: resuming context (${ctx.state})`);
      ctx.resume().catch(() => {});
    }
    if (this.unlocked && !this.blocked) return;
    if (this.blocked) this.debug("tap: playing what was waiting");
    const a = this.audio;
    if (!a || !a.paused || this.starting || this.priming || this.playPending) return;
    if (this.blocked) {
      this.setBlocked(false);
      const now = Date.now();
      this.queue = this.queue.filter((q) => now - q.at < KEEP_BLOCKED_MS);
      const next = this.queue.shift();
      if (next) { this.startPlayback(next); return; }
    }
    this.priming = true;
    a.src = silence();
    const seq = ++this.playSeq;
    a.play().then(() => {
      this.unlocked = true;
      if (seq === this.playSeq) { try { a.pause(); } catch {} }
    }).catch((err) => {
      // Aborted (the iPad, before the clip loaded) still counts: the tap's
      // permission sticks to the element. Refused doesn't.
      if ((err as DOMException)?.name === "AbortError") this.unlocked = true;
    }).finally(() => {
      this.priming = false;
      this.pumpQueue();
    });
  }

  private ensureWebAudio(): void {
    if (this.audio) return;
    this.audio = new Audio();
    this.audio.addEventListener("playing", this.onAudioPlaying);
    this.audio.addEventListener("pause", this.onAudioPause);
    this.audio.addEventListener("ended", this.onAudioEnded);
    this.audio.addEventListener("error", this.onAudioError);
  }

  /** The analyser for the visualiser. Only from unlock(), inside a tap, so
   * the context starts running; made once. */
  private ensureGraph(): void {
    if (APPLE_TOUCH || this.audioCtx || !this.audio) return;
    this.debug(`graph: made (element already played: ${this.unlocked})`);
    try {
      this.audioCtx = new AudioContext();
      const source = this.audioCtx.createMediaElementSource(this.audio);
      this.analyser = this.audioCtx.createAnalyser();
      this.analyser.fftSize = 512;
      this.analyser.smoothingTimeConstant = 0.45;
      source.connect(this.analyser);
      // CRITICAL: must connect to destination, otherwise the element
      // goes silent once routed through Web Audio.
      this.analyser.connect(this.audioCtx.destination);
    } catch (e) {
      // If Web Audio is unavailable for some reason, fall back to a bare
      // audio element with no visualiser hookup.
      console.warn("tts: web audio analyser unavailable:", e);
      this.audioCtx = null;
      this.analyser = null;
    }
  }

  private playbackActive = false;

  private handlePlaybackStart(): void {
    if (this.playbackActive) return;
    this.playbackActive = true;
    this.playStartedAt = performance.now();
    this.peak = this.analyser ? 0 : -1;
    if (this.analyser) {
      const an = this.analyser;
      const buf = new Uint8Array(an.fftSize);
      this.peakTimer = window.setInterval(() => {
        an.getByteTimeDomainData(buf);
        for (const v of buf) this.peak = Math.max(this.peak, Math.abs(v - 128));
      }, 100);
    }
    // Resume context if it was suspended by browser autoplay policy.
    if (this.audioCtx && this.audioCtx.state === "suspended") {
      this.audioCtx.resume().catch(() => {});
    }
    try { this.onPlaybackStart?.(); } catch (e) { console.warn(e); }
  }

  private handlePlaybackEnd(): void {
    if (!this.playbackActive) return;
    this.playbackActive = false;
    if (this.peakTimer !== null) { clearInterval(this.peakTimer); this.peakTimer = null; }
    // Only trouble is reported: a clip cut off at once, or one the
    // analyser heard as silence.
    const secs = (performance.now() - this.playStartedAt) / 1000;
    if (secs < 0.5 || (this.peak === 0 && secs > 0.5)) {
      this.debug(`played ${secs.toFixed(1)} s to t=${this.audio?.currentTime.toFixed(1) ?? "?"}, `
        + `peak ${this.peak < 0 ? "n/a (no graph)" : this.peak}, context ${this.audioCtx?.state ?? "none"}`);
    }
    if (this.playingUrl) {
      URL.revokeObjectURL(this.playingUrl);
      this.playingUrl = null;
    }
    void this._releaseWakeLock();
    try { this.onPlaybackEnd?.(); } catch (e) { console.warn(e); }
  }

  // True between a wakeLock.acquire we issued and its paired release.
  // Tracking it here lets us release on every play() exit path
  // (success, rejection, error event, dispose) without leaking the
  // refcount when play() rejects before the "playing" event fires.
  private wakeLockHeld = false;

  private async _acquireWakeLock(): Promise<void> {
    if (this.wakeLockHeld) return;
    this.wakeLockHeld = true;
    try { await wakeLock.acquire(); } catch {}
  }

  private async _releaseWakeLock(): Promise<void> {
    if (!this.wakeLockHeld) return;
    this.wakeLockHeld = false;
    try { await wakeLock.release(); } catch {}
  }

  private playBlob(item: Queued): void {
    this.ensureWebAudio();
    if (!this.audio) return;
    const ctx = this.audioCtx;
    if (ctx && ctx.state !== "running") {
      // Suspended (Safari does that in the background): the element would
      // "play" silence. Outside a tap Safari won't resume it, so give it a
      // moment and otherwise wait for a tap.
      this.starting = true;
      ctx.resume().catch(() => {});
      window.setTimeout(() => {
        this.starting = false;
        if (this.muted) return;
        if (ctx.state === "running") this.startPlayback(item);
        else this.blockUntilTap(item, `context ${ctx.state}`);
      }, 250);
      return;
    }
    this.startPlayback(item);
  }

  /** Play `blob` now, ahead of anything queued, muted or not: the voice
   * test in Settings. On Safari only one audio element plays at a time,
   * so the test must use this one, not its own. Settles with play(). */
  playNow(blob: Blob): Promise<void> {
    this.ensureWebAudio();
    const a = this.audio;
    if (!a) return Promise.reject(new Error("no audio element"));
    if (!a.paused) { try { a.pause(); } catch {} }
    this.setBlocked(false);
    return new Promise((resolve, reject) => {
      this.startPlayback({ blob, at: Date.now() }, { resolve, reject });
    });
  }

  private startPlayback(item: Queued,
                        settle?: { resolve: () => void; reject: (e: unknown) => void }): void {
    const audio = this.audio;
    if (!audio) return;
    if (this.playingUrl) URL.revokeObjectURL(this.playingUrl);
    this.playingUrl = URL.createObjectURL(item.blob);
    audio.src = this.playingUrl;
    // Acquire BEFORE play() so the screen doesn't dim during a long
    // sentence; release on every failure / end path below.
    void this._acquireWakeLock();
    this.playPending = true;
    const seq = ++this.playSeq;
    const fail = (err: unknown) => {
      this.playPending = false;
      // play() rejection means "playing" will never fire, so
      // handlePlaybackStart was never called and the wake-lock would
      // otherwise leak. Force-release symmetrically.
      void this._releaseWakeLock();
      this.handlePlaybackEnd();
      if (settle) { settle.reject(err); return; }
      if ((err as DOMException)?.name === "NotAllowedError") {
        // Autoplay refused: keep it for the next tap rather than lose it.
        this.blockUntilTap(item, "play() refused");
        return;
      }
      console.warn("tts: playback failed:", err);
      this.pumpQueue();
    };
    const attempt = (again: boolean) => {
      audio.play().then(() => {
        if (seq !== this.playSeq) return;
        this.playPending = false;
        this.unlocked = true;
        settle?.resolve();
      }).catch((err) => {
        if (seq !== this.playSeq) { settle?.reject(err); return; }
        const e = err as DOMException;
        this.debug(`play failed${again ? " again" : ""}: ${e?.name}: ${e?.message}`);
        // The iPad aborts a play() made before its clip has loaded (the
        // sound check, 2026-11-03: a new clip aborted, the same element
        // played once loaded). The attempt still counts as the tap's
        // permission, so play again once it has loaded.
        if (!again && e?.name === "AbortError") {
          const retry = () => {
            window.clearTimeout(giveUp);
            if (seq === this.playSeq) attempt(true);
          };
          const giveUp = window.setTimeout(() => {
            audio.removeEventListener("canplay", retry);
            if (seq === this.playSeq) fail(err);
          }, 4000);
          if (audio.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) retry();
          else {
            audio.addEventListener("canplay", retry, { once: true });
            try { audio.load(); } catch {}
          }
          return;
        }
        fail(err);
      });
    };
    attempt(false);
  }

  /** Fetch a fresh synth of *text* from the server and enqueue it for
   * playback through the same audio pipeline that streaming utterances
   * use. Used by the "replay last response" pill. Quietly no-ops if
   * muted or text is empty. */
  async playText(text: string): Promise<void> {
    if (this.muted) return;
    const t = (text || "").trim();
    if (!t) return;
    let blob: Blob;
    try {
      const res = await fetch("/api/tts/speak", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-By": "ccpipe",
        },
        body: JSON.stringify({ text: t }),
      });
      if (!res.ok) {
        console.warn("tts replay failed:", res.status);
        return;
      }
      blob = await res.blob();
    } catch (err) {
      console.warn("tts replay fetch error:", err);
      return;
    }
    // Re-check mute *after* the fetch resolves. The user might have
    // muted during the round-trip; without this guard the blob would
    // still be queued and play as soon as they unmute, which feels
    // like the mute button "didn't work".
    if (this.muted) return;
    this.queue.push({ blob, at: Date.now() });
    this.pumpQueue();
  }

  /** Release the AudioContext + audio element. Call when the parent
   * view tears down (e.g. session swap) — otherwise Chrome caps each
   * tab at ~6 concurrent AudioContexts and the next TtsPlayer silently
   * fails. Also clears the chunk buffer so stale audio from an
   * interrupted stream can't bleed into a later utterance.
   */
  dispose(): void {
    for (const g of GESTURES) {
      document.removeEventListener(g, this.onGesture, { capture: true });
    }
    this.onBlockedChange = null;
    this.current = [];
    this.queue = [];
    this.discardCurrentUtterance = true;
    if (this.audio) {
      // Detach listeners explicitly so the audio element + its handlers
      // can be GC'd without waiting for the audio element ref to drop
      // out of all closures. Each handler also captures `this`, so
      // skipping this step keeps the TtsPlayer alive longer than the
      // view that owned it.
      this.audio.removeEventListener("playing", this.onAudioPlaying);
      this.audio.removeEventListener("pause", this.onAudioPause);
      this.audio.removeEventListener("ended", this.onAudioEnded);
      this.audio.removeEventListener("error", this.onAudioError);
      try { this.audio.pause(); } catch {}
      this.audio.src = "";
      this.audio = null;
    }
    if (this.playingUrl) {
      try { URL.revokeObjectURL(this.playingUrl); } catch {}
      this.playingUrl = null;
    }
    this.analyser = null;
    if (this.audioCtx) {
      const ctx = this.audioCtx;
      this.audioCtx = null;
      if (ctx.state !== "closed") {
        ctx.close().catch(() => {});
      }
    }
    this.playbackActive = false;
    void this._releaseWakeLock();
    this.onPlaybackStart = null;
    this.onPlaybackEnd = null;
  }
}
