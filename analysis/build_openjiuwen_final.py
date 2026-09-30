"""Pick the scored attempt per task from data/openjiuwen_attempts.tsv.

Rule: the last attempt per (benchmark, model, task), as for every other harness.
Two adjustments reproduce the paper's openJiuwen scores exactly:
  * the Claude Terminal-Bench `biped-contact-dynamics` diagnostic replacement run is
    not the scored attempt (the score uses the earlier replacement run);
  * verifier-only regrades (no new model calls) replace the reward of the attempt
    they regraded: GPT-6 Astra `pretrain-shard-corruption` on Terminal-Bench -> 1.0.
Writes data/openjiuwen_task_outcomes.tsv.

    python3 build_openjiuwen_final.py
"""
import csv
import sys
from pathlib import Path

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
SRC = HERE / 'data' / 'openjiuwen_attempts.tsv'
OUT = HERE / 'data' / 'openjiuwen_task_outcomes.tsv'

DIAGNOSTIC_RUNS = {'pilot-tb4-openjiuwen-claude-opus5-biped-clean-replacement-20260920-r1'}
REGRADES = {('Terminal-Bench 4', 'GPT-6 Astra', 'pretrain-shard-corruption'): '1.0'}


def main():
    with SRC.open(encoding='utf-8', newline='') as f:
        rows = [r for r in csv.DictReader(f, delimiter='\t') if r['run_dir'] not in DIAGNOSTIC_RUNS]
    last = {}
    for r in rows:
        key = (r['benchmark'], r['model'], r['task_id'])
        if key not in last or r['start'] >= last[key]['start']:
            last[key] = r
    for key, reward in REGRADES.items():
        last[key]['reward'] = reward
    with OUT.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), delimiter='\t')
        w.writeheader()
        for key in sorted(last):
            w.writerow(last[key])
    print(OUT, len(last))


if __name__ == '__main__':
    main()
