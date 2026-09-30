"""Distribution of per-task costs (horizontal box plots), one page per benchmark.

Reads data/final_task_cost_ledger.csv, which has one row per task (final attempt)
for the 48 configurations with a task-level ledger. Tasks with a recorded cost of
$0 (final attempts that made no model call) are excluded. Boxes show the median
and interquartile range, whiskers extend to the furthest task within 1.5 x IQR,
and outliers are not drawn. Colored by harness, as in the original figure.

Run with the Anaconda interpreter, e.g.
    D:\\Anaconda\\python.exe plot_task_cost_distributions.py
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
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.lines import Line2D
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator

from plot_score_cost import AXIS, BENCHES, FONT, GRID, HARNESS_COLORS, INK, INK_2

HERE = Path(__file__).resolve().parent
LEDGER = HERE / 'data' / 'final_task_cost_ledger.csv'
OUT = HERE / 'student3_aa_task_cost_distributions.pdf'

LINE_W = 1.6        # whiskers and caps, pt


def load_task_costs():
    groups = defaultdict(list)
    with LEDGER.open(encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f):
            cost = float(r['cost_usd'])
            if cost > 0:
                groups[(r['harness'], r['model'], r['benchmark'])].append(cost)
    return groups


def draw_page(pdf, groups, bench):
    # Highest median at the top (boxplot row 1 is drawn at the bottom).
    keys = sorted((k for k in groups if k[2] == bench), key=lambda k: (np.median(groups[k]), k[0], k[1]))
    n = len(keys)

    fig = plt.figure(figsize=(12, 9.2), dpi=100)
    ax = fig.add_axes([0.235, 0.085, 0.72, 0.745])

    bp = ax.boxplot([groups[k] for k in keys], vert=False, widths=0.62, whis=1.5, showfliers=False,
                    patch_artist=True, medianprops={'color': 'white', 'linewidth': 2.2},
                    boxprops={'linewidth': 1.2}, whiskerprops={'linewidth': LINE_W},
                    capprops={'linewidth': LINE_W})
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
    ax.set_yticklabels([f'{h} \u00b7 {m}' for h, m, _ in keys], fontsize=11.5, color=INK, family=FONT)

    for s in ('top', 'right', 'left'):
        ax.spines[s].set_visible(False)
    ax.spines['bottom'].set_color(AXIS)
    ax.set_axisbelow(True)
    ax.grid(axis='x', color=GRID, lw=0.8, zorder=0)
    ax.tick_params(axis='y', length=0, pad=8)
    ax.tick_params(axis='x', colors=AXIS, labelcolor=INK_2, labelsize=12, length=5, width=1, pad=6)
    ax.set_xlabel('Recorded model cost per task (USD, log scale)', fontsize=13, color=INK, labelpad=10)

    fig.text(0.035, 0.945, bench, fontsize=21, weight='bold', color=INK, family=FONT)
    fig.text(0.035, 0.908, 'Per-task cost distribution \u00b7 box: median and interquartile range',
             fontsize=12.5, color=INK_2, family=FONT)
    present = [h for h in HARNESS_COLORS if any(k[0] == h for k in keys)]
    handles = [Line2D([], [], marker='s', ls='', ms=10, mfc=HARNESS_COLORS[h], mec=HARNESS_COLORS[h], label=h)
               for h in present]
    fig.legend(handles=handles, loc='upper left', bbox_to_anchor=(0.028, 0.885), ncol=len(handles), frameon=False,
               fontsize=12, handlelength=1.0, handletextpad=0.4, columnspacing=1.8)

    pdf.savefig(fig)
    plt.close(fig)


def main():
    groups = load_task_costs()
    plt.rcParams.update({'font.family': FONT, 'pdf.fonttype': 42, 'axes.unicode_minus': False,
                         'figure.facecolor': 'white', 'savefig.facecolor': 'white'})
    with PdfPages(OUT) as pdf:
        for bench in BENCHES:
            draw_page(pdf, groups, bench)
    print(OUT)


if __name__ == '__main__':
    main()
