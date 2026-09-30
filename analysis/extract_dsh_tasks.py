"""Per-task reward, agent time and steps for the 15 DSH runs (run on the server).

Streams each run archive (tar.zst) without unpacking it to disk and prints one TSV
row per task: benchmark, model, run_id, task_id, reward, agent_seconds, steps.
Harbor runs (TUA-Bench, Terminal-Bench 4) are read from result.json; ALE-CLI runs
from run.json. If a task has several trials in one run, the latest one is kept.

    RUNS_ROOT=/path/to/runs python3 extract_dsh_tasks.py > data/dsh_task_outcomes.tsv
"""
import glob
import json
import os
import subprocess
import sys
import tarfile
from datetime import datetime

ROOT = os.path.join(os.environ.get('RUNS_ROOT', 'runs'), 'external', 'deepseek-harness')
BENCH = {'tua-bench': 'TUA-Bench', 'ale-cli': 'ALE-CLI', 'terminal-bench-4': 'Terminal-Bench 4'}
MODEL = {'claude-opus-5': 'Claude Opus 5', 'gpt-6-astra': 'GPT-6 Astra', 'glm-5.3': 'GLM-5.3',
         'kimi-k3': 'Kimi K3', 'deepseek-v4-pro': 'DeepSeek V4 Pro'}


def ts(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


def harbor_row(d):
    task = d['task_name'].split('/')[-1]
    vr = d.get('verifier_result') or {}
    reward = (vr.get('rewards') or {}).get('reward')
    ae = d.get('agent_execution') or {}
    secs = ((ts(ae['finished_at']) - ts(ae['started_at'])).total_seconds()
            if ae.get('started_at') and ae.get('finished_at') else None)
    return task, d.get('started_at') or '', reward, secs, None


def ale_row(d):
    usage = d.get('usage') or {}
    return (d['task']['slug'], d.get('timestamp_utc') or '', d.get('score'),
            (d.get('timings') or {}).get('duration_s'), usage.get('total_steps'))


def main():
    print('benchmark\tmodel\trun_id\ttask_id\treward\tagent_seconds\tsteps')
    for arc in sorted(glob.glob(f'{ROOT}/*/*/*/*.tar.zst')):
        model_dir, bench_dir, run_id = arc.split('/')[-4:-1]
        proc = subprocess.Popen(['zstd', '-dc', arc], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        latest = {}
        with tarfile.open(fileobj=proc.stdout, mode='r|') as tar:
            for m in tar:
                name = os.path.basename(m.name)
                if '/raw/' not in m.name or name not in ('result.json', 'run.json'):
                    continue
                if bench_dir == 'ale-cli' and name != 'run.json':
                    continue
                if bench_dir != 'ale-cli' and (name != 'result.json' or m.name.count('/') < 5):
                    continue
                try:
                    d = json.load(tar.extractfile(m))
                    row = ale_row(d) if bench_dir == 'ale-cli' else harbor_row(d)
                except Exception as e:  # malformed or non-trial json
                    print(f'skip {m.name}: {e}', file=sys.stderr)
                    continue
                task, stamp = row[0], row[1]
                if task not in latest or stamp >= latest[task][1]:
                    latest[task] = row
        proc.wait()
        for task, (_, _, reward, secs, steps) in sorted(latest.items()):
            print('\t'.join(str(x) if x is not None else '' for x in
                            (BENCH[bench_dir], MODEL[model_dir], run_id, task, reward, secs, steps)))
        print(f'{run_id}: {len(latest)} tasks', file=sys.stderr)


if __name__ == '__main__':
    main()
