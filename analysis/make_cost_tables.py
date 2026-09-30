"""Write the appendix cost table from data/config_cost_summary.csv.

Output: tables_out/cost_table.tex (copy over tables/cost_table.tex in the paper).

    python3 make_cost_tables.py
"""
import csv
import sys
from pathlib import Path

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
DATA = HERE / 'data' / 'config_cost_summary.csv'
OUT_DIR = HERE / 'tables_out'

BENCHES = [('TUA-Bench', 'TUA-Bench', 120), ('ALE-CLI', 'ALE-CLI', 99), ('Terminal-Bench 4', 'Terminal-Bench~4', 63)]
MODELS = ['Claude Opus 5', 'GPT-6 Astra', 'GLM-5.3', 'Kimi K3', 'DeepSeek V4 Pro']
HARNESSES = ['OpenHands', 'DSH', 'PI', 'openJiuwen']
NATIVE = {'Claude Opus 5': 'Claude Code', 'GPT-6 Astra': 'Codex'}


def load():
    with DATA.open(encoding='utf-8', newline='') as f:
        return {(r['benchmark'], r['harness'], r['model']): r for r in csv.DictReader(f)}


def cost_cell(r):
    return '--' if r is None else f"{float(r['cost_usd']):.2f}"


def cost_table(rows):
    lines = [
        r'\begin{table}[ht]',
        r'\centering\footnotesize',
        r'\caption{Agent-model cost (USD, OpenRouter pricing) of each configuration, summed over the last run of '
        r'every task. Dividing by the task count (120/99/63) gives the cost per task in '
        r'Figure~\ref{fig:cost_performance}. OJW denotes openJiuwen; Native is Claude Code for Claude Opus~5 and '
        r'Codex for GPT-6 Astra.}',
        r'\label{tab:cost}',
        r'\setlength{\tabcolsep}{4pt}',
        r'\begin{tabular}{lrrrrr}',
        r'\toprule',
        r'Model & OpenHands & DSH & PI & OJW & Native \\',
    ]
    for bench, tex_name, n in BENCHES:
        lines += [r'\midrule', rf'\multicolumn{{6}}{{l}}{{\textit{{{tex_name}}} ({n} tasks)}} \\']
        for m in MODELS:
            cells = [cost_cell(rows.get((bench, h, m))) for h in HARNESSES]
            native = NATIVE.get(m)
            cells.append(cost_cell(rows.get((bench, native, m))) if native else '--')
            lines.append(f'{m} & ' + ' & '.join(cells) + r' \\')
    lines += [r'\bottomrule', r'\end{tabular}', r'\end{table}', '']
    return '\n'.join(lines)


def main():
    rows = load()
    assert len(rows) == 66, len(rows)
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / 'cost_table.tex').write_text(cost_table(rows), encoding='utf-8')
    print(OUT_DIR / 'cost_table.tex')


if __name__ == '__main__':
    main()
