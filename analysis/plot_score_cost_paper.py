"""Paper-size version of student3_score_cost_readable.pdf.

Same data, quadrants, frontier and label placement as plot_score_cost.py, but the
three benchmarks are stacked in one figure drawn at the NeurIPS text width
(5.5 in), so every font size below is the size printed in the paper. Include it
with \\includegraphics[width=\\linewidth]{...} (no page= needed).

    python3 plot_score_cost_paper.py
"""
import sys
from pathlib import Path

sys.dont_write_bytecode = True

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator

import plot_score_cost as base
from plot_score_cost import (AXIS, BENCHES, COLORS, FONT, FRONTIER, GOOD_Q, GRID, INK, INK_2, MODELS, POOR_Q,
                             label_text, load_rows, money, nice_log_ticks, pareto, place_labels, score_axis)

HERE = Path(__file__).resolve().parent
OUT = HERE / 'student3_score_cost_paper.pdf'

# Printed sizes in pt (paper body text is 10 pt, captions 9 pt).
LABEL_PT = 6.0
TICK_PT = 7
AXIS_PT = 7.5
TITLE_PT = 8.5
LEGEND_PT = 7
MARKER_S = 30
X_PAD_HI = 2.6                   # right-hand room for labels, as a multiple of the largest cost

# Vertical layout in inches; the figure plus caption must fit one float page (9 in).
FIG_W = 5.5
LEFT, RIGHT = 0.47, 0.06
TOP_HEADER = 0.33                # shared legend block
PANEL_TITLE = 0.18
PANEL_H = 2.08                   # smallest height at which every label still fits
PANEL_GAP = 0.25                 # tick labels of the panel above
BOTTOM = 0.33                    # x ticks + x-axis title of the last panel
FIG_H = TOP_HEADER + 3 * (PANEL_TITLE + PANEL_H) + 2 * PANEL_GAP + BOTTOM

# place_labels reads these module globals at call time.
base.LABEL_PT = LABEL_PT
base.MARKER_S = MARKER_S


def style_axes(ax, xlabel, ylabel):
    for s in ('top', 'right'):
        ax.spines[s].set_visible(False)
    for s in ('left', 'bottom'):
        ax.spines[s].set_color(AXIS)
        ax.spines[s].set_linewidth(0.6)
    ax.set_axisbelow(True)
    ax.grid(axis='y', color=GRID, lw=0.5, zorder=0.5)
    ax.tick_params(axis='both', colors=AXIS, labelcolor=INK_2, labelsize=TICK_PT, length=2.5, width=0.6, pad=2)
    if xlabel:
        ax.set_xlabel(xlabel, fontsize=AXIS_PT, color=INK, labelpad=3)
    ax.set_ylabel(ylabel, fontsize=AXIS_PT, color=INK, labelpad=3)


def draw_panel(fig, ax, rows, bench, last):
    group = [r for r in rows if r['benchmark'] == bench]
    xs = np.array([r['cost'] for r in group])
    ys = np.array([r['score_pct'] for r in group])

    ax.set_xscale('log')
    xlo, xhi = xs.min() / 1.3, xs.max() * X_PAD_HI
    ax.xaxis.set_major_locator(FixedLocator(nice_log_ticks(xlo, xhi)))
    ax.xaxis.set_minor_locator(NullLocator())
    ax.xaxis.set_major_formatter(FuncFormatter(money))
    ax.set_xlim(xlo, xhi)
    ylo, yhi = score_axis(ax, ys)

    mx, my = float(np.median(xs)), float(np.median(ys))
    ax.add_patch(Rectangle((xlo, my), mx - xlo, yhi - my, facecolor=GOOD_Q, edgecolor='none', zorder=0))
    ax.add_patch(Rectangle((mx, ylo), xhi - mx, my - ylo, facecolor=POOR_Q, edgecolor='none', zorder=0))

    style_axes(ax, 'Model cost per task (USD, OpenRouter basis, log scale)' if last else None,
               'Score (fixed denominator)')

    front = pareto(group)
    if len(front) > 1:
        ax.plot(*zip(*front), color=FRONTIER, lw=0.9, ls=(0, (0.1, 2.0)), dash_capstyle='round', zorder=2)
    for r, x, y in zip(group, xs, ys):
        ax.scatter(x, y, s=MARKER_S, facecolor=COLORS[r['model']], edgecolor='white', linewidth=0.6, zorder=4)
    place_labels(fig, ax, xs, ys, [label_text(r) for r in group], front)

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

    key = [Patch(facecolor=GOOD_Q, edgecolor='none', label='Most attractive quadrant'),
           Patch(facecolor=POOR_Q, edgecolor='none', label='Least attractive quadrant'),
           Line2D([], [], color=FRONTIER, lw=0.9, ls=(0, (0.1, 2.0)), dash_capstyle='round', label='Pareto frontier')]
    models = [Line2D([], [], marker='o', ls='', ms=4.5, mfc=COLORS[m], mec='white', mew=0.5, label=m)
              for m in MODELS]
    x_leg = (LEFT - 0.08) / FIG_W
    leg1 = fig.legend(handles=key, loc='upper left', bbox_to_anchor=(x_leg, 1.0), ncol=len(key), frameon=False,
                      fontsize=LEGEND_PT, handlelength=1.6, handleheight=0.9, handletextpad=0.5, columnspacing=1.5,
                      borderaxespad=0.1)
    fig.legend(handles=models, loc='upper left', bbox_to_anchor=(x_leg, 1.0 - 0.19 / FIG_H), ncol=len(MODELS),
               frameon=False, fontsize=LEGEND_PT, handlelength=1.0, handletextpad=0.3, columnspacing=1.5,
               borderaxespad=0.1)
    fig.add_artist(leg1)

    fig.savefig(OUT)
    plt.close(fig)
    print(OUT)


if __name__ == '__main__':
    main()
