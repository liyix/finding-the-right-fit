"""Score versus uncached input tokens per task, for the four configurable harnesses.

Uncached input is input minus cache reads, summed over each task's scored run and
divided by the fixed task count; it comes from data/task_level.tsv, where input
already includes cached tokens for every harness. Scores are the table values. The
three benchmarks are stacked in one figure at the NeurIPS text width (5.5 in).

    python3 plot_score_vs_prompt_tokens_paper.py
"""
import csv
import sys
from collections import Counter
from pathlib import Path

sys.dont_write_bytecode = True

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator

import plot_score_cost_paper as paper   # also sets the paper label/marker sizes used by place_labels
from plot_score_cost import BENCHES, FONT, HARNESS_COLORS, INK, label_text, load_rows, nice_log_ticks, place_labels, score_axis
from plot_score_cost_paper import (BOTTOM, FIG_W, LEFT, LEGEND_PT, MARKER_S, PANEL_GAP, PANEL_TITLE, RIGHT, TITLE_PT,
                                   style_axes)
from plot_score_vs_prompt_tokens import tokens

HERE = Path(__file__).resolve().parent
OUT = HERE / 'student3_score_vs_prompt_tokens_paper.pdf'

HARNESSES = ('OpenHands', 'DSH', 'PI', 'openJiuwen')
TOP_HEADER = 0.22                # one legend row
PANEL_H = 2.08
X_PAD_HI = 2.2
FIG_H = TOP_HEADER + 3 * (PANEL_TITLE + PANEL_H) + 2 * PANEL_GAP + BOTTOM


def uncached_per_task():
    tot, n = Counter(), Counter()
    with (HERE / 'data' / 'task_level.tsv').open(encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            k = (r['harness'], r['benchmark'], r['model'])
            tot[k] += float(r['input_tokens'] or 0) - float(r['cached_tokens'] or 0)
            n[k] += 1
    return {k: tot[k] / n[k] for k in tot}


def draw_panel(fig, ax, rows, bench, last):
    uncached = uncached_per_task()
    group = [r for r in rows if r['benchmark'] == bench and r['harness'] in HARNESSES]
    xs = np.array([uncached[(r['harness'], r['benchmark'], r['model'])] for r in group])
    ys = np.array([r['score_pct'] for r in group])

    ax.set_xscale('log')
    xlo, xhi = xs.min() / 1.3, xs.max() * X_PAD_HI
    ax.set_xlim(xlo, xhi)
    ax.xaxis.set_major_locator(FixedLocator(nice_log_ticks(xlo, xhi)))
    ax.xaxis.set_minor_locator(NullLocator())
    ax.xaxis.set_major_formatter(FuncFormatter(tokens))
    score_axis(ax, ys)
    style_axes(ax, 'Uncached input tokens per task (log scale)' if last else None,
               'Score (fixed denominator)')

    for r, x, y in zip(group, xs, ys):
        ax.scatter(x, y, s=MARKER_S, facecolor=HARNESS_COLORS[r['harness']], edgecolor='white', linewidth=0.6,
                   zorder=4)
    place_labels(fig, ax, xs, ys, [label_text(r) for r in group], [])

    pos = ax.get_position()
    fig.text(pos.x0, pos.y1 + 0.06 / FIG_H, bench, fontsize=TITLE_PT, weight='bold', color=INK, family=FONT,
             va='bottom')


def main():
    rows = load_rows()
    plt.rcParams.update({'font.family': FONT, 'pdf.fonttype': 42, 'axes.unicode_minus': False,
                         'figure.facecolor': 'white', 'savefig.facecolor': 'white'})
    fig = plt.figure(figsize=(FIG_W, FIG_H), dpi=300)

    y_top = FIG_H - TOP_HEADER - PANEL_TITLE
    for i, bench in enumerate(BENCHES):
        y0 = y_top - PANEL_H - i * (PANEL_H + PANEL_GAP + PANEL_TITLE)
        ax = fig.add_axes([LEFT / FIG_W, y0 / FIG_H, (FIG_W - LEFT - RIGHT) / FIG_W, PANEL_H / FIG_H])
        draw_panel(fig, ax, rows, bench, last=i == len(BENCHES) - 1)

    handles = [Line2D([], [], marker='o', ls='', ms=4.5, mfc=HARNESS_COLORS[h], mec='white', mew=0.5, label=h)
               for h in HARNESSES]
    fig.legend(handles=handles, loc='upper left', bbox_to_anchor=((LEFT - 0.08) / FIG_W, 1.0), ncol=len(handles),
               frameon=False, fontsize=LEGEND_PT, handlelength=1.0, handletextpad=0.3, columnspacing=1.5,
               borderaxespad=0.1)

    fig.savefig(OUT)
    plt.close(fig)
    print(OUT)


if __name__ == '__main__':
    main()
