"""Paper-size version of student3_aa_task_cost_distributions.pdf.

Reads data/task_level.tsv (build_task_level.py); same filtering and box definition as
plot_task_cost_distributions.py (median, interquartile range, whiskers to 1.5 x IQR, no
outliers, $0 tasks excluded), with the three benchmarks stacked in one figure at the NeurIPS text
width (5.5 in). Include it with \\includegraphics[width=\\linewidth]{...}.

    python3 plot_task_cost_distributions_paper.py
"""
import csv
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
OUT = HERE / 'student3_task_cost_distributions_paper.pdf'

ROW_LABEL_PT = 6
ROW_H = 0.105                    # inches per configuration row
LEFT, RIGHT = 1.42, 0.08         # row labels on the left
TOP_HEADER = 0.22
PANEL_TITLE = 0.18
PANEL_GAP = 0.22
BOTTOM = 0.36
LINE_W = 0.8


def load_task_costs():
    """Per-task costs from data/task_level.tsv; tasks with no model call ($0) are excluded."""
    groups = defaultdict(list)
    with (HERE / 'data' / 'task_level.tsv').open(encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            if r['cost_usd'] and float(r['cost_usd']) > 0:
                groups[(r['harness'], r['model'], r['benchmark'])].append(float(r['cost_usd']))
    return groups


def panel_keys(groups, bench):
    # Highest median at the top (boxplot row 1 is drawn at the bottom).
    return sorted((k for k in groups if k[2] == bench), key=lambda k: (np.median(groups[k]), k[0], k[1]))


def draw_panel(fig, ax, groups, keys, bench, last):
    n = len(keys)
    bp = ax.boxplot([groups[k] for k in keys], vert=False, widths=0.62, whis=1.5, showfliers=False,
                    patch_artist=True, medianprops={'color': 'white', 'linewidth': 1.1},
                    boxprops={'linewidth': 0.6}, whiskerprops={'linewidth': LINE_W}, capprops={'linewidth': LINE_W})
    for i, (h, _, _) in enumerate(keys):
        c = HARNESS_COLORS[h]
        bp['boxes'][i].set(facecolor=c, edgecolor=c)
        for artist in bp['whiskers'][2 * i:2 * i + 2] + bp['caps'][2 * i:2 * i + 2]:
            artist.set(color=c, solid_capstyle='round')

    ax.set_xscale('log')
    lo = min(float(np.min(w.get_xdata())) for w in bp['whiskers']) / 1.6
    hi = max(float(np.max(w.get_xdata())) for w in bp['whiskers']) * 1.6
    ax.set_xlim(lo, hi)
    ax.xaxis.set_major_locator(FixedLocator([10.0 ** e for e in range(-4, 4) if lo <= 10.0 ** e <= hi]))
    ax.xaxis.set_minor_locator(NullLocator())
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: f'${v:g}'))
    ax.set_ylim(0.4, n + 0.6)
    ax.set_yticks(range(1, n + 1))
    ax.set_yticklabels([f'{h} · {m}' for h, m, _ in keys], fontsize=ROW_LABEL_PT, color=INK, family=FONT)

    for s in ('top', 'right', 'left'):
        ax.spines[s].set_visible(False)
    ax.spines['bottom'].set_color(AXIS)
    ax.spines['bottom'].set_linewidth(0.6)
    ax.set_axisbelow(True)
    ax.grid(axis='x', color=GRID, lw=0.5, zorder=0)
    ax.tick_params(axis='y', length=0, pad=3)
    ax.tick_params(axis='x', colors=AXIS, labelcolor=INK_2, labelsize=TICK_PT, length=2.5, width=0.6, pad=2)
    if last:
        ax.set_xlabel('Recorded model cost per task (USD, log scale)', fontsize=AXIS_PT, color=INK, labelpad=3)

    pos = ax.get_position()
    fig.text(0.06 / FIG_W, pos.y1 + 0.05 / fig.get_figheight(), bench, fontsize=TITLE_PT, weight='bold', color=INK,
             family=FONT, va='bottom')


def main():
    groups = load_task_costs()
    plt.rcParams.update({'font.family': FONT, 'pdf.fonttype': 42, 'axes.unicode_minus': False,
                         'figure.facecolor': 'white', 'savefig.facecolor': 'white'})
    keys = {b: panel_keys(groups, b) for b in BENCHES}
    heights = {b: (len(keys[b]) + 0.2) * ROW_H for b in BENCHES}
    fig_h = TOP_HEADER + sum(PANEL_TITLE + h for h in heights.values()) + 2 * PANEL_GAP + BOTTOM
    fig = plt.figure(figsize=(FIG_W, fig_h), dpi=300)

    y = fig_h - TOP_HEADER
    for i, bench in enumerate(BENCHES):
        y -= PANEL_TITLE + heights[bench]
        ax = fig.add_axes([LEFT / FIG_W, y / fig_h, (FIG_W - LEFT - RIGHT) / FIG_W, heights[bench] / fig_h])
        draw_panel(fig, ax, groups, keys[bench], bench, last=i == len(BENCHES) - 1)
        y -= PANEL_GAP

    present = [h for h in HARNESS_COLORS if any(k[0] == h for ks in keys.values() for k in ks)]
    handles = [Line2D([], [], marker='s', ls='', ms=5, mfc=HARNESS_COLORS[h], mec=HARNESS_COLORS[h], label=h)
               for h in present]
    fig.legend(handles=handles, loc='upper left', bbox_to_anchor=(0.0, 1.0), ncol=len(handles), frameon=False,
               fontsize=LEGEND_PT, handlelength=1.0, handletextpad=0.3, columnspacing=1.5, borderaxespad=0.1)

    fig.savefig(OUT)
    plt.close(fig)
    print(OUT)


if __name__ == '__main__':
    main()
