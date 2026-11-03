// Mic capture: tap-to-toggle streamer. Tap the composer mic button to
// start, tap again to stop. Audio flows to the backend's virtual-mic
// pipe via the AudioWorklet → WS path.
//
// Also exposes an AnalyserNode tap-off so the UI can draw a realtime
// waveform from the same mic source without re-opening the device.
//
// Start and stop are serialized so rapid tap-tap can't interleave
// teardown and setup (notably on iOS during orientation changes).

import { FRAME_MIC_PCM, TerminalSocket } from "./ws";

export interface MicStreamerConfig {
  /** Whether the client-side VAD trips an automatic stop on sustained
   * silence. When false the mic is strictly tap-to-stop. */
  autoStopEnabled: boolean;
  /** Sustained silence (ms) required before the VAD trips. */
  silenceMs: number;
  /** Hard cap on a single recording. After this many seconds the mic
   * force-stops even with continuous voice; safety net for forgotten
   * mics, mirrors the backend bound. */
  maxRecordingSeconds: number;
}

export const DEFAULT_MIC_CONFIG: MicStreamerConfig = {
  autoStopEnabled: true,
  silenceMs: 2500,
  maxRecordingSeconds: 60,
};

export class MicStreamer {
  private ctx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private node: AudioWorkletNode | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private analyser: AnalyserNode | null = null;
  private pending: Promise<void> = Promise.resolve();
  private state: "idle" | "starting" | "running" | "stopping" = "idle";

  // Voice-activity-detection: poll the analyser's time-domain
  // amplitude at a small interval; if it stays below the gate for
  // `cfg.silenceMs`, fire onSilence. The release-PTT timing is now
  // owned by the backend (drain estimate + drain_pad_ms), so the
  // silence gate only has to be long enough to keep mid-utterance
  // pauses from tripping it — the trailing-tail problem is the
  // backend's responsibility.
  //
  // Threshold tuning:
  //   - RMS gate at 0.008: just above the room-silence floor with
  //     AGC on. Quieter sustained material still counts as voice.
  //   - Peak gate at 0.10: voiceless consonants ("s", "t", "th",
  //     "p") have low average energy but distinctive amplitude
  //     spikes; RMS alone smooths them into "silence", peak rescues
  //     them. Either gate above threshold counts as voice.
  private vadInterval: number | null = null;
  private vadSilenceStart: number | null = null;
  private vadHasHadVoice = false;
  private static readonly VAD_POLL_MS = 100;
  private static readonly VAD_THRESHOLD = 0.008;      // RMS, 0-1 scale
  private static readonly VAD_PEAK_THRESHOLD = 0.10;  // peak |sample|, 0-1

  // Force-stop timer for `maxRecordingSeconds`. Cleared on _teardown.
  private maxRecordTimer: number | null = null;

  private cfg: MicStreamerConfig = { ...DEFAULT_MIC_CONFIG };

  /** Callback fired after a sustained silence is detected. The receiver
   * should treat it like the user releasing PTT — stop recording + send
   * mic_stop so the backend can release /voice's PTT after drain. */
  onSilence: (() => void) | null = null;


  /** Called with (name, detail) when starting was slow or needed a retry:
   * main.ts sends it to the server's journal (as mic_error, logged only). */
  onDiag: ((name: string, detail: string) => void) | null = null;

  constructor(private readonly socket: TerminalSocket) {}

  /** Apply new VAD / max-record settings. Safe to call at any time;
   * a running recording adopts the new silence threshold on the next
   * VAD tick (existing accumulator is preserved). The max-record
   * timer is restarted from "now" rather than "recording-start" —
   * acceptable for a settings-modal save that's an explicit user
   * action; not pretending to be a continuation. */
  setConfig(cfg: MicStreamerConfig): void {
    this.cfg = { ...cfg };
    if (this.state === "running") {
      // Re-arm the max-record timer with the new value.
      this._armMaxRecordTimer();
      // Auto-stop toggle flip: tear down or stand up the VAD interval.
      if (cfg.autoStopEnabled && this.vadInterval === null) {
        this._startVad();
      } else if (!cfg.autoStopEnabled && this.vadInterval !== null) {
        this._stopVad();
      }
    }
  }

  /** Currently running and capturing. */
  get isRunning(): boolean {
    return this.state === "running";
  }

  /** Web Audio node exposing the live mic for visualisation.
   *
   * Returns the analyser as soon as it's been wired during _start, even
   * before state transitions to "running" (the analyser is created and
   * connected to the source mid-start). Cleared back to null by _teardown.
   * Callers that race the start (e.g. UI subscribed to a state change
   * that fires before await mic.start() completes) should poll a few
   * frames or hook a "mic ready" event. */
  getAnalyser(): AnalyserNode | null {
    return this.analyser;
  }

  // An AudioContext only starts if it's created or resumed from inside a
  // user-input handler. start() runs after several awaits (the /voice
  // trigger, getUserMedia), and Firefox on Android left a context created
  // there suspended for good: the mic opened, but no audio was processed
  // (2026-11-03; ctx "suspended", 0 frames). So the mic buttons call prime()
  // synchronously in their tap / press / keydown handler.
  //
  // And it's ONE context for this streamer's life, suspended between
  // recordings, not one per recording: a fresh context had to pass
  // Firefox's autoplay check every time and sometimes didn't, even in a
  // tap ("audio context suspended (a fresh one too)"), and closing the old
  // one held the streamer in "stopping" — where prime() used to bail — so a
  // quick second tap started its contexts outside the gesture. A context
  // that has run once resumes freely.
  private audio: AudioContext | null = null;
  private workletReady: Promise<void> | null = null;
  private idleTimer: number | null = null;
  private lastPrimeAt = 0;

  /** Create or wake the AudioContext. Call synchronously from the user's
   * gesture, before anything async. A no-op while recording. */
  prime(): void {
    if (this.state === "running" || this.state === "starting") return;
    try {
      if (!this.audio || this.audio.state === "closed") this._newAudio();
      void this.audio!.resume().catch(() => {});
      this.lastPrimeAt = performance.now();
    } catch {
      this.audio = null;
      return;
    }
    // Primed but never used (a cancelled press): don't leave it running.
    if (this.idleTimer !== null) clearTimeout(this.idleTimer);
    this.idleTimer = window.setTimeout(() => this.unprime(), 30_000);
  }

  /** Let a primed context idle again (e.g. a cancelled press). */
  unprime(): void {
    if (this.idleTimer !== null) { clearTimeout(this.idleTimer); this.idleTimer = null; }
    if (this.state === "idle") void this.audio?.suspend().catch(() => {});
  }

  /** Stop and release everything, the context included (session closed). */
  dispose(): void {
    void this.stop().then(() => {
      if (this.idleTimer !== null) { clearTimeout(this.idleTimer); this.idleTimer = null; }
      const ctx = this.audio;
      this.audio = null;
      this.workletReady = null;
      if (ctx && ctx.state !== "closed") void ctx.close().catch(() => {});
    });
  }

  private _newAudio(): AudioContext {
    const old = this.audio;
    if (old && old.state !== "closed") void old.close().catch(() => {});
    this.audio = new AudioContext();
    this.workletReady = null;
    return this.audio;
  }

  /** Start recording. Rejects if the mic can't start, so the caller can
   * unwind /voice and say so; the serialising chain itself never breaks.
   * (It used to resolve regardless, which left the UI "recording" with
   * nothing captured.) */
  start(): Promise<void> {
    const run = this.pending.then(() => this._start());
    this.pending = run.catch(() => {});
    return run;
  }

  stop(): Promise<void> {
    this.pending = this.pending.then(() => this._stop()).catch(() => {});
    return this.pending;
  }

  private async _start(): Promise<void> {
    if (this.state === "running" || this.state === "starting") return;
    this.state = "starting";
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      // The context prime() woke normally runs already. If not, keep asking
      // for a while: on Android the first start after the phone's audio has
      // idled can take well over half a second (the mic opening switches it
      // to call mode), and giving up at 0.6 s failed taps that were fine
      // (2026-11-03). Failing that, a fresh context. How long it took is
      // reported when it was slow (onDiag), so the cause stays measurable.
      const runs = async (ctx: AudioContext, ms: number): Promise<number | null> => {
        const t0 = performance.now();
        const deadline = t0 + ms;
        while ((ctx.state as AudioContextState) !== "running") {
          if (performance.now() >= deadline) return null;
          void ctx.resume().catch(() => {});
          await new Promise((r) => setTimeout(r, 100));
        }
        return Math.round(performance.now() - t0);
      };
      const wasState = this.audio?.state ?? "none";
      const sincePrime = this.lastPrimeAt ? Math.round(performance.now() - this.lastPrimeAt) : -1;
      let ctx = this.audio && this.audio.state !== "closed" ? this.audio : this._newAudio();
      let took = await runs(ctx, 3000);
      let fresh = false;
      if (took === null) {
        ctx = this._newAudio();
        fresh = true;
        took = await runs(ctx, 2000);
        if (took === null) {
          const act = (navigator as Navigator & { userActivation?: { isActive: boolean; hasBeenActive: boolean } }).userActivation;
          const err = new Error(`ctx ${ctx.state} after 5 s (fresh too); was ${wasState}; primed ${sincePrime}ms ago; `
            + `activation ${act ? `${act.isActive ? 1 : 0}/${act.hasBeenActive ? 1 : 0}` : "?"}; ${document.visibilityState}`);
          err.name = "MicBlocked";
          throw err;
        }
      }
      if (took > 300 || fresh) {
        this.onDiag?.("MicSlowStart", `ctx running after ${took}ms${fresh ? " (fresh)" : ""}; was ${wasState}; primed ${sincePrime}ms ago`);
      }
      this.ctx = ctx;
      if (this.idleTimer !== null) { clearTimeout(this.idleTimer); this.idleTimer = null; }
      if (!this.workletReady) {
        this.workletReady = ctx.audioWorklet.addModule("/mic-worklet.js");
        this.workletReady.catch(() => { this.workletReady = null; });
      }
      await this.workletReady;
      this.source = this.ctx.createMediaStreamSource(this.stream);

      // Tap-off for visualisation. fftSize 512 gives a smooth scope at
      // 30fps; smoothing is irrelevant for time-domain but cheap to set.
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 512;
      this.analyser.smoothingTimeConstant = 0.4;

      this.node = new AudioWorkletNode(this.ctx, "mic-capture");
      this.node.port.onmessage = (e: MessageEvent<ArrayBuffer>) => {
        const buf = new Uint8Array(e.data);
        this.socket.sendBinary(FRAME_MIC_PCM, buf);
      };
      // Same source feeds both branches. Worklet pulls PCM for the
      // backend; analyser pulls time-domain bytes for the waveform.
      this.source.connect(this.analyser);
      this.source.connect(this.node);
      // Note: deliberately not connecting to ctx.destination — we don't
      // want to hear our own mic.
      this.state = "running";
      if (this.cfg.autoStopEnabled) this._startVad();
      this._armMaxRecordTimer();
    } catch (err) {
      this.state = "idle";
      await this._teardown();
      throw err;
    }
  }

  private _startVad(): void {
    this._stopVad();
    this.vadHasHadVoice = false;
    this.vadSilenceStart = null;
    const buf = new Uint8Array(this.analyser?.fftSize ?? 512);
    this.vadInterval = window.setInterval(() => {
      if (!this.analyser || this.state !== "running") return;
      this.analyser.getByteTimeDomainData(buf);
      // Two gates: sustained energy (RMS) and brief loud bursts
      // (peak). Either one counts as voice. RMS alone smooths
      // voiceless consonants into silence; the peak gate rescues
      // them. See VAD_THRESHOLD / VAD_PEAK_THRESHOLD comments above
      // for the tuning rationale.
      let sum = 0;
      let peak = 0;
      for (let i = 0; i < buf.length; i++) {
        const v = (buf[i] - 128) / 128;
        sum += v * v;
        const a = v < 0 ? -v : v;
        if (a > peak) peak = a;
      }
      const rms = Math.sqrt(sum / buf.length);
      const now = performance.now();
      if (rms >= MicStreamer.VAD_THRESHOLD
          || peak >= MicStreamer.VAD_PEAK_THRESHOLD) {
        this.vadHasHadVoice = true;
        this.vadSilenceStart = null;
        return;
      }
      // Below threshold. Only count it once we've heard at least one
      // burst of voice — otherwise we'd auto-stop the moment the user
      // pressed record but hadn't started speaking yet.
      if (!this.vadHasHadVoice) return;
      if (this.vadSilenceStart === null) {
        this.vadSilenceStart = now;
        return;
      }
      if (now - this.vadSilenceStart >= this.cfg.silenceMs) {
        this._stopVad();
        try { this.onSilence?.(); } catch (e) { console.warn("VAD cb:", e); }
      }
    }, MicStreamer.VAD_POLL_MS);
  }

  private _stopVad(): void {
    if (this.vadInterval !== null) {
      clearInterval(this.vadInterval);
      this.vadInterval = null;
    }
  }

  /** (Re-)arm the safety-net timer that fires onSilence after
   * `cfg.maxRecordingSeconds` of continuous recording. Clears any
   * existing timer first so a setConfig() call mid-recording doesn't
   * stack timers. */
  private _armMaxRecordTimer(): void {
    if (this.maxRecordTimer !== null) {
      clearTimeout(this.maxRecordTimer);
      this.maxRecordTimer = null;
    }
    const seconds = Math.max(1, this.cfg.maxRecordingSeconds);
    this.maxRecordTimer = window.setTimeout(() => {
      this.maxRecordTimer = null;
      if (this.state !== "running") return;
      console.info(`mic max-recording cap (${seconds}s) hit; auto-stopping`);
      try { this.onSilence?.(); } catch (e) { console.warn("max-rec cb:", e); }
    }, seconds * 1000);
  }

  private _clearMaxRecordTimer(): void {
    if (this.maxRecordTimer !== null) {
      clearTimeout(this.maxRecordTimer);
      this.maxRecordTimer = null;
    }
  }

  private async _stop(): Promise<void> {
    if (this.state === "idle" || this.state === "stopping") return;
    this.state = "stopping";
    this._stopVad();
    this._clearMaxRecordTimer();
    await this._teardown();
    this.state = "idle";
  }

  private async _teardown(): Promise<void> {
    try { this.source?.disconnect(); } catch {}
    try { this.analyser?.disconnect(); } catch {}
    try { this.node?.disconnect(); } catch {}
    this.source = null;
    this.analyser = null;
    this.node = null;
    if (this.stream) {
      for (const t of this.stream.getTracks()) t.stop();
      this.stream = null;
    }
    // Keep the context (see prime()); just let it idle.
    this.ctx = null;
    if (this.audio && this.audio.state === "running") void this.audio.suspend().catch(() => {});
  }
}
