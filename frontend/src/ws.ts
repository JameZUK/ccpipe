// Minimal WS client with auto-reconnect. Text frames are JSON control messages;
// binary frames are tagged with a 1-byte type prefix.

export type ServerHello = {
  type: "hello";
  session: string;
  tts: boolean;
  voice: boolean;
};

export type ServerSessionEvent = {
  type: "session_event";
  event: string;
  args: string[];
};

export type ServerSessionGone = {
  type: "session_gone";
  session: string;
};

export type ServerTtsStart = {
  type: "tts_start";
  text: string;
};

export type ServerTtsEnd = {
  type: "tts_end";
};

export type ServerMessage =
  | ServerHello
  | ServerSessionEvent
  | ServerSessionGone
  | ServerTtsStart
  | ServerTtsEnd;

export type ClientMessage =
  | { type: "input"; data: string }
  | { type: "resize"; cols: number; rows: number }
  | { type: "ping" }
  // Mirror of the client mute state. Server skips the Kokoro round-trip
  // and TTS audio fan-out for any subscription whose `muted` is true.
  | { type: "tts_mute"; value: boolean };

// Binary frame type prefixes (must match backend ws.py). Every binary
// frame is now tagged so a PTY byte that happens to look like a TTS
// chunk's leading byte can't be misclassified.
export const FRAME_MIC_PCM    = 0x01;  // client → server
export const FRAME_TTS_AUDIO  = 0x02;  // server → client
export const FRAME_PTY_OUTPUT = 0x00;  // server → client

export interface TerminalSocketHandlers {
  // PTY output now arrives as binary (Uint8Array view over a raw UTF-8
  // byte stream). xterm.js's term.write() accepts both string and
  // Uint8Array, so we hand the bytes directly through.
  onOutput: (data: Uint8Array) => void;
  onHello: (msg: ServerHello) => void;
  onSessionEvent?: (msg: ServerSessionEvent) => void;
  onSessionGone?: (msg: ServerSessionGone) => void;
  onTtsStart?: (msg: ServerTtsStart) => void;
  onTtsAudio?: (chunk: Uint8Array) => void;
  onTtsEnd?: () => void;
  onStatus: (status: "connecting" | "open" | "closed" | "reconnecting",
             info?: { attempt: number; nextRetryMs?: number }) => void;
  /** Fires whenever a server frame arrives in response to one of our
   * keepalive pings — `ms` is the round-trip in milliseconds. The UI
   * surfaces this as the statusbar's latency indicator so the user
   * can see at a glance whether the connection is healthy. */
  onLatency?: (ms: number) => void;
}

export class TerminalSocket {
  private ws: WebSocket | null = null;
  private closed = false;
  private reconnectDelayMs = 500;
  private readonly maxReconnectDelayMs = 8000;
  private retryAttempt = 0;
  private hadOpenedOnce = false;
  // Cache of the latest viewport size so that a resize computed before
  // ws.onopen (or any subsequent reconnect) is re-sent on connection.
  // Without this the backend spawns the PTY at its fallback 120x40.
  private latestResize: { cols: number; rows: number } | null = null;
  // 30s idle ping so carrier NAT doesn't silently drop the connection.
  // Also drives the staleness probe: the server replies "pong" to every
  // ping, so if we don't see *any* server frame within ~45s we know the
  // socket is dead at the TCP layer (or the JS was just frozen by a
  // background tab on mobile) and we force-reconnect.
  private keepaliveTimer: number | null = null;
  private readonly keepaliveIntervalMs = 30_000;
  private lastReceivedAt = 0;
  private static readonly STALE_AFTER_MS = 45_000;
  private staleCheckTimer: number | null = null;
  // Timestamp of the most recent ping we sent. Subtracting from the
  // matching pong's arrival time gives us RTT. We only track a single
  // in-flight ping because the keepalive interval is much longer than
  // any realistic RTT, so the next ping always arrives well after the
  // previous pong.
  private lastPingSentAt = 0;
  // Pending retry handle so reconnectNow() can cancel an in-flight backoff
  // and dial immediately when the network/tab comes back.
  private pendingRetry: number | null = null;
  // Debounce reconnectNow() so a Wi-Fi handoff firing 'online' three or
  // four times in quick succession only produces one dial rather than
  // stacking parallel connect attempts that all race onopen.
  private lastReconnectNowAt = 0;
  private readonly reconnectNowMinIntervalMs = 250;
  private removeLifecycleHooks: (() => void) | null = null;

  constructor(
    // url is either a fixed string or a function that receives a flag
    // indicating whether this is a reconnect (true) vs. first connect
    // (false). Used to skip server-side history replay on reconnects so
    // the xterm buffer isn't duplicated.
    private readonly url: string | ((isReconnect: boolean) => string),
    private readonly handlers: TerminalSocketHandlers,
  ) {
    this.attachLifecycleHooks();
  }

  private resolveUrl(): string {
    return typeof this.url === "function"
      ? this.url(this.hadOpenedOnce)
      : this.url;
  }

  connect(): void {
    if (this.closed) return;
    // If a previous WS is still hanging around (browser hasn't yet
    // detected the TCP close, or some path dialed connect() without
    // closing first), force-close it before creating a new one. The
    // old subscription on the backend stays alive otherwise and every
    // TTS utterance fans out to it, which the user hears as duplicates.
    if (this.ws) {
      const rs = this.ws.readyState;
      if (rs === WebSocket.OPEN || rs === WebSocket.CONNECTING) {
        try { this.ws.close(); } catch {}
      }
    }
    this.handlers.onStatus(this.hadOpenedOnce ? "reconnecting" : "connecting",
                           { attempt: this.retryAttempt });
    const ws = new WebSocket(this.resolveUrl());
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      // Ignore events from a socket we already replaced — without this
      // guard a stale onopen would clobber retryAttempt + flip status
      // to 'open' even though `this.ws` now points at a different,
      // possibly-still-connecting socket.
      if (this.ws !== ws) return;
      this.reconnectDelayMs = 500;
      this.retryAttempt = 0;
      this.hadOpenedOnce = true;
      this.lastReceivedAt = Date.now();
      // Re-send the latest known viewport size BEFORE the status flips
      // to 'open'. Backend's PTY spawn waits briefly for this message
      // (see ws.py::handle_terminal_ws) so the tmux client attaches at
      // the right dimensions from the start.
      if (this.latestResize) {
        ws.send(JSON.stringify({ type: "resize", ...this.latestResize }));
      }
      this.startKeepalive();
      this.startStaleCheck();
      this.handlers.onStatus("open");
    };

    ws.onmessage = (ev) => {
      if (this.ws !== ws) return;
      this.lastReceivedAt = Date.now();
      if (typeof ev.data === "string") {
        // Text frames carry only JSON control messages now. PTY output
        // comes through as binary (see else branch).
        try {
          const parsed = JSON.parse(ev.data) as { type: string } & Record<string, unknown>;
          switch (parsed.type) {
            case "hello": this.handlers.onHello(parsed as unknown as ServerHello); return;
            case "session_event": this.handlers.onSessionEvent?.(parsed as unknown as ServerSessionEvent); return;
            case "session_gone": this.handlers.onSessionGone?.(parsed as unknown as ServerSessionGone); return;
            case "tts_start": this.handlers.onTtsStart?.(parsed as unknown as ServerTtsStart); return;
            case "tts_end": this.handlers.onTtsEnd?.(); return;
            case "pong":
              // Surface round-trip latency so the statusbar can show
              // it next to the connection dot. lastPingSentAt is 0 if
              // we never sent the matching ping (e.g. the server sent
              // an unsolicited pong); skip in that case.
              if (this.lastPingSentAt) {
                const rtt = Date.now() - this.lastPingSentAt;
                this.handlers.onLatency?.(rtt);
              }
              return;
          }
        } catch {
          // ignore non-JSON text frames
        }
        return;
      }
      // Binary frame: first byte is the channel tag, rest is payload.
      // Unknown tags are dropped silently so a server-side feature added
      // before a client refresh doesn't corrupt the terminal buffer.
      const buf = new Uint8Array(ev.data as ArrayBuffer);
      if (buf.length === 0) return;
      switch (buf[0]) {
        case FRAME_PTY_OUTPUT:
          this.handlers.onOutput(buf.subarray(1));
          return;
        case FRAME_TTS_AUDIO:
          this.handlers.onTtsAudio?.(buf.subarray(1));
          return;
        default:
          return;  // unknown frame tag — ignore
      }
    };

    ws.onclose = () => {
      // A stale onclose (from a socket we explicitly replaced in
      // connect() above) would otherwise re-schedule pendingRetry and
      // race the fresh socket — producing the multi-WS subscription
      // pile-up we hit last time. Drop these.
      if (this.ws !== ws) return;
      this.stopKeepalive();
      this.stopStaleCheck();
      this.ws = null;
      if (this.closed) {
        this.handlers.onStatus("closed");
        return;
      }
      this.retryAttempt += 1;
      const nextRetryMs = this.reconnectDelayMs;
      this.handlers.onStatus("reconnecting", {
        attempt: this.retryAttempt,
        nextRetryMs,
      });
      this.pendingRetry = window.setTimeout(() => {
        this.pendingRetry = null;
        this.connect();
      }, nextRetryMs);
      this.reconnectDelayMs = Math.min(this.reconnectDelayMs * 2, this.maxReconnectDelayMs);
    };

    ws.onerror = () => { if (this.ws === ws) ws.close(); };

    this.ws = ws;
  }

  send(msg: ClientMessage): void {
    if (msg.type === "resize") {
      this.latestResize = { cols: msg.cols, rows: msg.rows };
    }
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  private startKeepalive(): void {
    this.stopKeepalive();
    // Send an immediate ping so the latency indicator settles on a
    // real value within a few hundred ms of connect, rather than
    // sitting blank for 30 seconds until the first interval tick.
    this.sendPing();
    this.keepaliveTimer = window.setInterval(() => this.sendPing(),
                                              this.keepaliveIntervalMs);
  }

  private sendPing(): void {
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    this.lastPingSentAt = Date.now();
    this.ws.send(JSON.stringify({ type: "ping" }));
  }

  private stopKeepalive(): void {
    if (this.keepaliveTimer !== null) {
      clearInterval(this.keepaliveTimer);
      this.keepaliveTimer = null;
    }
  }

  /** Watchdog: every 5s while the tab is visible, check whether we've
   * received *any* server frame in the last STALE_AFTER_MS. The server
   * pongs every keepalive ping, so a silent window longer than
   * keepalive + slack means the WS is dead even though the browser may
   * still report it as OPEN. Force-reconnect in that case. */
  private startStaleCheck(): void {
    this.stopStaleCheck();
    this.staleCheckTimer = window.setInterval(() => {
      if (this.closed) return;
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
      if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
      const idle = Date.now() - this.lastReceivedAt;
      if (idle > TerminalSocket.STALE_AFTER_MS) {
        // Dead. Force a close + reconnect; the new socket will replace
        // this one and the old onclose's `this.ws !== ws` guard makes it
        // a no-op.
        try { this.ws.close(); } catch {}
        this.ws = null;
        this.reconnectNow(true);
      }
    }, 5_000);
  }

  private stopStaleCheck(): void {
    if (this.staleCheckTimer !== null) {
      clearInterval(this.staleCheckTimer);
      this.staleCheckTimer = null;
    }
  }

  sendBinary(frameType: number, payload: Uint8Array): void {
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    // Mic frames arrive at ~50 fps; if the uplink stalls, the browser's
    // own send buffer can balloon to seconds of audio that all dump on
    // reconnect and trample the server's mic-rate budget. Drop new
    // frames while we have >256 KiB queued — the next AnalyserNode
    // frame will repeat in 20ms anyway.
    if (frameType === FRAME_MIC_PCM && this.ws.bufferedAmount > 256 * 1024) {
      return;
    }
    const out = new Uint8Array(payload.length + 1);
    out[0] = frameType;
    out.set(payload, 1);
    this.ws.send(out);
  }

  close(): void {
    this.closed = true;
    this.stopKeepalive();
    this.stopStaleCheck();
    if (this.pendingRetry !== null) {
      clearTimeout(this.pendingRetry);
      this.pendingRetry = null;
    }
    this.removeLifecycleHooks?.();
    this.removeLifecycleHooks = null;
    this.ws?.close();
  }

  /** Force an immediate reconnect attempt — cancels any scheduled backoff
   * retry and dials now. Safe to call when already connected (no-op).
   *
   * Debounced: rapid duplicate calls (visibilitychange + online + focus
   * all firing during a mobile network handoff) collapse to a single
   * dial. Also skips when a CONNECTING socket is already in flight.
   *
   * `force=true` bypasses the debounce so the user-facing "retry now"
   * button in the offline banner is never silently ignored just because
   * a lifecycle event happened to fire 200ms ago. */
  reconnectNow(force = false): void {
    if (this.closed) return;
    if (this.ws) {
      const rs = this.ws.readyState;
      if (rs === WebSocket.OPEN || rs === WebSocket.CONNECTING) return;
    }
    if (!force) {
      const now = Date.now();
      if (now - this.lastReconnectNowAt < this.reconnectNowMinIntervalMs) return;
      this.lastReconnectNowAt = now;
    } else {
      // Treat a forced retry as a fresh user intent — keep the lifecycle
      // debounce timer in step with it so the next online/visibility
      // event doesn't fire a second dial right behind ours.
      this.lastReconnectNowAt = Date.now();
    }
    if (this.pendingRetry !== null) {
      clearTimeout(this.pendingRetry);
      this.pendingRetry = null;
    }
    this.reconnectDelayMs = 500;     // reset backoff for any subsequent failure
    this.retryAttempt = 0;
    this.connect();
  }

  /** Wire up browser-level events that signal "try the network again now":
   *   - window 'online' fires when the OS reports a connection is back
   *   - Page Visibility 'visible' fires when the tab comes back to focus
   * Both are common after a mobile signal returns or the user opens the
   * app from the background. We use them to short-circuit the exponential
   * backoff because the user is clearly waiting on us right now.
   *
   * Critical wrinkle: after a phone wakes from sleep, the browser may
   * still report the WebSocket as OPEN even though the underlying TCP
   * connection is dead. reconnectNow() short-circuits when readyState
   * is OPEN, so a pure call there would do nothing and the user would
   * be stuck on a zombie connection until a hard reload. So when we
   * regain visibility / network we proactively close any "OPEN" socket
   * to force the close + retry path to kick in. */
  /** Force-close any socket that isn't actively CONNECTING so the
   * subsequent reconnect path can start fresh. We can't tell from JS
   * whether an OPEN socket is actually alive on the wire (Android may
   * silently kill TCP during background freeze and never deliver the
   * close event), so we treat OPEN as suspect on any lifecycle hint. */
  private kickStaleSocket(): void {
    if (!this.ws) return;
    if (this.ws.readyState === WebSocket.CONNECTING) return;
    try { this.ws.close(); } catch {}
    this.ws = null;
  }

  private attachLifecycleHooks(): void {
    const kickAndRetry = (force = true) => {
      this.kickStaleSocket();
      this.reconnectNow(force);
    };
    const onOnline = () => kickAndRetry();
    const onFocus = () => kickAndRetry();
    const onVisible = () => {
      if (document.visibilityState === "visible") kickAndRetry();
    };
    // pageshow with persisted=true fires when the page comes back from
    // bfcache — JS state is restored but the underlying WS was killed
    // when the page entered the cache, so we must dial again.
    const onPageshow = (e: PageTransitionEvent) => {
      if (e.persisted) kickAndRetry();
    };
    window.addEventListener("online", onOnline);
    window.addEventListener("focus", onFocus);
    window.addEventListener("pageshow", onPageshow);
    document.addEventListener("visibilitychange", onVisible);
    this.removeLifecycleHooks = () => {
      window.removeEventListener("online", onOnline);
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("pageshow", onPageshow);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }
}
