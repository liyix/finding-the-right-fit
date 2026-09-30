"""Effort on solved versus failed tasks (appendix figure).

For every configuration with at least three solved (reward 1) and three failed
(reward 0) tasks, the median model calls (top row) and median agent minutes (bottom
row) on failed tasks (x) against solved tasks (y), one panel per benchmark. Points
above the diagonal spend more on the tasks they solve. Reads data/task_level.tsv.
Drawn at the NeurIPS text width (5.5 in).

    python3 plot_turns_by_outcome_paper.py
"""
import csv
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

sys.dont_write_bytecode = True

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator

from plot_score_cost import AXIS, BENCHES, FONT, GRID, HARNESS_COLORS, INK, INK_2
from plot_score_cost_paper import AXIS_PT, FIG_W, LEGEND_PT, TICK_PT, TITLE_PT

HERE = Path(__file__).resolve().parent
DATA = HERE / 'data' / 'task_level.tsv'
OUT = HERE / 'student3_turns_by_outcome_paper.pdf'

HARNESS_ORDER = ['OpenHands', 'DSH', 'PI', 'openJiuwen', 'Codex', 'Claude Code']
MIN_N = 3
ROWS = [('model_calls', 'Model calls', 1.0, [5, 10, 20, 50, 100, 200, 500]),
        ('agent_seconds', 'Agent minutes', 1 / 60, [1, 2, 5, 10, 20, 50, 100, 200])]

LEFT, RIGHT = 0.62, 0.08
COL_GAP = 0.32
TOP_HEADER = 0.24
TITLE_H = 0.18
ROW_GAP = 0.42
BOTTOM = 0.34


def load():
    groups = defaultdict(lambda: {m: {'s': [], 'f': []} for m, *_ in ROWS})
    with DATA.open(encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            reward = float(r['reward'])
            side = 's' if reward >= 0.999 else 'f' if reward <= 0.001 else None
            if side is None:
                continue
            for metric, _, scale, _ in ROWS:
                if r[metric]:
                    groups[(r['benchmark'], r['harness'], r['model'])][metric][side].append(float(r[metric]) * scale)
    out = defaultdict(dict)
    for key, g in groups.items():
        for metric, *_ in ROWS:
            s, f = g[metric]['s'], g[metric]['f']
            if len(s) >= MIN_N and len(f) >= MIN_N:
                out[metric][key] = (st.median(f), st.median(s))
    return out


def main():
    data = load()
    plt.rcParams.update({'font.family': FONT, 'pdf.fonttype': 42, 'axes.unicode_minus': False,
                         'figure.facecolor': 'white', 'savefig.facecolor': 'white'})
    side = (FIG_W - LEFT - RIGHT - 2 * COL_GAP) / 3
    fig_h = TOP_HEADER + 2 * (TITLE_H + side) + ROW_GAP + BOTTOM
    fig = plt.figure(figsize=(FIG_W, fig_h), dpi=300)
    present = set()

    for ri, (metric, label, _, ticks_all) in enumerate(ROWS):
        y0 = fig_h - TOP_HEADER - TITLE_H - side - ri * (side + TITLE_H + ROW_GAP)
        for bi, bench in enumerate(BENCHES):
            x0 = LEFT + bi * (side + COL_GAP)
            ax = fig.add_axes([x0 / FIG_W, y0 / fig_h, side / FIG_W, side / fig_h])
            pts = {k: v for k, v in data[metric].items() if k[0] == bench}
            vals = np.array([v for pair in pts.values() for v in pair])
            lo, hi = vals.min() / 1.35, vals.max() * 1.35
            ax.plot([lo, hi], [lo, hi], color='#b9b8b0', lw=0.7, ls=(0, (3, 2)), zorder=1)
            for (_, h, _), (fx, sy) in sorted(pts.items(), key=lambda kv: HARNESS_ORDER.index(kv[0][1])):
                present.add(h)
                ax.scatter(fx, sy, s=15, facecolor=HARNESS_COLORS[h], edgecolor='white', linewidth=0.4, zorder=3)
            above = sum(sy > fx for fx, sy in pts.values())
            ax.text(0.04, 0.96, f'{above}/{len(pts)} above', transform=ax.transAxes, fontsize=TICK_PT - 0.5,
                    color=INK_2, family=FONT, va='top')
            ax.set_xscale('log')     # set the scale first: it resets locators and formatters
            ax.set_yscale('log')
            for axis in (ax.xaxis, ax.yaxis):
                axis.set_major_locator(FixedLocator([t for t in ticks_all if lo <= t <= hi]))
                axis.set_minor_locator(NullLocator())
                axis.set_major_formatter(FuncFormatter(lambda v, _p: f'{v:g}'))
            ax.set_xlim(lo, hi)
            ax.set_ylim(lo, hi)
            for s_ in ('top', 'right'):
                ax.spines[s_].set_visible(False)
            for s_ in ('left', 'bottom'):
                ax.spines[s_].set_color(AXIS)
                ax.spines[s_].set_linewidth(0.6)
            ax.set_axisbelow(True)
            ax.grid(color=GRID, lw=0.5, zorder=0)
            ax.tick_params(colors=AXIS, labelcolor=INK_2, labelsize=TICK_PT - 0.5, length=2.5, width=0.6, pad=2)
            ax.set_xlabel(f'{label}, failed tasks', fontsize=AXIS_PT - 0.5, color=INK, labelpad=2)
            if bi == 0:
                ax.set_ylabel(f'{label}, solved tasks', fontsize=AXIS_PT - 0.5, color=INK, labelpad=2)
            if ri == 0:
                ax.set_title(bench, fontsize=TITLE_PT, weight='bold', color=INK, family=FONT, loc='left', pad=5)

    handles = [Line2D([], [], marker='o', ls='', ms=4.5, mfc=HARNESS_COLORS[h], mec='white', mew=0.5, label=h)
               for h in HARNESS_ORDER if h in present]
    fig.legend(handles=handles, loc='upper left', bbox_to_anchor=(0.0, 1.0), ncol=len(handles), frameon=False,
               fontsize=LEGEND_PT, handlelength=1.0, handletextpad=0.3, columnspacing=1.5, borderaxespad=0.1)
    fig.savefig(OUT)
    plt.close(fig)
    print(OUT)


if __name__ == '__main__':
    main()
