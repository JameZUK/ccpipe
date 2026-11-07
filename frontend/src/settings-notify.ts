// Settings → Notifications: what happens when Claude asks you something, or
// finishes a turn, in a session you aren't looking at.
//
// - Sound and Say it: this device only (attention.ts, localStorage).
// - Push: this device's subscription (push.ts); its choices are kept by the
//   server with the subscription, which decides what to send it.
// - The wait before pushing while ccpipe is on screen elsewhere: the whole
//   server (notify.push_wait_s).
// - The open tab's own notification when it's in the background
//   (notifications.ts): this browser only.

import { h } from "./dom";
import { DEFAULT_ALERTS, loadAlerts, previewChime, saveAlerts, type Level } from "./attention";
import { deviceClass } from "./mobile";
import {
  disablePush, enablePush, forgetDevice, pushState, pushStatus, pushSupport,
  setPushPrefs, setPushWait, testPush, type PushDevice, type PushPrefs,
} from "./push";
import { isEnabled, notificationsSupported, requestPermission, setEnabled } from "./notifications";

const WANT_KEY = "ccpipe.pushWant";     // the push choices before push is on
const WAITS: Array<[number, string]> = [
  [0, "don't wait"], [30, "30 s"], [60, "1 min"], [120, "2 min"], [300, "5 min"], [900, "15 min"],
];

function checkbox(checked: boolean, label: string): HTMLInputElement {
  const c = h("input", "checkbox");
  c.type = "checkbox";
  c.checked = checked;
  c.setAttribute("aria-label", label);
  return c;
}

function row(label: string, hint: string, control: HTMLElement): HTMLLabelElement {
  const r = h("label", "row");
  const l = h("span", "row__label", label);
  if (hint) l.append(h("span", "row__hint", hint));
  r.append(l, control);
  return r;
}

function loadWant(): PushPrefs {
  const d: PushPrefs = { ask: true, done: false, details: true };
  try {
    const raw = JSON.parse(localStorage.getItem(WANT_KEY) || "{}");
    for (const k of ["ask", "done", "details"] as const) if (typeof raw[k] === "boolean") d[k] = raw[k];
  } catch { /* defaults */ }
  return d;
}

function saveWant(p: PushPrefs): void {
  try { localStorage.setItem(WANT_KEY, JSON.stringify(p)); } catch { /* private mode */ }
}

function ago(unix: number): string {
  if (!unix) return "nothing delivered yet";
  const s = Math.max(0, Date.now() / 1000 - unix);
  if (s < 90) return "last delivered just now";
  if (s < 5400) return `last delivered ${Math.round(s / 60)} min ago`;
  if (s < 172800) return `last delivered ${Math.round(s / 3600)} h ago`;
  return `last delivered ${Math.round(s / 86400)} days ago`;
}

export function buildNotifySection(): HTMLElement {
  const sec = h("section", "modal__section");
  sec.append(h("h3", "modal__section-title", "notifications"));
  sec.append(h("p", "modal__note",
    "When Claude asks you something (a question, or permission to run something), or finishes "
    + "a turn in a session you aren't looking at. The session list marks them, and a session's "
    + "top bar shows a bell for the others."));

  // ── The grid: what happens, for each level ──
  const alerts = loadAlerts();
  const want = loadWant();
  const grid = h("div", "notify-grid");
  grid.setAttribute("role", "table");
  const headRow = h("div", "notify-grid__row notify-grid__row--head");
  headRow.setAttribute("role", "row");
  for (const t of ["", "sound", "say it", "push"]) {
    const c = h("span", "notify-grid__h", t);
    c.setAttribute("role", "columnheader");
    headRow.append(c);
  }
  grid.append(headRow);
  const pushBoxes: Record<Level, HTMLInputElement> = {} as Record<Level, HTMLInputElement>;
  const levels: Array<[Level, string, string]> = [
    ["ask", "Questions & permission", "urgent"],
    ["done", "Finished turns", "quiet"],
  ];
  for (const [lv, label, hint] of levels) {
    const r = h("div", "notify-grid__row");
    r.setAttribute("role", "row");
    const name = h("span", "notify-grid__name", label);
    name.append(h("span", "row__hint", hint));
    const sound = checkbox(alerts[lv].sound, `${label}: sound`);
    const speak = checkbox(alerts[lv].speak, `${label}: say which session`);
    const push = checkbox(want[lv], `${label}: push notification`);
    pushBoxes[lv] = push;
    sound.addEventListener("change", () => {
      const p = loadAlerts();
      p[lv].sound = sound.checked;
      saveAlerts(p);
      if (sound.checked) previewChime(lv);
    });
    speak.addEventListener("change", () => {
      const p = loadAlerts();
      p[lv].speak = speak.checked;
      saveAlerts(p);
    });
    push.addEventListener("change", () => {
      const w = loadWant();
      w[lv] = push.checked;
      saveWant(w);
      void savePushPrefs({ [lv]: push.checked });
    });
    for (const c of [name, sound, speak, push]) c.setAttribute("role", "cell");
    r.append(name, sound, speak, push);
    grid.append(r);
  }
  sec.append(grid);
  sec.append(h("p", "modal__note",
    `Sound and Say it are for this ${deviceClass()}: a chime, and the session's name in ccpipe's `
    + "voice (quiet on a muted session page, or with Voice → What to read aloud set to Off). "
    + "A session's own questions are read out in full, as before. Defaults: "
    + `questions ${DEFAULT_ALERTS.ask.sound ? "chime" : "are silent"} and are said; finished turns chime softly.`));

  // ── Push on this device ──
  const pushSec = h("div", "notify-push");
  const pushHead = h("div", "notify-push__head");
  const pushState_ = h("span", "notify-push__state", "checking…");
  const onBtn = h("button", "btn btn--primary", "Turn on");
  onBtn.type = "button";
  const testBtn = h("button", "btn", "Send a test");
  testBtn.type = "button";
  const offBtn = h("button", "btn btn--ghost", "Turn off");
  offBtn.type = "button";
  for (const b of [onBtn, testBtn, offBtn]) b.hidden = true;
  const btns = h("div", "notify-push__btns");
  btns.append(onBtn, testBtn, offBtn);
  pushHead.append(h("span", "row__label", "Push notifications on this device"), btns);
  const details = checkbox(want.details, "Show the question or reply in the notification");
  const detailsRow = row("Show the question or the reply", "off: only which session, for a lock screen", details);
  const wait = h("select", "select");
  for (const [v, label] of WAITS) {
    const o = h("option", undefined, label);
    o.value = String(v);
    wait.append(o);
  }
  const waitRow = row("While ccpipe is open on another device, wait",
    "in case you answer it there · for every device", wait);
  const devices = h("div", "notify-devices");
  const status = h("span", "modal__status");
  pushSec.append(pushHead, pushState_, detailsRow, waitRow, devices, status);
  sec.append(pushSec);

  const setStatus = (msg: string, error = false): void => {
    status.textContent = msg;
    status.classList.toggle("modal__status--error", error);
  };

  let mine: PushSubscription | null = null;
  let myId = "";
  const support = pushSupport();

  const paintDevices = (list: PushDevice[]): void => {
    devices.replaceChildren();
    if (!list.length) return;
    devices.append(h("div", "notify-devices__title", "devices with push on"));
    for (const d of list) {
      const isMe = !!mine && !!myId && d.id === myId;
      const r = h("div", "notify-devices__row");
      const what = [d.prefs.ask ? "questions" : "", d.prefs.done ? "finished turns" : ""].filter(Boolean).join(", ");
      r.append(h("span", "notify-devices__name", `${d.label || "a device"}${isMe ? " (this one)" : ""}`),
        h("span", "notify-devices__meta", `${what || "nothing"} · ${ago(d.ok_at)}`));
      if (!isMe) {
        const rm = h("button", "btn btn--ghost notify-devices__rm", "Remove");
        rm.type = "button";
        rm.addEventListener("click", async () => {
          rm.disabled = true;
          try { await forgetDevice(d.id); await refresh(); } catch (e) {
            setStatus(`couldn't remove it: ${(e as Error).message}`, true);
            rm.disabled = false;
          }
        });
        r.append(rm);
      }
      devices.append(r);
    }
  };

  const paint = (): void => {
    const on = !!mine;
    if (!support.ok) {
      pushState_.textContent = support.why;
    } else if (on) {
      pushState_.textContent = "On for this device.";
    } else if (typeof Notification !== "undefined" && Notification.permission === "denied") {
      pushState_.textContent = "Blocked: allow notifications for ccpipe in this browser's settings, then turn them on.";
    } else {
      pushState_.textContent = "Off for this device. They reach it even with ccpipe closed.";
    }
    onBtn.hidden = !support.ok || on;
    testBtn.hidden = !on;
    offBtn.hidden = !on;
    for (const b of Object.values(pushBoxes)) b.disabled = !support.ok;
    details.disabled = !support.ok;
  };

  const refresh = async (): Promise<void> => {
    let me: PushDevice | null = null;
    myId = "";
    if (support.ok) ({ sub: mine, device: me } = await pushStatus());
    else mine = null;
    try {
      const st = await pushState();
      wait.value = String(st.push_wait_s);
      if (![...wait.options].some((o) => o.value === wait.value)) {
        const o = h("option", undefined, `${st.push_wait_s} s`);
        o.value = String(st.push_wait_s);
        wait.append(o);
        wait.value = o.value;
      }
      if (me) {
        // The server's copy is the truth once push is on.
        myId = me.id;
        pushBoxes.ask.checked = me.prefs.ask;
        pushBoxes.done.checked = me.prefs.done;
        details.checked = me.prefs.details;
        saveWant(me.prefs);
      }
      paintDevices(st.devices);
    } catch (e) {
      setStatus(`couldn't load: ${(e as Error).message}`, true);
    }
    paint();
  };

  async function savePushPrefs(p: Partial<PushPrefs>): Promise<void> {
    if (!mine) return;
    try {
      await setPushPrefs(p);
      setStatus("saved");
      setTimeout(() => setStatus(""), 1200);
    } catch (e) {
      setStatus(`save failed: ${(e as Error).message}`, true);
    }
  }

  details.addEventListener("change", () => {
    const w = loadWant();
    w.details = details.checked;
    saveWant(w);
    void savePushPrefs({ details: details.checked });
  });
  wait.addEventListener("change", async () => {
    try {
      await setPushWait(parseInt(wait.value, 10) || 0);
      setStatus("saved");
      setTimeout(() => setStatus(""), 1200);
    } catch (e) {
      setStatus(`save failed: ${(e as Error).message}`, true);
    }
  });
  onBtn.addEventListener("click", () => {
    // Straight from the tap: the permission prompt has to be the first
    // thing it waits for.
    setStatus("turning on…");
    onBtn.disabled = true;
    enablePush(loadWant()).then(async () => {
      setStatus("on: a test is on its way");
      await refresh();
      const r = await testPush();
      if (!r.ok) setStatus(`the test didn't go: ${r.status || ""} ${r.detail}`.trim(), true);
    }).catch((e) => {
      setStatus((e as Error).message, true);
    }).finally(() => { onBtn.disabled = false; });
  });
  testBtn.addEventListener("click", async () => {
    setStatus("sending…");
    const r = await testPush().catch((e) => ({ ok: false, status: 0, detail: (e as Error).message }));
    if (r.ok) setStatus("sent: it should show in a moment");
    else setStatus(`didn't go: ${r.status || ""} ${r.detail}`.trim(), true);
  });
  offBtn.addEventListener("click", async () => {
    setStatus("turning off…");
    await disablePush().catch(() => {});
    setStatus("");
    await refresh();
  });
  void refresh();

  // ── The open tab's own notification ──
  const tabNotify = checkbox(isEnabled(), "Notify from this tab when it's in the background");
  const tabRow = row("Notify from an open tab in the background",
    "this browser only · a notification while the tab is hidden; push does this for devices with it on",
    tabNotify);
  if (!notificationsSupported()) {
    tabNotify.disabled = true;
    tabNotify.title = "this browser doesn't support notifications";
  }
  tabNotify.addEventListener("change", async () => {
    if (tabNotify.checked) {
      const perm = await requestPermission();
      if (perm !== "granted") {
        tabNotify.checked = false;
        setStatus("notification permission denied", true);
        return;
      }
      setEnabled(true);
    } else {
      setEnabled(false);
    }
  });
  const rows = h("div", "modal__rows");
  rows.append(tabRow);
  sec.append(rows);
  return sec;
}
