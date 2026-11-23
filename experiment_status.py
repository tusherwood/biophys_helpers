"""
experiment_status.py

Where an experiment is in the analysis pipeline, read entirely from disk, for
analysis_wizard.py's Overview step and step sidebar (so reopening the wizard
on a half-done experiment resumes where it left off).

    scan(superdir) -> {stage key: StageStatus}

Stages (keys) and what counts as done:
    mass      every sample has a *_mass_results dir (the MATLAB SMR analysis,
              run outside the wizard; partial is normal for FXM-only samples)
    baseline  a *_baseline_density.csv exists and density_categories.json
              puts every sample with mass results in a category
    imaging   every non-Skip category analysed at its own density
              (density_categories.sample_analysis_status, read from the HDF5s)
    gating    every sample with mass or imaging data has a gate file
    compile   a *_compiled dir exists, and nothing it depends on (gate files,
              the samples' newest CELLGROUPED HDF5s, compile_annotations.json)
              changed after it was written -- otherwise 'stale'
    new_dir   <DATA_ANALYSIS_DIR>/<experiment>/data/experiment_data.xlsx exists
              (new_analysis_dir.py's paths)

Each StageStatus has a state (DONE / PARTIAL / TODO / STALE), a one-line
detail for display, and `step` (the wizard step key to jump to).

Also: sample_dirs() (which subdirs are samples), latest_compiled(), and
records_fingerprint() (changes whenever anything compile_experiment() reads
changes, so the wizard can reuse already-loaded records).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import density_categories as dc
from fsutil import is_appledouble
from gating.gates import find_gate_file

DONE, PARTIAL, TODO, STALE = 'done', 'partial', 'todo', 'stale'

STAGE_TITLES = {
    'mass':     'Buoyant mass (MATLAB, outside the wizard)',
    'baseline': 'Baseline density & categories',
    'imaging':  'Imaging analysis by category',
    'gating':   'Gating',
    'compile':  'Compile',
    'new_dir':  'New analysis dir',
}
# Stages "Continue where I left off" walks, in order (mass is run outside).
RESUME_ORDER = ('baseline', 'imaging', 'gating', 'compile', 'new_dir')

_MASS_RE = re.compile(r'_mass_results$')
_RESULTS_RE = re.compile(r'^\d{8}\.\d{6}_imaging_fxm_results$')
_COMPILED_RE = re.compile(r'^(\d{8})[._](\d{6})_compiled$')
_BASELINE_CSV_RE = re.compile(r'^\d{8}_\d{6}_baseline_density\.csv$')


@dataclass
class StageStatus:
    key: str
    state: str
    detail: str

    @property
    def title(self) -> str:
        return STAGE_TITLES[self.key]


# ---------------------------------------------------------------------------
# Samples
# ---------------------------------------------------------------------------

def _subdirs(path: Path) -> list[Path]:
    try:
        return sorted(d for d in path.iterdir() if d.is_dir() and not is_appledouble(d))
    except OSError:
        return []


def has_mass(sample_dir: Path) -> bool:
    return any(_MASS_RE.search(d.name) for d in _subdirs(sample_dir))


def has_imaging(sample_dir: Path) -> bool:
    return (any(_RESULTS_RE.match(d.name) for d in _subdirs(sample_dir))
            or dc._has_image_binaries(sample_dir))


def sample_dirs(superdir: Path) -> list[Path]:
    """Immediate subdirs holding mass results, imaging results or raw image
    binaries (output dirs like *_compiled / *_gating_summary hold none)."""
    return [d for d in _subdirs(Path(superdir)) if has_mass(d) or has_imaging(d)]


def latest_compiled(superdir: Path) -> Path | None:
    found = [d for d in _subdirs(Path(superdir)) if _COMPILED_RE.match(d.name)]
    return found[-1] if found else None


def _compiled_time(path: Path) -> float:
    m = _COMPILED_RE.match(path.name)
    try:
        return datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S').timestamp()
    except (AttributeError, ValueError):
        return path.stat().st_mtime


def _mtime(path: Path | None) -> float:
    try:
        return path.stat().st_mtime if path else 0.0
    except OSError:
        return 0.0


def _latest_hdf5s(sample_dir: Path) -> list[Path]:
    return dc._latest_run_hdf5s(sample_dir)[1]


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

def _counted(n: int, total: int, what: str) -> tuple[str, str]:
    if total == 0:
        return TODO, f'no samples {what}'
    state = DONE if n == total else PARTIAL if n else TODO
    return state, f'{n}/{total} samples {what}'


def _load_categories(superdir: Path) -> list | None:
    path = superdir / dc.CATEGORIES_FILENAME
    try:
        return json.loads(path.read_text(encoding='utf-8')).get('categories', [])
    except (OSError, ValueError, AttributeError):
        return None


def scan(superdir) -> dict:
    superdir = Path(superdir)
    samples = sample_dirs(superdir)
    out = {}

    mass_samples = [s for s in samples if has_mass(s)]
    state, detail = _counted(len(mass_samples), len(samples), 'with *_mass_results')
    out['mass'] = StageStatus('mass', state, detail)

    # baseline + categories
    csvs = sorted(f.name for f in superdir.glob('*_baseline_density.csv')
                  if _BASELINE_CSV_RE.match(f.name))
    cats = _load_categories(superdir)
    if not csvs:
        out['baseline'] = StageStatus('baseline', TODO, 'not calculated yet')
    else:
        assigned = {s for c in (cats or []) for s in c.get('samples', [])}
        n_mass = len(mass_samples)
        n_assigned = sum(1 for s in mass_samples if s.name in assigned)
        if n_mass and n_assigned == n_mass:
            n_cats = sum(1 for c in cats if not c.get('skip'))
            out['baseline'] = StageStatus('baseline', DONE,
                                          f'{csvs[-1]}; {n_cats} categor{"y" if n_cats == 1 else "ies"}')
        else:
            out['baseline'] = StageStatus('baseline', PARTIAL,
                                          f'calculated; {n_assigned}/{n_mass} samples categorised')

    # imaging, per category at its own density
    analysis_cats = [c for c in (cats or []) if not c.get('skip') and c.get('samples')
                     and c.get('mean_density_g_per_mL') is not None]
    if not analysis_cats:
        out['imaging'] = StageStatus('imaging', TODO, 'no density categories yet')
    else:
        done, rerun = 0, 0
        for c in analysis_cats:
            states = {dc.sample_analysis_status(superdir / s, float(c['mean_density_g_per_mL'])).state
                      for s in c['samples']}
            if dc.RERUN in states:
                rerun += 1
            elif dc.PENDING not in states:
                done += 1
        n = len(analysis_cats)
        detail = f'{done}/{n} categories analysed' + (f', {rerun} need a rerun' if rerun else '')
        out['imaging'] = StageStatus('imaging', DONE if done == n else PARTIAL if done or rerun
                                     else TODO, detail)

    # gating
    gate_files = {s.name: find_gate_file(s) for s in samples}
    n_gated = sum(1 for f in gate_files.values() if f is not None)
    state, detail = _counted(n_gated, len(samples), 'gated')
    out['gating'] = StageStatus('gating', state, detail)

    # compile (+ staleness)
    compiled = latest_compiled(superdir)
    if compiled is None:
        out['compile'] = StageStatus('compile', TODO, 'not compiled yet')
    else:
        t = _compiled_time(compiled)
        reasons = []
        if any(_mtime(f) > t for f in gate_files.values()):
            reasons.append('gates')
        if any(_mtime(h) > t for s in samples for h in _latest_hdf5s(s)):
            reasons.append('imaging results')
        if _mtime(superdir / 'compile_annotations.json') > t + 5:
            reasons.append('annotations')
        if reasons:
            out['compile'] = StageStatus('compile', STALE,
                                         f'{compiled.name}; {" and ".join(reasons)} changed since '
                                         f'— recompile')
        else:
            out['compile'] = StageStatus('compile', DONE, compiled.name)

    # new analysis dir
    try:
        import new_analysis_dir
        xlsx = new_analysis_dir.DATA_ANALYSIS_DIR / superdir.name / 'data' / 'experiment_data.xlsx'
        if xlsx.is_file():
            out['new_dir'] = StageStatus('new_dir', DONE, str(xlsx.parent.parent))
        else:
            out['new_dir'] = StageStatus('new_dir', TODO, 'not created / compiled data not copied')
    except Exception as exc:     # paths are per-computer constants in new_analysis_dir.py
        out['new_dir'] = StageStatus('new_dir', TODO, f'unknown ({exc})')
    return out


def first_unfinished(status: dict) -> str | None:
    """Where to resume: the first stage in RESUME_ORDER after the furthest
    DONE one that isn't DONE itself (so an experiment compiled before
    categories existed doesn't send you back to the baseline step), or the
    first unfinished stage if nothing is done yet; None when all are done."""
    keys = [k for k in RESUME_ORDER if k in status]
    done = [i for i, k in enumerate(keys) if status[k].state == DONE]
    start = done[-1] + 1 if done else 0
    return next((k for k in keys[start:] if status[k].state != DONE), None)


def records_fingerprint(superdir) -> tuple:
    """Changes whenever an input of compile_experiment() changes: sample set,
    mass CSV dirs, gate files, newest CELLGROUPED HDF5s / crop caches."""
    superdir = Path(superdir)
    parts = []
    for s in sample_dirs(superdir):
        mass = [d for d in _subdirs(s) if _MASS_RE.search(d.name)]
        runs = [d for d in _subdirs(s) if _RESULTS_RE.match(d.name)]
        files = []
        if runs:
            try:
                files = sorted((e.name, e.stat().st_mtime_ns, e.stat().st_size)
                               for e in os.scandir(runs[-1]) if e.is_file())
            except OSError:
                files = []
        parts.append((s.name, mass[-1].name if mass else '', _mtime(mass[-1]) if mass else 0,
                      str(find_gate_file(s) or ''), _mtime(find_gate_file(s)),
                      runs[-1].name if runs else '', tuple(files)))
    return (str(superdir.resolve()), tuple(parts))
