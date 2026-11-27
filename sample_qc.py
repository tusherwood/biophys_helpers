"""
sample_qc.py

Per-sample QC table for analysis_wizard.py's "Sample QC" step, shown after
gating and before compiling, so a bad sample is caught while it's still cheap
to fix (re-gate, re-analyse, move to Skip) rather than in the plots.

For each sample: its density category and baseline density, SMR mass cells
(before / after gates), the CELLGROUPED HDF5's mass<->volume pairing
(paired cells as a share of the mass cells and of the imaged cells — the
density table has one row per imaged cell), cells kept in the workbook
block after gating, crops kept, gate file present, and whether the imaging
run used its category's density. Flags (rows highlighted):
    ungated              no gate file (compiled ungated)
    low pairing          paired / mass cells < 50% of the experiment median,
                         or < 20%
    gates drop a lot     fraction kept by the gates < 50% of the median
    few cells            < 100 cells kept in the workbook block
    imaging: …           pending, or analysed at a different density than
                         the sample's category (density_categories)

Data comes from compile_experiment.compile_experiment() — the same records
the Compile step writes — through RecordsCache, which reloads only when
experiment_status.records_fingerprint() says an input changed, so the Compile
step reuses what QC already loaded instead of reading everything twice.
"""
from __future__ import annotations

import json
import math
import statistics
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import ttk

import density_categories as dc
import experiment_status as es
import tk_gc

FEW_CELLS = 100
LOW_PAIRING_ABS = 0.20
RELATIVE = 0.5


class RecordsCache:
    """compile_experiment() records for one superdir, reloaded only when an
    input changed. Thread-safe: QC loads in a worker thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._key = None
        self._records = None

    def get(self, superdir: Path, *, force: bool = False) -> list:
        import compile_experiment
        superdir = Path(superdir)
        key = es.records_fingerprint(superdir)
        with self._lock:
            if not force and key == self._key and self._records is not None:
                return self._records
        records = compile_experiment.compile_experiment(superdir)
        with self._lock:
            self._key, self._records = key, records
        return records


@dataclass
class QCRow:
    sample: str
    category: str = ''
    baseline_density: float = math.nan
    n_mass: int | None = None
    n_mass_kept: int | None = None
    n_density_rows: int | None = None
    n_paired: int | None = None
    n_kept: int | None = None          # workbook block (paired, else volume, else mass) after gates
    n_kept_before: int | None = None
    n_crops_kept: int | None = None
    gated: bool = False
    imaging: str = ''
    flags: list = field(default_factory=list)

    @property
    def pairing_rate(self) -> float | None:
        """Paired cells as a fraction of the SMR mass cells (before gates):
        did the weighed cells get a volume?"""
        if self.n_mass and self.n_paired is not None:
            return self.n_paired / self.n_mass
        return None

    @property
    def imaged_rate(self) -> float | None:
        """Paired cells as a fraction of the imaged cells (the density
        table has one row per imaged cell; 'unmatched_mass' = no SMR match)."""
        if self.n_density_rows:
            return self.n_paired / self.n_density_rows
        return None

    @property
    def kept_fraction(self) -> float | None:
        if self.n_kept_before:
            return self.n_kept / self.n_kept_before
        return None


def _status_counts(hdf5_path: Path, table: str) -> tuple[int, int] | None:
    """(rows, 'ok' rows) of an HDF5 cells table, reading only status_code."""
    import h5py
    import numpy as np
    try:
        with h5py.File(hdf5_path, 'r', locking=False) as f:
            if table not in f:
                return None
            codes = f[table].fields('status_code')[:]
    except Exception:
        return None
    return int(codes.size), int(np.count_nonzero(codes == b'ok'))


def _categories(superdir: Path) -> dict:
    """sample -> (category name, mean density or None, skip)."""
    try:
        doc = json.loads((superdir / dc.CATEGORIES_FILENAME).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    out = {}
    for c in doc.get('categories', []):
        for s in c.get('samples', []):
            out[s] = (c.get('name', ''), c.get('mean_density_g_per_mL'), bool(c.get('skip')))
    return out


def collect(superdir, records: list) -> list:
    import calculate_baseline_density as cbd
    import compile_experiment as ce

    superdir = Path(superdir)
    cats = _categories(superdir)
    densities = {}
    saved = cbd.load_latest_output(superdir)
    if saved is not None:
        rows, cal, _ = saved
        densities = {r[0]: cbd.apply_calculation(r[2], cal, r[4]) for r in rows}

    out = []
    for rec in records:
        name = rec['name']
        row = QCRow(name, baseline_density=densities.get(name, math.nan))
        counts = rec['gate_counts']
        if 'mass' in counts:
            row.n_mass, row.n_mass_kept = counts['mass']
        for blk in ('paired', 'volume', 'mass'):
            if blk in counts:
                row.n_kept_before, row.n_kept = counts[blk]
                break
        if 'vqvae' in counts:
            row.n_crops_kept = counts['vqvae'][1]
        row.gated = rec['gates'] is not None
        h5 = ce._discover_sample(superdir / name).get('cellgrouped_path')
        if h5 is not None:
            dens = _status_counts(h5, 'analysis/density/cells')
            if dens is not None:
                row.n_density_rows, row.n_paired = dens
        cat = cats.get(name)
        if cat is not None:
            row.category = cat[0]
            if cat[2]:
                row.imaging = 'skipped'
            elif cat[1] is not None:
                row.imaging = dc.sample_analysis_status(superdir / name, float(cat[1])).text
        out.append(row)
    _flag(out)
    return out


def _flag(rows: list):
    rates = [r.pairing_rate for r in rows if r.pairing_rate is not None]
    kepts = [r.kept_fraction for r in rows if r.kept_fraction is not None]
    med_rate = statistics.median(rates) if rates else None
    med_kept = statistics.median(kepts) if kepts else None
    for r in rows:
        if not r.gated:
            r.flags.append('ungated')
        rate = r.pairing_rate
        if rate is not None and (rate < LOW_PAIRING_ABS
                                 or (med_rate and rate < RELATIVE * med_rate)):
            r.flags.append(f'low pairing ({rate:.0%})')
        kept = r.kept_fraction
        if kept is not None and med_kept and kept < RELATIVE * med_kept:
            r.flags.append(f'gates drop a lot (keep {kept:.0%})')
        if r.n_kept is not None and r.n_kept < FEW_CELLS:
            r.flags.append(f'few cells ({r.n_kept})')
        if r.imaging and r.imaging not in ('analysed', 'skipped', 'no imaging data'):
            r.flags.append(f'imaging: {r.imaging}')


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def _n(v) -> str:
    return '—' if v is None else f'{v:,}'


def _pct(v) -> str:
    return '—' if v is None else f'{v:.0%}'


_COLUMNS = (
    ('sample', 'Sample', 230, lambda r: r.sample),
    ('category', 'Category', 120, lambda r: r.category or '—'),
    ('density', 'Baseline density', 105,
     lambda r: f'{r.baseline_density:.4f}' if math.isfinite(r.baseline_density) else '—'),
    ('mass', 'Mass cells (kept)', 115,
     lambda r: '—' if r.n_mass is None else f'{r.n_mass:,} ({r.n_mass_kept:,})'),
    ('paired', 'Paired cells', 85, lambda r: _n(r.n_paired)),
    ('rate', '% of mass cells', 95, lambda r: _pct(r.pairing_rate)),
    ('imaged', '% of imaged', 80, lambda r: _pct(r.imaged_rate)),
    ('kept', 'Kept after gates', 110,
     lambda r: '—' if r.n_kept is None else f'{r.n_kept:,} ({_pct(r.kept_fraction)})'),
    ('crops', 'Crops kept', 80, lambda r: _n(r.n_crops_kept)),
    ('gated', 'Gated', 50, lambda r: 'yes' if r.gated else 'NO'),
    ('imaging', 'Imaging', 150, lambda r: r.imaging or '—'),
    ('flags', 'Flags', 260, lambda r: '; '.join(r.flags)),
)
_SORT_KEYS = {
    'rate': lambda r: (r.pairing_rate is None, r.pairing_rate or 0),
    'imaged': lambda r: (r.imaged_rate is None, r.imaged_rate or 0),
    'kept': lambda r: (r.n_kept is None, r.n_kept or 0),
    'crops': lambda r: (r.n_crops_kept is None, r.n_crops_kept or 0),
    'mass': lambda r: (r.n_mass is None, r.n_mass or 0),
    'paired': lambda r: (r.n_paired is None, r.n_paired or 0),
    'density': lambda r: (not math.isfinite(r.baseline_density), r.baseline_density),
    'flags': lambda r: (-len(r.flags), r.sample),
}


class QCPage(ttk.Frame):
    """Sortable per-sample QC table, loaded in a worker thread."""

    def __init__(self, parent, *, superdir_var: tk.StringVar, cache: RecordsCache):
        super().__init__(parent)
        self._superdir = superdir_var
        self._cache = cache
        self._rows: list = []
        self._loaded = False
        self._sort = ('flags', False)
        self._job = None

        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, padx=8, pady=(4, 4))
        self._summary = ttk.Label(bar, text='', font=('TkDefaultFont', 10, 'bold'))
        self._summary.pack(side=tk.LEFT)
        ttk.Button(bar, text='Recompute', command=lambda: self.refresh(force=True)).pack(
            side=tk.RIGHT)
        self._progress = ttk.Progressbar(bar, mode='indeterminate', length=120)
        ttk.Label(self, text=(
            f'Flags: no gate file; paired share of mass cells under {LOW_PAIRING_ABS:.0%} or under half the '
            f'experiment median; gates keeping under half the median fraction; under '
            f'{FEW_CELLS} cells kept; imaging pending or run at a different density than the '
            f"sample's category. Click a column header to sort."),
            foreground='#555', wraplength=1000, justify='left').pack(anchor='w', padx=8)

        frame = ttk.Frame(self)
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)
        self._tree = ttk.Treeview(frame, columns=[c[0] for c in _COLUMNS], show='headings',
                                  selectmode='browse')
        for key, title, width, _fn in _COLUMNS:
            self._tree.heading(key, text=title, command=lambda k=key: self._sort_by(k))
            self._tree.column(key, width=width, stretch=(key in ('sample', 'flags')), anchor='w')
        vsb = ttk.Scrollbar(frame, orient='vertical', command=self._tree.yview)
        hsb = ttk.Scrollbar(frame, orient='horizontal', command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self._tree.tag_configure('flagged', background='#fde2e2')
        self._tree.tag_configure('ok', background='#ffffff')

    def loaded_rows(self) -> list | None:
        """The QC rows of the last completed load (None while none has
        finished, or while one is running)."""
        return None if self._job is not None or not self._loaded else list(self._rows)

    def refresh(self, *, force: bool = False):
        text = self._superdir.get().strip()
        if not text or not Path(text).is_dir():
            self._summary.configure(text='Choose an experiment directory first.')
            return
        if self._job is not None:
            return
        self._summary.configure(text='Loading samples (gates applied)…')
        self._progress.pack(side=tk.RIGHT, padx=8)
        self._progress.start(12)
        result: dict = {}

        def work():
            try:
                records = self._cache.get(Path(text), force=force)
                result['rows'] = collect(Path(text), records)
            except Exception as exc:     # shown in the page, not lost on the thread
                result['error'] = f'{type(exc).__name__}: {exc}'

        tk_gc.pause()     # GC stays on Tk's thread while the worker runs
        self._job = threading.Thread(target=work, daemon=True)
        self._job.start()
        self.after(150, lambda: self._poll(result))

    def _poll(self, result: dict):
        if self._job is not None and self._job.is_alive():
            tk_gc.tick()
            self.after(150, lambda: self._poll(result))
            return
        self._job = None
        tk_gc.resume()
        self._progress.stop()
        self._progress.pack_forget()
        if 'error' in result:
            self._summary.configure(text=f'Could not load the samples — {result["error"]}')
            return
        self._rows = result.get('rows', [])
        self._loaded = True
        n_flag = sum(1 for r in self._rows if r.flags)
        self._summary.configure(text=(
            f'{len(self._rows)} samples — '
            + (f'{n_flag} flagged (highlighted)' if n_flag else 'nothing flagged')))
        self._fill()

    def _sort_by(self, key: str):
        col, rev = self._sort
        self._sort = (key, not rev if col == key else False)
        self._fill()

    def _fill(self):
        key, rev = self._sort
        sort_key = _SORT_KEYS.get(key, lambda r, k=key: dict(
            (c[0], c[3]) for c in _COLUMNS)[k](r).lower())
        rows = sorted(self._rows, key=sort_key, reverse=rev)
        self._tree.delete(*self._tree.get_children())
        for r in rows:
            self._tree.insert('', tk.END, values=[fn(r) for _k, _t, _w, fn in _COLUMNS],
                              tags=('flagged' if r.flags else 'ok',))
