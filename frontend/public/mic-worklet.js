// AudioWorkletProcessor: capture mic audio at the AudioContext's sample
// rate (typically 48000 Hz) and emit 16 kHz mono Int16 PCM in ~50ms
// batches.
//
// We carry leftover samples between render quanta so the decimation
// doesn't drift in phase (48000/16000 is integer 3 but block size 128
// doesn't divide by 3 evenly). We also coalesce many quanta before
// posting to the main thread — at 16 kHz, 50ms = 800 samples = 1600
// bytes, ~20 messages/sec instead of ~375.

const TARGET_RATE = 16000;
const BATCH_SAMPLES = 800;  // 50ms at 16kHz

class MicCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.leftover = new Float32Array(0);
    this.decimation = Math.max(1, Math.round(sampleRate / TARGET_RATE));
    this.outBuf = new Int16Array(BATCH_SAMPLES);
    this.outIdx = 0;
  }

  flush() {
    if (this.outIdx === 0) return;
    // Slice exactly the populated region so the receiver gets a tight buffer.
    const out = new Int16Array(this.outIdx);
    out.set(this.outBuf.subarray(0, this.outIdx));
    this.outIdx = 0;
    this.port.postMessage(out.buffer, [out.buffer]);
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel || channel.length === 0) return true;

    // Carry-over + new input
    const combined = new Float32Array(this.leftover.length + channel.length);
    combined.set(this.leftover, 0);
    combined.set(channel, this.leftover.length);

    const N = Math.floor(combined.length / this.decimation);
    for (let j = 0; j < N; j++) {
      let s = combined[j * this.decimation];
      if (s > 1) s = 1; else if (s < -1) s = -1;
      this.outBuf[this.outIdx++] = s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7fff);
      if (this.outIdx >= BATCH_SAMPLES) this.flush();
    }
    const consumed = N * this.decimation;
    this.leftover = combined.subarray(consumed).slice();

    return true;
  }
}

registerProcessor("mic-capture", MicCapture);
