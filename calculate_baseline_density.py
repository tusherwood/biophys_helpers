"""
calculate_baseline_density.py

Computes the fluid baseline density for every sample in an experiment superdir
from its buoyant mass (SMR) data, and writes a single timestamped summary CSV.

For each sample subdir the newest ``*_mass_results`` folder is located (the same
discovery convention used by gate_experiments_inplace.py and
aggregate_bm_vol_files.py) and its buoyant mass CSV is read. The mean of the
per-cell ``avg_baseline`` column is taken and converted to a fluid baseline
density using a base-frequency / density calibration:

    baseline_density = (rfreq - mean_avg_baseline - intercept) / slope

where ``slope`` and ``intercept`` come from a calibration JSON (see --calib-json,
required) and ``rfreq`` is the resonant (reference) frequency in Hz. This
mirrors the per-run calculation in the standalone baseline_density_calc.py
analysis script, generalised to sweep a whole superdir.

``rfreq`` comes from one of two sources:
    single      one value (--rfreq) applied to every sample.
    per-sample  (--per-sample-rfreq) each sample's own value, read from the
                'reference_frequency' field of the newest
                <YYYYMMDD_HHMM>_reference_values.json directly inside that
                sample's subdir (written by the acquisition LabVIEW). A sample
                without a readable one gets rfreq = baseline_density = NaN.

Output (written at the same level as the sample subdirs, i.e. inside <superdir>):
    <YYYYMMDD_HHMMSS>_baseline_density.csv
        one row per sample: sample, n_cells, mean_avg_baseline, baseline_density,
        rfreq, rfreq_source, slope, intercept, source_csv
        (rfreq_source is 'user' in single mode, else the reference-values JSON
        path relative to <superdir>, or empty when it was missing)

Usage:
    python calculate_baseline_density.py <superdir> --rfreq <hz> --calib-json <path>
    python calculate_baseline_density.py <superdir> --per-sample-rfreq --calib-json <path>
    python calculate_baseline_density.py                      # opens a GUI form

    <superdir>          Experiment directory whose immediate subdirs are samples,
                        each containing a *_mass_results folder.
    --rfreq             Resonant frequency in Hz, applied to every sample.
    --per-sample-rfreq  Use each sample's reference-values JSON instead.
    --calib-json        Path to a calibration JSON with 'slope' and 'intercept' keys.

If <superdir>, --calib-json and one rfreq source are all given on the command
line, the computation runs headlessly and prints its report to stdout.
Otherwise a small GUI form opens to collect whatever is still missing; its
"Use per-sample reference frequency" checkbox picks the rfreq source, and in
per-sample mode the results table gains a per-sample rfreq column.

In the GUI, ticked samples can be grouped into density categories (samples
analysed downstream at one shared media density; a built-in Skip category
holds samples not to analyse). Rows are coloured by category and a sidebar
lists each category's mean density and members, with a Copy button whose
payload SMRFXMAnalysis' batch window takes via "Paste category". Categories
are saved to <superdir>/density_categories.json on every change and restored
when that superdir is computed again. See density_categories.py.

The GUI's calibration field fills itself while it is empty (or still holds an
automatic pick): with the newest <date>_density_baseline_calibration.json
directly in the superdir, else with the calibration of the last successful
run (remembered in ~/.biophys_helpers/wizard_state.json). A note under the
field says which; typing or Browse always wins and is never replaced.
"""
import argparse
import json
import re
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import numpy as np
import pandas as pd

import density_categories
import user_state
from fsutil import is_appledouble


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute per-sample fluid baseline density from buoyant "
                    "mass CSVs under an experiment superdir."
    )
    parser.add_argument('superdir', type=str, nargs='?', default=None,
                        help='Experiment directory whose subdirs are samples, '
                             'each with a *_mass_results folder. If omitted '
                             '(along with --rfreq), a GUI form opens instead.')
    rfreq_src = parser.add_mutually_exclusive_group()
    rfreq_src.add_argument('--rfreq', type=float, default=None,
                           help='Resonant frequency in Hz, applied to every sample')
    rfreq_src.add_argument('--per-sample-rfreq', action='store_true',
                           help="Read each sample's rfreq from the "
                                "reference_frequency field of the newest "
                                "<YYYYMMDD_HHMM>_reference_values.json in its "
                                "subdir, instead of one --rfreq for all")
    parser.add_argument('--calib-json', type=str, default=None,
                        help="Path to a calibration JSON with 'slope' and "
                             "'intercept' keys. Required for headless runs; if "
                             "omitted (along with superdir/--rfreq), a GUI form "
                             "opens instead.")
    args = parser.parse_args()

    if args.calib_json is not None and not Path(args.calib_json).is_file():
        raise FileNotFoundError(f"Calibration JSON not found: {args.calib_json}")
    return args


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def load_calibration(path: Path) -> dict:
    """Loads a calibration JSON providing 'slope' and 'intercept'."""
    with open(path) as f:
        cal = json.load(f)
    if 'slope' not in cal or 'intercept' not in cal:
        raise ValueError(f"Calibration JSON {path} must contain 'slope' and "
                         f"'intercept' keys.")
    print(f"Calibration ({cal.get('date', 'no date')}): "
          f"slope={cal['slope']:.6g}  intercept={cal['intercept']:.6g}")
    return cal


CALIBRATION_GLOB = '*_density_baseline_calibration.json'


def find_experiment_calibration(superdir) -> Path | None:
    """The newest <date>_density_baseline_calibration.json directly inside
    `superdir` (by name — the date prefix — then mtime), or None."""
    try:
        found = [p for p in Path(superdir).glob(CALIBRATION_GLOB)
                 if p.is_file() and not is_appledouble(p)]
        return max(found, key=lambda p: (p.name, p.stat().st_mtime)) if found else None
    except OSError:
        return None


def apply_calculation(mean_baseline: float, cal: dict, rfreq: float) -> float:
    """Converts a sample's mean avg_baseline to fluid baseline density."""
    return (rfreq - mean_baseline - cal['intercept']) / cal['slope']


_REFERENCE_VALUES_PATTERN = re.compile(r'^\d{8}_\d{4}_reference_values\.json$')


def find_reference_frequency(sample_dir: Path) -> tuple[float, Path] | None:
    """
    (reference_frequency, source_path) from the newest
    <YYYYMMDD_HHMM>_reference_values.json directly inside sample_dir (written
    by the acquisition LabVIEW), or None if there's no such file or it has no
    usable reference_frequency.
    """
    if not sample_dir.is_dir():
        return None
    files = sorted(f for f in sample_dir.iterdir()
                   if f.is_file() and _REFERENCE_VALUES_PATTERN.match(f.name))
    if not files:
        return None
    path = files[-1]   # names are timestamp-prefixed, so last = newest
    try:
        with open(path) as f:
            return float(json.load(f)['reference_frequency']), path
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"  [warn] could not read reference_frequency from {path.name}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Data discovery
# ---------------------------------------------------------------------------

def per_sample_avg_baseline(superdir: Path) -> list:
    """
    Finds the newest *_mass_results buoyant mass CSV for each sample subdir and
    returns [(sample_name, n_cells, mean_avg_baseline, source_csv)] sorted by
    sample name.

    Searches two levels deep (superdir -> sample_subdir -> *_mass_results),
    matching the discovery convention in gate_experiments_inplace.py. If a sample
    has multiple mass_results dirs, the lexicographically last one (newest
    timestamp) is used. Files named curation_index*.csv and AppleDouble sidecars
    are skipped.
    """
    run_dir_pattern = re.compile(r'.+_mass_results$')
    rows = []

    for sample_dir in sorted(superdir.iterdir()):
        if not sample_dir.is_dir():
            continue
        run_dirs = sorted(
            d for d in sample_dir.iterdir()
            if d.is_dir() and run_dir_pattern.match(d.name)
        )
        if not run_dirs:
            continue
        run_dir = run_dirs[-1]      # most recent if multiple

        for f in sorted(run_dir.iterdir()):
            if not (f.is_file() and f.suffix == '.csv'
                    and not is_appledouble(f)
                    and not f.name.startswith('curation_index')):
                continue
            df = pd.read_csv(f)
            if 'mass_pg' not in df.columns or 'avg_baseline' not in df.columns:
                continue
            ab = df['avg_baseline'].to_numpy()
            ab = ab[np.isfinite(ab)]
            if ab.size == 0:
                continue
            rows.append((sample_dir.name, int(ab.size), float(ab.mean()),
                         str(f.relative_to(superdir))))
            break

    rows.sort(key=lambda r: r[0])
    return rows


def attach_rfreq(superdir: Path, rows: list, rfreq: float | None) -> list:
    """
    Extends each per_sample_avg_baseline() row to
    (sample_name, n_cells, mean_avg_baseline, source_csv, rfreq, rfreq_source).

    rfreq given: that value for every sample, rfreq_source 'user'.
    rfreq None:  each sample's own value from its subdir's reference-values
                 JSON (rfreq_source = that path relative to superdir), or NaN
                 and '' when the sample has none.
    """
    out = []
    for name, n, mean_b, source in rows:
        if rfreq is not None:
            out.append((name, n, mean_b, source, rfreq, 'user'))
            continue
        found = find_reference_frequency(superdir / name)
        if found is None:
            print(f"  [warn] {name}: no readable *_reference_values.json; "
                  f"baseline density left NaN")
            out.append((name, n, mean_b, source, float('nan'), ''))
        else:
            value, path = found
            out.append((name, n, mean_b, source, value,
                        str(path.relative_to(superdir))))
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_output(superdir: Path, rows: list, cal: dict) -> Path:
    """Writes attach_rfreq()-style rows (each carrying its own rfreq)."""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = superdir / f'{timestamp}_baseline_density.csv'

    slope = cal['slope']
    intercept = cal['intercept']

    out_rows = []
    for name, n, mean_b, source, rfreq, rfreq_source in rows:
        out_rows.append({
            'sample':            name,
            'n_cells':           n,
            'mean_avg_baseline': mean_b,
            'baseline_density':  apply_calculation(mean_b, cal, rfreq),
            'rfreq':             rfreq,
            'rfreq_source':      rfreq_source,
            'slope':             slope,
            'intercept':         intercept,
            'source_csv':        source,
        })

    pd.DataFrame(out_rows).to_csv(out_path, index=False)
    print(f"Written: {out_path}  ({len(out_rows)} sample(s))")
    return out_path


def format_report(superdir_name: str, rfreq: float | None, rows: list,
                  cal: dict) -> str:
    """The sample/n_cells/mean_baseline/baseline_density table as printable
    text; rfreq None (per-sample mode) adds a per-row rfreq column."""
    per_sample = rfreq is None
    title = 'per-sample rfreq' if per_sample else f'rfreq={rfreq:g}'
    lines = [f"=== baseline density ({superdir_name}, {title}) ==="]
    header = f"{'sample':<28}{'n_cells':>9}{'mean_baseline':>16}"
    if per_sample:
        header += f"{'rfreq':>16}"
    lines.append(header + f"{'baseline_density':>18}")
    lines.append("-" * len(lines[-1]))
    for name, n, mean_b, _source, row_rfreq, _rsrc in rows:
        density = apply_calculation(mean_b, cal, row_rfreq)
        line = f"{name:<28}{n:>9d}{mean_b:>16.6g}"
        if per_sample:
            line += f"{row_rfreq:>16.10g}"
        lines.append(line + f"{density:>18.6g}")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Computation (shared by the headless and GUI entry points)
# ---------------------------------------------------------------------------

def run_computation(superdir: Path, rfreq: float | None,
                    calib_json: Path) -> tuple[list, dict, Path | None]:
    """
    Runs the full compute-and-write pipeline. rfreq None means per-sample
    (see attach_rfreq). Returns (rows, cal, out_path), rows as attach_rfreq()
    returns them; out_path (and rows) is empty/None when no buoyant mass CSVs
    were found under superdir.
    """
    cal = load_calibration(calib_json)
    rows = per_sample_avg_baseline(superdir)
    if not rows:
        return [], cal, None
    rows = attach_rfreq(superdir, rows, rfreq)
    out_path = write_output(superdir, rows, cal)
    return rows, cal, out_path


_OUTPUT_RE = re.compile(r'^\d{8}_\d{6}_baseline_density\.csv$')


def load_latest_output(superdir: Path) -> tuple[list, dict, Path] | None:
    """
    (rows, cal, csv_path) from the newest <timestamp>_baseline_density.csv
    in superdir — rows in attach_rfreq()'s shape, cal with the slope /
    intercept it was computed with — or None if there is none (or it can't
    be read). Lets the GUI show a previous result without recomputing (and
    writing yet another CSV).
    """
    superdir = Path(superdir)
    try:
        csvs = sorted(f for f in superdir.iterdir()
                      if f.is_file() and _OUTPUT_RE.match(f.name))
    except OSError:
        return None
    if not csvs:
        return None
    try:
        df = pd.read_csv(csvs[-1])
        rows = []
        if 'rfreq_source' not in df.columns:      # CSVs from before per-sample rfreq
            df['rfreq_source'] = 'user'
        for r in df.itertuples(index=False):
            rsrc = '' if pd.isna(r.rfreq_source) else str(r.rfreq_source)
            rows.append((str(r.sample), int(r.n_cells), float(r.mean_avg_baseline),
                         str(r.source_csv), float(r.rfreq), rsrc))
        cal = {'slope': float(df['slope'].iloc[0]), 'intercept': float(df['intercept'].iloc[0])}
    except (OSError, ValueError, KeyError, AttributeError, IndexError) as exc:
        print(f"  [warn] could not read {csvs[-1].name}: {exc}")
        return None
    return rows, cal, csvs[-1]


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

_CHECKED = '☑'    # ☑
_UNCHECKED = '☐'  # ☐


class BaselineDensityGUI(ttk.Frame):
    """
    Simple form: a superdir picker, a calibration-JSON picker, and a
    resonant-frequency entry (or, with the per-sample checkbox ticked, each
    sample's value from its own reference-values JSON, shown as an extra
    table column), with a Run button that computes and writes the
    same CSV as the headless path. Results are shown in a fixed-height,
    scrollable table with a checkbox per sample; Select All / Select None /
    Invert Selection buttons and a live average-density readout let the user
    pick a subset of samples to average.

    Ticked samples can be grouped into density categories ("Add selection to
    category", including a built-in Skip category for samples not to
    analyse): rows are coloured by category, and a resizable sidebar on the
    right lists one box per category (mean, range, members, rename / delete
    and a Copy button for SMRFXMAnalysis' batch window). See
    density_categories.py; the categories are saved beside the CSV as
    density_categories.json and restored when the same superdir is computed
    again.

    A ttk.Frame so it can be packed into any parent widget — a throwaway
    standalone root (see run_gui()) or a page of a larger embedding GUI.
    """

    _COLUMNS = ('check', 'sample', 'n_cells', 'mean_baseline', 'rfreq', 'density')

    def __init__(self, parent: tk.Widget, *, superdir: str | None,
                 calib_json: str | None, rfreq: float | None,
                 superdir_var: tk.StringVar | None = None,
                 per_sample_rfreq: bool = False,
                 category_model: density_categories.CategoryModel | None = None,
                 on_computed=None):
        super().__init__(parent)

        self._superdir = superdir_var if superdir_var is not None else tk.StringVar(value=superdir or '')
        self._calib_json = tk.StringVar(value=calib_json or '')
        # Where the calibration path came from: 'user' (typed / Browse / CLI —
        # never replaced), 'folder' or 'last' (automatic picks, re-resolved
        # when the directory changes), or None (empty).
        self._calib_source = 'user' if calib_json else None
        self._calib_setting = False
        self._calib_json.trace_add('write', lambda *_a: self._calib_edited())
        self._on_computed = on_computed
        self._rfreq = tk.StringVar(value='' if rfreq is None else f'{rfreq:g}')
        self._per_sample = tk.BooleanVar(value=per_sample_rfreq)
        self._checked: dict[str, bool] = {}
        self._densities: dict[str, float] = {}
        self._names: dict[str, str] = {}
        self._model = category_model if category_model is not None else density_categories.CategoryModel()

        # Calculator on the left, category sidebar on the right (draggable sash).
        self._panes = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        self._panes.pack(fill=tk.BOTH, expand=True)
        root = ttk.Frame(self._panes)
        self._panes.add(root, weight=4)
        self._sidebar = density_categories.CategorySidebar(self._panes, self._model)
        self._panes.add(self._sidebar, weight=1)
        self._sash_placed = False
        self._panes.bind('<Configure>', self._place_sash)
        self._panes.bind('<ButtonRelease-1>', self._save_sash, add='+')

        pad = {'padx': 8, 'pady': 6}
        form = ttk.Frame(root)
        form.pack(fill=tk.X, **pad)
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text='Superdir:').grid(row=0, column=0, sticky='w')
        ttk.Entry(form, textvariable=self._superdir).grid(row=0, column=1, sticky='ew', padx=(6, 6))
        ttk.Button(form, text='Browse…', command=self._pick_superdir).grid(row=0, column=2)

        ttk.Label(form, text='Calibration JSON:').grid(row=1, column=0, sticky='w', pady=(6, 0))
        ttk.Entry(form, textvariable=self._calib_json).grid(row=1, column=1, sticky='ew', padx=(6, 6), pady=(6, 0))
        ttk.Button(form, text='Browse…', command=self._pick_calib_json).grid(row=1, column=2, pady=(6, 0))
        self._calib_note = ttk.Label(form, text='', foreground='#666')
        self._calib_note.grid(row=2, column=1, columnspan=2, sticky='w', padx=(6, 6))

        ttk.Checkbutton(
            form, variable=self._per_sample,
            text="Use per-sample reference frequency (from each sample's "
                 "*_reference_values.json)",
            command=self._per_sample_toggled).grid(
            row=3, column=1, columnspan=2, sticky='w', padx=(6, 6), pady=(6, 0))

        ttk.Label(form, text='Reference frequency (Hz):').grid(row=4, column=0, sticky='w', pady=(6, 0))
        self._rfreq_entry = ttk.Entry(form, textvariable=self._rfreq, width=16)
        self._rfreq_entry.grid(row=4, column=1, sticky='w', padx=(6, 6), pady=(6, 0))
        self._update_rfreq_entry()

        self._run_button = ttk.Button(form, text='Run', command=self._run)
        self._run_button.grid(row=5, column=1, sticky='w', pady=(10, 0))

        self._message = ttk.Label(root, text='', foreground='#a00')
        self._message.pack(fill=tk.X, padx=8)

        button_bar = ttk.Frame(root)
        button_bar.pack(fill=tk.X, padx=8, pady=(4, 0))
        ttk.Button(button_bar, text='Select All', command=self._select_all).pack(side=tk.LEFT)
        ttk.Button(button_bar, text='Select None', command=self._select_none).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(button_bar, text='Invert Selection', command=self._invert_selection).pack(side=tk.LEFT, padx=(6, 0))
        # Category assignment of the ticked samples (rows are then coloured by
        # category; see the sidebar).
        ttk.Button(button_bar, text='Remove Selection from Category',
                   command=self._remove_selection_from_category).pack(side=tk.RIGHT)
        self._add_button = ttk.Button(button_bar, text='Add Selection to Category ▾',
                                      command=self._show_category_menu)
        self._add_button.pack(side=tk.RIGHT, padx=(0, 6))

        table_frame = ttk.Frame(root)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(6, 0))

        self._tree = ttk.Treeview(table_frame, columns=self._COLUMNS, show='headings',
                                  height=14, selectmode='none')
        headings = {'check': '', 'sample': 'Sample', 'n_cells': 'n cells',
                    'mean_baseline': 'Mean baseline', 'rfreq': 'Ref. frequency (Hz)',
                    'density': 'Baseline density'}
        widths = {'check': 30, 'sample': 220, 'n_cells': 70,
                 'mean_baseline': 130, 'rfreq': 140, 'density': 140}
        for col in self._COLUMNS:
            anchor = 'center' if col == 'check' else 'w'
            self._tree.heading(col, text=headings[col])
            self._tree.column(col, width=widths[col], anchor=anchor,
                              stretch=(col == 'sample'))
        # Each row carries exactly one of these tags, so there's no reliance
        # on Treeview's tag-priority rules.
        self._tree.tag_configure('even', background='#ffffff')
        self._tree.tag_configure('odd', background='#ececec')
        # plus one 'cat_<id>' tag per category, configured in _recolor_rows

        vsb = ttk.Scrollbar(table_frame, orient='vertical', command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        self._tree.bind('<Button-1>', self._on_tree_click)
        self._show_rfreq_column(False)

        self._current_average: float | None = None

        average_bar = ttk.Frame(root)
        average_bar.pack(fill=tk.X, padx=8, pady=8)
        self._average_label = ttk.Label(average_bar, text='Average density of selected samples: n/a',
                                        font=('Segoe UI', 10, 'bold'))
        self._average_label.pack(side=tk.LEFT)
        self._copy_button = ttk.Button(average_bar, text='Copy value', command=self._copy_average,
                                       state=tk.DISABLED)
        self._copy_button.pack(side=tk.LEFT, padx=(10, 0))

        self._range_label = ttk.Label(root, text='Min: n/a    Max: n/a')
        self._range_label.pack(anchor='w', padx=8, pady=(0, 8))

        self._model.add_listener(self._recolor_rows)

        # Show the superdir's last saved result (and its categories) as soon
        # as a directory is chosen, so resuming doesn't need a re-Run.
        self._loaded_dir: str | None = None
        self._autoload_job = None
        self._superdir.trace_add('write', lambda *_a: self._schedule_autoload())
        self.after_idle(self._autoload)

    # -- calibration auto-pick ----------------------------------------------

    def _calib_edited(self):
        if self._calib_setting:
            return
        # Typed or browsed: the user's choice from now on (until cleared).
        self._calib_source = 'user' if self._calib_json.get().strip() else None
        self._calib_note.configure(text='')

    def _set_calib(self, path: str, source: str | None, note: str):
        self._calib_setting = True
        try:
            self._calib_json.set(path)
        finally:
            self._calib_setting = False
        self._calib_source = source
        self._calib_note.configure(text=note)

    def _auto_calibration(self, superdir: Path | None):
        """Fill the calibration field unless the user chose it: the newest
        calibration JSON in the experiment folder, else the last one used."""
        if self._calib_source == 'user':
            return
        found = find_experiment_calibration(superdir) if superdir is not None else None
        if found is not None:
            self._set_calib(str(found), 'folder',
                            f'Found in the experiment folder ({found.name}) — Browse to change.')
            return
        last = user_state.get('last_calibration')
        if isinstance(last, str) and last and Path(last).is_file():
            self._set_calib(last, 'last',
                            'None in the experiment folder — using the calibration from the '
                            'last run. Browse to change.')
        elif self._calib_source is not None:
            self._set_calib('', None, '')

    # -- remembered layout / options ----------------------------------------

    def _per_sample_toggled(self):
        self._update_rfreq_entry()
        user_state.update(per_sample_rfreq=bool(self._per_sample.get()))

    def _save_sash(self, _event=None):
        try:
            width = self._panes.winfo_width()
            sidebar = width - self._panes.sashpos(0)
        except tk.TclError:
            return
        if width > 100 and sidebar > 0:
            user_state.update(baseline_sidebar_width=int(sidebar))

    # -- autoload --------------------------------------------------------------

    def _schedule_autoload(self):
        if self._autoload_job is not None:
            self.after_cancel(self._autoload_job)
        self._autoload_job = self.after(400, self._autoload)

    def _autoload(self):
        self._autoload_job = None
        text = self._superdir.get().strip()
        if not text or not Path(text).is_dir():
            self._auto_calibration(None)
            return
        self._auto_calibration(Path(text))
        key = density_categories._norm(text)
        if key == self._loaded_dir:
            return
        loaded = load_latest_output(Path(text))
        self._loaded_dir = key
        if loaded is None:
            return
        rows, cal, csv_path = loaded
        per_sample = any(r[5] != 'user' for r in rows)
        self._per_sample.set(per_sample)
        self._update_rfreq_entry()
        self._populate_table(rows, cal, per_sample)
        self._message.configure(
            text=f'Showing the saved result {csv_path.name} — Run to recalculate.',
            foreground='#555')

    def _place_sash(self, event):
        # Sidebar starts at its remembered width (else ~22% of the width);
        # after that the user owns the sash.
        if not self._sash_placed and event.width > 100:
            self._sash_placed = True
            saved = user_state.get('baseline_sidebar_width')
            if isinstance(saved, int) and 150 <= saved <= event.width - 400:
                self._panes.sashpos(0, event.width - saved)
            else:
                self._panes.sashpos(0, int(event.width * 0.78))

    def _pick_superdir(self):
        chosen = filedialog.askdirectory(title='Select experiment superdir',
                                         initialdir=self._superdir.get() or None)
        if chosen:
            self._superdir.set(chosen)

    def _update_rfreq_entry(self):
        # The single-value field only applies when not using per-sample values.
        self._rfreq_entry.state(['disabled'] if self._per_sample.get() else ['!disabled'])

    def _pick_calib_json(self):
        chosen = filedialog.askopenfilename(
            title='Select calibration JSON',
            initialdir=str(Path(self._calib_json.get()).parent) if self._calib_json.get() else None,
            filetypes=[('JSON files', '*.json'), ('All files', '*.*')])
        if chosen:
            self._calib_json.set(chosen)

    # -- table population ---------------------------------------------------

    def _show_rfreq_column(self, show: bool):
        self._tree.configure(displaycolumns=[c for c in self._COLUMNS
                                             if show or c != 'rfreq'])

    def _populate_table(self, rows: list, cal: dict, per_sample: bool):
        self._tree.delete(*self._tree.get_children())
        self._checked.clear()
        self._densities.clear()
        self._names.clear()
        self._show_rfreq_column(per_sample)
        infos = []

        for i, (name, n, mean_b, _source, rfreq, _rsrc) in enumerate(rows):
            iid = str(i)
            density = apply_calculation(mean_b, cal, rfreq)
            has_density = np.isfinite(density)
            # A sample with no reference frequency has no density to average,
            # so it starts (and stays effectively) out of the selection.
            self._checked[iid] = bool(has_density)
            self._densities[iid] = density
            self._names[iid] = name
            self._tree.insert('', tk.END, iid=iid, values=(
                _CHECKED if has_density else _UNCHECKED, name, n, f'{mean_b:.6g}',
                f'{rfreq:.10g}' if np.isfinite(rfreq) else 'no JSON',
                f'{density:.6g}' if has_density else 'n/a',
            ))
            infos.append(density_categories.SampleInfo(name, int(n), float(density), float(rfreq)))

        # Restores saved categories for a new superdir; recolours via the listener.
        self._model.set_samples(Path(self._superdir.get().strip()), infos)
        self._update_average()

    # -- categories / row colouring -------------------------------------------

    def _recolor_rows(self):
        if not self.winfo_exists():
            return
        for cat in self._model.categories:
            self._tree.tag_configure(f'cat_{cat.id}', background=cat.color)
        for iid, name in self._names.items():
            cat = self._model.category_of(name)
            if cat is not None:
                tag = f'cat_{cat.id}'
            else:
                tag = 'odd' if self._tree.index(iid) % 2 else 'even'
            self._tree.item(iid, tags=(tag,))

    def _checked_names(self) -> list[str]:
        return [self._names[iid] for iid, checked in self._checked.items() if checked]

    def _show_category_menu(self):
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label='New category…', command=lambda: self._assign_selection(None))
        user_cats = [c for c in self._model.ordered_categories() if not c.skip]
        if user_cats:
            menu.add_separator()
            for cat in user_cats:
                menu.add_command(label=cat.name, background=cat.color,
                                 command=lambda cid=cat.id: self._assign_selection(cid))
        menu.add_separator()
        menu.add_command(label=density_categories.SKIP_NAME,
                         background=density_categories.SKIP_COLOR,
                         command=lambda: self._assign_selection(density_categories.SKIP_ID))
        button = self._add_button
        try:
            menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            menu.grab_release()

    def _assign_selection(self, cat_id: str | None):
        """Put the ticked samples in category `cat_id` (None: a new one)."""
        names = self._checked_names()
        if not names:
            messagebox.showinfo('Categories', 'Tick the samples to add first.', parent=self)
            return
        moving = [n for n in names
                  if (c := self._model.category_of(n)) is not None and c.id != cat_id]
        if moving:
            listed = '\n'.join(f'  {n}  ({self._model.category_of(n).name})' for n in moving[:15])
            more = f'\n  … and {len(moving) - 15} more' if len(moving) > 15 else ''
            if not messagebox.askyesno(
                    'Categories', f'{len(moving)} ticked sample(s) are already in another category '
                                  f'and will be moved:\n{listed}{more}\n\nContinue?', parent=self):
                return
        if cat_id is None:
            name = simpledialog.askstring('New category', 'Name for the new category:',
                                          initialvalue=self._model.default_name(), parent=self)
            if name is None:
                return
            self._model.new_category(name, names)
        else:
            self._model.assign(names, cat_id)
        self._select_none()

    def _remove_selection_from_category(self):
        names = [n for n in self._checked_names() if self._model.category_of(n) is not None]
        if not names:
            messagebox.showinfo('Categories', 'None of the ticked samples is in a category.',
                                parent=self)
            return
        self._model.unassign(names)
        self._select_none()

    def _on_tree_click(self, event):
        # Clicking anywhere on a row toggles its checkbox.
        if self._tree.identify_region(event.x, event.y) != 'cell':
            return
        iid = self._tree.identify_row(event.y)
        if not iid:
            return
        self._set_checked(iid, not self._checked[iid])
        self._update_average()

    def _set_checked(self, iid: str, value: bool):
        self._checked[iid] = value
        self._tree.set(iid, 'check', _CHECKED if value else _UNCHECKED)

    def _select_all(self):
        for iid in self._checked:
            self._set_checked(iid, True)
        self._update_average()

    def _select_none(self):
        for iid in self._checked:
            self._set_checked(iid, False)
        self._update_average()

    def _invert_selection(self):
        for iid, value in self._checked.items():
            self._set_checked(iid, not value)
        self._update_average()

    def _update_average(self):
        selected_iids = [iid for iid, checked in self._checked.items()
                         if checked and np.isfinite(self._densities[iid])]
        if not selected_iids:
            self._current_average = None
            self._average_label.configure(text='Average density of selected samples: n/a')
            self._range_label.configure(text='Min: n/a    Max: n/a')
            self._copy_button.configure(state=tk.DISABLED)
            return
        selected = [self._densities[iid] for iid in selected_iids]
        avg = sum(selected) / len(selected)
        self._current_average = avg
        self._average_label.configure(
            text=f'Average density of selected samples: {avg:.4f}  (n={len(selected)})')
        min_iid = min(selected_iids, key=self._densities.__getitem__)
        max_iid = max(selected_iids, key=self._densities.__getitem__)
        self._range_label.configure(
            text=f'Min: {self._densities[min_iid]:.4f} ({self._names[min_iid]})    '
                 f'Max: {self._densities[max_iid]:.4f} ({self._names[max_iid]})')
        self._copy_button.configure(state=tk.NORMAL)

    def _copy_average(self):
        if self._current_average is None:
            return
        self.clipboard_clear()
        self.clipboard_append(f'{self._current_average:.4f}')

    # -- run ------------------------------------------------------------

    def _run(self):
        superdir_text = self._superdir.get().strip()
        if not superdir_text:
            messagebox.showerror('Baseline Density Calculator', 'Choose a superdir first.')
            return
        superdir = Path(superdir_text)
        if not superdir.is_dir():
            messagebox.showerror('Baseline Density Calculator', f'Directory not found:\n{superdir}')
            return

        per_sample = self._per_sample.get()
        rfreq = None
        if not per_sample:
            rfreq_text = self._rfreq.get().strip()
            try:
                rfreq = float(rfreq_text)
            except ValueError:
                messagebox.showerror('Baseline Density Calculator',
                                     f'Reference frequency must be a number, got: {rfreq_text!r}')
                return

        calib_text = self._calib_json.get().strip()
        if not calib_text:
            messagebox.showerror('Baseline Density Calculator', 'Choose a calibration JSON first.')
            return
        calib_json = Path(calib_text)
        if not calib_json.is_file():
            messagebox.showerror('Baseline Density Calculator', f'Calibration JSON not found:\n{calib_json}')
            return

        self._run_button.configure(state=tk.DISABLED)
        try:
            rows, cal, out_path = run_computation(superdir, rfreq, calib_json)
        except Exception as exc:
            messagebox.showerror('Baseline Density Calculator', str(exc))
            return
        finally:
            self._run_button.configure(state=tk.NORMAL)

        if not rows:
            self._message.configure(text=f'No buoyant mass CSVs found under {superdir}.')
            self._tree.delete(*self._tree.get_children())
            self._checked.clear()
            self._densities.clear()
            self._names.clear()
            self._model.set_samples(superdir, [])
            self._update_average()
            return

        missing = [r[0] for r in rows if not np.isfinite(r[4])]
        self._message.configure(text=(
            f"No readable *_reference_values.json for {len(missing)} sample(s), "
            f"left out of the average: {', '.join(missing)}") if missing else '',
            foreground='#a00')
        self._loaded_dir = density_categories._norm(superdir)
        self._populate_table(rows, cal, per_sample)
        user_state.update(last_calibration=str(calib_json))
        if self._on_computed is not None:
            source = {'folder': 'found in the experiment folder',
                      'last': 'last used'}.get(self._calib_source, 'chosen manually')
            try:
                self._on_computed({
                    'superdir': superdir, 'calib_json': calib_json, 'calib_source': source,
                    'slope': cal['slope'], 'intercept': cal['intercept'],
                    'per_sample': per_sample, 'rfreq': rfreq, 'out_path': out_path,
                    'n_samples': len(rows), 'missing_rfreq': missing})
            except Exception as exc:   # a host hook must not break the calculator
                print(f'  [warn] on_computed failed: {exc}')
        messagebox.showinfo('Baseline Density Calculator', f'Written:\n{out_path}')

    def run(self):
        """Public alias of the Run button (e.g. a host's keyboard shortcut)."""
        if str(self._run_button['state']) != tk.DISABLED:
            self._run()


def run_gui(*, superdir: str | None, calib_json: str | None, rfreq: float | None,
            per_sample_rfreq: bool = False):
    root = tk.Tk()
    root.title('Baseline Density Calculator')
    root.minsize(900, 520)
    gui = BaselineDensityGUI(root, superdir=superdir, calib_json=calib_json, rfreq=rfreq,
                             per_sample_rfreq=per_sample_rfreq)
    gui.pack(fill=tk.BOTH, expand=True)
    root.mainloop()


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def build_embedded_page(parent: tk.Widget, *, superdir: str | None = None,
                        calib_json: str | None = None,
                        rfreq: float | None = None,
                        superdir_var: tk.StringVar | None = None,
                        per_sample_rfreq: bool = False,
                        category_model: density_categories.CategoryModel | None = None,
                        on_computed=None,
                        ) -> ttk.Frame:
    """
    Build this tool's GUI as a Frame suitable for embedding in a larger
    application (e.g. a wizard page), rather than owning its own Tk root.

    Pass an existing `superdir_var` (rather than `superdir`) to bind this
    page's directory field to a StringVar shared with another page — e.g. a
    wizard sharing one directory across several steps. `per_sample_rfreq`
    sets the initial state of the per-sample reference-frequency checkbox.
    Pass a `category_model` to share the density categories with other
    pages (the wizard's category review step); otherwise the page keeps its
    own. `on_computed(info)` is called after each successful Run with a dict
    (superdir, calib_json, calib_source, slope, intercept, per_sample, rfreq,
    out_path, n_samples, missing_rfreq) — e.g. for the wizard's analysis log.
    """
    return BaselineDensityGUI(parent, superdir=superdir, calib_json=calib_json,
                              rfreq=rfreq, superdir_var=superdir_var,
                              per_sample_rfreq=per_sample_rfreq,
                              category_model=category_model,
                              on_computed=on_computed)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_headless(args: argparse.Namespace):
    superdir = Path(args.superdir)
    if not superdir.is_dir():
        raise FileNotFoundError(f"Directory not found: {superdir}")

    rfreq = None if args.per_sample_rfreq else args.rfreq
    rows, cal, _ = run_computation(superdir, rfreq, Path(args.calib_json))
    if not rows:
        print(f"No buoyant mass CSVs found under {superdir}.")
        return
    print(format_report(superdir.name, rfreq, rows, cal))


def main():
    args = parse_cli_args()
    has_rfreq = args.rfreq is not None or args.per_sample_rfreq
    if args.superdir is not None and has_rfreq and args.calib_json is not None:
        run_headless(args)
    else:
        run_gui(superdir=args.superdir, calib_json=args.calib_json, rfreq=args.rfreq,
                per_sample_rfreq=args.per_sample_rfreq)


if __name__ == '__main__':
    main()
