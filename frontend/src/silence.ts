// A moment of silence as an audio URL. Playing it inside a tap unlocks an
// <audio> element on Safari (iPad, iPhone), which then plays sound the page
// starts by itself later. Used by the TTS player and the voice test.

let silentUrl: string | null = null;
/** 50 ms of silence, played inside a tap to unlock the audio element. */
export function silence(): string {
  if (silentUrl) return silentUrl;
  const n = 400;
  const buf = new ArrayBuffer(44 + n);
  const v = new DataView(buf);
  const tag = (o: number, t: string) => { for (let i = 0; i < t.length; i++) v.setUint8(o + i, t.charCodeAt(i)); };
  tag(0, "RIFF"); v.setUint32(4, 36 + n, true); tag(8, "WAVE");
  tag(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, 8000, true); v.setUint32(28, 8000, true); v.setUint16(32, 1, true); v.setUint16(34, 8, true);
  tag(36, "data"); v.setUint32(40, n, true);
  new Uint8Array(buf, 44).fill(128);
  silentUrl = URL.createObjectURL(new Blob([buf], { type: "audio/wav" }));
  return silentUrl;
}
