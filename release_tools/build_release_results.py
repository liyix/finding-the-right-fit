"""Build the released results tables from the trajectory files and check the release.

Reads every data/trajectories/<benchmark>/<harness>/<model>.jsonl.gz, writes
  data/results/task_level.tsv          one row per scored run
  data/results/config_cost_summary.csv one row per configuration (public columns only)
and checks, per configuration, the record count (120 / 99 / 63) and that the mean reward
matches the paper score. Finally it scans the whole data/ and code/ trees for secrets and
host paths (the host-specific check_patterns of the JSON file named by $SANITIZE_RULES, see
common.py). Exits non-zero if any check fails.

    SANITIZE_RULES=/path/to/sanitize_rules.local.json \
        python3 build_release_results.py --release /path/to/harness_release
"""
import argparse
import csv
import gzip
import json
import re
import sys
from pathlib import Path

from common import CHECK_PATTERNS_LOCAL

N = {'TUA-Bench': 120, 'ALE-CLI': 99, 'Terminal-Bench 4': 63}
TASK_COLUMNS = ['harness', 'benchmark', 'model', 'task_id', 'reward', 'reward_status', 'model_calls',
                'input_tokens', 'cached_tokens', 'output_tokens', 'agent_seconds', 'cost_usd',
                'trajectory_available']
SUMMARY_COLUMNS = ['harness', 'model', 'benchmark', 'score', 'denominator', 'score_sum', 'cost_usd',
                   'cost_per_task_usd', 'prompt_tokens', 'cached_prompt_tokens', 'completion_tokens',
                   'reasoning_tokens', 'model_calls', 'token_semantics']
SECRET = re.compile(r'sk-or-v1-[A-Za-z0-9]{16,}|sk-ant-[A-Za-z0-9_-]{16,}|\bsk-[A-Za-z0-9]{32,}|\bhf_[A-Za-z0-9]{30,}'
                    r'|\bgh[po]_[A-Za-z0-9]{30,}|Bearer\s+(?!\[REDACTED\])[A-Za-z0-9._-]{24,}')
HOST = list(CHECK_PATTERNS_LOCAL.values())   # host-specific, from the local rules file


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--release', required=True)
    ap.add_argument('--summary', help='internal config summary csv with the paper scores')
    args = ap.parse_args()
    root = Path(args.release)
    summary_src = Path(args.summary or root / 'work' / 'config_cost_summary.csv')
    paper = {(r['harness'], r['benchmark'], r['model']): r for r in csv.DictReader(summary_src.open())}

    rows, problems = [], []
    for path in sorted((root / 'data' / 'trajectories').glob('*/*/*.jsonl.gz')):
        recs = [json.loads(line) for line in gzip.open(path, 'rt', encoding='utf-8')]
        key = (recs[0]['harness'], recs[0]['benchmark'], recs[0]['model'])
        n = N[key[1]]
        score = sum(float(r['reward'] or 0) for r in recs) / n
        ok_n = len(recs) == n and len({r['task_id'] for r in recs}) == n
        ok_s = key in paper and abs(score - float(paper[key]['score'])) < 6e-5
        if not (ok_n and ok_s):
            problems.append(f'{path.relative_to(root)}: records={len(recs)} score={score:.5f} '
                            f'paper={paper.get(key, {}).get("score")}')
        for r in recs:
            u = r.get('usage') or {}
            rows.append([r['harness'], r['benchmark'], r['model'], r['task_id'], r['reward'], r.get('reward_status'),
                         u.get('model_calls'), u.get('input_tokens'), u.get('cached_input_tokens'),
                         u.get('output_tokens'), r.get('agent_seconds'), r.get('cost_usd'),
                         r.get('trajectory_available')])
    configs = {(r[0], r[1], r[2]) for r in rows}
    missing = set(paper) - configs
    if missing:
        problems.append(f'missing configurations: {sorted(missing)}')

    out = root / 'data' / 'results'
    out.mkdir(parents=True, exist_ok=True)
    with (out / 'task_level.tsv').open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f, delimiter='\t')
        w.writerow(TASK_COLUMNS)
        w.writerows([['' if v is None else v for v in row] for row in rows])
    with (out / 'config_cost_summary.csv').open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS, extrasaction='ignore')
        w.writeheader()
        for key in sorted(paper):
            w.writerow(paper[key])

    leaks = []
    for tree in (root / 'data', root / 'code'):
        for p in tree.rglob('*'):
            if not p.is_file() or p.suffix in ('.lock',):
                continue
            opener = gzip.open if p.suffix == '.gz' else open
            try:
                with opener(p, 'rt', encoding='utf-8', errors='ignore') as f:
                    for i, line in enumerate(f, 1):
                        if SECRET.search(line) or any(h.search(line) for h in HOST):
                            leaks.append(f'{p.relative_to(root)}:{i}')
                            break
            except (OSError, EOFError):
                continue
    print(f'{len(rows)} runs in {len(configs)} configurations')
    print('reconciliation problems:', problems or 'none')
    print('files with secrets or host paths:', leaks or 'none')
    sys.exit(1 if problems or leaks or len(rows) != 6204 else 0)


if __name__ == '__main__':
    main()
