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
  // trigger delay, getUserMedia), and Firefox on Android left a context
  // created there suspended for good: the mic opened, but no audio was ever
  // processed (2026-11-03; diag showed ctx "suspended", 0 frames). So the
  // mic buttons call prime() synchronously in their tap / press / keydown
  // handler, and start() takes that context. Unused, it's closed after 30 s
  // (long enough for a first-time permission prompt).
  private primed: AudioContext | null = null;
  private primedTimer: number | null = null;

  /** Create or wake the AudioContext. Call synchronously from the user's
   * gesture, before anything async. A no-op while recording. */
  prime(): void {
    if (this.state === "running" || this.state === "starting" || this.state === "stopping") return;
    try {
      if (!this.primed || this.primed.state === "closed") this.primed = new AudioContext();
      void this.primed.resume().catch(() => {});
    } catch {
      this.primed = null;
      return;
    }
    if (this.primedTimer !== null) clearTimeout(this.primedTimer);
    this.primedTimer = window.setTimeout(() => this.unprime(), 30_000);
  }

  /** Drop a primed context that won't be used (e.g. a cancelled press). */
  unprime(): void {
    const ctx = this._takePrimed();
    if (ctx) void ctx.close().catch(() => {});
  }

  private _takePrimed(): AudioContext | null {
    if (this.primedTimer !== null) {
      clearTimeout(this.primedTimer);
      this.primedTimer = null;
    }
    const ctx = this.primed;
    this.primed = null;
    return ctx && ctx.state !== "closed" ? ctx : null;
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
      // The primed context (mic buttons, prime()) normally runs already.
      // If it doesn't, one more resume(); failing that a fresh context: on a
      // phone the press creates the context, but a touch only counts as user
      // activity once the finger lifts, so on a freshly loaded page the
      // press's context can stay suspended while one made now can run.
      const runs = async (ctx: AudioContext): Promise<boolean> => {
        if (ctx.state === "running") return true;
        await Promise.race([
          ctx.resume().catch(() => {}),
          new Promise((r) => setTimeout(r, 600)),
        ]);
        return (ctx.state as AudioContextState) === "running";
      };
      let ctx = this._takePrimed() ?? new AudioContext();
      if (!(await runs(ctx))) {
        void ctx.close().catch(() => {});
        ctx = new AudioContext();
        if (!(await runs(ctx))) {
          const state = ctx.state;
          void ctx.close().catch(() => {});
          const err = new Error(`audio context ${state} (a fresh one too)`);
          err.name = "MicBlocked";
          throw err;
        }
      }
      this.ctx = ctx;
      await this.ctx.audioWorklet.addModule("/mic-worklet.js");
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
    const ctx = this.ctx;
    this.ctx = null;
    if (ctx && ctx.state !== "closed") {
      try { await ctx.close(); } catch {}
    }
  }
}
