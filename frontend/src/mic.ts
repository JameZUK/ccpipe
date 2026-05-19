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
  // SILENCE_MS, fire onSilence. Lets the UI auto-finalise a recording
  // the user forgot about, before the mic streams forever into a dead
  // WS.
  //
  // Tuning history:
  //   - Original 1500 ms / 0.02 RMS: stopped too aggressively. Natural
  //     mid-utterance pauses and quiet syllables tripped the
  //     silence-accumulator before the user was done.
  //   - 2500 ms / 0.012 RMS: still cut off the *tail* of utterances
  //     where the user's voice softened at sentence-end. Specifically
  //     reproduced as "the words 'the' and 'sentence' are missing"
  //     when those trailing words were below the RMS gate.
  //
  // Current shape:
  //   - RMS gate lowered to 0.008 so quieter sustained material counts
  //     as voice. Just above typical room-silence floor with AGC on.
  //   - Peak-amplitude gate added (0.10) alongside RMS. Voiceless
  //     consonants ("s", "t", "th", "p") have very low average energy
  //     but distinctive peaks; RMS alone smooths them into "silence".
  //     Tracking the max absolute sample in each window catches those
  //     bursts and resets the silence timer through the word.
  //   - Either gate above its threshold counts as voice.
  // The user can still tap the mic explicitly to submit immediately.
  private vadInterval: number | null = null;
  private vadSilenceStart: number | null = null;
  private vadHasHadVoice = false;
  private static readonly VAD_POLL_MS = 100;
  private static readonly VAD_SILENCE_MS = 2500;
  private static readonly VAD_THRESHOLD = 0.008;      // RMS, 0-1 scale
  private static readonly VAD_PEAK_THRESHOLD = 0.10;  // peak |sample|, 0-1

  /** Callback fired after a sustained silence is detected. The receiver
   * should treat it like the user releasing PTT — stop recording + send
   * /voice the second time to submit. */
  onSilence: (() => void) | null = null;

  constructor(private readonly socket: TerminalSocket) {}

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

  start(): Promise<void> {
    this.pending = this.pending.then(() => this._start()).catch(() => {});
    return this.pending;
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
      this.ctx = new AudioContext();
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
      this._startVad();
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
      if (now - this.vadSilenceStart >= MicStreamer.VAD_SILENCE_MS) {
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

  private async _stop(): Promise<void> {
    if (this.state === "idle" || this.state === "stopping") return;
    this.state = "stopping";
    this._stopVad();
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
