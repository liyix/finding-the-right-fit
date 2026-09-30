"""Per-attempt agent time for the OpenHands and PI archives (run on the server).

Streams every archive under runs/external/{openhands,pi} and prints one TSV row per
trial attempt: harness, model dir, benchmark dir, source run id, task, start time,
agent seconds, reward. Harbor trials (TUA-Bench, Terminal-Bench 4) come from
result.json; ALE-CLI trials from run.json. The source run id is the path component
right after `source-runs/` (OpenHands bundles) or the archive name (PI).

    RUNS_ROOT=/path/to/runs python3 extract_oh_pi_times.py > data/oh_pi_attempt_times.tsv
"""
import glob
import json
import os
import subprocess
import sys
import tarfile
from datetime import datetime

ROOT = os.path.join(os.environ.get('RUNS_ROOT', 'runs'), 'external')


def ts(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


def main():
    print('harness\tmodel_dir\tbench_dir\trun_id\ttask_id\tstart\tagent_seconds\treward')
    for harness in ('openhands', 'pi'):
        for arc in sorted(glob.glob(f'{ROOT}/{harness}/*/*/*/*.tar.zst')):
            model_dir, bench_dir, archive = arc.split('/')[-4:-1]
            proc = subprocess.Popen(['zstd', '-dc', arc], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            n = 0
            with tarfile.open(fileobj=proc.stdout, mode='r|') as tar:
                for m in tar:
                    base = os.path.basename(m.name)
                    if base not in ('result.json', 'run.json') or not m.isfile() or m.size > 5_000_000:
                        continue
                    parts = m.name.split('/')
                    run_id = parts[parts.index('source-runs') + 1] if 'source-runs' in parts else archive
                    try:
                        d = json.load(tar.extractfile(m))
                    except Exception:
                        continue
                    if base == 'run.json' and 'task' in d and 'timings' in d:
                        task = d['task']['slug']
                        start = parts[-2]
                        secs = (d.get('timings') or {}).get('duration_s')
                        reward = d.get('score')
                    elif base == 'result.json' and 'trial_name' in d and 'agent_execution' in d:
                        task = d['task_name'].split('/')[-1]
                        ae = d.get('agent_execution') or {}
                        start = ae.get('started_at') or d.get('started_at') or ''
                        secs = ((ts(ae['finished_at']) - ts(ae['started_at'])).total_seconds()
                                if ae.get('started_at') and ae.get('finished_at') else None)
                        reward = ((d.get('verifier_result') or {}).get('rewards') or {}).get('reward')
                    else:
                        continue
                    n += 1
                    print('\t'.join('' if x is None else str(x) for x in
                                    (harness, model_dir, bench_dir, run_id, task, start, secs, reward)), flush=True)
            proc.wait()
            print(f'{archive}: {n} attempts', file=sys.stderr, flush=True)


if __name__ == '__main__':
    main()
