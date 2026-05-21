// Global push-to-talk: hold Option+Space to record, release either key
// to stop. Mirrors the muscle memory of claude's own /voice tap mode
// (which also lives on Option+Space on macOS) so a user who switches
// between the native Terminal.app and ccpipe doesn't have to relearn
// the chord.
//
// Why not bare Space:
//   Space is the most common terminal text input. Capturing it
//   unconditionally would break typing.
// Why not Ctrl+Space / Cmd+Space:
//   Cmd+Space is Spotlight on macOS; Ctrl+Space is i18n input switch
//   on Linux. Option (AltLeft/AltRight) is the smallest modifier no
//   common terminal command binds.
//
// Listeners attach in CAPTURE phase on document so the chord is
// intercepted before xterm's keypress handler can forward it to the
// PTY. preventDefault stops Firefox/Safari from scrolling on Space
// when nothing has focus, and stops xterm from passing Option+Space
// to the shell when the terminal is focused. We only call
// preventDefault on the keys that actually complete the chord —
// solo Option or solo Space remain functional.
//
// Edge: if the user Cmd+Tabs away mid-hold, keyup never fires and
// the chord would stick. window.blur acts as a forced release.
export interface PttHandlers {
  onHoldStart: () => void;
  onHoldEnd: () => void;
}

export interface PttDetach {
  (): void;
}

export function attachOptionSpacePtt(handlers: PttHandlers): PttDetach {
  let optDown = false;
  let spaceDown = false;
  let held = false;
  const chord = (): boolean => optDown && spaceDown;

  const onKeyDown = (e: KeyboardEvent): void => {
    if (e.code === "AltLeft" || e.code === "AltRight") optDown = true;
    if (e.code === "Space") spaceDown = true;
    if (!chord() || held || e.repeat) return;
    held = true;
    e.preventDefault();
    e.stopPropagation();
    handlers.onHoldStart();
  };

  const onKeyUp = (e: KeyboardEvent): void => {
    if (e.code === "AltLeft" || e.code === "AltRight") optDown = false;
    if (e.code === "Space") spaceDown = false;
    // Either side released → end the hold. Both releases fire this
    // twice but onHoldEnd is expected to be idempotent (the consumer
    // uses pttHoldActive to gate the actual stop).
    if (held && (!optDown || !spaceDown)) {
      held = false;
      e.preventDefault();
      e.stopPropagation();
      handlers.onHoldEnd();
    }
  };

  const onBlur = (): void => {
    if (held) {
      held = false;
      optDown = false;
      spaceDown = false;
      handlers.onHoldEnd();
    }
  };

  document.addEventListener("keydown", onKeyDown, true);
  document.addEventListener("keyup", onKeyUp, true);
  window.addEventListener("blur", onBlur);

  return () => {
    document.removeEventListener("keydown", onKeyDown, true);
    document.removeEventListener("keyup", onKeyUp, true);
    window.removeEventListener("blur", onBlur);
  };
}
