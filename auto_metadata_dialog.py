"""
auto_metadata_dialog.py

"Auto-detect from names" review dialog for compile_experiment.py's annotation
table: shows the columns sample_name_parser proposes from the sample names
(include / rename / text-or-yes-no each), a preview of every sample's parsed
values, and the tokens no rule recognised, which can be turned into a new
column ("Make column…" — each token a value), a yes/no column ("Make yes/no
column…"), or ignored. Those three actions can be remembered in
~/.biophys_helpers/sample_name_rules.json so later experiments detect them
automatically; "Rules…" edits that file directly.

Without an `on_apply` (review_only — the analysis wizard's up-front name
check) there is no table to fill: the dialog only reviews the parse and
edits the saved rules (always remembered), and `on_close()` is called when
it closes.

Apply hands the included columns to `on_apply(columns, overwrite)`:
    columns    [{'name', 'kind' ('text' | 'checkbox'), 'source',
                 'values': {sample: str}}]
    overwrite  replace non-empty cells of same-named existing columns
               (otherwise only empty cells are filled)
"""
from __future__ import annotations

import json
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import sample_name_parser as snp

_KIND_LABELS = {'text': 'text', 'checkbox': 'yes / no'}
_LABEL_KINDS = {v: k for k, v in _KIND_LABELS.items()}
_RESERVED = {'sample', 'coulter_col'}


class AutoMetadataDialog(tk.Toplevel):

    def __init__(self, parent: tk.Widget, samples: list, *, existing_columns: dict | None = None,
                 on_apply=None, on_close=None):
        """`existing_columns`: {name: 'text' | 'checkbox'} already in the table.
        No `on_apply`: review-only (see the module docstring)."""
        super().__init__(parent)
        self._review_only = on_apply is None
        self._on_close = on_close
        self.title('Sample names — what auto-detect will find' if self._review_only
                   else 'Auto-detect metadata from sample names')
        self.transient(parent.winfo_toplevel())
        self.minsize(900, 560)
        self._samples = list(samples)
        self._existing = dict(existing_columns or {})
        self._on_apply = on_apply
        self._rules = snp.load_rules()
        self._col_state: dict = {}     # proposed name -> {'include','name','kind'} vars
        self._result: snp.ParseResult | None = None

        self._build()
        self._reparse()
        self.protocol('WM_DELETE_WINDOW', self.destroy)
        self.bind('<Escape>', lambda _e: self.destroy())

    def destroy(self):
        callback, self._on_close = self._on_close, None
        super().destroy()
        if callback is not None:
            callback()

    # -- layout ---------------------------------------------------------------

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=10, pady=(10, 4))
        self._header = ttk.Label(top, text='', font=('TkDefaultFont', 10, 'bold'))
        self._header.pack(side=tk.LEFT)
        ttk.Button(top, text='Rules…', command=self._edit_rules).pack(side=tk.RIGHT)

        panes = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        left = ttk.Frame(panes)
        right = ttk.Frame(panes)
        panes.add(left, weight=4)
        panes.add(right, weight=1)

        ttk.Label(left, text='Proposed columns (untick to leave out; names and types are '
                             'editable):').pack(anchor='w')
        self._cols_frame = ttk.Frame(left)
        self._cols_frame.pack(fill=tk.X, pady=(2, 8))

        ttk.Label(left, text='Preview (rows with unrecognised tokens are highlighted):').pack(
            anchor='w')
        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill=tk.BOTH, expand=True)
        self._tree = ttk.Treeview(tree_frame, show='headings', height=14, selectmode='none')
        vsb = ttk.Scrollbar(tree_frame, orient='vertical', command=self._tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient='horizontal', command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self._tree.tag_configure('leftover', background='#fff3c4')
        self._tree.tag_configure('plain', background='#ffffff')

        ttk.Label(right, text='Unrecognised tokens', font=('TkDefaultFont', 10, 'bold')).pack(
            anchor='w', padx=(8, 0))
        ttk.Label(right, text='(token — number of samples)', foreground='#555').pack(
            anchor='w', padx=(8, 0))
        lb_frame = ttk.Frame(right)
        lb_frame.pack(fill=tk.BOTH, expand=True, padx=(8, 0), pady=4)
        self._leftovers = tk.Listbox(lb_frame, selectmode=tk.EXTENDED, exportselection=False)
        lsb = ttk.Scrollbar(lb_frame, orient='vertical', command=self._leftovers.yview)
        self._leftovers.configure(yscrollcommand=lsb.set)
        self._leftovers.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._leftover_tokens: list = []
        for text, cmd in (('Make column…', self._make_column),
                          ('Make yes/no column…', self._make_checkbox_column),
                          ('Ignore', self._ignore_tokens)):
            ttk.Button(right, text=text, command=cmd).pack(fill=tk.X, padx=(8, 0), pady=1)
        self._remember = tk.BooleanVar(master=self, value=True)
        if self._review_only:
            # Nothing to apply to: rule edits only matter if they're saved.
            ttk.Label(right, text='Changes are saved to your rules for every experiment.',
                      foreground='#555', wraplength=200, justify='left').pack(
                anchor='w', padx=(8, 0), pady=(4, 0))
        else:
            ttk.Checkbutton(right, text='Remember for future experiments',
                            variable=self._remember).pack(anchor='w', padx=(8, 0), pady=(4, 0))

        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, padx=10, pady=(4, 10))
        self._overwrite = tk.BooleanVar(master=self, value=False)
        if self._review_only:
            ttk.Label(bottom, text='The columns are filled in at the compile step '
                                   '("Auto-detect from names").', foreground='#555').pack(
                side=tk.LEFT)
            ttk.Button(bottom, text='Close', command=self.destroy).pack(side=tk.RIGHT)
            return
        ttk.Checkbutton(bottom, text='Overwrite non-empty cells of existing columns with the '
                                     'same name', variable=self._overwrite).pack(side=tk.LEFT)
        ttk.Button(bottom, text='Cancel', command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(bottom, text='Apply', command=self._apply).pack(side=tk.RIGHT, padx=(0, 6))

    # -- parsing / display ------------------------------------------------------

    def _reparse(self):
        self._result = snp.parse_names(self._samples, self._rules)
        for col in self._result.columns:
            if col.name not in self._col_state:
                kind = snp.CHECKBOX if col.kind == snp.CHECKBOX else 'text'
                if col.name in self._existing:
                    kind = self._existing[col.name]
                state = {'include': tk.BooleanVar(master=self, value=True),
                         'name': tk.StringVar(master=self, value=col.name),
                         'kind': tk.StringVar(master=self, value=_KIND_LABELS[kind])}
                for var in state.values():
                    var.trace_add('write', lambda *_a: self._refresh_preview())
                self._col_state[col.name] = state
        self._build_column_rows()
        self._refresh_leftovers()
        self._refresh_preview()

    def _build_column_rows(self):
        for child in self._cols_frame.winfo_children():
            child.destroy()
        cols = self._result.columns
        n = len(self._samples)
        self._header.configure(text=f'{len(cols)} column(s) detected from {n} sample name(s)')
        if not cols:
            ttk.Label(self._cols_frame, text='No rule recognised anything in these names — '
                                             'build columns from the tokens on the right.',
                      foreground='#555').grid(row=0, column=0, sticky='w')
            return
        for j, h in enumerate(('', 'Column', 'Type', 'Filled', 'From')):
            ttk.Label(self._cols_frame, text=h, foreground='#555').grid(
                row=0, column=j, sticky='w', padx=(0, 8))
        for i, col in enumerate(cols, start=1):
            st = self._col_state[col.name]
            ttk.Checkbutton(self._cols_frame, variable=st['include']).grid(row=i, column=0)
            ttk.Entry(self._cols_frame, textvariable=st['name'], width=22).grid(
                row=i, column=1, sticky='w', padx=(0, 8))
            ttk.Combobox(self._cols_frame, textvariable=st['kind'], state='readonly', width=8,
                         values=list(_KIND_LABELS.values())).grid(row=i, column=2, padx=(0, 8))
            ttk.Label(self._cols_frame, text=f'{col.n_filled}/{n}').grid(
                row=i, column=3, sticky='w', padx=(0, 8))
            note = col.source
            target = st['name'].get().strip()
            if target in self._existing:
                note += '  → fills existing column'
            ttk.Label(self._cols_frame, text=note, foreground='#555').grid(
                row=i, column=4, sticky='w')

    def _included(self) -> list:
        out = []
        for col in self._result.columns:
            st = self._col_state[col.name]
            if st['include'].get():
                out.append((col, st['name'].get().strip(), _LABEL_KINDS[st['kind'].get()]))
        return out

    def _refresh_preview(self):
        if self._result is None:
            return
        included = self._included()
        cols = ['sample'] + [f'c{i}' for i in range(len(included))] + ['unrecognised']
        self._tree.delete(*self._tree.get_children())
        self._tree['columns'] = cols
        self._tree.heading('sample', text='Sample')
        self._tree.column('sample', width=260, stretch=False)
        for i, (_col, name, kind) in enumerate(included):
            self._tree.heading(f'c{i}', text=('☑ ' if kind == 'checkbox' else '') + (name or '?'))
            self._tree.column(f'c{i}', width=110, stretch=False)
        self._tree.heading('unrecognised', text='Unrecognised')
        self._tree.column('unrecognised', width=160, stretch=True)
        for s in self._samples:
            left = self._result.leftovers.get(s, [])
            values = [s] + [_display(col.values.get(s, ''), kind) for col, _n, kind in included]
            values.append(', '.join(left))
            self._tree.insert('', tk.END, values=values, tags=('leftover' if left else 'plain',))

    def _refresh_leftovers(self):
        counts = self._result.leftover_counts()
        self._leftover_tokens = list(counts)
        self._leftovers.delete(0, tk.END)
        for tok, n in counts.items():
            self._leftovers.insert(tk.END, f'{tok}  —  {n}')

    # -- leftover actions ---------------------------------------------------------

    def _selected_tokens(self) -> list:
        tokens = [self._leftover_tokens[i] for i in self._leftovers.curselection()]
        if not tokens:
            messagebox.showinfo('Unrecognised tokens', 'Select one or more tokens first.',
                                parent=self)
        return tokens

    def _change_rules(self, edit):
        """Apply `edit(rules) -> rules` to this session, and to the saved rules
        when "Remember" is ticked."""
        self._rules = edit(self._rules)
        if self._remember.get():
            try:
                snp.save_rules(edit(snp.load_rules()))
            except OSError as exc:
                messagebox.showwarning('Rules', f'Could not save the rules:\n{exc}', parent=self)
        self._reparse()

    def _make_column(self):
        tokens = self._selected_tokens()
        if not tokens:
            return
        name = simpledialog.askstring(
            'Make column', f"Column for {', '.join(tokens)}\n(each token becomes a value):",
            parent=self)
        if name and name.strip():
            self._change_rules(lambda r: snp.add_word_column(r, name, tokens))

    def _make_checkbox_column(self):
        tokens = self._selected_tokens()
        if not tokens:
            return
        name = simpledialog.askstring(
            'Make yes/no column',
            f"Yes/no column — 'yes' where {' or '.join(tokens)} appears, 'no' elsewhere:",
            initialvalue=tokens[0] if len(tokens) == 1 else '', parent=self)
        if name and name.strip():
            self._change_rules(lambda r: snp.add_word_column(r, name, tokens, checkbox=True))

    def _ignore_tokens(self):
        tokens = self._selected_tokens()
        if tokens:
            self._change_rules(lambda r: snp.add_ignored(r, tokens))

    # -- rules editor ---------------------------------------------------------------

    def _edit_rules(self):
        RulesEditor(self, on_saved=self._rules_saved)

    def _rules_saved(self, rules: dict):
        self._rules = rules
        self._reparse()

    # -- apply ------------------------------------------------------------------

    def _apply(self):
        included = self._included()
        if not included:
            messagebox.showinfo('Auto-detect', 'No columns are ticked.', parent=self)
            return
        names = [name for _c, name, _k in included]
        bad = [n for n in names if not n or n in _RESERVED]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if bad or dupes:
            messagebox.showerror('Auto-detect', 'Fix the column names first: '
                                 + ('empty or reserved names; ' if bad else '')
                                 + (f"duplicated: {', '.join(dupes)}" if dupes else ''),
                                 parent=self)
            return
        columns = [{'name': name, 'kind': kind, 'source': col.source,
                    'values': {s: _store(col.values.get(s, ''), kind) for s in self._samples}}
                   for col, name, kind in included]
        overwrite = self._overwrite.get()
        self.destroy()
        if self._on_apply is not None:
            self._on_apply(columns, overwrite)


def _display(value: str, kind: str) -> str:
    if kind == 'checkbox':
        return '✓' if value == 'yes' else ''
    return value


def _store(value: str, kind: str) -> str:
    """Table storage: checkbox columns hold 'yes' or ''."""
    if kind == 'checkbox':
        return 'yes' if value == 'yes' else ''
    return value


class RulesEditor(tk.Toplevel):
    """Edit ~/.biophys_helpers/sample_name_rules.json: the pattern rules'
    on/off and column names, plus the word lists and ignored tokens (JSON)."""

    def __init__(self, parent, *, on_saved):
        super().__init__(parent)
        self.title('Sample-name rules')
        self.transient(parent)
        self.grab_set()
        self._on_saved = on_saved
        rules = snp.load_rules()
        self._vars: dict = {}

        ttk.Label(self, text=f'Saved in {snp.RULES_PATH}', foreground='#555').pack(
            anchor='w', padx=10, pady=(10, 4))
        form = ttk.LabelFrame(self, text='Pattern rules')
        form.pack(fill=tk.X, padx=10, pady=4)
        labels = {'time': 'time  (2p38h → 2.38)', 'dose': 'dose  (4uM-wnk463)',
                  'osmolarity': 'osmolarity  (300mOsmL[-osmolyte])',
                  'flag': 'flag  (+name / -name → yes/no)', 'rep': 'replicate  (rep2)'}
        for i, (name, cfg) in enumerate(rules['builtin'].items()):
            en = tk.BooleanVar(master=self, value=bool(cfg.get('enabled', True)))
            ttk.Checkbutton(form, text=labels.get(name, name), variable=en).grid(
                row=i, column=0, sticky='w', padx=6, pady=2)
            fields = {}
            for j, key in enumerate(k for k in cfg if k != 'enabled'):
                ttk.Label(form, text=key.replace('_', ' ') + ':').grid(
                    row=i, column=1 + 2 * j, sticky='e', padx=(8, 2))
                var = tk.StringVar(master=self, value=str(cfg[key]))
                ttk.Entry(form, textvariable=var, width=18).grid(row=i, column=2 + 2 * j, sticky='w')
                fields[key] = var
            self._vars[name] = (en, fields)

        ttk.Label(self, text='Word lists and ignored tokens (JSON):').pack(
            anchor='w', padx=10, pady=(8, 0))
        self._text = tk.Text(self, width=80, height=16, font=('Consolas', 9))
        self._text.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        self._text.insert('1.0', json.dumps({'words': rules['words'], 'ignore': rules['ignore']},
                                            indent=2))

        buttons = ttk.Frame(self)
        buttons.pack(fill=tk.X, padx=10, pady=(4, 10))
        ttk.Button(buttons, text='Reset to defaults', command=self._reset).pack(side=tk.LEFT)
        ttk.Button(buttons, text='Cancel', command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text='Save', command=self._save).pack(side=tk.RIGHT, padx=(0, 6))

    def _reset(self):
        if messagebox.askyesno('Sample-name rules',
                               'Replace your saved rules with the defaults?', parent=self):
            self._finish(json.loads(json.dumps(snp.DEFAULT_RULES)))

    def _save(self):
        try:
            extra = json.loads(self._text.get('1.0', tk.END))
            words, ignore = extra.get('words', []), extra.get('ignore', [])
            if not isinstance(words, list) or not all(snp._valid_word_rule(w) for w in words):
                raise ValueError("'words' must be a list of {column, values} objects")
            if not isinstance(ignore, list):
                raise ValueError("'ignore' must be a list of tokens")
        except (ValueError, AttributeError) as exc:
            messagebox.showerror('Sample-name rules', f'Invalid JSON: {exc}', parent=self)
            return
        rules = snp.load_rules()
        for name, (en, fields) in self._vars.items():
            rules['builtin'][name]['enabled'] = en.get()
            for key, var in fields.items():
                if var.get().strip():
                    rules['builtin'][name][key] = var.get().strip()
        rules['words'], rules['ignore'] = words, [str(t) for t in ignore]
        self._finish(rules)

    def _finish(self, rules: dict):
        try:
            snp.save_rules(rules)
        except OSError as exc:
            messagebox.showerror('Sample-name rules', f'Could not save:\n{exc}', parent=self)
            return
        self.destroy()
        self._on_saved(snp.load_rules())
