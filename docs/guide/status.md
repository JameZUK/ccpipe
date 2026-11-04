# Status: the machine and your Claude plan

[← Documentation](../README.md)

- [System status](#system-status)
- [Claude plan usage](#claude-plan-usage)

## System status

Beside the ping in a session's top bar:

- **On a wide screen** there's a CPU graph, memory and disk bars, and the
  project's size.
- **On a phone or a narrow window** that becomes four thin bars: CPU,
  memory, disk and Claude plan usage, from left to right. Each bar is
  amber below 70 %, orange from 70 % and red from 90 %.

Click or tap it for a pop-up with the graphs and the exact figures. It
also shows this session's own memory (its whole process tree), CPU (as
in `top`, 100% per core) and project size. On the compact meter the
pop-up starts with a key to the bars, and tapping it on a phone doesn't
close the soft keyboard.

Each part can be switched off per device, and the refresh interval set
(2–30 s), under Settings → Display → *status & graphs*. A part that's
switched off stops asking the server too.

<p align="center">
  <img src="../screenshots/Desktop-Status.png" width="620" alt="A session's top bar on a desktop: the inline CPU graph, memory, disk and plan bars and project size, with the status pop-up open below showing CPU and memory graphs, disk use, the Claude plan's meters with reset times and pace ticks, usage credits, and this session's memory, CPU, project size and context fill">
</p>
<p align="center">
  <img src="../screenshots/Mobile-Status.jpg" width="220" alt="The status pop-up opened from the four-bar meter: a key naming the cpu, mem, disk and plan bars and their colours, CPU and memory graphs, disk use, the Claude plan's 5-hour, weekly and per-model meters with their reset times and pace ticks, usage credits, and this session's memory, CPU, project size and context fill">
</p>

## Claude plan usage

The top bar, its pop-up and the landing page also show how much of your
Claude subscription's usage limits you've used.

- **The top bar and the landing page** show one figure: the meter
  Anthropic picks for a single readout, usually the week across all
  models. On a phone it's the fourth bar in the meter.
- **The pop-up** lists every meter Anthropic reports for your plan, each
  with when it resets. That can be the weekly limit for all models, a
  weekly limit for one model, or the 5-hour window on plans that have
  one. A tick on each bar marks how much of its window has gone, so a bar
  reaching past its tick is using the plan faster than an even pace.
  Below them are your usage credits and how old the figures are.
- **This session** in the pop-up adds how full its context window is.

The figures come from Claude Code itself, so ccpipe never touches your
Claude login:

- **After each reply,** from what Claude gives its status line. ccpipe
  starts Claude with a status-line command that records it and then runs
  your own status line, so Claude's footer doesn't change. Sessions
  already running when you update ccpipe start reporting once they're
  restarted or resumed.
- **Every 15 minutes while ccpipe is open**, and when you open the pop-up
  on figures more than 5 minutes old, ccpipe runs `claude -p /usage`. That
  makes no model call and uses no tokens, and it also sees use from
  claude.ai, the apps and other machines. One run takes about 13 s of one
  CPU core and up to 400 MB of memory.

Settings → Display → *status & graphs* → *Claude plan usage* hides it on
one device. To turn it off altogether, or change how often it checks, see
[Configuration → Claude plan usage](../configuration.md#claude-plan-usage).
