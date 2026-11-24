"""
analysis_log.py

Append-only, human-readable provenance log of the analysis wizard's
checkpoints for one experiment: <experiment dir>/analysis_log.md.

Each wizard session that logs anything gets a "## Session <date time>"
heading (once per experiment per process); each checkpoint is one bullet with
a time, a bold title, a one-line summary, and optional indented detail
bullets:

    ## Session 2026-11-24 09:12

    - **09:15 Baseline density** — calibration 20261104_density_baseline_calibration.json (found in the experiment folder), per-sample reference frequency
      - wrote 20261124_091502_baseline_density.csv (34 samples)

Only checkpoints are logged (a run, a category set confirmed, a stage
finished), never every edit. Writes are best-effort: a log that can't be
written prints a warning and never interrupts the analysis.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

LOG_FILENAME = 'analysis_log.md'

_sessions: set[str] = set()   # experiment dirs that got this process's session heading


def log_path(superdir) -> Path:
    return Path(superdir) / LOG_FILENAME


def _key(superdir) -> str:
    return str(Path(superdir).resolve()).lower()


def _one_line(text) -> str:
    return ' '.join(str(text).split())


def append(superdir, title: str, summary: str = '', details=(), *, now: datetime | None = None):
    """Append one checkpoint to <superdir>/analysis_log.md. Returns the path,
    or None if it couldn't be written (or superdir isn't a directory)."""
    if not superdir or not Path(superdir).is_dir():
        return None
    now = now or datetime.now()
    path = log_path(superdir)
    chunks = []
    try:
        new_file = not path.exists()
        if new_file:
            chunks.append(f'# Analysis log — {Path(superdir).name}\n\n'
                          'Checkpoints recorded by the analysis wizard '
                          '(analysis_wizard.py), one section per session.\n')
        if new_file or _key(superdir) not in _sessions:
            _sessions.add(_key(superdir))
            chunks.append(f'\n## Session {now:%Y-%m-%d %H:%M}\n\n')
        line = f'- **{now:%H:%M} {_one_line(title)}**'
        if summary:
            line += f' — {_one_line(summary)}'
        chunks.append(line + '\n')
        for d in details:
            if str(d).strip():
                chunks.append(f'  - {_one_line(d)}\n')
        with open(path, 'a', encoding='utf-8', newline='\n') as fh:
            fh.write(''.join(chunks))
    except OSError as exc:
        print(f'  [warn] could not write {path}: {exc}')
        return None
    return path
