"""Render one `driftzero watch` replay frame to SVG (how docs/watch/*.png were made; rasterised with headless Chrome).

Usage: python scripts/watch_frames.py <run-id> <out.svg> [ticks into iteration 2]  (omit the last arg for the final frame)
"""
import os, sys
from pathlib import Path
from rich.console import Console
from rich.terminal_theme import MONOKAI
from driftzero import watch

runs, rid, out = Path("docs/runs"), sys.argv[1], Path(sys.argv[2])
ticks = watch.replay_ticks(runs, rid)
state = watch.WatchState(run_id=rid, replay_speed=10)
start2 = next(i for i, tk in enumerate(ticks) if tk[1] == "muse_start" and tk[2] == 2)
cut = start2 + int(sys.argv[3]) if len(sys.argv) > 3 else len(ticks)
for tk in ticks[:cut]:
    state.apply(*tk)
now = ticks[cut - 1][0]
if cut == len(ticks) and not state.done:
    state.apply(now, "exit", "log ended")
c = Console(width=120, height=watch.HEIGHT, record=True, file=open(os.devnull, "w"), force_terminal=True,
            color_system="truecolor")
c.print(watch.render(state, now))
out.write_text(c.export_svg(title=f"driftzero watch {rid} --replay 10", theme=MONOKAI))
