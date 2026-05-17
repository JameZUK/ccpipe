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
// connected to ctx.destination. We initialise lazily on first play so
// the AudioContext starts after a user gesture (TTS toggle or mic press
// counts as one).

import { loadTtsMuted, saveTtsMuted } from "./display-prefs";

export class TtsPlayer {
  // Per-utterance chunk buffer (between onStart and onEnd).
  private current: Uint8Array[] = [];
  // Completed blobs waiting to play.
  private queue: Blob[] = [];

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
  private readonly onAudioPlaying = () => this.handlePlaybackStart();
  private readonly onAudioPause = () => this.handlePlaybackEnd();
  private readonly onAudioEnded = () => {
    this.handlePlaybackEnd();
    this.pumpQueue();
  };
  private readonly onAudioError = () => {
    this.handlePlaybackEnd();
    this.pumpQueue();
  };

  // Fired when actual playback (sound leaving the speakers) starts and
  // ends. Consumers use these to drive UI affordances like a visualiser.
  onPlaybackStart: (() => void) | null = null;
  onPlaybackEnd: (() => void) | null = null;

  constructor(session?: string) {
    this.session = session;
    this.muted = loadTtsMuted(session);
  }

  /** Web Audio node tapping the audio output. Valid only while audio is wired. */
  getAnalyser(): AnalyserNode | null {
    return this.analyser;
  }

  get isMuted(): boolean { return this.muted; }

  setMuted(muted: boolean): void {
    this.muted = muted;
    saveTtsMuted(muted, this.session);
    if (muted) {
      this.queue = [];
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
    this.queue.push(blob);
    this.pumpQueue();
  }

  private pumpQueue(): void {
    if (this.muted) return;
    if (this.audio && !this.audio.paused) return;
    if (this.queue.length === 0) return;
    const blob = this.queue.shift()!;
    this.playBlob(blob);
  }

  private ensureWebAudio(): void {
    if (this.audio) return;
    this.audio = new Audio();
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
    this.audio.addEventListener("playing", this.onAudioPlaying);
    this.audio.addEventListener("pause", this.onAudioPause);
    this.audio.addEventListener("ended", this.onAudioEnded);
    this.audio.addEventListener("error", this.onAudioError);
  }

  private playbackActive = false;

  private handlePlaybackStart(): void {
    if (this.playbackActive) return;
    this.playbackActive = true;
    // Resume context if it was suspended by browser autoplay policy.
    if (this.audioCtx && this.audioCtx.state === "suspended") {
      this.audioCtx.resume().catch(() => {});
    }
    try { this.onPlaybackStart?.(); } catch (e) { console.warn(e); }
  }

  private handlePlaybackEnd(): void {
    if (!this.playbackActive) return;
    this.playbackActive = false;
    if (this.playingUrl) {
      URL.revokeObjectURL(this.playingUrl);
      this.playingUrl = null;
    }
    try { this.onPlaybackEnd?.(); } catch (e) { console.warn(e); }
  }

  private playBlob(blob: Blob): void {
    this.ensureWebAudio();
    if (!this.audio) return;
    if (this.playingUrl) URL.revokeObjectURL(this.playingUrl);
    this.playingUrl = URL.createObjectURL(blob);
    this.audio.src = this.playingUrl;

    // Web Audio: an AudioContext created lazily (i.e. not inside a user
    // gesture handler) starts in 'suspended' state on Chrome/Edge. While
    // suspended, audio routed through MediaElementSource → AnalyserNode →
    // destination produces no sound. resume() is fine here because there
    // HAS been a user gesture (login, session pick, or the TTS toggle)
    // earlier in this tab — autoplay policy permits resume() after any
    // gesture in the page.
    if (this.audioCtx && this.audioCtx.state === "suspended") {
      this.audioCtx.resume().catch((e) =>
        console.warn("tts: ctx resume failed:", e));
    }

    this.audio.play().catch((err) => {
      console.warn("tts: playback failed:", err);
      this.handlePlaybackEnd();
      this.pumpQueue();
    });
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
    this.queue.push(blob);
    this.pumpQueue();
  }

  /** Release the AudioContext + audio element. Call when the parent
   * view tears down (e.g. session swap) — otherwise Chrome caps each
   * tab at ~6 concurrent AudioContexts and the next TtsPlayer silently
   * fails. Also clears the chunk buffer so stale audio from an
   * interrupted stream can't bleed into a later utterance.
   */
  dispose(): void {
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
    this.onPlaybackStart = null;
    this.onPlaybackEnd = null;
  }
}
