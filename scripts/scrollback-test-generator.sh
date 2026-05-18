#!/usr/bin/env bash
# Test data generator for scrollback-doctor.
#
# Emits a series of labelled phases designed to stress tmux's
# pane / scrollback / capture-pane handling and to give the doctor
# a wide variety of byte patterns to validate.  Each phase is
# wrapped with `=== PHASE X: NAME ===` and `--- END PHASE X ---`
# so the doctor can scope its per-phase assertions.  The whole run
# ends with `<<<DOCTOR-EOT <id>>>` so the doctor can synchronise on
# generator completion without races.
#
# Usage:
#   scripts/scrollback-test-generator.sh <run-id>
#
# The doctor passes a unique run-id (PID + timestamp typically) so a
# stale capture from a previous run can't false-match the current
# run's sentinel.

set -u

RUN_ID="${1:-$$}"

phase()     { printf '=== PHASE %s: %s ===\n' "$1" "$2"; }
phase_end() { printf -- '--- END PHASE %s ---\n' "$1"; }

# ─── PHASE A — numbered baseline (500 lines, hits scrollback) ────
# Mirrors the existing scrollback-doctor numbered-lines test so
# regressions in the simple case still trip a familiar assertion.
phase A "numbered baseline"
for i in $(seq 1 500); do
    printf 'line %04d\n' "$i"
done
phase_end A

# ─── PHASE B — wrapped long lines ────────────────────────────────
# Each line is much longer than the 80-col default so it soft-wraps
# across multiple terminal rows.  After replay the doctor reflows
# the wrapped visible cells back into a logical line and confirms
# the leading [wrap-N] / trailing [end-wrap-N] tags both survive.
LONG_BASE="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_+=*/%~"
phase B "wrapped long lines"
for n in 1 2 3 5 8; do
    target_len=$(( 80 * n + 3 ))
    line=""
    while [ ${#line} -lt "$target_len" ]; do
        line+="$LONG_BASE"
    done
    line="${line:0:$target_len}"
    printf '[wrap-%d] %s [end-wrap-%d]\n' "$n" "$line" "$n"
done
phase_end B

# ─── PHASE C — SGR colour & attribute coverage ───────────────────
# 8 basic + 8 bright fg, a few 256-colour and 24-bit truecolour
# variants, plus bold / italic / underline / combined.  pyte tracks
# fg/bg + booleans per cell, so the doctor can verify the right
# attributes landed on the right cells.
phase C "SGR attributes"
for c in 30 31 32 33 34 35 36 37; do
    printf '\x1b[%dmbasic-fg-%d\x1b[0m\n' "$c" "$c"
done
for c in 90 91 92 93 94 95 96 97; do
    printf '\x1b[%dmbright-fg-%d\x1b[0m\n' "$c" "$c"
done
for i in 16 82 196 226 51 208; do
    printf '\x1b[38;5;%dmpalette-%d\x1b[0m\n' "$i" "$i"
done
printf '\x1b[38;2;255;128;0mtruecolor-orange\x1b[0m\n'
printf '\x1b[38;2;0;200;200mtruecolor-cyan\x1b[0m\n'
printf '\x1b[1mbold\x1b[0m \x1b[3mitalic\x1b[0m \x1b[4munder\x1b[0m \x1b[9mstrike\x1b[0m\n'
printf '\x1b[1;3;4;31mall-on-red\x1b[0m\n'
# Background colour
printf '\x1b[41mred-bg\x1b[0m \x1b[42mgreen-bg\x1b[0m \x1b[44mblue-bg\x1b[0m\n'
phase_end C

# ─── PHASE D — cursor-overwrite (\r) ─────────────────────────────
# The classic progress-bar pattern.  After the trailing \n the only
# surviving content for each logical row should be the LAST string
# written before the \n.  Catches buggy line-accumulation paths.
phase D "cursor overwrite"
printf 'loading...\rmidway   \rdone     \n'
printf 'progress: 0%%\rprogress: 50%%\rprogress: 100%%\n'
printf 'pre-overwrite\rfinal-value\n'
phase_end D

# ─── PHASE E — erase-line / erase-display sequences ──────────────
# \x1b[2K erases the whole current line; \x1b[K from cursor to end
# of line.  We use them to "clean up" noisy text and verify that
# the noisy half doesn't appear in the captured pane.
phase E "erase sequences"
printf 'noisy-text-that-should-disappear\r\x1b[2Kclean-after-2K\n'
printf 'partial-erase-test ###\b\b\b\x1b[Kdone\n'
phase_end E

# ─── PHASE F — UTF-8, emoji, combining marks, box-drawing ────────
phase F "utf8 and box drawing"
printf 'emoji: 🦊 🚀 ✨ 🎉 (4 emoji)\n'
printf 'box: ┌─────────┐\n'
printf 'box: │  hello  │\n'
printf 'box: └─────────┘\n'
printf 'math: ∑ ∫ ∞ √2 ≈ 1.414  π = 3.14\n'
printf 'accents: café naïve résumé über Zürich\n'
printf 'cjk: 日本語 中文 한국어\n'
printf 'combining: e\xcc\x81 (e + acute) o\xcc\x88 (o + diaeresis)\n'
phase_end F

# ─── PHASE G — rapid burst (2000 lines, no delays) ───────────────
# Pumps a large volume of output as fast as the shell can emit it.
# Stresses tmux's pipe buffer, scrollback eviction, and ccpipe's
# PTY pump.  Every line in this phase must end up in the captured
# scrollback when the test rows + tmux history limit are sized to
# fit.
phase G "rapid burst"
for i in $(seq 1 2000); do
    printf 'burst-%05d\n' "$i"
done
phase_end G

# ─── PHASE H — mixed interleave ──────────────────────────────────
phase H "mixed interleave"
printf 'plain line before color\n'
printf '\x1b[33myellow chunk\x1b[0m | \x1b[32mgreen chunk\x1b[0m\n'
printf 'updating...\rdone updating\n'
printf 'plain line after\n'
phase_end H

# ─── PHASE I — claude-like banner + prompt + reply ───────────────
# Mock of a realistic Claude Code session opening: a banner using
# box-drawing chars, an assistant intro, a user prompt indicator,
# an assistant reply, and a tool-use marker.  Not a literal copy of
# claude's TUI but close enough to flush the same ANSI patterns
# through capture-pane.
phase I "claude-like banner"
printf '┌─ Claude Code ────────────────────────────┐\n'
printf '│ Welcome back, James!                     │\n'
printf '│ Opus 4.7 (1M context) with extended tools│\n'
printf '└──────────────────────────────────────────┘\n'
printf '\n'
printf '\x1b[2m>\x1b[0m walk me through the tmux integration\n'
printf '\x1b[1m●\x1b[0m The PTY relay attaches via tmux attach-session…\n'
printf '\x1b[36m✻\x1b[0m Worked for 3s\n'
phase_end I

# ─── PHASE J — edge cases (blank lines, single chars, tabs) ─────
phase J "edge cases"
printf '\n'                       # blank #1
printf '\n'                       # blank #2
printf '\n'                       # blank #3
printf 'X\n'                      # single char line
printf 'Y\nZ\n'                   # two back-to-back single chars
printf 'col-a\tcol-b\tcol-c\n'    # tab-separated
printf 'trailing-spaces   \n'     # trailing whitespace
printf ' leading-spaces\n'        # leading whitespace
phase_end J

# ─── Sentinel — tells the doctor the run is fully written ────────
# Random RUN_ID inside the marker so a stale capture from a previous
# run can't false-match this run's completion.
printf '<<<DOCTOR-EOT %s>>>\n' "$RUN_ID"
