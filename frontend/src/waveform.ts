// Oscilloscope visualization for the mic input. Reads time-domain samples
// from an AnalyserNode at ~30fps and strokes them onto a canvas. The
// renderer is deliberately small and dependency-free — ~50 lines total.
//
// Usage:
//   const wave = new Waveform(canvasEl, analyserNode);
//   wave.start();   // begins animation
//   wave.stop();    // halts and clears
//   wave.dispose(); // detach observer / RAF

export class Waveform {
  private rafId: number | null = null;
  private resizeObserver: ResizeObserver | null = null;
  // Explicit ArrayBuffer generic so this satisfies the
  // Uint8Array<ArrayBuffer> overload of getByteTimeDomainData added in
  // TS 5.7's lib.dom typings.
  private buf: Uint8Array<ArrayBuffer>;
  private cssWidth = 0;
  private cssHeight = 0;

  constructor(
    private readonly canvas: HTMLCanvasElement,
    private readonly analyser: AnalyserNode,
    private readonly color: string = "#f5a524",
  ) {
    // Explicit ArrayBuffer so TS 5.7+ infers Uint8Array<ArrayBuffer> rather
    // than Uint8Array<ArrayBufferLike>; the former is what getByteTimeDomainData
    // accepts.
    this.buf = new Uint8Array(new ArrayBuffer(analyser.fftSize));
    this.resizeObserver = new ResizeObserver(() => this.fit());
    this.resizeObserver.observe(canvas);
    this.fit();
  }

  start(): void {
    if (this.rafId !== null) return;
    const loop = () => {
      this.analyser.getByteTimeDomainData(this.buf);
      this.render();
      this.rafId = requestAnimationFrame(loop);
    };
    this.rafId = requestAnimationFrame(loop);
  }

  stop(): void {
    if (this.rafId !== null) {
      cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }
    this.clear();
  }

  dispose(): void {
    this.stop();
    this.resizeObserver?.disconnect();
    this.resizeObserver = null;
  }

  private fit(): void {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;
    this.cssWidth = rect.width;
    this.cssHeight = rect.height;
    this.canvas.width = Math.round(rect.width * dpr);
    this.canvas.height = Math.round(rect.height * dpr);
    const ctx = this.canvas.getContext("2d");
    ctx?.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  private clear(): void {
    const ctx = this.canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, this.cssWidth, this.cssHeight);
  }

  private render(): void {
    const ctx = this.canvas.getContext("2d");
    if (!ctx) return;
    const w = this.cssWidth;
    const h = this.cssHeight;
    if (w === 0 || h === 0) return;
    ctx.clearRect(0, 0, w, h);

    // Subtle center line
    ctx.strokeStyle = "rgba(245, 165, 36, 0.18)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, h / 2);
    ctx.lineTo(w, h / 2);
    ctx.stroke();

    // Waveform
    ctx.strokeStyle = this.color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    const N = this.buf.length;
    const step = w / (N - 1);
    for (let i = 0; i < N; i++) {
      // 128 is the zero-crossing for Uint8 time-domain; ±1 around it.
      const v = (this.buf[i] - 128) / 128;
      const x = i * step;
      const y = h / 2 + v * (h / 2 - 1);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }
}
