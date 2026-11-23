"""
analysis_wizard.py

One-stop-shop wizard that walks through the full analysis pipeline as a
sequence of "step" pages, embedding the existing per-stage GUIs rather than
reimplementing them. A sidebar lists every step with its status read from
disk (experiment_status.py) and jumps to any step on click; Previous / Next
walk them in order.

    1. Overview           — the experiment directory (remembered from the
                             last session; type, paste or Browse to change
                             it) and a checklist of where the experiment is,
                             read from disk: buoyant mass (the MATLAB SMR
                             analysis, run outside the wizard), baseline
                             density & categories, imaging per category,
                             gating, compile (flagged stale when gates,
                             imaging results or annotations changed after
                             it), new analysis dir. "Continue where I left
                             off" jumps to the first unfinished step after
                             the furthest finished one.
    2. Baseline density   — calculate_baseline_density.py's GUI (embedded)
                             for the fluid baseline density calculation, with
                             its density-category sidebar: group the samples
                             that will be imaging-analysed at one shared media
                             density (Skip for samples not to analyse). The
                             directory's last saved result and categories are
                             shown without re-running. Next needs every
                             sample in a category.
    3. Imaging analysis   — density_categories.ReviewPage: one card per
                             category (samples, densities, on-disk analysis
                             status) with a Copy button for SMRFXMAnalysis'
                             batch window ("Paste category" there). The
                             batch window opens automatically (in the
                             paella_analysis env, already scanned on the
                             experiment directory) the first time this step
                             is shown. Next needs every category analysed at
                             its own density, as read from the HDF5s.
    4. Gating             — gate_experiments_inplace.py embedded in
                             'combined' mode: the four BM attribute
                             histograms plus iFXM volume, gated per-sample
                             together. Auto-discovers as soon as the
                             directory is in place. Next needs "Finalize
                             gating" — or every sample already having a gate
                             file on disk (kept as they are).
    5. Sample QC          — sample_qc.QCPage: per-sample cell counts, pairing
                             rate, cells and crops kept after gating, imaging
                             status, with outliers flagged, before compiling.
                             The loaded samples are cached and reused by
                             step 6 while nothing on disk changes.
    6. Compile experiment — compile_experiment.py embedded; its directory
                             field is seeded from the shared directory the
                             first time this page is built, but still
                             requires an explicit "Discover Samples" click.
                             Annotations are saved as you edit
                             (compile_annotations.json) and restored on the
                             next Discover (or seeded from the previous
                             compile); a fresh table opens "Auto-detect from
                             names". Clicking its Done button advances.
    7. New analysis dir   — new_analysis_dir.py embedded, using the shared
                             directory as its raw data dir: Create scaffolds
                             the code + data_analysis dirs named after it,
                             then offers to copy in its *_compiled and
                             *_gating_summary dirs. Its Close button closes
                             the wizard.

Steps 1, 2, 4 and 7 share one experiment-directory field (typing in any
updates the others live); step 3 shows the directory the categories were
computed for; step 6 seeds from it once. Steps 2 and 3 share one
density_categories.CategoryModel. The last experiment directory is kept in
~/.biophys_helpers/wizard_state.json.

Each embedded page is built by a `build_embedded_page(parent, ...)` function
(or page class) that lives in the corresponding script, so every script's
standalone CLI/GUI entry point keeps working unmodified. Pages are built
lazily on first visit and cached (shown/hidden rather than destroyed) so
in-progress work is preserved when navigating back and forth.

Usage:
    python analysis_wizard.py
"""
import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import calculate_baseline_density
import compile_experiment
import density_categories
import experiment_status as es
import gate_experiments_inplace
import new_analysis_dir
import sample_qc

STATE_PATH = Path.home() / '.biophys_helpers' / 'wizard_state.json'


def _load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text(encoding='utf-8'))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(**updates):
    state = _load_state()
    state.update(updates)
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding='utf-8')
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Page texts
# ---------------------------------------------------------------------------

_BASELINE_DENSITY_TEXT = (
    "Compute each sample's fluid baseline density from its buoyant mass data, "
    "then group the samples to be imaging-analysed at one shared media density "
    "into categories: tick them and use \"Add Selection to Category\" (Skip "
    "holds samples not to analyse). Every sample needs a category to continue."
)

_REVIEW_TEXT = (
    "Check each category's samples and density, then click its Copy button and "
    "\"Paste category\" in the SMRFXMAnalysis batch window (opened for you; reopen "
    "it with the button below): that ticks exactly the category's samples and "
    "locks the media density to its mean. Run it, then move on to the next "
    "category. Status is read from the samples' HDF5s (every 30 s, or Refresh); "
    "Next unlocks once every category is analysed at its own density. A sample "
    "that can't be analysed can be moved to Skip back in step 2."
)

_QC_TEXT = (
    "Per-sample QC of what the compile will contain (gates applied). Check the "
    "flagged rows before compiling: re-gate in step 4, re-analyse in step 3, or "
    "move a sample to Skip in step 2. Nothing here blocks Next."
)

_NEW_ANALYSIS_DIR_TEXT = (
    "Scaffold the downstream analysis for this experiment: Create makes the "
    "code and data_analysis dirs named after the experiment directory, then "
    "you can copy in its compiled data and gating summary."
)

_MASS_NOTE = (
    "The buoyant mass (SMR) analysis runs in MATLAB, outside this wizard; each "
    "sample needs its *_mass_results folder before the baseline density step."
)

# Sidebar / checklist icons per experiment_status state.
_ICONS = {es.DONE: ('✓', '#15803d'), es.PARTIAL: ('◐', '#b45309'),
          es.STALE: ('!', '#b45309'), es.TODO: ('○', '#9ca3af')}


def _labelled_page(parent, text: str) -> tuple[ttk.Frame, ttk.Frame]:
    """A page with an explanatory label on top; returns (page, body)."""
    page = ttk.Frame(parent)
    ttk.Label(page, text=text, wraplength=900, justify='left').pack(
        anchor='nw', padx=8, pady=(8, 8))
    body = ttk.Frame(page)
    body.pack(fill=tk.BOTH, expand=True)
    return page, body


# ---------------------------------------------------------------------------
# Wizard shell
# ---------------------------------------------------------------------------

class Wizard:
    """
    Step-wizard shell: a step sidebar on the left, a title and a content area
    holding one page at a time, and a Previous/Next bar.

    Each step is a dict:
        title:            shown in the header and the sidebar
        builder:          builder(parent) -> ttk.Frame, called once per page
                          on first visit (or prebuild()); the frame is cached
                          and toggled with pack/pack_forget on later visits so
                          page state survives navigation.
        stage:            optional experiment_status stage key; its state
                          gives the step's sidebar icon.
        requires_finalize: if True, Next stays disabled on this step until
                          mark_complete(index) is called (and is disabled
                          again by mark_incomplete(index)) — unless
                          finalize_satisfied() returns True.
        finalize_satisfied: optional callable() -> bool; see above.
        satisfied_hint:   text shown when finalize_satisfied lets Next through.
        next_check:       optional callable() -> bool; Next stays disabled
                          on this step whenever it returns False (checked
                          live — call refresh_nav() when something this
                          depends on changes, e.g. a directory field).
        next_hint:        text (or callable() -> text) shown beside Next while
                          next_check fails.
        on_show:          optional callable() run each time the step is shown
                          (after its page is built).

    `status_provider()` -> {stage: StageStatus} (or {}) feeds the sidebar;
    refresh_status() re-reads it.
    """

    def __init__(self, root: tk.Tk, steps: list, *, status_provider=None):
        self._root = root
        self._steps = steps
        self._pages: dict[int, ttk.Frame] = {}
        self._completed: dict[int, bool] = {}
        self._index = 0
        self._status_provider = status_provider
        self.status: dict = {}

        body = ttk.Frame(root)
        body.pack(fill=tk.BOTH, expand=True)

        side = tk.Frame(body, background='#f3f4f6', width=230)
        side.pack(side=tk.LEFT, fill=tk.Y)
        side.pack_propagate(False)
        tk.Label(side, text='Steps', background='#f3f4f6', anchor='w',
                 font=('TkDefaultFont', 10, 'bold')).pack(fill=tk.X, padx=10, pady=(12, 6))
        self._side_rows = []
        for i, step in enumerate(steps):
            row = tk.Frame(side, background='#f3f4f6', cursor='hand2')
            row.pack(fill=tk.X, padx=4, pady=1)
            icon = tk.Label(row, text='', width=2, background='#f3f4f6',
                            font=('TkDefaultFont', 10, 'bold'))
            icon.pack(side=tk.LEFT, padx=(4, 2))
            label = tk.Label(row, text=f'{i + 1}. {step["title"]}', anchor='w',
                             background='#f3f4f6', justify='left', wraplength=180)
            label.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=4)
            for w in (row, icon, label):
                w.bind('<Button-1>', lambda _e, idx=i: self.goto(idx))
            self._side_rows.append((row, icon, label))

        main = ttk.Frame(body)
        main.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._title_var = tk.StringVar()
        ttk.Label(main, textvariable=self._title_var,
                  font=('TkDefaultFont', 13, 'bold')).pack(anchor='w', padx=12, pady=(12, 4))
        self._content = ttk.Frame(main)
        self._content.pack(fill=tk.BOTH, expand=True, padx=12)

        nav = ttk.Frame(root)
        nav.pack(fill=tk.X, padx=12, pady=12)
        self._prev_btn = ttk.Button(nav, text='← Previous', command=self._go_prev)
        self._prev_btn.pack(side=tk.LEFT)
        self._hint_var = tk.StringVar()
        ttk.Label(nav, textvariable=self._hint_var, foreground='#a00').pack(
            side=tk.LEFT, expand=True)
        self._next_btn = ttk.Button(nav, text='Next →', command=self._go_next)
        self._next_btn.pack(side=tk.RIGHT)

        self._show(0)

    # -- step status ------------------------------------------------------

    def refresh_status(self):
        """Re-read the on-disk status and redraw the sidebar (and Next)."""
        if self._status_provider is not None:
            try:
                self.status = self._status_provider() or {}
            except Exception as exc:     # a status glitch must not break navigation
                print(f'  [warn] could not read the experiment status: {exc}')
                self.status = {}
        self._draw_sidebar()
        self._update_nav()

    def _draw_sidebar(self):
        for i, (row, icon, label) in enumerate(self._side_rows):
            current = i == self._index
            bg = '#dbeafe' if current else '#f3f4f6'
            stage = self._steps[i].get('stage')
            st = self.status.get(stage) if stage else None
            text, color = _ICONS.get(st.state, ('', '#000')) if st else ('', '#000')
            for w in (row, icon, label):
                w.configure(background=bg)
            icon.configure(text=text, foreground=color)
            label.configure(font=('TkDefaultFont', 9, 'bold' if current else 'normal'))

    # -- step completion (for requires_finalize steps) ----------------------

    def mark_complete(self, index: int):
        self._completed[index] = True
        if self._index == index:
            self._update_nav()

    def mark_incomplete(self, index: int):
        self._completed[index] = False
        if self._index == index:
            self._update_nav()

    def refresh_nav(self):
        """Re-evaluate the current step's `next_check` (call after something
        it depends on changes, e.g. a shared directory field)."""
        self._update_nav()

    # -- navigation -----------------------------------------------------

    def prebuild(self, index: int):
        """Build a step's page now (hidden), e.g. so it starts loading data."""
        if index not in self._pages:
            self._pages[index] = self._steps[index]['builder'](self._content)

    def goto(self, index: int):
        """Jump to any step (the sidebar); a step's own gates only govern Next."""
        if 0 <= index < len(self._steps) and index != self._index:
            self._show(index)

    def _show(self, index: int):
        self.prebuild(index)
        for i, page in self._pages.items():
            if i == index:
                page.pack(fill=tk.BOTH, expand=True)
            else:
                page.pack_forget()

        self._index = index
        n = len(self._steps)
        self._title_var.set(f"Step {index + 1} of {n} — {self._steps[index]['title']}")
        self.refresh_status()
        on_show = self._steps[index].get('on_show')
        if on_show is not None:
            on_show()

    def _finalize_ok(self, index: int) -> bool:
        step = self._steps[index]
        if not step.get('requires_finalize', False) or self._completed.get(index, False):
            return True
        satisfied = step.get('finalize_satisfied')
        return bool(satisfied and satisfied())

    def _step_ready(self, index: int) -> bool:
        """Whether this step's own requirements (finalize / next_check) are
        satisfied, i.e. whether Next may leave it."""
        if not self._finalize_ok(index):
            return False
        check = self._steps[index].get('next_check')
        return check is None or bool(check())

    def _update_nav(self):
        index = self._index
        n = len(self._steps)
        self._prev_btn.config(state=tk.NORMAL if index > 0 else tk.DISABLED)
        ready = self._step_ready(index)
        has_next = index < n - 1
        self._next_btn.config(state=tk.NORMAL if has_next and ready else tk.DISABLED)

        step = self._steps[index]
        hint = ''
        if has_next and not ready:
            if not self._finalize_ok(index):
                hint = 'Click "Finalize gating" below to continue.'
            else:
                hint = step.get('next_hint', 'Complete this step to continue.')
                if callable(hint):
                    hint = hint()
        elif (step.get('requires_finalize') and not self._completed.get(index, False)
              and has_next):
            hint = step.get('satisfied_hint', '')
        self._hint_var.set(hint)

    def _go_prev(self):
        if self._index > 0:
            self._show(self._index - 1)

    def go_next(self):
        """Advance as the Next button would (no-op if this step isn't ready)."""
        self._go_next()

    def _go_next(self):
        if not self._step_ready(self._index):
            return   # guard directly, don't rely solely on the button's disabled state
        if self._index < len(self._steps) - 1:
            self._show(self._index + 1)


# ---------------------------------------------------------------------------
# Overview page
# ---------------------------------------------------------------------------

class OverviewPage(ttk.Frame):
    """Experiment directory + on-disk progress checklist + Continue."""

    def __init__(self, parent, *, superdir_var: tk.StringVar, wizard_ref, stage_steps: dict):
        super().__init__(parent)
        self._superdir = superdir_var
        self._wiz = wizard_ref           # callable() -> Wizard
        self._stage_steps = stage_steps  # stage key -> step index

        form = ttk.Frame(self)
        form.pack(fill=tk.X, padx=8, pady=(8, 4))
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text='Experiment directory:').grid(row=0, column=0, sticky='w')
        ttk.Entry(form, textvariable=superdir_var).grid(row=0, column=1, sticky='ew', padx=6)
        ttk.Button(form, text='Browse…', command=self._browse).grid(row=0, column=2)

        self._table = ttk.Frame(self)
        self._table.pack(fill=tk.X, padx=8, pady=(12, 4))

        buttons = ttk.Frame(self)
        buttons.pack(fill=tk.X, padx=8, pady=8)
        self._continue_btn = ttk.Button(buttons, text='Continue where I left off →',
                                        command=self._continue)
        self._continue_btn.pack(side=tk.LEFT)
        ttk.Button(buttons, text='Refresh status', command=self.refresh).pack(
            side=tk.LEFT, padx=(8, 0))
        self._continue_note = ttk.Label(buttons, text='', foreground='#555')
        self._continue_note.pack(side=tk.LEFT, padx=12)

        ttk.Label(self, text=_MASS_NOTE, foreground='#555', wraplength=900,
                  justify='left').pack(anchor='w', padx=8, pady=(8, 0))

    def _browse(self):
        chosen = filedialog.askdirectory(title='Select experiment directory',
                                         initialdir=self._superdir.get() or None)
        if chosen:
            self._superdir.set(chosen)

    def refresh(self):
        wiz = self._wiz()
        if wiz is not None:
            wiz.refresh_status()
        self.render(wiz.status if wiz is not None else {})

    def render(self, status: dict):
        for child in self._table.winfo_children():
            child.destroy()
        text = self._superdir.get().strip()
        if not text or not Path(text).is_dir():
            ttk.Label(self._table, text='Choose an experiment directory to see its progress.',
                      foreground='#555').grid(row=0, column=0, sticky='w')
            self._continue_btn.state(['disabled'])
            self._continue_note.configure(text='')
            return
        for j, h in enumerate(('', 'Step', 'Status', '')):
            ttk.Label(self._table, text=h, foreground='#555').grid(
                row=0, column=j, sticky='w', padx=(0, 12))
        for i, key in enumerate(es.STAGE_TITLES, start=1):
            st = status.get(key)
            if st is None:
                continue
            icon, color = _ICONS.get(st.state, ('', '#000'))
            tk.Label(self._table, text=icon, foreground=color,
                     font=('TkDefaultFont', 11, 'bold')).grid(row=i, column=0, padx=(0, 6))
            ttk.Label(self._table, text=st.title).grid(row=i, column=1, sticky='w', padx=(0, 12))
            ttk.Label(self._table, text=st.detail, foreground='#374151').grid(
                row=i, column=2, sticky='w', padx=(0, 12))
            if key in self._stage_steps:
                ttk.Button(self._table, text='Open',
                           command=lambda k=key: self._wiz().goto(self._stage_steps[k])).grid(
                    row=i, column=3, sticky='w', pady=1)
        target = es.first_unfinished(status)
        if target is None:
            self._continue_btn.state(['disabled'])
            self._continue_note.configure(text='Every step is done for this experiment.')
        else:
            self._continue_btn.state(['!disabled'])
            self._continue_note.configure(text=f'→ {es.STAGE_TITLES[target]}')
        self._target = target

    def _continue(self):
        target = getattr(self, '_target', None)
        if target in self._stage_steps:
            self._wiz().goto(self._stage_steps[target])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    root = tk.Tk()
    root.title('Biophysics Analysis Wizard')
    root.minsize(1200, 760)

    # Shared across steps 1 (overview), 2 (baseline density), 4 (gating) and 7
    # (new analysis dir): their directory fields are bound to this same
    # StringVar, so typing in any updates the others live, and step 7 shows
    # it as its raw data dir. The compile page (step 6) only reads its
    # current value once, as an initial seed, since compilation may
    # reasonably target a different directory afterward. Starts at the last
    # experiment used.
    last = _load_state().get('last_experiment', '')
    experiment_superdir = tk.StringVar(value=last if last and Path(last).is_dir() else '')

    # Density categories, shared by step 2 (assigning) and step 3 (review /
    # copy to SMRFXMAnalysis' batch window).
    categories = density_categories.CategoryModel()
    batch_window = density_categories.BatchWindowLauncher()
    records_cache = sample_qc.RecordsCache()
    pages: dict = {}           # page objects step builders hand back for later calls
    auto_opened = [False]      # the batch window opens by itself only once
    wiz_holder: list = []

    STAGE_STEPS = {'baseline': 1, 'imaging': 2, 'gating': 3, 'compile': 5, 'new_dir': 6}

    def _superdir_path() -> Path | None:
        text = experiment_superdir.get().strip()
        return Path(text) if text and Path(text).is_dir() else None

    def _status():
        sd = _superdir_path()
        return es.scan(sd) if sd is not None else {}

    def _has_superdir():
        return _superdir_path() is not None

    # -- step 1: overview ------------------------------------------------

    def build_overview(parent):
        pages['overview'] = OverviewPage(
            parent, superdir_var=experiment_superdir,
            wizard_ref=lambda: wiz_holder[0] if wiz_holder else None,
            stage_steps=STAGE_STEPS)
        return pages['overview']

    def _on_show_overview():
        # Runs once inside Wizard() itself (step 1 is shown first), before
        # `wiz` is bound — hence the holder.
        pages['overview'].render(wiz_holder[0].status if wiz_holder else _status())

    # -- step 2: baseline density & categories ---------------------------

    def build_baseline_density(parent):
        page, body = _labelled_page(parent, _BASELINE_DENSITY_TEXT)
        panel = calculate_baseline_density.build_embedded_page(
            body, superdir_var=experiment_superdir, per_sample_rfreq=True,
            category_model=categories)
        panel.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        return page

    def _same_dir_as_categories() -> bool:
        text = experiment_superdir.get().strip()
        return (bool(text) and categories.superdir is not None
                and density_categories.same_path(text, categories.superdir))

    def _baseline_ready():
        return _has_superdir() and _same_dir_as_categories() and categories.ready_problem() is None

    def _baseline_hint():
        if not _has_superdir():
            return 'Choose an experiment directory to continue.'
        if not _same_dir_as_categories():
            return 'Run the baseline density calculation for this directory to continue.'
        return categories.ready_problem() or ''

    # -- step 3: imaging by category ---------------------------------------

    def open_batch_window():
        target = categories.superdir or _superdir_path()
        if target is None:
            messagebox.showinfo('Batch analysis', 'Choose an experiment directory first.',
                                parent=root)
            return
        if batch_window.is_running():
            messagebox.showinfo(
                'Batch analysis',
                'The SMRFXMAnalysis batch window is already open (it may be behind this '
                'window).', parent=root)
            return
        error = batch_window.launch(str(target))
        if error:
            messagebox.showerror('Batch analysis', error, parent=root)
            return

        def _check_started():
            code = batch_window.exit_code()
            if code is not None and code != 0:
                messagebox.showerror(
                    'Batch analysis',
                    f'The batch window exited during startup (code {code}).\n\n'
                    f'{batch_window.log_tail()}\n\nFull log: {batch_window.log_path}',
                    parent=root)
        root.after(5000, _check_started)

    def build_review(parent):
        page, body = _labelled_page(parent, _REVIEW_TEXT)
        pages['review'] = density_categories.ReviewPage(
            body, categories, on_open_batch=open_batch_window,
            on_status_change=lambda: wiz.refresh_status())
        pages['review'].pack(fill=tk.BOTH, expand=True)
        return page

    def _on_show_review():
        pages['review'].refresh_status()   # categories may have changed in step 2
        if not auto_opened[0] and categories.analysis_categories():
            auto_opened[0] = True
            open_batch_window()

    def _review_ready():
        return 'review' in pages and pages['review'].all_analysed()

    def _review_hint():
        return pages['review'].remaining_hint() if 'review' in pages else ''

    # -- step 4: gating -----------------------------------------------------

    def build_gating(parent):
        return gate_experiments_inplace.build_embedded_page(
            parent, superdir_var=experiment_superdir,
            on_finalized=lambda: (wiz.mark_complete(3), wiz.refresh_status()),
            on_discover=lambda: wiz.mark_incomplete(3))

    def _gates_on_disk():
        st = wiz.status.get('gating')
        return st is not None and st.state == es.DONE

    # -- step 5: sample QC --------------------------------------------------

    def build_qc(parent):
        page, body = _labelled_page(parent, _QC_TEXT)
        pages['qc'] = sample_qc.QCPage(body, superdir_var=experiment_superdir,
                                       cache=records_cache)
        pages['qc'].pack(fill=tk.BOTH, expand=True)
        return page

    # -- step 6: compile ------------------------------------------------------

    def _compiled():
        wiz.refresh_status()
        wiz.go_next()

    def build_compile(parent):
        return compile_experiment.build_embedded_page(
            parent, initial_superdir=experiment_superdir.get(),
            on_done=_compiled, auto_detect_names=True,
            records_provider=records_cache.get)

    # -- step 7: new analysis dir -------------------------------------------

    def build_new_analysis_dir(parent):
        page, body = _labelled_page(parent, _NEW_ANALYSIS_DIR_TEXT)
        panel = new_analysis_dir.build_embedded_page(
            body, raw_dir_var=experiment_superdir, on_close=root.destroy)
        panel.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        # Closing the window mid-copy would kill the copy thread mid-file;
        # route it through the panel so the copy is cancelled cleanly first.
        root.protocol('WM_DELETE_WINDOW', panel.on_close)
        return page

    steps = [
        {'title': 'Overview', 'builder': build_overview, 'on_show': _on_show_overview,
         'next_check': _has_superdir,
         'next_hint': 'Choose an experiment directory to continue.'},
        {'title': 'Baseline Density & Categories', 'builder': build_baseline_density,
         'stage': 'baseline', 'next_check': _baseline_ready, 'next_hint': _baseline_hint},
        {'title': 'Imaging Analysis by Category', 'builder': build_review, 'stage': 'imaging',
         'on_show': _on_show_review, 'next_check': _review_ready, 'next_hint': _review_hint},
        {'title': 'Gating: Buoyant Mass + Volume', 'builder': build_gating, 'stage': 'gating',
         'requires_finalize': True, 'finalize_satisfied': _gates_on_disk,
         'satisfied_hint': 'Every sample already has a gate file — Next keeps them; '
                           'Finalize to replace them.'},
        {'title': 'Sample QC', 'builder': build_qc,
         'on_show': lambda: pages['qc'].refresh()},
        {'title': 'Compile Experiment', 'builder': build_compile, 'stage': 'compile'},
        {'title': 'New Analysis Dir', 'builder': build_new_analysis_dir, 'stage': 'new_dir'},
    ]

    wiz = Wizard(root, steps, status_provider=_status)
    wiz_holder.append(wiz)

    # When the directory changes (typing, Browse, another page): remember it,
    # re-read the status, and build the baseline page in the background so
    # its saved result and categories load (step 3 needs them even if step 2
    # is skipped via the sidebar). Debounced so typing doesn't rescan per key.
    pending = [None]

    def _on_dir_changed():
        pending[0] = None
        sd = _superdir_path()
        if sd is not None:
            _save_state(last_experiment=str(sd))
            wiz.prebuild(1)
        wiz.refresh_status()
        if 'overview' in pages:
            pages['overview'].render(wiz.status)

    def _schedule_dir_changed(*_a):
        if pending[0] is not None:
            root.after_cancel(pending[0])
        pending[0] = root.after(500, _on_dir_changed)

    experiment_superdir.trace_add('write', _schedule_dir_changed)
    categories.add_listener(wiz.refresh_nav)
    if _has_superdir():
        root.after_idle(_on_dir_changed)
    root.mainloop()


if __name__ == '__main__':
    main()
