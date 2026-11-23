"""
density_categories.py

Density categories: groups of an experiment's samples that share one media
(baseline) density for the SMRFXMAnalysis imaging / volume / density batch
run. Used by calculate_baseline_density.py's GUI (assigning samples, the
category sidebar) and by analysis_wizard.py (the category review step and
opening SMRFXMAnalysis's batch window).

Pieces:
    CategoryModel     the samples (name, baseline density, rfreq), the
                      categories (name, colour, members) and the built-in
                      Skip category, with change listeners. Persisted to
                      <experiment dir>/density_categories.json on every change
                      and reloaded the next time that directory is computed.
    category_payload  the JSON text a category's Copy button puts on the
                      clipboard; SMRFXMAnalysis's batch window ("Paste
                      category", gui/bulk_run_window.py) ticks exactly its
                      samples and locks the media density to its mean. Keep
                      CATEGORY_FORMAT / CATEGORY_FORMAT_VERSION in step with
                      parse_category_payload there.
    CategorySidebar   scrollable column of category boxes (rename, delete,
                      copy, expandable sample list) for the density page.
    ReviewPage        full-page grid of category cards with per-sample
                      densities, a Copy button each, and on-disk analysis
                      status (see sample_analysis_status).
    BatchWindowLauncher
                      opens SMRFXMAnalysis's batch window directly, already
                      scanned on the experiment dir, in the paella_analysis
                      env (paths overridable with SMRFXM_PYTHON / SMRFXM_REPO).

"Analysed" is read from disk, never assumed: a sample counts when every
system HDF5 of its newest *_imaging_fxm_results run has finished volume
analysis and has a density analysis whose media density equals its
category's mean (to the batch window's 4 decimals). BF-only systems have no
volume/density and count once their image stage is done; samples with no
image binaries at all have nothing to analyse. HDF5s are opened with file
locking off, so checking never blocks a batch run that is writing them.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

CATEGORY_FORMAT = 'biophys_density_category'
CATEGORY_FORMAT_VERSION = 1
CATEGORIES_FILENAME = 'density_categories.json'

SKIP_ID = 'skip'
SKIP_NAME = "Skip (don't analyse)"
SKIP_COLOR = '#d4d4d4'

# Light fills that keep black text readable and stay distinct from each other
# and from the table's white / light-grey striping.
PALETTE = ['#a7d8f5', '#b9e6a5', '#ffd59e', '#e3bdf2', '#f7b2b7', '#9fe0d6',
           '#f5e49a', '#c7cdf7', '#f2c2a0', '#cde59a', '#f0b8dc', '#b5d3e8']

# The batch window's media density box has 4 decimals; the mean is sent and
# compared at that precision.
DENSITY_DECIMALS = 4
_DENSITY_TOL = 0.6 * 10 ** -DENSITY_DECIMALS


@dataclass
class SampleInfo:
    name: str
    n_cells: int
    density: float     # NaN when the sample has no reference frequency
    rfreq: float


@dataclass
class Category:
    id: str
    name: str
    color: str
    skip: bool = False


def _finite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


@dataclass
class CategoryStats:
    mean: float | None
    n: int            # members
    n_finite: int     # members with a density
    min: float | None
    max: float | None


class CategoryModel:
    """Samples, categories and assignments, with change notification."""

    def __init__(self):
        self.superdir: Path | None = None
        self.samples: dict[str, SampleInfo] = {}   # table order
        self.categories: list[Category] = [Category(SKIP_ID, SKIP_NAME, SKIP_COLOR, skip=True)]
        self._assign: dict[str, str] = {}          # sample -> category id
        self._listeners: list = []
        self._next_id = 1

    # -- listeners ------------------------------------------------------------

    def add_listener(self, callback):
        self._listeners.append(callback)

    def _changed(self, *, save: bool = True):
        if save:
            self.save()
        for callback in list(self._listeners):
            callback()

    # -- samples --------------------------------------------------------------

    def set_samples(self, superdir: Path, infos: list[SampleInfo]):
        """
        Replace the sample set (after a baseline-density run). A new
        experiment dir starts from its saved categories, if any; recomputing
        the same dir keeps the current assignments for samples still present.
        """
        superdir = Path(superdir)
        new_dir = self.superdir is None or _norm(self.superdir) != _norm(superdir)
        self.superdir = superdir
        self.samples = {info.name: info for info in infos}
        if new_dir:
            self.categories = [c for c in self.categories if c.skip]
            self._assign = {}
            self._load()
        self._assign = {s: c for s, c in self._assign.items() if s in self.samples}
        self._drop_empty()
        # Loading (or reloading) changes no assignment the user made, so it
        # doesn't rewrite density_categories.json; only edits do.
        self._changed(save=False)

    # -- queries --------------------------------------------------------------

    def category(self, cat_id: str) -> Category | None:
        return next((c for c in self.categories if c.id == cat_id), None)

    def category_of(self, sample: str) -> Category | None:
        cat_id = self._assign.get(sample)
        return self.category(cat_id) if cat_id else None

    def members(self, cat_id: str) -> list[str]:
        return [s for s in self.samples if self._assign.get(s) == cat_id]

    def ordered_categories(self) -> list[Category]:
        """User categories in creation order, then Skip."""
        return [c for c in self.categories if not c.skip] + [c for c in self.categories if c.skip]

    def stats(self, cat_id: str) -> CategoryStats:
        members = self.members(cat_id)
        values = [self.samples[s].density for s in members if _finite(self.samples[s].density)]
        if not values:
            return CategoryStats(None, len(members), 0, None, None)
        return CategoryStats(sum(values) / len(values), len(members), len(values),
                             min(values), max(values))

    def unassigned(self) -> list[str]:
        return [s for s in self.samples if s not in self._assign]

    def ready_problem(self) -> str | None:
        """None when every sample is categorised and every category has a
        mean; otherwise why not (for the wizard's Next hint)."""
        if not self.samples:
            return 'Run the baseline density calculation to continue.'
        unassigned = self.unassigned()
        if unassigned:
            return (f'{len(unassigned)} sample(s) not in a category yet — assign every '
                    f'sample (Skip for ones not to analyse) to continue.')
        for c in self.categories:
            if not c.skip and self.members(c.id) and self.stats(c.id).mean is None:
                return f"Category '{c.name}' has no sample with a density — it has no mean."
        return None

    def analysis_categories(self) -> list[Category]:
        """Non-Skip categories that have members."""
        return [c for c in self.categories if not c.skip and self.members(c.id)]

    # -- edits ----------------------------------------------------------------

    def default_name(self) -> str:
        taken = {c.name for c in self.categories}
        k = 1
        while f'Category {k}' in taken:
            k += 1
        return f'Category {k}'

    def _free_color(self) -> str:
        used = {c.color for c in self.categories}
        free = [c for c in PALETTE if c not in used]
        return free[0] if free else PALETTE[len(self.categories) % len(PALETTE)]

    def new_category(self, name: str, samples: list[str]) -> str:
        cat_id = f'c{self._next_id}'
        self._next_id += 1
        self.categories.append(Category(cat_id, name.strip() or self.default_name(),
                                        self._free_color()))
        self.assign(samples, cat_id)
        return cat_id

    def assign(self, samples: list[str], cat_id: str):
        for s in samples:
            if s in self.samples:
                self._assign[s] = cat_id
        self._drop_empty()
        self._changed()

    def unassign(self, samples: list[str]):
        for s in samples:
            self._assign.pop(s, None)
        self._drop_empty()
        self._changed()

    def rename(self, cat_id: str, name: str):
        cat = self.category(cat_id)
        if cat is None or cat.skip or not name.strip():
            return
        cat.name = name.strip()
        self._changed()

    def delete(self, cat_id: str):
        cat = self.category(cat_id)
        if cat is None or cat.skip:
            return
        self.categories.remove(cat)
        self._assign = {s: c for s, c in self._assign.items() if c != cat_id}
        self._changed()

    def _drop_empty(self):
        """User categories left with no members disappear (Skip always stays)."""
        used = set(self._assign.values())
        self.categories = [c for c in self.categories if c.skip or c.id in used]

    # -- clipboard payload ----------------------------------------------------

    def payload(self, cat_id: str) -> str:
        """JSON for SMRFXMAnalysis's batch window "Paste category"."""
        cat = self.category(cat_id)
        mean = self.stats(cat_id).mean
        if cat is None or cat.skip or mean is None:
            raise ValueError('Only a category with a mean density can be copied.')
        return json.dumps({
            'format': CATEGORY_FORMAT,
            'version': CATEGORY_FORMAT_VERSION,
            'category': cat.name,
            'media_density_g_per_mL': round(mean, DENSITY_DECIMALS),
            'samples': self.members(cat_id),
            'experiment_dir': str(self.superdir) if self.superdir else '',
        }, indent=1)

    # -- persistence ----------------------------------------------------------

    def _path(self) -> Path | None:
        return self.superdir / CATEGORIES_FILENAME if self.superdir else None

    def save(self):
        path = self._path()
        if path is None or not self.samples:
            return
        doc = {
            'version': 1,
            'experiment_dir': str(self.superdir),
            'updated': datetime.now().isoformat(timespec='seconds'),
            'categories': [{
                'name': c.name,
                'color': c.color,
                'skip': c.skip,
                'mean_density_g_per_mL': (None if c.skip or self.stats(c.id).mean is None
                                          else round(self.stats(c.id).mean, DENSITY_DECIMALS)),
                'samples': self.members(c.id),
            } for c in self.ordered_categories()],
        }
        try:
            path.write_text(json.dumps(doc, indent=2), encoding='utf-8')
        except OSError as exc:
            print(f'  [warn] could not save {path}: {exc}')

    def _load(self):
        path = self._path()
        if path is None or not path.is_file():
            return
        try:
            doc = json.loads(path.read_text(encoding='utf-8'))
            entries = list(doc.get('categories', []))
        except (OSError, ValueError, AttributeError) as exc:
            print(f'  [warn] could not read {path}: {exc}')
            return
        for entry in entries:
            samples = [s for s in entry.get('samples', []) if isinstance(s, str)]
            if entry.get('skip'):
                for s in samples:
                    self._assign[s] = SKIP_ID
                continue
            cat_id = f'c{self._next_id}'
            self._next_id += 1
            color = entry.get('color') if isinstance(entry.get('color'), str) else self._free_color()
            self.categories.append(Category(cat_id, str(entry.get('name') or self.default_name()),
                                            color))
            for s in samples:
                self._assign[s] = cat_id


def _norm(path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def same_path(a, b) -> bool:
    return _norm(a) == _norm(b)


# ---------------------------------------------------------------------------
# On-disk analysis status (mirrors SMRFXMAnalysis' core.run_identity /
# core.qc.batch_report layout rules, read with h5py only)
# ---------------------------------------------------------------------------

_IMAGE_BINARY_RE = re.compile(r'^.*_(VolumeImages|Images)_\d{3}.*\.bin$', re.IGNORECASE)
_RESULTS_DIR_RE = re.compile(r'^\d{8}\.\d{6}_imaging_fxm_results$')
_STAGE1_DIR = 'stage1_image_processing'

# Per-sample states, worst first when a sample has several systems.
PENDING, RERUN, DONE, NO_IMAGING = 'pending', 'rerun', 'done', 'no_imaging'


@dataclass
class SampleStatus:
    state: str
    text: str


@dataclass
class CategoryStatus:
    state: str                       # PENDING / RERUN / DONE
    samples: dict = field(default_factory=dict)   # sample -> SampleStatus

    @property
    def label(self) -> str:
        return {DONE: 'Analysed', RERUN: 'Needs rerun', PENDING: 'Pending'}[self.state]


_hdf5_cache: dict[str, tuple] = {}   # path -> ((mtime, size), result)


def _has_image_binaries(sample_dir: Path) -> bool:
    try:
        with os.scandir(sample_dir) as entries:
            return any(e.is_file() and _IMAGE_BINARY_RE.match(e.name) for e in entries)
    except OSError:
        return False


def _latest_run_hdf5s(sample_dir: Path) -> tuple[str | None, list[Path]]:
    try:
        runs = sorted(e.name for e in os.scandir(sample_dir)
                      if e.is_dir() and _RESULTS_DIR_RE.match(e.name))
    except OSError:
        return None, []
    if not runs:
        return None, []
    run_dir = sample_dir / runs[-1]
    found = []
    for directory in (run_dir, run_dir / _STAGE1_DIR):
        if directory.is_dir():
            found.extend(sorted(directory.glob('*_CELLGROUPED.hdf5')))
    return runs[-1], found


def _inspect_hdf5(path: Path) -> tuple[str, float | None]:
    """('bf_ok' | 'bf_incomplete' | 'no_volume' | 'no_density' | 'density' |
    'unreadable', recorded media density or None). Cached on mtime + size."""
    try:
        st = path.stat()
    except OSError:
        return 'unreadable', None
    key = (st.st_mtime_ns, st.st_size)
    cached = _hdf5_cache.get(str(path))
    if cached and cached[0] == key:
        return cached[1]

    import h5py
    import numpy as np
    try:
        with h5py.File(path, 'r', locking=False) as h:
            legacy_bf = bool(int(h.attrs.get('LEGACY_BF_ONLY', 0)))
            has_fl = h.attrs.get('HAS_FL_CHANNEL', None)
            if legacy_bf or (has_fl is not None and not bool(int(has_fl))):
                frames = h.get('meta/frames')
                ok = (frames is not None and frames.shape[0] > 0
                      and 'cell_id' in (frames.dtype.names or ())
                      and bool(np.any(np.asarray(frames['cell_id']) >= 0)))
                result = ('bf_ok' if ok else 'bf_incomplete', None)
            elif 'analysis/volume/cells' not in h:
                result = ('no_volume', None)
            else:
                density = _recorded_media_density(h)
                result = ('no_density', None) if density is None else ('density', density)
    except Exception:
        return 'unreadable', None   # mid-write or locked: not cached, retried next time
    _hdf5_cache[str(path)] = (key, result)
    return result


def _recorded_media_density(h) -> float | None:
    """The media density a committed density analysis used, or None."""
    summary = h.get('analysis/density/run_qc_summary')
    if summary is not None and 'media_density_g_per_mL' in summary.attrs:
        try:
            return float(_attr_text(summary.attrs['media_density_g_per_mL']))
        except (TypeError, ValueError):
            pass
    config = h.get('analysis/density/config')
    if config is not None and 'config_json' in config.attrs:
        try:
            return float(json.loads(_attr_text(config.attrs['config_json']))['media_density_g_per_mL'])
        except (KeyError, TypeError, ValueError):
            pass
    return None


def _attr_text(value) -> str:
    if isinstance(value, bytes):
        value = value.decode('utf-8', 'replace')
    return str(value).strip().strip('"')


def sample_analysis_status(sample_dir: Path, expected_density: float) -> SampleStatus:
    """Whether a sample's newest imaging run is complete at `expected_density`."""
    if not _has_image_binaries(sample_dir):
        return SampleStatus(NO_IMAGING, 'no imaging data')
    run, hdf5s = _latest_run_hdf5s(sample_dir)
    if run is None:
        return SampleStatus(PENDING, 'not analysed')
    if not hdf5s:
        return SampleStatus(PENDING, 'run in progress / incomplete')

    target = round(expected_density, DENSITY_DECIMALS)
    wrong: list[float] = []
    for path in hdf5s:
        kind, value = _inspect_hdf5(path)
        if kind == 'unreadable':
            return SampleStatus(PENDING, 'HDF5 busy or unreadable')
        if kind == 'bf_incomplete':
            return SampleStatus(PENDING, 'image stage incomplete')
        if kind == 'no_volume':
            return SampleStatus(PENDING, 'volume not analysed')
        if kind == 'no_density':
            return SampleStatus(PENDING, 'density not analysed')
        if kind == 'density' and abs(value - target) > _DENSITY_TOL:
            wrong.append(value)
    if wrong:
        ran = ', '.join(sorted({f'{v:.4f}' for v in wrong}))
        return SampleStatus(RERUN, f'ran at {ran}')
    return SampleStatus(DONE, 'analysed')


def category_status(model: CategoryModel, cat_id: str) -> CategoryStatus:
    mean = model.stats(cat_id).mean
    samples = {}
    for s in model.members(cat_id):
        if mean is None or model.superdir is None:
            samples[s] = SampleStatus(PENDING, 'no category density')
        else:
            samples[s] = sample_analysis_status(model.superdir / s, mean)
    states = {st.state for st in samples.values()}
    if RERUN in states:
        state = RERUN
    elif PENDING in states:
        state = PENDING
    else:
        state = DONE
    return CategoryStatus(state, samples)


# ---------------------------------------------------------------------------
# Opening SMRFXMAnalysis' batch window
# ---------------------------------------------------------------------------

def smrfxm_paths() -> tuple[Path, Path]:
    """(python, repo) for SMRFXMAnalysis. Defaults: the `paella_analysis`
    conda env beside this one, and an `SMRFXMAnalysis` checkout beside this
    repo; override with the SMRFXM_PYTHON / SMRFXM_REPO environment variables."""
    exe = Path(sys.executable)
    envs_dir = exe.parent.parent if exe.parent.parent.name.lower() == 'envs' else exe.parent / 'envs'
    python = Path(os.environ.get('SMRFXM_PYTHON') or envs_dir / 'paella_analysis' / 'python.exe')
    repo = Path(os.environ.get('SMRFXM_REPO') or Path(__file__).resolve().parent.parent / 'SMRFXMAnalysis')
    return python, repo


# -c (not `python gui/bulk_run_window.py`): keeps the repo root on sys.path
# for its `core.` / `cli.` imports, and keeps the window's breadcrumb log
# (written beside sys.argv[0], i.e. the cwd for -c) in the repo root, where
# its .gitignore expects it.
_LAUNCH_CODE = ('import sys; from gui.bulk_run_window import launch_bulk_run; '
                'sys.exit(launch_bulk_run(sys.argv[1]))')


class BatchWindowLauncher:
    """Starts (and remembers) one SMRFXMAnalysis batch window process."""

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self.log_path = Path(tempfile.gettempdir()) / 'analysis_wizard_batch_window.log'

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def exit_code(self) -> int | None:
        return None if self._proc is None else self._proc.poll()

    def launch(self, superdir: str) -> str | None:
        """Start the window on `superdir`; returns an error message or None."""
        python, repo = smrfxm_paths()
        if not python.is_file():
            return (f'paella_analysis Python not found:\n{python}\n\n'
                    'Set the SMRFXM_PYTHON environment variable to its python.exe.')
        if not (repo / 'gui' / 'bulk_run_window.py').is_file():
            return (f'SMRFXMAnalysis checkout not found:\n{repo}\n\n'
                    'Set the SMRFXM_REPO environment variable to the repo folder.')
        env = dict(os.environ, KMP_DUPLICATE_LIB_OK='TRUE')
        flags = 0
        if os.name == 'nt':
            # No console of its own, and out of this console's Ctrl+C group, so
            # it outlives the wizard.
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            log = open(self.log_path, 'w', encoding='utf-8')
            self._proc = subprocess.Popen(
                [str(python), '-c', _LAUNCH_CODE, str(superdir)], cwd=str(repo), env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=flags)
            log.close()   # the child holds its own handle
        except OSError as exc:
            return f'Could not start the batch window: {exc}'
        return None

    def log_tail(self, n: int = 25) -> str:
        try:
            return '\n'.join(self.log_path.read_text(encoding='utf-8', errors='replace')
                             .splitlines()[-n:])
        except OSError:
            return ''


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class ScrollFrame(ttk.Frame):
    """A vertically scrollable frame; put children in `.inner`."""

    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        self._canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        vsb = ttk.Scrollbar(self, orient='vertical', command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.inner = ttk.Frame(self._canvas)
        self._window = self._canvas.create_window((0, 0), window=self.inner, anchor='nw')
        self.inner.bind('<Configure>', lambda _e: self._canvas.configure(
            scrollregion=self._canvas.bbox('all')))
        self._canvas.bind('<Configure>', lambda e: self._canvas.itemconfigure(
            self._window, width=e.width))
        # Wheel scrolls whichever ScrollFrame the pointer is over. One global
        # binding per frame (add='+'), each checking the pointer is inside it:
        # Enter/Leave toggling breaks as the pointer crosses child widgets.
        self.bind_all('<MouseWheel>', self._on_wheel, add='+')

    def _on_wheel(self, event):
        try:
            if not self.winfo_exists() or not self.winfo_ismapped():
                return
            under = self.winfo_containing(event.x_root, event.y_root)
        except (tk.TclError, KeyError):
            return
        if under is None or not str(under).startswith(str(self)):
            return
        if self._canvas.yview() != (0.0, 1.0):
            self._canvas.yview_scroll(int(-event.delta / 120) or (-1 if event.delta > 0 else 1), 'units')


def copy_category(widget: tk.Widget, model: CategoryModel, cat_id: str,
                  button: ttk.Button | None = None):
    """Put a category's payload on the clipboard; flash the button."""
    try:
        text = model.payload(cat_id)
    except ValueError as exc:
        messagebox.showerror('Copy category', str(exc))
        return
    widget.clipboard_clear()
    widget.clipboard_append(text)
    if button is not None:
        original = button.cget('text')
        button.configure(text='Copied ✓')
        button.after(1500, lambda: button.winfo_exists() and button.configure(text=original))


def _fmt(x, spec='.4f', missing='n/a') -> str:
    return format(x, spec) if _finite(x) else missing


class CategorySidebar(ttk.Frame):
    """The density page's category list: one box per category (Skip last)."""

    def __init__(self, parent, model: CategoryModel):
        super().__init__(parent)
        self._model = model
        self._expanded: set[str] = set()
        ttk.Label(self, text='Categories', font=('TkDefaultFont', 11, 'bold')).pack(
            anchor='w', padx=6, pady=(6, 0))
        self._summary = ttk.Label(self, text='', wraplength=220, justify='left')
        self._summary.pack(anchor='w', fill=tk.X, padx=6, pady=(0, 4))
        self._scroll = ScrollFrame(self)
        self._scroll.pack(fill=tk.BOTH, expand=True)
        self.bind('<Configure>', lambda e: self._summary.configure(wraplength=max(e.width - 16, 80)))
        model.add_listener(self.refresh)
        self.refresh()

    def refresh(self):
        if not self.winfo_exists():
            return
        model = self._model
        n_un = len(model.unassigned())
        if not model.samples:
            self._summary.configure(text='Run the calculation, then tick samples and use '
                                         '"Add selection to category".', foreground='')
        elif n_un:
            self._summary.configure(text=f'{n_un} of {len(model.samples)} sample(s) not in a '
                                         f'category.', foreground='#a00')
        else:
            self._summary.configure(text=f'All {len(model.samples)} samples categorised.',
                                    foreground='#14532d')
        for child in self._scroll.inner.winfo_children():
            child.destroy()
        for cat in model.ordered_categories():
            if cat.skip and not model.members(cat.id):
                continue
            self._card(cat).pack(fill=tk.X, padx=4, pady=4)

    def _card(self, cat: Category) -> tk.Frame:
        model = self._model
        stats = model.stats(cat.id)
        card = tk.Frame(self._scroll.inner, background=cat.color, borderwidth=1, relief='solid')
        head = tk.Frame(card, background=cat.color)
        head.pack(fill=tk.X, padx=4, pady=(4, 0))
        tk.Label(head, text=cat.name, background=cat.color, anchor='w',
                 font=('TkDefaultFont', 10, 'bold')).pack(side=tk.LEFT, fill=tk.X, expand=True)
        if not cat.skip:
            for text, tip_cmd in (('🗑', lambda: self._delete(cat)),
                                  ('✎', lambda: self._rename(cat))):
                tk.Button(head, text=text, command=tip_cmd, relief='flat', background=cat.color,
                          activebackground='#ffffff', padx=2, pady=0).pack(side=tk.RIGHT)

        body = tk.Frame(card, background='#ffffff')
        body.pack(fill=tk.X, padx=4, pady=4)
        if cat.skip:
            line = f'{stats.n} sample(s) — not analysed'
        else:
            line = f'Mean {_fmt(stats.mean)} g/mL   n={stats.n}'
            if stats.n_finite < stats.n:
                line += f' ({stats.n - stats.n_finite} without density)'
        tk.Label(body, text=line, background='#ffffff', anchor='w').pack(fill=tk.X, padx=4)
        if not cat.skip and stats.n_finite:
            tk.Label(body, text=f'min {_fmt(stats.min)}   max {_fmt(stats.max)}',
                     background='#ffffff', anchor='w', foreground='#555').pack(fill=tk.X, padx=4)

        row = tk.Frame(body, background='#ffffff')
        row.pack(fill=tk.X, padx=2, pady=(2, 0))
        expanded = cat.id in self._expanded
        tk.Button(row, text=('▾ ' if expanded else '▸ ') + 'samples', relief='flat',
                  background='#ffffff', command=lambda: self._toggle(cat.id)).pack(side=tk.LEFT)
        if not cat.skip:
            copy_btn = ttk.Button(row, text='Copy')
            copy_btn.configure(command=lambda b=copy_btn: copy_category(self, model, cat.id, b))
            copy_btn.pack(side=tk.RIGHT)
        if expanded:
            for s in model.members(cat.id):
                tk.Label(body, text=f'{s}   {_fmt(model.samples[s].density)}',
                         background='#ffffff', anchor='w').pack(fill=tk.X, padx=14)
        return card

    def _toggle(self, cat_id: str):
        self._expanded ^= {cat_id}
        self.refresh()

    def _rename(self, cat: Category):
        name = simpledialog.askstring('Rename category', 'Name:', initialvalue=cat.name,
                                      parent=self)
        if name and name.strip():
            self._model.rename(cat.id, name)

    def _delete(self, cat: Category):
        n = len(self._model.members(cat.id))
        if messagebox.askyesno('Delete category',
                               f"Delete '{cat.name}'? Its {n} sample(s) become uncategorised.",
                               parent=self):
            self._model.delete(cat.id)


_STATE_COLORS = {DONE: ('#bbf7d0', '#14532d'), RERUN: ('#fecaca', '#7f1d1d'),
                 PENDING: ('#e5e7eb', '#374151'), NO_IMAGING: ('#f3f4f6', '#6b7280')}


class ReviewPage(ttk.Frame):
    """
    The wizard's category review step: a full-page grid of category cards
    (samples, densities, on-disk analysis status, Copy), plus a button to
    (re)open the batch window. `all_analysed()` gates the wizard's Next.
    """

    CARD_MIN_WIDTH = 360
    POLL_MS = 30_000

    def __init__(self, parent, model: CategoryModel, *, on_open_batch,
                 on_status_change=None):
        super().__init__(parent)
        self._model = model
        self._on_status_change = on_status_change
        self._status: dict[str, CategoryStatus] = {}
        self._dirty = True
        self._columns = 0
        self._cards: list[tk.Frame] = []

        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, padx=8, pady=(4, 4))
        self._dir_label = ttk.Label(bar, text='')
        self._dir_label.pack(side=tk.LEFT)
        ttk.Button(bar, text='Refresh status', command=self.refresh_status).pack(side=tk.RIGHT)
        ttk.Button(bar, text='Open batch analysis window', command=on_open_batch).pack(
            side=tk.RIGHT, padx=(0, 6))
        self._checked_label = ttk.Label(bar, text='', foreground='#555')
        self._checked_label.pack(side=tk.RIGHT, padx=(0, 10))
        self._summary = ttk.Label(self, text='', foreground='#555')
        self._summary.pack(anchor='w', padx=8)

        self._scroll = ScrollFrame(self)
        self._scroll.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._scroll.bind('<Configure>', self._on_resize)

        model.add_listener(self._mark_dirty)
        # The owner calls refresh_status() whenever the page is shown; this
        # poll keeps it current while it stays on screen.
        self.after(self.POLL_MS, self._poll)

    # -- status ---------------------------------------------------------------

    def _mark_dirty(self):
        self._dirty = True
        # Categories loaded / edited while this page is on screen: redraw
        # soon (coalesced), rather than at the next 30 s poll.
        if self.winfo_exists() and self.winfo_ismapped() and not getattr(self, '_redraw_job', None):
            def redraw():
                self._redraw_job = None
                self.refresh_status()
            self._redraw_job = self.after(200, redraw)

    def _poll(self):
        if not self.winfo_exists():
            return
        if self.winfo_ismapped():
            self.refresh_status()
        self.after(self.POLL_MS, self._poll)

    def refresh_status(self):
        model = self._model
        status = {c.id: category_status(model, c.id) for c in model.analysis_categories()}
        changed = self._dirty or _status_key(status) != _status_key(self._status)
        self._status = status
        self._checked_label.configure(text=f"Status checked {datetime.now():%H:%M:%S}")
        if changed:
            self._dirty = False
            self._rebuild()
        if self._on_status_change:
            self._on_status_change()

    def all_analysed(self) -> bool:
        cats = self._model.analysis_categories()
        return bool(cats) and all(c.id in self._status and self._status[c.id].state == DONE
                                  for c in cats)

    def remaining_hint(self) -> str:
        cats = self._model.analysis_categories()
        if not cats:
            return 'No categories to analyse.'
        left = [c.name for c in cats
                if not (c.id in self._status and self._status[c.id].state == DONE)]
        return (f"Analyse every category at its density to continue "
                f"({len(left)} left: {', '.join(left[:4])}{'…' if len(left) > 4 else ''}).")

    # -- layout ---------------------------------------------------------------

    def _on_resize(self, event):
        cols = max(1, event.width // self.CARD_MIN_WIDTH)
        if cols != self._columns:
            self._columns = cols
            self._grid_cards()

    def _rebuild(self):
        model = self._model
        self._dir_label.configure(text=f'Experiment: {model.superdir or "—"}')
        cats = model.analysis_categories()
        n_done = sum(1 for c in cats if self._status.get(c.id) and self._status[c.id].state == DONE)
        self._summary.configure(text=(
            f'{n_done} of {len(cats)} categories analysed. Copy a category, then click '
            f'"Paste category" in the batch window and run it.'))
        for card in self._cards:
            card.destroy()
        self._cards = [self._card(c) for c in model.ordered_categories()
                       if model.members(c.id)]
        self._columns = max(1, self._scroll.winfo_width() // self.CARD_MIN_WIDTH)
        self._grid_cards()

    def _grid_cards(self):
        inner = self._scroll.inner
        for i in range(12):
            inner.columnconfigure(i, weight=0, uniform='')
        for i in range(self._columns):
            inner.columnconfigure(i, weight=1, uniform='card')
        for i, card in enumerate(self._cards):
            card.grid(row=i // self._columns, column=i % self._columns, sticky='nsew',
                      padx=6, pady=6)

    def _card(self, cat: Category) -> tk.Frame:
        model = self._model
        stats = model.stats(cat.id)
        status = None if cat.skip else self._status.get(cat.id)
        card = tk.Frame(self._scroll.inner, background=cat.color, borderwidth=1, relief='solid')

        head = tk.Frame(card, background=cat.color)
        head.pack(fill=tk.X, padx=8, pady=(6, 4))
        tk.Label(head, text=cat.name, background=cat.color, anchor='w',
                 font=('TkDefaultFont', 12, 'bold')).pack(side=tk.LEFT)
        if cat.skip:
            badge_text, (bg, fg) = 'Skipped', _STATE_COLORS[NO_IMAGING]
        elif status is None:
            badge_text, (bg, fg) = 'Checking…', _STATE_COLORS[PENDING]
        else:
            badge_text, (bg, fg) = status.label, _STATE_COLORS[status.state]
        tk.Label(head, text=f' {badge_text} ', background=bg, foreground=fg,
                 font=('TkDefaultFont', 9, 'bold')).pack(side=tk.RIGHT)

        body = tk.Frame(card, background='#ffffff')
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        if cat.skip:
            tk.Label(body, text="Not analysed — these samples won't be sent to the batch window.",
                     background='#ffffff', foreground='#555', anchor='w').pack(
                fill=tk.X, padx=6, pady=(6, 2))
        else:
            tk.Label(body, text=f'{_fmt(stats.mean)} g/mL', background='#ffffff', anchor='w',
                     font=('TkDefaultFont', 16, 'bold')).pack(fill=tk.X, padx=6, pady=(6, 0))
            tk.Label(body, text=f'n={stats.n}   min {_fmt(stats.min)}   max {_fmt(stats.max)}',
                     background='#ffffff', foreground='#555', anchor='w').pack(fill=tk.X, padx=6)

        table = tk.Frame(body, background='#ffffff')
        table.pack(fill=tk.X, padx=6, pady=6)
        headers = ['Sample', 'Density', 'Ref. freq (Hz)'] + ([] if cat.skip else ['Status'])
        for j, h in enumerate(headers):
            tk.Label(table, text=h, background='#ffffff', foreground='#555',
                     font=('TkDefaultFont', 9, 'bold'), anchor='w').grid(
                row=0, column=j, sticky='w', padx=(0, 10))
        table.columnconfigure(0, weight=1)
        for i, s in enumerate(model.members(cat.id), start=1):
            info = model.samples[s]
            values = [s, _fmt(info.density), _fmt(info.rfreq, '.10g', 'no JSON')]
            for j, v in enumerate(values):
                tk.Label(table, text=v, background='#ffffff', anchor='w').grid(
                    row=i, column=j, sticky='w', padx=(0, 10))
            if not cat.skip:
                st = status.samples.get(s) if status else None
                sbg, sfg = _STATE_COLORS[st.state] if st else _STATE_COLORS[PENDING]
                tk.Label(table, text=st.text if st else '…', background=sbg, foreground=sfg,
                         anchor='w').grid(row=i, column=3, sticky='w')

        if not cat.skip:
            copy_btn = ttk.Button(body, text='Copy for batch window')
            copy_btn.configure(command=lambda b=copy_btn: copy_category(self, model, cat.id, b))
            copy_btn.pack(anchor='e', padx=6, pady=(0, 6))
        return card


def _status_key(status: dict) -> dict:
    return {cid: (cs.state, tuple(sorted((s, st.state, st.text) for s, st in cs.samples.items())))
            for cid, cs in status.items()}
