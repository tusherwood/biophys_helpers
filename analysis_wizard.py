"""
analysis_wizard.py

One-stop-shop wizard that walks through the full analysis pipeline as a
sequence of "step" pages, embedding the existing per-stage GUIs rather than
reimplementing them. A resizable sidebar lists every step with its status
read from disk (experiment_status.py) and jumps to any step on click;
Previous / Next walk them in order.

    1. Overview           — the experiment directory (remembered from the
                             last session; type, paste or Browse to change
                             it, or pick from "Recent" / "Newest in
                             raw_data") and a checklist of where the
                             experiment is, read from disk: buoyant mass
                             (the MATLAB SMR analysis, run outside the
                             wizard), baseline density & categories, imaging
                             per category, gating, compile (flagged stale
                             when gates, imaging results or annotations
                             changed after it), new analysis dir. A sample
                             name check flags name tokens the auto-detect
                             rules don't recognise and likely typos (review
                             / teach the rules with "Review names…").
                             "Continue where I left off" jumps to the first
                             unfinished step after the furthest finished one.
    2. Baseline density   — calculate_baseline_density.py's GUI (embedded)
                             for the fluid baseline density calculation, with
                             its density-category sidebar: group the samples
                             that will be imaging-analysed at one shared media
                             density (Skip for samples not to analyse). The
                             directory's last saved result and categories are
                             shown without re-running; the calibration JSON
                             is picked from the experiment folder (else the
                             last one used) unless chosen by hand. Next
                             needs every sample in a category.
    3. Imaging analysis   — density_categories.ReviewPage: one card per
                             category (samples, densities, on-disk analysis
                             status) with a Copy button for SMRFXMAnalysis'
                             batch window ("Paste category" there). The
                             batch window opens automatically (in the
                             paella_analysis env, already scanned on the
                             experiment directory) the first time this step
                             is shown. A message bar, sound (mutable) and
                             taskbar flash announce each category finishing
                             and the batch window closing. Next needs every
                             category analysed at its own density, as read
                             from the HDF5s.
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
                             names". Done writes in the background with a
                             progress bar (navigation is locked meanwhile)
                             and then advances.
    7. New analysis dir   — new_analysis_dir.py embedded, using the shared
                             directory as its raw data dir: Create scaffolds
                             the code + data_analysis dirs named after it,
                             then offers to copy in its *_compiled and
                             *_gating_summary dirs (and the analysis log).
                             A finished copy moves on to the summary; Close
                             closes the wizard.
    8. Summary            — what was produced (compiled folder, workbook,
                             crop cache, analysis dirs, log) with Open
                             buttons, a ready-to-paste prompt for the
                             biophys-plotting plugin, the final checklist and
                             any QC flags still standing.

Steps 1, 2, 4 and 7 share one experiment-directory field (typing in any
updates the others live); step 3 shows the directory the categories were
computed for; step 6 seeds from it once. Steps 2 and 3 share one
density_categories.CategoryModel.

Provenance: checkpoints (baseline run, categories confirmed, a category's
imaging done, gating finalized / kept, compile with its QC flags, new
analysis dir, copy) are appended to <experiment>/analysis_log.md
(analysis_log.py), which is copied into the analysis dir with the data.

Shortcuts: Alt+→ / Alt+← Next / Previous, Ctrl+1…8 jump to a step,
Ctrl+Enter the step's main action (Continue, Run, Refresh status, Finalize
gating, Recompute QC, Discover / Done, Create / Copy), F5 refresh status.
All but Ctrl+Enter are ignored while typing in a text field.

Per-user settings (user_state.py, ~/.biophys_helpers/wizard_state.json):
last and recent experiments, window size / position / maximized, sidebar
widths, the per-sample reference-frequency box, the last calibration JSON,
and the notification sound.

Each embedded page is built by a `build_embedded_page(parent, ...)` function
(or page class) that lives in the corresponding script, so every script's
standalone CLI/GUI entry point keeps working unmodified. Pages are built
lazily on first visit and cached (shown/hidden rather than destroyed) so
in-progress work is preserved when navigating back and forth.

Usage:
    python analysis_wizard.py
"""
import os
import re
import shutil
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import analysis_log
import auto_metadata_dialog
import calculate_baseline_density
import compile_experiment
import density_categories
import desktop_notify
import experiment_status as es
import gate_experiments_inplace
import new_analysis_dir
import sample_name_parser
import sample_qc
import user_state


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
    "you can copy in its compiled data and gating summary (the analysis log "
    "comes along). A finished copy moves on to the summary."
)

_MASS_NOTE = (
    "The buoyant mass (SMR) analysis runs in MATLAB, outside this wizard; each "
    "sample needs its *_mass_results folder before the baseline density step."
)

_SHORTCUTS_HINT = 'Alt+←/→ steps · Ctrl+1…8 jump · Ctrl+Enter main action · F5 refresh'

# Sidebar / checklist icons per experiment_status state.
_ICONS = {es.DONE: ('✓', '#15803d'), es.PARTIAL: ('◐', '#b45309'),
          es.STALE: ('!', '#b45309'), es.TODO: ('○', '#9ca3af')}

_TEXT_INPUT_CLASSES = {'Entry', 'TEntry', 'Text', 'TCombobox', 'Spinbox', 'TSpinbox'}


def _labelled_page(parent, text: str) -> tuple[ttk.Frame, ttk.Frame]:
    """A page with an explanatory label on top; returns (page, body)."""
    page = ttk.Frame(parent)
    ttk.Label(page, text=text, wraplength=900, justify='left').pack(
        anchor='nw', padx=8, pady=(8, 8))
    body = ttk.Frame(page)
    body.pack(fill=tk.BOTH, expand=True)
    return page, body


def open_path(path) -> str | None:
    """Open a file or folder with its default application; an error message
    or None."""
    try:
        if os.name == 'nt':
            os.startfile(str(path))   # noqa: S606 — a local path the user chose
        else:
            import subprocess
            subprocess.Popen(['xdg-open', str(path)])
    except OSError as exc:
        return str(exc)
    return None


def fmt_bytes(n: float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'


_RAW_DIR_RE = re.compile(r'^\d{4}-\d{2}-\d{2}')


def newest_raw_experiment(raw_root: Path | None = None) -> Path | None:
    """The newest experiment folder in raw_data: date-named (YYYY-MM-DD_…),
    latest name first, calibration folders excluded."""
    root = Path(raw_root or new_analysis_dir.RAW_DATA_DIR)
    try:
        dirs = [d for d in root.iterdir() if d.is_dir() and _RAW_DIR_RE.match(d.name)
                and 'calibration' not in d.name.lower()]
    except OSError:
        return None
    return max(dirs, key=lambda d: d.name) if dirs else None


class Tooltip:
    """Hover text for a widget."""

    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 500):
        self._widget, self._text, self._delay = widget, text, delay_ms
        self._job = None
        self._tip = None
        widget.bind('<Enter>', self._schedule, add='+')
        widget.bind('<Leave>', self._hide, add='+')
        widget.bind('<ButtonPress>', self._hide, add='+')

    def _schedule(self, _event=None):
        self._hide()
        self._job = self._widget.after(self._delay, self._show)

    def _show(self):
        self._job = None
        if not self._widget.winfo_exists():
            return
        x = self._widget.winfo_rootx() + 10
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._tip = tk.Toplevel(self._widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f'+{x}+{y}')
        tk.Label(self._tip, text=self._text, background='#ffffe0', relief='solid',
                 borderwidth=1, padx=6, pady=2).pack()

    def _hide(self, _event=None):
        if self._job is not None:
            self._widget.after_cancel(self._job)
            self._job = None
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


def render_checklist(frame: ttk.Frame, status: dict, on_open=None):
    """The stage checklist (icon, stage, detail, optional Open button) into
    `frame`, replacing what was there. `on_open(stage)` adds the buttons."""
    for child in frame.winfo_children():
        child.destroy()
    for j, h in enumerate(('', 'Step', 'Status', '')):
        ttk.Label(frame, text=h, foreground='#555').grid(row=0, column=j, sticky='w',
                                                          padx=(0, 12))
    for i, key in enumerate(es.STAGE_TITLES, start=1):
        st = status.get(key)
        if st is None:
            continue
        icon, color = _ICONS.get(st.state, ('', '#000'))
        tk.Label(frame, text=icon, foreground=color,
                 font=('TkDefaultFont', 11, 'bold')).grid(row=i, column=0, padx=(0, 6))
        ttk.Label(frame, text=st.title).grid(row=i, column=1, sticky='w', padx=(0, 12))
        ttk.Label(frame, text=st.detail, foreground='#374151').grid(
            row=i, column=2, sticky='w', padx=(0, 12))
        if on_open is not None:
            button = on_open(key)
            if button is not None:
                ttk.Button(frame, text='Open', command=button).grid(
                    row=i, column=3, sticky='w', pady=1)


# ---------------------------------------------------------------------------
# Wizard shell
# ---------------------------------------------------------------------------

class Wizard:
    """
    Step-wizard shell: a resizable step sidebar on the left, a title and a
    content area holding one page at a time, and a Previous/Next bar.

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
        on_leave:         optional callable() run when navigating away from
                          the step.
        primary:          optional callable() for Ctrl+Enter on this step
                          (only once its page is built).
        refresh:          optional callable() for F5 (after refresh_status).

    `status_provider()` -> {stage: StageStatus} (or {}) feeds the sidebar;
    refresh_status() re-reads it. set_busy() locks navigation (sidebar,
    Previous / Next, shortcuts) while something must not be interrupted.
    """

    SIDEBAR_WIDTH = 230

    def __init__(self, root: tk.Tk, steps: list, *, status_provider=None):
        self._root = root
        self._steps = steps
        self._pages: dict[int, ttk.Frame] = {}
        self._completed: dict[int, bool] = {}
        self._index = 0
        self._status_provider = status_provider
        self._busy: str | None = None
        self.status: dict = {}

        body = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)
        self._body = body

        side = tk.Frame(body, background='#f3f4f6', width=self.SIDEBAR_WIDTH)
        body.add(side, weight=0)
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
            if i < 9:
                Tooltip(row, f'Ctrl+{i + 1}')
            self._side_rows.append((row, icon, label))
        side.bind('<Configure>', lambda e: [lbl.configure(wraplength=max(e.width - 50, 60))
                                            for _r, _i, lbl in self._side_rows])

        main = ttk.Frame(body)
        body.add(main, weight=1)
        self._title_var = tk.StringVar()
        ttk.Label(main, textvariable=self._title_var,
                  font=('TkDefaultFont', 13, 'bold')).pack(anchor='w', padx=12, pady=(12, 4))
        self._content = ttk.Frame(main)
        self._content.pack(fill=tk.BOTH, expand=True, padx=12)

        # Sidebar width: remembered, applied once the pane has a size.
        self._sash_placed = False
        body.bind('<Configure>', self._place_sash, add='+')
        body.bind('<ButtonRelease-1>', self._save_sash, add='+')

        nav = ttk.Frame(root)
        nav.pack(fill=tk.X, padx=12, pady=(6, 12))
        self._prev_btn = ttk.Button(nav, text='← Previous', command=self._go_prev)
        self._prev_btn.pack(side=tk.LEFT)
        Tooltip(self._prev_btn, 'Alt+←')
        self._hint_var = tk.StringVar()
        ttk.Label(nav, textvariable=self._hint_var, foreground='#a00').pack(
            side=tk.LEFT, expand=True)
        self._next_btn = ttk.Button(nav, text='Next →', command=self._go_next)
        self._next_btn.pack(side=tk.RIGHT)
        Tooltip(self._next_btn, 'Alt+→')
        ttk.Label(nav, text=_SHORTCUTS_HINT, foreground='#888').pack(side=tk.RIGHT, padx=12)

        self._bind_shortcuts()
        self._show(0)

    # -- layout ---------------------------------------------------------------

    def _place_sash(self, event):
        if self._sash_placed or event.width < 300:
            return
        self._sash_placed = True
        saved = user_state.get('wizard_sidebar_width')
        width = saved if isinstance(saved, int) and 150 <= saved <= 500 else self.SIDEBAR_WIDTH
        self._body.sashpos(0, width)

    def _save_sash(self, _event=None):
        try:
            width = self._body.sashpos(0)
        except tk.TclError:
            return
        if 100 < width < 800:
            user_state.update(wizard_sidebar_width=int(width))

    # -- shortcuts ------------------------------------------------------------

    def _bind_shortcuts(self):
        root = self._root
        root.bind_all('<Alt-Right>', lambda e: self._shortcut(e, self._go_next), add='+')
        root.bind_all('<Alt-Left>', lambda e: self._shortcut(e, self._go_prev), add='+')
        root.bind_all('<F5>', lambda e: self._shortcut(e, self.refresh_current), add='+')
        root.bind_all('<Control-Return>',
                      lambda e: self._shortcut(e, self.primary_action, in_text=True), add='+')
        for i in range(min(len(self._steps), 9)):
            root.bind_all(f'<Control-Key-{i + 1}>',
                          lambda e, idx=i: self._shortcut(e, lambda: self.goto(idx)), add='+')

    def _shortcut(self, event, action, *, in_text: bool = False):
        """Run a shortcut for this window only (not its dialogs), not while
        busy, and — unless `in_text` — not while typing in a text field."""
        try:
            widget = event.widget
            if not isinstance(widget, tk.Misc) or widget.winfo_toplevel() is not self._root:
                return None
            if not in_text and widget.winfo_class() in _TEXT_INPUT_CLASSES:
                return None
        except (tk.TclError, KeyError, AttributeError):
            return None
        if self._busy:
            return 'break'
        action()
        return 'break'

    def primary_action(self):
        """The current step's main action (Ctrl+Enter)."""
        primary = self._steps[self._index].get('primary')
        if primary is None or self._index not in self._pages:
            return
        focus = self._root.focus_get()
        if focus is not None and focus.winfo_class() in ('Entry', 'TEntry'):
            # Commit an in-place cell edit (the compile table's editor
            # commits on Return) before acting on the table.
            focus.event_generate('<Return>')
            self._root.after_idle(primary)
        else:
            primary()

    def refresh_current(self):
        """F5: re-read the on-disk status, and the step's own refresh."""
        self.refresh_status()
        refresh = self._steps[self._index].get('refresh')
        if refresh is not None and self._index in self._pages:
            refresh()

    # -- busy lock ------------------------------------------------------------

    def set_busy(self, reason: str | None):
        """Lock (reason text) or unlock (None) navigation."""
        self._busy = reason
        for row, icon, label in self._side_rows:
            for w in (row, icon, label):
                w.configure(cursor='watch' if reason else 'hand2')
        self._update_nav()

    @property
    def busy(self) -> str | None:
        return self._busy

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

    def is_complete(self, index: int) -> bool:
        return self._completed.get(index, False)

    def refresh_nav(self):
        """Re-evaluate the current step's `next_check` (call after something
        it depends on changes, e.g. a shared directory field)."""
        self._update_nav()

    # -- navigation -----------------------------------------------------

    @property
    def index(self) -> int:
        return self._index

    def page(self, index: int):
        return self._pages.get(index)

    def prebuild(self, index: int):
        """Build a step's page now (hidden), e.g. so it starts loading data."""
        if index not in self._pages:
            self._pages[index] = self._steps[index]['builder'](self._content)

    def goto(self, index: int):
        """Jump to any step (the sidebar); a step's own gates only govern Next."""
        if self._busy:
            return
        if 0 <= index < len(self._steps) and index != self._index:
            self._show(index)

    def _show(self, index: int):
        if index != self._index:
            on_leave = self._steps[self._index].get('on_leave')
            if on_leave is not None and self._index in self._pages:
                try:
                    on_leave()
                except Exception as exc:
                    print(f'  [warn] leaving step {self._index + 1}: {exc}')
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
        if self._busy:
            self._prev_btn.config(state=tk.DISABLED)
            self._next_btn.config(state=tk.DISABLED)
            self._hint_var.set(self._busy)
            return
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
        if self._busy:
            return
        if self._index > 0:
            self._show(self._index - 1)

    def go_next(self):
        """Advance as the Next button would (no-op if this step isn't ready)."""
        self._go_next()

    def _go_next(self):
        if self._busy or not self._step_ready(self._index):
            return   # guard directly, don't rely solely on the button's disabled state
        if self._index < len(self._steps) - 1:
            self._show(self._index + 1)


# ---------------------------------------------------------------------------
# Overview page
# ---------------------------------------------------------------------------

class OverviewPage(ttk.Frame):
    """Experiment directory (+ recent / newest pickers), on-disk progress
    checklist, sample-name check, Continue and the analysis log."""

    def __init__(self, parent, *, superdir_var: tk.StringVar, wizard_ref, stage_steps: dict,
                 summary_step: int | None = None):
        super().__init__(parent)
        self._superdir = superdir_var
        self._wiz = wizard_ref           # callable() -> Wizard
        self._stage_steps = stage_steps  # stage key -> step index
        self._summary_step = summary_step
        self._target = None
        self._names_key = None

        form = ttk.Frame(self)
        form.pack(fill=tk.X, padx=8, pady=(8, 4))
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text='Experiment directory:').grid(row=0, column=0, sticky='w')
        ttk.Entry(form, textvariable=superdir_var).grid(row=0, column=1, sticky='ew', padx=6)
        ttk.Button(form, text='Browse…', command=self._browse).grid(row=0, column=2)
        self._recent_btn = ttk.Menubutton(form, text='Recent ▾')
        self._recent_menu = tk.Menu(self._recent_btn, tearoff=False,
                                    postcommand=self._fill_recent_menu)
        self._recent_btn.configure(menu=self._recent_menu)
        self._recent_btn.grid(row=0, column=3, padx=(6, 0))
        newest = ttk.Button(form, text='Newest in raw_data', command=self._pick_newest)
        newest.grid(row=0, column=4, padx=(6, 0))
        Tooltip(newest, f'The latest date-named folder in {new_analysis_dir.RAW_DATA_DIR}')

        self._table = ttk.Frame(self)
        self._table.pack(fill=tk.X, padx=8, pady=(12, 4))

        buttons = ttk.Frame(self)
        buttons.pack(fill=tk.X, padx=8, pady=8)
        self._continue_btn = ttk.Button(buttons, text='Continue where I left off →',
                                        command=self.continue_)
        self._continue_btn.pack(side=tk.LEFT)
        Tooltip(self._continue_btn, 'Ctrl+Enter')
        ttk.Button(buttons, text='Refresh status', command=self.refresh).pack(
            side=tk.LEFT, padx=(8, 0))
        self._continue_note = ttk.Label(buttons, text='', foreground='#555')
        self._continue_note.pack(side=tk.LEFT, padx=12)
        self._log_btn = ttk.Button(buttons, text='Open analysis log', command=self._open_log)
        self._log_btn.pack(side=tk.RIGHT)

        # Sample-name check.
        names = ttk.LabelFrame(self, text='Sample names')
        names.pack(fill=tk.X, padx=8, pady=(8, 4))
        self._names_label = tk.Label(names, text='', anchor='w', justify='left', wraplength=900)
        self._names_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6, pady=4)
        self._names_btn = ttk.Button(names, text='Review names…', command=self._review_names)
        self._names_btn.pack(side=tk.RIGHT, padx=6, pady=4)
        names.bind('<Configure>', lambda e: self._names_label.configure(
            wraplength=max(e.width - 160, 200)))

        ttk.Label(self, text=_MASS_NOTE, foreground='#555', wraplength=900,
                  justify='left').pack(anchor='w', padx=8, pady=(8, 0))

    # -- directory pickers ------------------------------------------------------

    def _browse(self):
        chosen = filedialog.askdirectory(title='Select experiment directory',
                                         initialdir=self._superdir.get() or None)
        if chosen:
            self._superdir.set(chosen)

    def _fill_recent_menu(self):
        menu = self._recent_menu
        menu.delete(0, tk.END)
        recent = user_state.recent_experiments()
        if not recent:
            menu.add_command(label='(no recent experiments yet)', state=tk.DISABLED)
            return
        for path in recent:
            menu.add_command(label=f'{Path(path).name}    —    {Path(path).parent}',
                             command=lambda p=path: self._superdir.set(p))

    def _pick_newest(self):
        newest = newest_raw_experiment()
        if newest is None:
            messagebox.showinfo('Newest experiment',
                                f'No date-named experiment folders found in '
                                f'{new_analysis_dir.RAW_DATA_DIR}.', parent=self)
            return
        self._superdir.set(str(newest))

    def _superdir_path(self) -> Path | None:
        text = self._superdir.get().strip()
        return Path(text) if text and Path(text).is_dir() else None

    # -- status -------------------------------------------------------------------

    def refresh(self):
        wiz = self._wiz()
        if wiz is not None:
            wiz.refresh_status()
        self._names_key = None    # re-check the names too
        self.render(wiz.status if wiz is not None else {})

    def render(self, status: dict):
        sd = self._superdir_path()
        self._log_btn.state(['!disabled'] if sd is not None and analysis_log.log_path(sd).is_file()
                            else ['disabled'])
        if sd is None:
            for child in self._table.winfo_children():
                child.destroy()
            ttk.Label(self._table, text='Choose an experiment directory to see its progress.',
                      foreground='#555').grid(row=0, column=0, sticky='w')
            self._continue_btn.state(['disabled'])
            self._continue_note.configure(text='')
            self._render_names(None)
            return
        wiz = self._wiz

        def opener(key):
            if key not in self._stage_steps:
                return None
            return lambda k=key: wiz().goto(self._stage_steps[k])

        render_checklist(self._table, status, opener)
        target = es.first_unfinished(status)
        self._target = target
        if target is None:
            if self._summary_step is not None:
                self._continue_btn.configure(text='Go to the summary →')
                self._continue_btn.state(['!disabled'])
            else:
                self._continue_btn.state(['disabled'])
            self._continue_note.configure(text='Every step is done for this experiment.')
        else:
            self._continue_btn.configure(text='Continue where I left off →')
            self._continue_btn.state(['!disabled'])
            self._continue_note.configure(text=f'→ {es.STAGE_TITLES[target]}')
        self._render_names(sd)

    def continue_(self):
        target = self._target
        if target in self._stage_steps:
            self._wiz().goto(self._stage_steps[target])
        elif target is None and self._summary_step is not None and self._superdir_path():
            self._wiz().goto(self._summary_step)

    # -- sample names ----------------------------------------------------------------

    def _sample_names(self, sd: Path) -> list[str]:
        try:
            return [d.name for d in es.sample_dirs(sd)]
        except OSError:
            return []

    def _render_names(self, sd: Path | None):
        if sd is None:
            self._names_label.configure(text='—', foreground='#555')
            self._names_btn.state(['disabled'])
            return
        names = self._sample_names(sd)
        key = (str(sd), tuple(names))
        if key == self._names_key:
            return
        self._names_key = key
        if not names:
            self._names_label.configure(text='No sample folders found yet.', foreground='#555')
            self._names_btn.state(['disabled'])
            return
        self._names_btn.state(['!disabled'])
        try:
            check = sample_name_parser.check_names(names)
        except Exception as exc:
            self._names_label.configure(text=f'Could not check the names: {exc}',
                                        foreground='#a00')
            return
        self._names_label.configure(text=name_check_text(check),
                                    foreground='#14532d' if check.ok and not check.value_typos
                                    else '#92400e')

    def _review_names(self):
        sd = self._superdir_path()
        if sd is None:
            return
        names = self._sample_names(sd)
        if not names:
            return

        def closed():
            if self.winfo_exists():
                self._names_key = None
                self._render_names(self._superdir_path())

        auto_metadata_dialog.AutoMetadataDialog(self, names, on_close=closed)

    def _open_log(self):
        sd = self._superdir_path()
        if sd is not None and analysis_log.log_path(sd).is_file():
            error = open_path(analysis_log.log_path(sd))
            if error:
                messagebox.showerror('Analysis log', error, parent=self)


def name_check_text(check) -> str:
    """One readable paragraph for a sample_name_parser.NameCheck."""
    cols = ', '.join(check.columns) if check.columns else 'no columns'
    if check.ok and not check.value_typos:
        return f'✓ {check.n_names} names parsed — {len(check.columns)} column(s): {cols}.'
    lines = []
    if check.leftovers:
        shown = ', '.join(f'{t} ({n})' for t, n in list(check.leftovers.items())[:8])
        more = '…' if len(check.leftovers) > 8 else ''
        lines.append(f'◐ {len(check.leftovers)} unrecognised token(s) in '
                     f'{len(check.samples_with_leftovers)} of {check.n_names} names: '
                     f'{shown}{more}')
    else:
        lines.append(f'✓ {check.n_names} names parsed — {len(check.columns)} column(s): {cols}.')
    typos = [f'"{t}" (≈ {w})' for t, w in check.typos.items()]
    typos += [f'{col} "{a}" ≈ "{b}"' for col, a, b in check.value_typos]
    if typos:
        lines.append('Possible typos: ' + '; '.join(typos)
                     + ' — rename the folder before analysing if so.')
    if check.leftovers:
        lines.append('Review names… to turn tokens into columns or ignore them (saved to your rules).')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Summary page
# ---------------------------------------------------------------------------

class SummaryPage(ttk.Frame):
    """What the analysis produced, where, and what to do next."""

    def __init__(self, parent, *, superdir_var: tk.StringVar, status_ref, qc_rows_ref,
                 on_close):
        super().__init__(parent)
        self._superdir = superdir_var
        self._status_ref = status_ref    # callable() -> status dict
        self._qc_rows_ref = qc_rows_ref  # callable() -> [QCRow] | None
        self._prompt = ''

        ttk.Label(self, text='Outputs', font=('TkDefaultFont', 11, 'bold')).pack(
            anchor='w', padx=8, pady=(8, 2))
        self._outputs = ttk.Frame(self)
        self._outputs.pack(fill=tk.X, padx=8)

        ttk.Label(self, text='Plot it', font=('TkDefaultFont', 11, 'bold')).pack(
            anchor='w', padx=8, pady=(14, 2))
        ttk.Label(self, text='Paste this into Claude Code (the biophys-plotting plugin builds '
                             'the standard figure grid from the compiled workbook):',
                  foreground='#555').pack(anchor='w', padx=8)
        prompt_row = ttk.Frame(self)
        prompt_row.pack(fill=tk.X, padx=8, pady=(2, 0))
        self._prompt_entry = ttk.Entry(prompt_row, state='readonly')
        self._prompt_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._copy_btn = ttk.Button(prompt_row, text='Copy', command=self.copy_prompt)
        self._copy_btn.pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(self, text='Final status', font=('TkDefaultFont', 11, 'bold')).pack(
            anchor='w', padx=8, pady=(14, 2))
        self._checklist = ttk.Frame(self)
        self._checklist.pack(fill=tk.X, padx=8)
        self._qc_label = tk.Label(self, text='', anchor='w', justify='left', wraplength=900)
        self._qc_label.pack(fill=tk.X, padx=8, pady=(10, 0))

        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, padx=8, pady=12)
        ttk.Button(bottom, text='Close the wizard', command=on_close).pack(side=tk.RIGHT)

    def _row(self, r: int, label: str, text: str, path: Path | None, *, ok: bool = True):
        ttk.Label(self._outputs, text=label, foreground='#555').grid(
            row=r, column=0, sticky='w', padx=(0, 12), pady=1)
        ttk.Label(self._outputs, text=text, foreground='#111' if ok else '#a00').grid(
            row=r, column=1, sticky='w', padx=(0, 12))
        if path is not None and path.exists():
            ttk.Button(self._outputs, text='Open', command=lambda p=path: self._open(p)).grid(
                row=r, column=2, sticky='w', pady=1)

    def _open(self, path: Path):
        error = open_path(path)
        if error:
            messagebox.showerror('Open', error, parent=self)

    def refresh(self):
        for child in self._outputs.winfo_children():
            child.destroy()
        text = self._superdir.get().strip()
        sd = Path(text) if text and Path(text).is_dir() else None
        if sd is None:
            self._row(0, 'Experiment', 'Choose an experiment directory first.', None, ok=False)
            self._set_prompt('')
            return

        compiled = es.latest_compiled(sd)
        data_root = new_analysis_dir.DATA_ANALYSIS_DIR / sd.name
        code_dir = new_analysis_dir.BIOPHYS_CODE_DIR / sd.name
        log = analysis_log.log_path(sd)
        r = 0
        self._row(r, 'Experiment', str(sd), sd)
        r += 1
        if compiled is None:
            self._row(r, 'Compiled data', 'not compiled yet', None, ok=False)
            r += 1
        else:
            self._row(r, 'Compiled data', compiled.name, compiled)
            r += 1
            xlsx = compiled / 'experiment_data.xlsx'
            self._row(r, 'Workbook', _file_text(xlsx), xlsx if xlsx.is_file() else None,
                      ok=xlsx.is_file())
            r += 1
            self._row(r, 'Crop cache', _crop_cache_text(compiled), None)
            r += 1
        self._row(r, 'Analysis folder', str(data_root) + ('' if data_root.is_dir()
                                                           else '  (not created)'),
                  data_root, ok=data_root.is_dir())
        r += 1
        self._row(r, 'Code folder', str(code_dir) + ('' if code_dir.is_dir()
                                                     else '  (not created)'),
                  code_dir, ok=code_dir.is_dir())
        r += 1
        self._row(r, 'Analysis log', log.name if log.is_file() else 'none yet',
                  log if log.is_file() else None)

        copied = data_root / 'data' / 'experiment_data.xlsx'
        target = copied if copied.is_file() else (
            compiled / 'experiment_data.xlsx' if compiled is not None else None)
        self._set_prompt(f'Plot my experiment: {target}' if target is not None else '')

        render_checklist(self._checklist, self._status_ref() or {})
        rows = self._qc_rows_ref()
        if rows is None:
            self._qc_label.configure(text='Sample QC was not computed this session '
                                          '(step 5).', foreground='#555')
        else:
            flagged = [r for r in rows if r.flags]
            if not flagged:
                self._qc_label.configure(text=f'✓ Sample QC: nothing flagged '
                                              f'({len(rows)} samples).', foreground='#14532d')
            else:
                lines = [f'{r.sample}: {"; ".join(r.flags)}' for r in flagged[:12]]
                more = f'\n… and {len(flagged) - 12} more' if len(flagged) > 12 else ''
                self._qc_label.configure(
                    text=f'Sample QC flags still standing ({len(flagged)} of {len(rows)}):\n'
                         + '\n'.join(lines) + more, foreground='#92400e')

    def _set_prompt(self, text: str):
        self._prompt = text
        self._prompt_entry.configure(state='normal')
        self._prompt_entry.delete(0, tk.END)
        self._prompt_entry.insert(0, text or '(compile the experiment first)')
        self._prompt_entry.configure(state='readonly')
        self._copy_btn.state(['!disabled'] if text else ['disabled'])

    def copy_prompt(self):
        if not self._prompt:
            return
        self.clipboard_clear()
        self.clipboard_append(self._prompt)
        self._copy_btn.configure(text='Copied ✓')
        self.after(1500, lambda: self._copy_btn.winfo_exists()
                   and self._copy_btn.configure(text='Copy'))


def _file_text(path: Path) -> str:
    try:
        return f'{path.name}  ({fmt_bytes(path.stat().st_size)})'
    except OSError:
        return f'{path.name}  (missing)'


def _crop_cache_text(compiled: Path) -> str:
    pt = compiled / 'concat_vqvae_cache.pt'
    meta = compiled / 'concat_vqvae_cache_metadata.parquet'
    if not pt.is_file():
        return 'none (no crop caches in this experiment, or not written)'
    text = f'{pt.name}  ({fmt_bytes(pt.stat().st_size)})'
    try:
        import pyarrow.parquet as pq
        text += f', {pq.ParquetFile(str(meta)).metadata.num_rows:,} crops'
    except Exception:
        pass
    return text


# ---------------------------------------------------------------------------
# Window geometry
# ---------------------------------------------------------------------------

_GEOMETRY_RE = re.compile(r'^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$')


def restore_window(root: tk.Tk):
    """Apply the remembered size / position / maximized state, unless the
    saved position is no longer on any monitor."""
    saved = user_state.get('window') or {}
    if not isinstance(saved, dict):
        return
    m = _GEOMETRY_RE.match(str(saved.get('geometry', '')))
    if m:
        w, h, x, y = map(int, m.groups())
        # The title bar's middle must land on a monitor, or the window
        # could open where it can't be seen (or dragged back).
        if desktop_notify.on_screen(x + min(w // 2, 200), y + 10, root):
            root.geometry(f'{w}x{h}+{x}+{y}')
        else:
            root.geometry(f'{w}x{h}')
    if saved.get('maximized'):
        try:
            root.state('zoomed')
        except tk.TclError:
            pass


class WindowTracker:
    """Keeps the last non-maximized geometry, to save on close."""

    def __init__(self, root: tk.Tk):
        self._root = root
        self.normal_geometry = None
        root.bind('<Configure>', self._on_configure, add='+')

    def _on_configure(self, event):
        if event.widget is self._root:
            try:
                if self._root.state() == 'normal':
                    self.normal_geometry = self._root.geometry()
            except tk.TclError:
                pass

    def save(self):
        try:
            maximized = self._root.state() == 'zoomed'
            geometry = self.normal_geometry or (None if maximized else self._root.geometry())
        except tk.TclError:
            return
        window = {'maximized': maximized}
        if geometry:
            window['geometry'] = geometry
        user_state.update(window=window)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    root = tk.Tk()
    root.title('Biophysics Analysis Wizard')
    root.minsize(1200, 760)
    restore_window(root)
    tracker = WindowTracker(root)

    # Shared across steps 1 (overview), 2 (baseline density), 4 (gating) and 7
    # (new analysis dir): their directory fields are bound to this same
    # StringVar, so typing in any updates the others live, and step 7 shows
    # it as its raw data dir. The compile page (step 6) only reads its
    # current value once, as an initial seed, since compilation may
    # reasonably target a different directory afterward. Starts at the last
    # experiment used.
    last = user_state.get('last_experiment', '')
    experiment_superdir = tk.StringVar(
        value=last if isinstance(last, str) and last and Path(last).is_dir() else '')

    # Density categories, shared by step 2 (assigning) and step 3 (review /
    # copy to SMRFXMAnalysis' batch window).
    categories = density_categories.CategoryModel()
    batch_window = density_categories.BatchWindowLauncher()
    records_cache = sample_qc.RecordsCache()
    pages: dict = {}           # page objects step builders hand back for later calls
    auto_opened = [False]      # the batch window opens by itself only once
    wiz_holder: list = []
    logged: dict = {}          # what the analysis log already recorded this session

    STAGE_STEPS = {'baseline': 1, 'imaging': 2, 'gating': 3, 'compile': 5, 'new_dir': 6}
    SUMMARY_STEP = 7

    def wiz_():
        return wiz_holder[0] if wiz_holder else None

    def _superdir_path() -> Path | None:
        text = experiment_superdir.get().strip()
        return Path(text) if text and Path(text).is_dir() else None

    def _status():
        sd = _superdir_path()
        return es.scan(sd) if sd is not None else {}

    def _has_superdir():
        return _superdir_path() is not None

    def _log(title, summary='', details=(), superdir=None):
        analysis_log.append(superdir or _superdir_path(), title, summary, details)

    def _qc_rows():
        return pages['qc'].loaded_rows() if 'qc' in pages else None

    # -- step 1: overview ------------------------------------------------

    def build_overview(parent):
        pages['overview'] = OverviewPage(
            parent, superdir_var=experiment_superdir, wizard_ref=wiz_,
            stage_steps=STAGE_STEPS, summary_step=SUMMARY_STEP)
        return pages['overview']

    def _on_show_overview():
        # Runs once inside Wizard() itself (step 1 is shown first), before
        # the wizard is in the holder.
        pages['overview'].render(wiz_().status if wiz_() else _status())

    # -- step 2: baseline density & categories ---------------------------

    def _baseline_computed(info):
        mode = ('per-sample reference frequency' if info['per_sample']
                else f'reference frequency {info["rfreq"]:g} Hz')
        details = [f'wrote {Path(info["out_path"]).name} ({info["n_samples"]} samples)',
                   f'slope {info["slope"]:.6g}, intercept {info["intercept"]:.6g}']
        if info['missing_rfreq']:
            details.append('no reference frequency for: ' + ', '.join(info['missing_rfreq']))
        _log('Baseline density',
             f'calibration {Path(info["calib_json"]).name} ({info["calib_source"]}), {mode}',
             details, superdir=info['superdir'])

    def build_baseline_density(parent):
        page, body = _labelled_page(parent, _BASELINE_DENSITY_TEXT)
        panel = calculate_baseline_density.build_embedded_page(
            body, superdir_var=experiment_superdir,
            per_sample_rfreq=bool(user_state.get('per_sample_rfreq', True)),
            category_model=categories, on_computed=_baseline_computed)
        panel.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        pages['baseline'] = panel
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

    def _categories_signature():
        out = []
        for c in categories.ordered_categories():
            members = tuple(categories.members(c.id))
            if not members:
                continue
            mean = categories.stats(c.id).mean
            out.append((c.name, c.skip, None if mean is None else round(mean, 4), members))
        return tuple(out)

    def _on_categories_changed():
        # The first state seen for an experiment (loaded from disk) is the
        # baseline; only later changes are logged, when leaving step 2.
        key = density_categories._norm(categories.superdir) if categories.superdir else None
        if key is not None and logged.get('categories_dir') != key:
            logged['categories_dir'] = key
            logged['categories_sig'] = _categories_signature()

    def _leave_baseline():
        if categories.superdir is None:
            return
        sig = _categories_signature()
        if sig == logged.get('categories_sig'):
            return
        logged['categories_sig'] = sig
        details = []
        for name, skip, mean, members in sig:
            head = f'{name} — {len(members)} sample(s)' if skip else \
                f'{name} — {mean:.4f} g/mL — {len(members)} sample(s)' if mean is not None else \
                f'{name} — no mean density — {len(members)} sample(s)'
            details.append(f'{head}: {", ".join(members)}')
        n_cats = sum(1 for _n, skip, _m, _s in sig if not skip)
        unassigned = categories.unassigned()
        summary = f'{n_cats} categor{"y" if n_cats == 1 else "ies"} to analyse'
        if unassigned:
            summary += f'; {len(unassigned)} sample(s) not yet in a category'
        _log('Density categories', summary, details, superdir=categories.superdir)

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

    def _category_analysed(cat):
        stats = categories.stats(cat.id)
        members = categories.members(cat.id)
        _log('Imaging analysed',
             f'{cat.name} at {stats.mean:.4f} g/mL ({len(members)} sample(s))'
             if stats.mean is not None else f'{cat.name} ({len(members)} sample(s))',
             [', '.join(members)], superdir=categories.superdir)

    def build_review(parent):
        page, body = _labelled_page(parent, _REVIEW_TEXT)
        pages['review'] = density_categories.ReviewPage(
            body, categories, on_open_batch=open_batch_window,
            on_status_change=lambda: wiz_() and wiz_().refresh_status(),
            launcher=batch_window, on_category_analysed=_category_analysed)
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

    def _gating_finalized():
        wiz.mark_complete(3)
        wiz.refresh_status()
        st = wiz.status.get('gating')
        _log('Gating finalized', st.detail if st is not None else 'gate files written')
        logged['gates_kept'] = density_categories._norm(_superdir_path() or '')

    def build_gating(parent):
        page = gate_experiments_inplace.build_embedded_page(
            parent, superdir_var=experiment_superdir,
            on_finalized=_gating_finalized,
            on_discover=lambda: wiz.mark_incomplete(3))
        pages['gating'] = page
        return page

    def _gates_on_disk():
        st = wiz.status.get('gating')
        return st is not None and st.state == es.DONE

    def _leave_gating():
        # Moving past gating without finalizing keeps the gate files on disk;
        # record that once per experiment per session.
        sd = _superdir_path()
        key = density_categories._norm(sd) if sd is not None else None
        if (key is not None and not wiz.is_complete(3) and _gates_on_disk()
                and logged.get('gates_kept') != key):
            logged['gates_kept'] = key
            _log('Gating', 'kept the existing gate files', [wiz.status['gating'].detail])

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

    def _compile_logged(info):
        sources = info['column_sources']
        cols = [f'{c} (from names: {sources[c]})' if c in sources else c
                for c in info['custom_cols']]
        details = [f'annotation columns: {", ".join(cols) if cols else "none"}']
        if info['coulter_pairs']:
            details.append(f'{info["coulter_pairs"]} sample(s) paired with a Coulter column')
        if not info['completed']:
            details.append('annotation window closed without Done — no annotations')
        out_dir = Path(info['out_dir'])
        _log('Compiled', f'{out_dir.name} ({info["n_samples"]} samples)', details,
             superdir=out_dir.parent)
        rows = _qc_rows()
        if rows is not None:
            flagged = [r for r in rows if r.flags]
            _log('Sample QC at compile',
                 f'{len(flagged)} of {len(rows)} samples flagged' if flagged
                 else f'nothing flagged ({len(rows)} samples)',
                 [f'{r.sample}: {"; ".join(r.flags)}' for r in flagged],
                 superdir=out_dir.parent)

    def _compile_busy(busy):
        wiz.set_busy('Compiling — the wizard unlocks when the write finishes.' if busy else None)

    def build_compile(parent):
        page = compile_experiment.build_embedded_page(
            parent, initial_superdir=experiment_superdir.get(),
            on_done=_compiled, auto_detect_names=True,
            records_provider=records_cache.get,
            on_busy=_compile_busy, on_compiled=_compile_logged)
        pages['compile'] = page
        return page

    # -- step 7: new analysis dir -------------------------------------------

    def _dir_created(raw_dir, code_dir, data_dir):
        _log('New analysis dir', f'created {data_dir}', [f'code: {code_dir}'],
             superdir=raw_dir)

    def _dir_copied(data_dir, jobs):
        sd = _superdir_path()
        details = [f'{Path(src).name} → {Path(dst)}' for src, dst in jobs]
        _log('Copied into the analysis dir', str(data_dir), details, superdir=sd)
        # The log travels with the data (copied after its own entry).
        if sd is not None and analysis_log.log_path(sd).is_file():
            try:
                shutil.copy2(analysis_log.log_path(sd), Path(data_dir) / analysis_log.LOG_FILENAME)
            except OSError as exc:
                print(f'  [warn] could not copy the analysis log: {exc}')
        wiz.refresh_status()
        wiz.goto(SUMMARY_STEP)

    def build_new_analysis_dir(parent):
        page, body = _labelled_page(parent, _NEW_ANALYSIS_DIR_TEXT)
        panel = new_analysis_dir.build_embedded_page(
            body, raw_dir_var=experiment_superdir, on_close=_close_now,
            on_created=_dir_created, on_copied=_dir_copied)
        panel.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        pages['new_dir'] = panel
        return page

    # -- step 8: summary -------------------------------------------------------

    def build_summary(parent):
        pages['summary'] = SummaryPage(
            parent, superdir_var=experiment_superdir, status_ref=lambda: wiz.status,
            qc_rows_ref=_qc_rows, on_close=_request_close)
        return pages['summary']

    # -- closing -----------------------------------------------------------------

    def _close_now():
        tracker.save()
        root.destroy()

    def _request_close():
        if 'compile' in pages and pages['compile'].is_busy():
            if not messagebox.askyesno(
                    'Compile in progress',
                    'The compiled workbook / crop cache is still being written; quitting now '
                    'leaves an incomplete compiled folder.\n\nQuit anyway?', parent=root):
                return
        if 'new_dir' in pages:
            # Cancels (and waits out) a running copy before calling _close_now.
            tracker.save()
            pages['new_dir'].on_close()
        else:
            _close_now()

    steps = [
        {'title': 'Overview', 'builder': build_overview, 'on_show': _on_show_overview,
         'next_check': _has_superdir,
         'next_hint': 'Choose an experiment directory to continue.',
         'primary': lambda: pages['overview'].continue_(),
         'refresh': lambda: pages['overview'].refresh()},
        {'title': 'Baseline Density & Categories', 'builder': build_baseline_density,
         'stage': 'baseline', 'next_check': _baseline_ready, 'next_hint': _baseline_hint,
         'on_leave': _leave_baseline, 'primary': lambda: pages['baseline'].run()},
        {'title': 'Imaging Analysis by Category', 'builder': build_review, 'stage': 'imaging',
         'on_show': _on_show_review, 'next_check': _review_ready, 'next_hint': _review_hint,
         'primary': lambda: pages['review'].refresh_status(),
         'refresh': lambda: pages['review'].refresh_status()},
        {'title': 'Gating: Buoyant Mass + Volume', 'builder': build_gating, 'stage': 'gating',
         'requires_finalize': True, 'finalize_satisfied': _gates_on_disk,
         'satisfied_hint': 'Every sample already has a gate file — Next keeps them; '
                           'Finalize to replace them.',
         'on_leave': _leave_gating, 'primary': lambda: pages['gating'].primary_action()},
        {'title': 'Sample QC', 'builder': build_qc,
         'on_show': lambda: pages['qc'].refresh(),
         'primary': lambda: pages['qc'].refresh(force=True),
         'refresh': lambda: pages['qc'].refresh()},
        {'title': 'Compile Experiment', 'builder': build_compile, 'stage': 'compile',
         'primary': lambda: pages['compile'].primary_action()},
        {'title': 'New Analysis Dir', 'builder': build_new_analysis_dir, 'stage': 'new_dir',
         'primary': lambda: pages['new_dir'].primary_action()},
        {'title': 'Summary', 'builder': build_summary,
         'on_show': lambda: pages['summary'].refresh(),
         'primary': lambda: pages['summary'].copy_prompt(),
         'refresh': lambda: pages['summary'].refresh()},
    ]

    wiz = Wizard(root, steps, status_provider=_status)
    wiz_holder.append(wiz)
    root.protocol('WM_DELETE_WINDOW', _request_close)

    # When the directory changes (typing, Browse, another page): remember it,
    # re-read the status, and build the baseline page in the background so
    # its saved result and categories load (step 3 needs them even if step 2
    # is skipped via the sidebar). Debounced so typing doesn't rescan per key.
    pending = [None]

    def _on_dir_changed():
        pending[0] = None
        sd = _superdir_path()
        if sd is not None:
            user_state.add_recent_experiment(sd)
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
    categories.add_listener(_on_categories_changed)
    if _has_superdir():
        root.after_idle(_on_dir_changed)
    root.mainloop()


if __name__ == '__main__':
    main()
