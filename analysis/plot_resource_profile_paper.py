"""Resource-use profile of every configuration (appendix figure).

Five metrics side by side -- model calls, input tokens per call, cache hit rate,
output tokens, and agent minutes, each per task -- for each benchmark (panel rows)
and model (rows within a panel), one dot per harness. Reads data/task_level.tsv
(build_task_level.py), where input tokens always include cached tokens. Drawn at the
NeurIPS text width (5.5 in).

    python3 plot_resource_profile_paper.py
"""
import csv
import sys
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
OUT = HERE / 'student3_resource_profile_paper.pdf'

MODELS = ['Claude Opus 5', 'GPT-6 Astra', 'GLM-5.3', 'Kimi K3', 'DeepSeek V4 Pro']
HARNESS_ORDER = ['OpenHands', 'DSH', 'PI', 'openJiuwen', 'Codex', 'Claude Code']
ROW_LABEL_PT = 6.5
DOT_S = 16
DODGE = 0.13                     # vertical offset between harnesses within a model row

LEFT, RIGHT = 0.95, 0.10
COL_GAP = 0.13
TOP_HEADER = 0.42                # legend + column titles
PANEL_TITLE = 0.17
PANEL_H = 1.05
PANEL_GAP = 0.08
BOTTOM = 0.30


def k_fmt(v, _p=None):
    return f'{v / 1e3:g}K' if v < 1e6 else f'{v / 1e6:g}M'


METRICS = [
    # key, column title, log scale, ticks, formatter
    ('calls', 'Calls per task', True, [10, 30, 100], lambda v, _p=None: f'{v:g}'),
    ('in_per_call', 'Input per call', True, [3e4, 1e5, 3e5], k_fmt),
    ('cache', 'Cache hit rate', False, [0.5, 0.75, 1.0], lambda v, _p=None: f'{v * 100:.0f}%'),
    ('out', 'Output per task', True, [3e3, 3e4, 3e5], k_fmt),
    ('minutes', 'Minutes per task', True, [3, 10, 30, 100], lambda v, _p=None: f'{v:g}'),
]


def load():
    """Per-configuration means over scored tasks; NaN where a metric has no records."""
    sums = {}
    with DATA.open(encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            a = sums.setdefault((r['benchmark'], r['harness'], r['model']), {
                'n': 0, 'calls': 0.0, 'inp': 0.0, 'cached': 0.0, 'out': 0.0, 'secs': 0.0, 'timed': 0})
            a['n'] += 1
            a['calls'] += float(r['model_calls'] or 0)
            a['inp'] += float(r['input_tokens'] or 0)
            a['cached'] += float(r['cached_tokens'] or 0)
            a['out'] += float(r['output_tokens'] or 0)
            if r['agent_seconds']:
                a['secs'] += float(r['agent_seconds'])
                a['timed'] += 1
    out = {}
    for k, a in sums.items():
        out[k] = {
            'calls': a['calls'] / a['n'],
            'in_per_call': a['inp'] / a['calls'] if a['calls'] else np.nan,
            'cache': a['cached'] / a['inp'] if a['inp'] else np.nan,
            'out': a['out'] / a['n'],
            'minutes': a['secs'] / a['timed'] / 60 if a['timed'] >= 0.9 * a['n'] else np.nan,
        }
    return out


def style(ax, log, ticks, fmt, last):
    for s in ('top', 'right', 'left'):
        ax.spines[s].set_visible(False)
    ax.spines['bottom'].set_color(AXIS)
    ax.spines['bottom'].set_linewidth(0.6)
    ax.set_axisbelow(True)
    ax.grid(axis='x', color=GRID, lw=0.5, zorder=0)
    if log:
        ax.set_xscale('log')
    ax.xaxis.set_major_locator(FixedLocator(ticks))
    ax.xaxis.set_minor_locator(NullLocator())
    ax.xaxis.set_major_formatter(FuncFormatter(fmt))
    ax.tick_params(axis='x', colors=AXIS, labelcolor=INK_2, labelsize=TICK_PT - 0.5, length=2.5, width=0.6, pad=2,
                   labelbottom=last)
    ax.tick_params(axis='y', length=0, pad=3)


def main():
    data = load()
    plt.rcParams.update({'font.family': FONT, 'pdf.fonttype': 42, 'axes.unicode_minus': False,
                         'figure.facecolor': 'white', 'savefig.facecolor': 'white'})
    fig_h = TOP_HEADER + 3 * (PANEL_TITLE + PANEL_H) + 2 * PANEL_GAP + BOTTOM
    fig = plt.figure(figsize=(FIG_W, fig_h), dpi=300)
    col_w = (FIG_W - LEFT - RIGHT - COL_GAP * (len(METRICS) - 1)) / len(METRICS)
    harnesses = [h for h in HARNESS_ORDER if any(k[1] == h for k in data)]
    offsets = {h: (i - (len(harnesses) - 1) / 2) * DODGE for i, h in enumerate(harnesses)}

    # Shared x-limits per metric so benchmarks can be compared down a column.
    lims = {}
    for key, _, log, _, _ in METRICS:
        vals = np.array([v[key] for v in data.values() if np.isfinite(v[key])])
        lims[key] = (vals.min() / 1.35, vals.max() * 1.35) if log else (0.45, 1.03)

    y = fig_h - TOP_HEADER
    for bi, bench in enumerate(BENCHES):
        y -= PANEL_TITLE + PANEL_H
        fig.text(0.06 / FIG_W, (y + PANEL_H + 0.04) / fig_h, bench, fontsize=TITLE_PT, weight='bold', color=INK,
                 family=FONT, va='bottom')
        for mi, (key, title, log, ticks, fmt) in enumerate(METRICS):
            x0 = LEFT + mi * (col_w + COL_GAP)
            ax = fig.add_axes([x0 / FIG_W, y / fig_h, col_w / FIG_W, PANEL_H / fig_h])
            for ri, m in enumerate(MODELS):
                yc = len(MODELS) - ri
                if ri % 2 == 0:
                    ax.axhspan(yc - 0.5, yc + 0.5, color='#f6f5f1', zorder=0, lw=0)
                for h in harnesses:
                    v = data.get((bench, h, m))
                    if v is None or not np.isfinite(v[key]):
                        continue
                    ax.scatter(v[key], yc - offsets[h], s=DOT_S, facecolor=HARNESS_COLORS[h], edgecolor='white',
                               linewidth=0.4, zorder=3)
            ax.set_xlim(*lims[key])
            ax.set_ylim(0.5, len(MODELS) + 0.5)
            ax.set_yticks(range(1, len(MODELS) + 1))
            ax.set_yticklabels(MODELS[::-1] if mi == 0 else [], fontsize=ROW_LABEL_PT, color=INK, family=FONT)
            style(ax, log, [t for t in ticks if lims[key][0] <= t <= lims[key][1]], fmt, last=bi == len(BENCHES) - 1)
            if bi == 0:
                ax.set_title(title, fontsize=AXIS_PT, color=INK, family=FONT, pad=4)
        y -= PANEL_GAP

    handles = [Line2D([], [], marker='o', ls='', ms=4.5, mfc=HARNESS_COLORS[h], mec='white', mew=0.5, label=h)
               for h in harnesses]
    fig.legend(handles=handles, loc='upper left', bbox_to_anchor=(0.0, 1.0), ncol=len(handles), frameon=False,
               fontsize=LEGEND_PT, handlelength=1.0, handletextpad=0.3, columnspacing=1.5, borderaxespad=0.1)

    fig.savefig(OUT)
    plt.close(fig)
    print(OUT)


if __name__ == '__main__':
    main()
