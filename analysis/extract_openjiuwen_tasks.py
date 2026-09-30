"""Per-task outcomes of every openJiuwen attempt (run on the server).

Scans all openJiuwen run folders (main runs and recovery reruns; verifier-only
regrade folders are skipped) and prints one TSV row per trial attempt: benchmark,
model, run folder, task, agent start time, reward, agent seconds, input tokens
(including cached), cached tokens, output tokens, model calls. Selecting the last
attempt per task is done locally.

    RUNS_ROOT=/path/to/runs python3 extract_openjiuwen_tasks.py > data/openjiuwen_attempts.tsv
"""
import glob
import json
import os
import sys
from datetime import datetime

ROOT = os.environ.get('RUNS_ROOT', 'runs')
MODELS = [('claude', 'Claude Opus 5'), ('gpt-6', 'GPT-6 Astra'), ('glm', 'GLM-5.3'), ('kimi', 'Kimi K3'),
          ('deepseek', 'DeepSeek V4 Pro')]
BENCH = {'tua': 'TUA-Bench', 'ale': 'ALE-CLI', 'tb4': 'Terminal-Bench 4'}


def model_label(name):
    name = (name or '').lower()
    for key, label in MODELS:
        if key in name:
            return label
    return name


def ts(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


def harbor(d):
    ar = d.get('agent_result') or {}
    meta = ((ar.get('metadata') or {}).get('openjiuwen') or {})
    ae = d.get('agent_execution') or {}
    secs = ((ts(ae['finished_at']) - ts(ae['started_at'])).total_seconds()
            if ae.get('started_at') and ae.get('finished_at') else None)
    reward = ((d.get('verifier_result') or {}).get('rewards') or {}).get('reward')
    return (model_label(((d.get('agent_info') or {}).get('model_info') or {}).get('name')),
            d['task_name'].split('/')[-1], ae.get('started_at') or d.get('started_at') or '', reward, secs,
            ar.get('n_input_tokens'), ar.get('n_cache_tokens'), ar.get('n_output_tokens'),
            meta.get('model_calls_with_usage'), (d.get('exception_info') or {}).get('exception_type', ''))


def ale(d, path):
    u = d.get('usage') or {}
    stamp = path.split('/')[-2]            # .../v0/<YYYYmmdd_HHMMSS>/run.json = start time
    return (model_label((d.get('agent') or {}).get('model')), d['task']['slug'], stamp, d.get('score'),
            (d.get('timings') or {}).get('duration_s'), u.get('total_input_tokens'),
            u.get('total_cache_read_tokens'), u.get('total_output_tokens'), u.get('total_steps'),
            (d.get('termination') or {}).get('reason', ''))


def native_usage(trial_dir):
    """Sum usage over after_model_call events; None when the trial has no event log."""
    for dirpath, _, files in os.walk(trial_dir):
        if 'native-events.jsonl' in files:
            calls = inp = cached = out = cost = 0
            with open(os.path.join(dirpath, 'native-events.jsonl')) as f:
                for line in f:
                    if '"after_model_call"' not in line:
                        continue
                    try:
                        u = json.loads(line).get('usage') or {}
                    except ValueError:
                        continue
                    calls += 1
                    inp += u.get('input_tokens') or 0
                    cached += u.get('cache_read_tokens') or u.get('cache_tokens') or 0
                    out += u.get('output_tokens') or 0
                    cost += u.get('total_cost') or 0
            return inp, cached, out, calls, cost
    return None


def main():
    print('benchmark\tmodel\trun_dir\ttask_id\tstart\treward\tagent_seconds\tinput_tokens\tcached_tokens\t'
          'output_tokens\tmodel_calls\tstatus\tcost_usd')
    for run in sorted(glob.glob(f'{ROOT}/pilot-*-openjiuwen-*')):
        run_dir = os.path.basename(run)
        bench = BENCH[run_dir.split('-')[1]]
        n = 0
        for dirpath, _, files in os.walk(os.path.join(run, 'raw')):
            name = 'run.json' if bench == 'ALE-CLI' else 'result.json'
            if name not in files:
                continue
            path = os.path.join(dirpath, name)
            try:
                d = json.load(open(path))
                if bench == 'ALE-CLI':
                    if 'task' not in d or 'usage' not in d:
                        continue
                    row = ale(d, path)
                else:
                    if 'trial_name' not in d or 'agent_execution' not in d:
                        continue
                    row = harbor(d)
            except Exception as e:
                print(f'skip {path}: {e}', file=sys.stderr)
                continue
            usage = native_usage(dirpath)
            if usage is not None:
                row = row[:5] + usage[:4] + row[9:] + (usage[4],)
            else:
                row = row + ('',)
            n += 1
            print('\t'.join('' if x is None else str(x) for x in (bench, row[0], run_dir) + row[1:]))
        print(f'{run_dir}: {n} attempts', file=sys.stderr)


if __name__ == '__main__':
    main()
