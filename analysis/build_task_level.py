"""Merge all per-task sources into data/task_level.tsv (one row per scored task).

Columns: harness, benchmark, model, task_id, reward, model_calls, input_tokens
(always including cached tokens), cached_tokens, output_tokens, agent_seconds, cost_usd.

Sources
  final_task_cost_ledger.csv   OpenHands, PI, DSH, Codex (ALE), Claude Code (TB4): calls and
                               tokens (prompt and cached reported separately), reward
  dsh_task_outcomes.tsv        DSH reward and agent time
  openjiuwen_task_outcomes.tsv openJiuwen reward, calls, tokens (input includes cached), time
  oh_pi_attempt_times.tsv      OpenHands and PI agent time, matched on run id and task; the merged
                               OpenHands bundles do not keep run ids, so those fall back to the
                               last attempt of the task (the same rule the ledger uses)

An empty reward (unresolved outcome) counts as 0, as in the paper.

    python3 build_task_level.py
"""
import csv
import sys
from pathlib import Path

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
D = HERE / 'data'
OUT = D / 'task_level.tsv'
FIELDS = ['harness', 'benchmark', 'model', 'task_id', 'reward', 'model_calls', 'input_tokens', 'cached_tokens',
          'output_tokens', 'agent_seconds', 'cost_usd']
BENCH_DIR = {'tua-bench': 'TUA-Bench', 'ale-cli': 'ALE-CLI', 'terminal-bench-4': 'Terminal-Bench 4'}
MODEL_DIR = {'claude-opus-5': 'Claude Opus 5', 'gpt-6-astra': 'GPT-6 Astra', 'glm-5.3': 'GLM-5.3',
             'kimi-k3': 'Kimi K3', 'deepseek-v4-pro': 'DeepSeek V4 Pro'}


def read(path, delimiter=','):
    with path.open(encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f, delimiter=delimiter))


def main():
    dsh = {(r['benchmark'], r['model'], r['task_id']): r for r in read(D / 'dsh_task_outcomes.tsv', '\t')}
    times, latest = {}, {}
    if (D / 'oh_pi_attempt_times.tsv').exists():
        for r in read(D / 'oh_pi_attempt_times.tsv', '\t'):
            harness = 'OpenHands' if r['harness'] == 'openhands' else 'PI'
            cell = (harness, BENCH_DIR[r['bench_dir']], MODEL_DIR[r['model_dir']])
            if not r['agent_seconds']:
                continue
            for table, key in ((times, cell + (r['run_id'], r['task_id'])), (latest, cell + (r['task_id'],))):
                if key not in table or r['start'] >= table[key][0]:
                    table[key] = (r['start'], r['agent_seconds'])

    rows = []
    for r in read(D / 'final_task_cost_ledger.csv'):
        if r['harness'] == 'openJiuwen':
            continue
        key = (r['benchmark'], r['model'], r['task_id'])
        reward, secs = r['reward'], ''
        if r['harness'] == 'DSH':
            d = dsh.get(key, {})   # two TB4 DeepSeek tasks have no trial in the run: reward 0
            reward, secs = d.get('reward', ''), d.get('agent_seconds', '')
        elif r['harness'] in ('OpenHands', 'PI'):
            cell = (r['harness'],) + key[:2]
            hit = times.get(cell + (r['run_id'], r['task_id'])) or latest.get(cell + (r['task_id'],))
            secs = hit[1] if hit else ''
        p, c = float(r['prompt_tokens'] or 0), float(r['cached_prompt_tokens'] or 0)
        rows.append([r['harness'], r['benchmark'], r['model'], r['task_id'], reward or '0', r['model_calls'],
                     p + c, c, r['completion_tokens'], secs, r['cost_usd']])
    for r in read(D / 'openjiuwen_task_outcomes.tsv', '\t'):
        rows.append(['openJiuwen', r['benchmark'], r['model'], r['task_id'], r['reward'] or '0', r['model_calls'],
                     r['input_tokens'], r['cached_tokens'], r['output_tokens'], r['agent_seconds'], r['cost_usd']])

    with OUT.open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t')
        w.writerow(FIELDS)
        w.writerows(rows)
    timed = sum(1 for r in rows if r[9] not in ('', None))
    print(OUT, len(rows), 'rows,', timed, 'with agent time')


if __name__ == '__main__':
    main()
