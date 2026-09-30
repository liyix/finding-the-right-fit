"""Shared helpers of the release tools (stdlib only): default locations and the local
sanitization rules.

Locations
  RELEASE_ROOT  release folder holding work/ (internal tables, scratch space) and
                data/trajectories/ (output); default: the current directory.
  RUNS_ROOT     raw run repository (runs/external/..., runs/pilot-..., .cache/...);
                default: ./harness_test.
Every converter takes command-line options that override these defaults.

Host-specific sanitization rules
  Absolute path prefixes of the machines the runs were made on and the user names found in
  them are not part of the public code. They are read from the local JSON file named by the
  environment variable SANITIZE_RULES:
      {
        "path_subs":      [["<regex>", "<replacement>"], ...],
        "user_names":     ["<name>", ...],
        "check_patterns": ["<regex>", ...]
      }
  path_subs are applied in order after secret redaction and before the generic built-in
  path rules of each converter (e.g. /home/... -> <host>/...); user_names are replaced by
  <user> when they appear as a path component after <host>; check_patterns are counted as
  leftovers by the validators and by build_release_results.py. Without SANITIZE_RULES only
  the generic built-in rules apply.
"""
import json
import os
import re


def release_root():
    return os.environ.get('RELEASE_ROOT') or '.'


def runs_root():
    return os.environ.get('RUNS_ROOT') or 'harness_test'


def release_path(*parts):
    return os.path.join(release_root(), *parts)


def runs_path(*parts):
    return os.path.join(runs_root(), *parts)


def load_sanitize_rules(path=None):
    """-> (path_subs [(compiled, replacement)], user_names [str], check_patterns {regex: compiled})."""
    path = path or os.environ.get('SANITIZE_RULES')
    if not path:
        return [], [], {}
    with open(path, encoding='utf-8') as f:
        d = json.load(f)
    subs = [(re.compile(p), rep) for p, rep in d.get('path_subs', [])]
    users = [str(u) for u in d.get('user_names', [])]
    checks = {p: re.compile(p) for p in d.get('check_patterns', [])}
    return subs, users, checks


def host_user_regex(template, users):
    """Compile a <host>/.../<name> pattern; `template` holds {users} for the name alternation.
    Returns None when no user names are configured."""
    if not users:
        return None
    return re.compile(template.replace('{users}', '|'.join(re.escape(u) for u in users)))


PATH_SUBS_LOCAL, USER_NAMES, CHECK_PATTERNS_LOCAL = load_sanitize_rules()
