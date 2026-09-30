"""Score-vs-cost scatter per benchmark, styled after the Artificial Analysis agent chart.

Reads the original summary scores and costs from data/config_cost_summary.csv
and writes one landscape page per benchmark to student3_score_cost_readable.pdf.

Run with the Anaconda interpreter, e.g.
    D:\\Anaconda\\python.exe plot_score_cost.py
"""
import csv
import math
import random
import sys
from pathlib import Path

sys.dont_write_bytecode = True

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator

HERE = Path(__file__).resolve().parent
DATA = HERE / 'data' / 'config_cost_summary.csv'
OUT =HERE / 'student3_score_cost_readable.pdf'
X_SCALE = 'log'  # 'log' spreads the 10-25x cost range; 'linear' matches the reference chart literally

BENCHES = ['TUA-Bench', 'ALE-CLI', 'Terminal-Bench 4']

# Model colors: fixed order, validated all-pairs (scatter) for CVD dE >= 8 and normal-vision dE >= 15.
MODELS = ['Claude Opus 5', 'GPT-6 Astra', 'DeepSeek V4 Pro', 'Kimi K3', 'GLM-5.3']
COLORS = dict(zip(MODELS, ['#d1492e', '#139a74', '#2a78d6', '#4a3aa7', '#e3a600']))
# Harness colors for the figures that encode harness instead of model: the original supplementary hues,
# re-stepped so all six pairs clear CVD dE >= 8 and normal-vision dE >= 15.
HARNESS_COLORS = {'OpenHands': '#3976C5', 'DSH': '#D9673D', 'PI': '#1BA578',
                  'openJiuwen': '#4A3AA7', 'Codex': '#2B2F36', 'Claude Code': '#7A4A1E'}

INK = '#1f1f1d'
INK_2 = '#52514e'
AXIS = '#c3c2b7'
GRID = '#ebeae5'
GOOD_Q = '#e3f5dd'   # most attractive quadrant
POOR_Q = '#f2f1ee'   # least attractive quadrant
FRONTIER = '#2b2b29'
LEADER = '#a3a29b'

LABEL_PT = 11.5
MARKER_S = 115          # marker area in pt^2 (diameter ~10.7pt)
FONT = 'Arial'


def load_rows():
    with DATA.open(encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r['cost'] = float(r['cost_per_task_usd'])
        r['score_pct'] = float(r['score']) * 100
    return rows


def pareto(group):
    """Configurations no other one beats on both axes (cost <= and score >=, one strictly)."""
    front = [r for r in group if not any(
        q['cost'] <= r['cost'] and q['score_pct'] >= r['score_pct']
        and (q['cost'] < r['cost'] or q['score_pct'] > r['score_pct']) for q in group)]
    return sorted((r['cost'], r['score_pct']) for r in front)


def label_text(r):
    return f"{r['harness']} \u00b7 {r['model']}"


# ---------------------------------------------------------------- label placement
LEADER_MIN = 6      # pt beyond the marker edge before a leader line is drawn
LEADER_SAMPLES = 12


def place_labels(fig, ax, xs, ys, texts, frontier):
    """Pick a non-overlapping position for every label.

    Candidates sit around each point. Hard constraints: labels stay inside the
    axes and never overlap each other or any point, and leader lines never pass
    through another point or label. Among feasible layouts we minimise distance
    to the point, preferring the right side (as in the reference chart), and
    penalise covering the frontier line or sitting closer to a foreign point.
    """
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    k = fig.dpi / 72
    pts = ax.transData.transform(np.column_stack([xs, ys]))
    area = ax.get_window_extent()
    ax_x0, ax_y0, ax_x1, ax_y1 = area.x0 + 3 * k, area.y0 + 3 * k, area.x1 - 3 * k, area.y1 - 3 * k

    sizes = []
    for t in texts:
        tmp = ax.text(0, 0, t, fontsize=LABEL_PT, family=FONT)
        bb = tmp.get_window_extent(renderer)
        sizes.append((bb.width, bb.height))
        tmp.remove()

    r_pt = math.sqrt(MARKER_S) / 2 * k + 1.5 * k
    point_boxes = np.array([[px - r_pt, py - r_pt, px + r_pt, py + r_pt] for px, py in pts])

    # Densely sampled frontier polyline in display space.
    fpts = []
    if len(frontier) > 1:
        fd = ax.transData.transform(np.array(frontier))
        for (x0, y0), (x1, y1) in zip(fd[:-1], fd[1:]):
            n = max(2, int(math.hypot(x1 - x0, y1 - y0) / (3 * k)))
            fpts.extend(zip(np.linspace(x0, x1, n), np.linspace(y0, y1, n)))
    fpts = np.array(fpts) if fpts else np.zeros((0, 2))

    def leader(px, py, box):
        """Leader endpoints (marker edge -> nearest label edge), or None when the label is close."""
        x0, y0, x1, y1 = box
        nx, ny = min(max(px, x0), x1), min(max(py, y0), y1)
        gap = math.hypot(nx - px, ny - py)
        if gap <= r_pt + LEADER_MIN * k:
            return None, gap
        ux, uy = (nx - px) / gap, (ny - py) / gap
        return (px + ux * (r_pt + k), py + uy * (r_pt + k), nx - ux * 2 * k, ny - uy * 2 * k), gap

    def in_boxes(samples, boxes, pad):
        """(S,2) samples vs (B,4) boxes -> True where any sample falls in any box."""
        return ((samples[None, :, 0] > boxes[:, None, 0] - pad) & (samples[None, :, 0] < boxes[:, None, 2] + pad)
                & (samples[None, :, 1] > boxes[:, None, 1] - pad) & (samples[None, :, 1] < boxes[:, None, 3] + pad))

    dxs = [6, 12, 22, 36, 55, 80, 110, 145]
    dys = [0, 8, -8, 16, -16, 26, -26, 38, -38, 52, -52, 70, -70, 92, -92, 118, -118]
    no_leader = np.full((LEADER_SAMPLES, 2), np.nan)
    cands = []
    for i, ((px, py), (w, h)) in enumerate(zip(pts, sizes)):
        others = np.delete(point_boxes, i, axis=0)
        other_pts = np.delete(pts, i, axis=0)
        boxes, costs, leads, segs = [], [], [], []

        def consider(x0, y0, pref):
            x1, y1 = x0 + w, y0 + h
            box = (x0, y0, x1, y1)
            if x0 < ax_x0 or y0 < ax_y0 or x1 > ax_x1 or y1 > ax_y1:
                return
            hit = ((point_boxes[:, 0] < x1 + 1.5 * k) & (point_boxes[:, 2] > x0 - 1.5 * k)
                   & (point_boxes[:, 1] < y1 + 1.5 * k) & (point_boxes[:, 3] > y0 - 1.5 * k))
            if hit.any():
                return
            seg, gap = leader(px, py, box)
            dist = gap / k
            cost = dist + pref
            if seg is not None:
                samples = np.column_stack([np.linspace(seg[0], seg[2], LEADER_SAMPLES),
                                           np.linspace(seg[1], seg[3], LEADER_SAMPLES)])
                if len(others) and in_boxes(samples, others, 1.5 * k).any():
                    return
                cost += 8 + max(0.0, dist - 30)   # short leaders read best
            else:
                samples = no_leader
            # Ambiguity: a foreign point hugging the label reads as its owner.
            if len(other_pts):
                ox = np.clip(other_pts[:, 0], x0, x1) - other_pts[:, 0]
                oy = np.clip(other_pts[:, 1], y0, y1) - other_pts[:, 1]
                nearest_other = np.hypot(ox, oy).min() / k
                if seg is None and nearest_other < dist + 12:
                    cost += 60 + 2 * (dist + 12 - nearest_other)
                elif seg is not None and nearest_other < 12:
                    cost += 30 + 3 * (12 - nearest_other)
            if len(fpts) and in_boxes(fpts, np.array([box]), 2 * k).any():
                cost += 90
            boxes.append(box)
            costs.append(cost)
            leads.append(samples)
            segs.append(seg)

        for dy in dys:
            for dx in dxs:
                yc = py + dy * k
                consider(px + dx * k, yc - h / 2, abs(dy) * 0.12)             # right of point
                consider(px - dx * k - w, yc - h / 2, 5 + abs(dy) * 0.12)     # left of point
        for dy in [12, 20, 30, 42, 56, 72]:
            for shift in [0, 0.3, -0.3]:
                x0 = px - w / 2 + shift * w
                consider(x0, py + dy * k - h / 2, 4)                            # above
                consider(x0, py - dy * k - h / 2, 4)                            # below
        order = np.argsort(costs)
        seg_arr = np.array([s if s is not None else (np.nan,) * 4 for s in segs]).reshape(-1, 4)
        cands.append({'box': np.array(boxes)[order], 'cost': np.array(costs)[order],
                      'lead': np.array(leads)[order], 'seg': [segs[o] for o in order], 'segarr': seg_arr[order]})

    n = len(texts)
    pad = 2.5 * k
    CROWD, CROWD_W, CROSS = 16, 4.0, 40   # labels closer than CROWD pt pay CROWD_W per pt; crossing leaders pay CROSS

    def ccw(ax_, ay, bx, by, cx, cy):
        return (bx - ax_) * (cy - ay) - (by - ay) * (cx - ax_)

    def pair_terms(i, j, pj):
        """Feasibility mask and soft penalty of every candidate of label i against label j's pick."""
        c = cands[i]
        boxes, leads, segs = c['box'], c['lead'], c['segarr']
        b, lj, sj = cands[j]['box'][pj], cands[j]['lead'][pj], cands[j]['segarr'][pj]
        ok = ~((boxes[:, 0] < b[2] + pad) & (boxes[:, 2] > b[0] - pad)
               & (boxes[:, 1] < b[3] + pad) & (boxes[:, 3] > b[1] - pad))
        ok &= ~in_boxes(lj, boxes, k).any(axis=1)                            # j's leader through our label
        ok &= ~in_boxes(leads.reshape(-1, 2), np.array([b]), k).reshape(len(boxes), -1).any(axis=1)
        gx = np.maximum(0, np.maximum(b[0] - boxes[:, 2], boxes[:, 0] - b[2]))
        gy = np.maximum(0, np.maximum(b[1] - boxes[:, 3], boxes[:, 1] - b[3]))
        pen = CROWD_W * np.maximum(0, CROWD - np.hypot(gx, gy) / k)
        with np.errstate(invalid='ignore'):
            d1 = ccw(segs[:, 0], segs[:, 1], segs[:, 2], segs[:, 3], sj[0], sj[1])
            d2 = ccw(segs[:, 0], segs[:, 1], segs[:, 2], segs[:, 3], sj[2], sj[3])
            d3 = ccw(sj[0], sj[1], sj[2], sj[3], segs[:, 0], segs[:, 1])
            d4 = ccw(sj[0], sj[1], sj[2], sj[3], segs[:, 2], segs[:, 3])
            pen = pen + CROSS * ((d1 * d2 < 0) & (d3 * d4 < 0))
        return ok, pen

    def best_for(i, picks):
        c = cands[i]
        if not len(c['box']):
            return None
        ok = np.ones(len(c['box']), bool)
        total = c['cost'].copy()
        for j, pj in picks.items():
            if j == i:
                continue
            m, pen = pair_terms(i, j, pj)
            ok &= m
            total += pen
            if not ok.any():
                return None
        return int(np.argmin(np.where(ok, total, np.inf)))

    def run(order):
        picks = {}
        for i in order:
            c = best_for(i, picks)
            if c is None:
                return None
            picks[i] = c
        for _ in range(6):
            changed = False
            for i in order:
                c = best_for(i, picks)
                if c is not None and c != picks[i]:
                    picks[i], changed = c, True
            if not changed:
                break
        score = sum(cands[i]['cost'][picks[i]] for i in range(n))
        for i in range(n):
            for j in range(i + 1, n):
                score += pair_terms(i, j, picks[j])[1][picks[i]]
        return score, picks

    density = [sum(math.hypot(*(p - q)) < 90 * k for q in pts) for p in pts]
    orders = [sorted(range(n), key=lambda j: (-density[j], -pts[j][1])),
              sorted(range(n), key=lambda j: -pts[j][1]),
              sorted(range(n), key=lambda j: pts[j][0])]
    rng = random.Random(7)
    for _ in range(150):
        o = list(range(n))
        rng.shuffle(o)
        orders.append(o)
    best = None
    for o in orders:
        res = run(o)
        if res and (best is None or res[0] < best[0]):
            best = res
    if best is None:
        raise RuntimeError('could not place every label without overlap')

    inv = ax.transData.inverted()
    for i, t in enumerate(texts):
        p = best[1][i]
        x0, y0, x1, y1 = cands[i]['box'][p]
        seg = cands[i]['seg'][p]
        if seg is not None:
            (dsx, dsy), (dex, dey) = inv.transform([(seg[0], seg[1]), (seg[2], seg[3])])
            ax.plot([dsx, dex], [dsy, dey], color=LEADER, lw=0.7, zorder=3, solid_capstyle='round')
        lx, ly = inv.transform((x0, (y0 + y1) / 2))
        ax.text(lx, ly, t, fontsize=LABEL_PT, family=FONT, color=INK, ha='left', va='center', zorder=6)


# ---------------------------------------------------------------- axes helpers
def nice_log_ticks(lo, hi):
    ticks = []
    for e in range(-3, 10):
        for m in (1, 2, 5):
            v = m * 10 ** e
            if lo <= v <= hi:
                ticks.append(v)
    return ticks


def score_axis(ax, ys):
    """Score (%) y-axis padded around the data, with round-number ticks."""
    span = ys.max() - ys.min()
    ylo, yhi = ys.min() - 0.09 * span, ys.max() + 0.09 * span
    step = 5 if span < 30 else 10
    ax.yaxis.set_major_locator(FixedLocator([v for v in range(0, 101, step) if ylo <= v <= yhi]))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f'{v:g}%'))
    ax.set_ylim(ylo, yhi)
    return ylo, yhi


def style_axes(ax, xlabel):
    """Recessive L-shaped axes, y hairlines, and the shared axis titles."""
    for s in ('top', 'right'):
        ax.spines[s].set_visible(False)
    for s in ('left', 'bottom'):
        ax.spines[s].set_color(AXIS)
        ax.spines[s].set_linewidth(1)
    ax.set_axisbelow(True)
    ax.grid(axis='y', color=GRID, lw=0.8, zorder=0.5)
    ax.tick_params(axis='both', colors=AXIS, labelcolor=INK_2, labelsize=12.5, length=5, width=1, pad=6)
    ax.set_xlabel(xlabel, fontsize=13.5, color=INK, labelpad=10)
    ax.set_ylabel('Score (fixed denominator)', fontsize=13.5, color=INK, labelpad=10)


def money(v, _pos=None):
    if v == 0:
        return '$0'
    if v >= 1:
        return f'${v:g}'
    return f'${v:.2f}'.rstrip('0') if v < 0.1 else f'${v:.1f}'


def draw_page(pdf, rows, bench):
    group = [r for r in rows if r['benchmark'] == bench]
    xs = np.array([r['cost'] for r in group])
    ys = np.array([r['score_pct'] for r in group])

    fig = plt.figure(figsize=(14, 7.9), dpi=100)
    ax = fig.add_axes([0.075, 0.125, 0.905, 0.65])

    # Limits
    if X_SCALE == 'log':
        ax.set_xscale('log')
        xlo, xhi = xs.min() / 1.3, xs.max() * 1.9
        ax.xaxis.set_major_locator(FixedLocator(nice_log_ticks(xlo, xhi)))
    else:
        xlo, xhi = 0, xs.max() * 1.08
    ax.xaxis.set_minor_locator(NullLocator())
    ax.xaxis.set_major_formatter(FuncFormatter(money))
    ax.set_xlim(xlo, xhi)
    ylo, yhi = score_axis(ax, ys)

    # Quadrants split at the benchmark medians.
    mx, my = float(np.median(xs)), float(np.median(ys))
    ax.add_patch(Rectangle((xlo, my), mx - xlo, yhi - my, facecolor=GOOD_Q, edgecolor='none', zorder=0))
    ax.add_patch(Rectangle((mx, ylo), xhi - mx, my - ylo, facecolor=POOR_Q, edgecolor='none', zorder=0))

    style_axes(ax, 'Model cost per task (USD, OpenRouter basis' + (', log scale)' if X_SCALE == 'log' else ')'))

    front = pareto(group)
    if len(front) > 1:
        ax.plot(*zip(*front), color=FRONTIER, lw=1.8, ls=(0, (0.1, 2.6)), dash_capstyle='round', zorder=2)

    for r, x, y in zip(group, xs, ys):
        ax.scatter(x, y, s=MARKER_S, facecolor=COLORS[r['model']], edgecolor='white', linewidth=1.3, zorder=4)

    place_labels(fig, ax, xs, ys, [label_text(r) for r in group], front)

    # Header: title, subtitle, two legend rows.
    fig.text(0.075, 0.945, bench, fontsize=21, weight='bold', color=INK, family=FONT)
    key = [Patch(facecolor=GOOD_Q, edgecolor='none', label='Most attractive quadrant'),
           Patch(facecolor=POOR_Q, edgecolor='none', label='Least attractive quadrant'),
           Line2D([], [], color=FRONTIER, lw=1.8, ls=(0, (0.1, 2.6)), dash_capstyle='round', label='Pareto frontier')]
    leg1 = fig.legend(handles=key, loc='upper left', bbox_to_anchor=(0.068, 0.915), ncol=len(key), frameon=False,
                      fontsize=12.5, handlelength=1.6, handleheight=1.0, handletextpad=0.55, columnspacing=1.8)
    models = [Line2D([], [], marker='o', ls='', ms=9, mfc=COLORS[m], mec='white', mew=1, label=m) for m in MODELS]
    fig.legend(handles=models, loc='upper left', bbox_to_anchor=(0.068, 0.865), ncol=len(MODELS), frameon=False,
               fontsize=12.5, handlelength=1.0, handletextpad=0.35, columnspacing=1.8)
    fig.add_artist(leg1)

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
