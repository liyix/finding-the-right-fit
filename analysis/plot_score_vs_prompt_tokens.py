"""Score versus prompt tokens per task, one page per benchmark.

Only OpenHands, DSH and PI are plotted: their token counts share one OpenRouter
convention (prompt and cached fields reported separately), whereas openJiuwen's
native input count includes cached tokens, and Codex / Claude Code report tokens
for a single benchmark each. Scores are the original summary scores.

Run with the Anaconda interpreter, e.g.
    D:\\Anaconda\\python.exe plot_score_vs_prompt_tokens.py
"""
import sys
from pathlib import Path

sys.dont_write_bytecode = True

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.lines import Line2D
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator

from plot_score_cost import (BENCHES, FONT, HARNESS_COLORS, INK, INK_2, MARKER_S, label_text, load_rows,
                             nice_log_ticks, place_labels, score_axis, style_axes)

HERE = Path(__file__).resolve().parent
OUT = HERE / 'student3_aa_score_vs_prompt_tokens.pdf'
HARNESSES = ('OpenHands', 'DSH', 'PI')   # colored by harness, as in the original figure


def tokens(v, _pos=None):
    if v >= 1e6:
        return f'{v / 1e6:g}M'
    return f'{v / 1e3:g}K'


def draw_page(pdf, rows, bench):
    group = [r for r in rows if r['benchmark'] == bench and r['harness'] in HARNESSES]
    xs = np.array([float(r['prompt_tokens']) / int(r['denominator']) for r in group])
    ys = np.array([r['score_pct'] for r in group])

    fig = plt.figure(figsize=(14, 7.9), dpi=100)
    ax = fig.add_axes([0.075, 0.125, 0.905, 0.69])

    ax.set_xscale('log')
    xlo, xhi = xs.min() / 1.3, xs.max() * 1.9
    ax.set_xlim(xlo, xhi)
    ax.xaxis.set_major_locator(FixedLocator(nice_log_ticks(xlo, xhi)))
    ax.xaxis.set_minor_locator(NullLocator())
    ax.xaxis.set_major_formatter(FuncFormatter(tokens))
    score_axis(ax, ys)
    style_axes(ax, 'Prompt tokens per task (reported prompt field, log scale)')

    for r, x, y in zip(group, xs, ys):
        ax.scatter(x, y, s=MARKER_S, facecolor=HARNESS_COLORS[r['harness']], edgecolor='white', linewidth=1.3, zorder=4)
    place_labels(fig, ax, xs, ys, [label_text(r) for r in group], [])

    fig.text(0.075, 0.945, bench, fontsize=21, weight='bold', color=INK, family=FONT)
    fig.text(0.075, 0.905, 'Score versus prompt tokens per task \u00b7 OpenHands, DSH and PI',
             fontsize=13, color=INK_2, family=FONT)
    harnesses = [Line2D([], [], marker='o', ls='', ms=9, mfc=HARNESS_COLORS[h], mec='white', mew=1, label=h)
                 for h in HARNESSES]
    fig.legend(handles=harnesses, loc='upper left', bbox_to_anchor=(0.068, 0.89), ncol=len(harnesses), frameon=False,
               fontsize=12.5, handlelength=1.0, handletextpad=0.35, columnspacing=1.8)

    pdf.savefig(fig)
    plt.close(fig)


def main():
    rows = load_rows()
    plt.rcParams.update({'font.family': FONT, 'pdf.fonttype': 42, 'axes.unicode_minus': False,
                         'figure.facecolor': 'white', 'savefig.facecolor': 'white'})
    with PdfPages(OUT) as pdf:
        for bench in BENCHES:
            draw_page(pdf, rows, bench)
    print(OUT)


if __name__ == '__main__':
    main()
