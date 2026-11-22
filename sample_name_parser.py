"""
sample_name_parser.py

Proposes per-sample metadata from sample directory names, e.g.

    3p84h_starving_300mOsmL_4uM-wnk463_rep2
      -> time_h=3.84, state=starving, osmolarity_mOsmL=300,
         drug=wnk463, dose_uM=4, rep=2

Names are split into tokens on '_' only: '-' usually joins the parts of one
field ('4uM-wnk463', 'wnk-in-11', '300mOsmL-RPMI+150mM-sucrose', '-l-glut'),
so it is kept inside the token. Each token is then recognised by its
*pattern*, wherever it sits in the name (fields are not positional: some
names omit a field, some add one). A token no rule recognises is retried
split on '-' (so 'deox-n2+1pctSMS' can still match piecewise); whatever is
still unrecognised is reported as a leftover for the user to turn into a
column, or ignore.

Rules (see DEFAULT_RULES; user edits live in ~/.biophys_helpers/
sample_name_rules.json, falling back to the defaults key by key):

    builtin  pattern rules with configurable column names / on-off:
        time        <int>p<frac>h | <n>h              -> time_h (hours; 'p' = '.')
        dose        <n><nM|uM|mM>-<name>              -> drug, dose_uM (in uM)
        osmolarity  <n>mOsmL[-<rest>]                 -> osmolarity_mOsmL, osmolyte
        flag        +<name> / -<name>                 -> yes/no column <name>
        rep         rep<n>                            -> rep
    words    token vocabularies: {column, values: {value: [synonyms]},
             also: {other column: value}, checkbox: bool}. A checkbox word
             rule writes 'yes' where a synonym appears, 'no' elsewhere.
             Defaults: state (prolif/proliferating, starve/starving/...) and
             the dmso vehicle (drug=dmso, dose_uM=0).
    ignore   tokens to drop silently.

A second match of the same rule in one name goes to suffixed columns
(drug_2 / dose_uM_2). Column names follow the plotting plugin's role
conventions (time_h -> time, dose_uM -> ordered gradient, yes/no -> boolean).

Pure Python, no GUI; auto_metadata_dialog.py is the review dialog that
compile_experiment.py's annotation table opens.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

RULES_PATH = Path.home() / '.biophys_helpers' / 'sample_name_rules.json'

TEXT, NUMBER, CHECKBOX = 'text', 'number', 'checkbox'

DEFAULT_RULES: dict = {
    'version': 1,
    'builtin': {
        'time':       {'enabled': True, 'column': 'time_h'},
        'dose':       {'enabled': True, 'drug_column': 'drug', 'dose_column': 'dose_uM'},
        'osmolarity': {'enabled': True, 'column': 'osmolarity_mOsmL',
                       'osmolyte_column': 'osmolyte'},
        'flag':       {'enabled': True},
        'rep':        {'enabled': True, 'column': 'rep'},
    },
    'words': [
        {'column': 'state',
         'values': {'proliferating': ['proliferating', 'prolif'],
                    'starving': ['starving', 'starve', 'starved', 'starvation']}},
        {'column': 'drug', 'values': {'dmso': ['dmso']}, 'also': {'dose_uM': 0}},
    ],
    'ignore': [],
}

_TIME_RE = re.compile(r'^(\d+)(?:p(\d+))?h$', re.IGNORECASE)
_DOSE_RE = re.compile(r'^(\d+(?:p\d+)?)(nM|uM|mM)-(.+)$', re.IGNORECASE)
_OSM_RE = re.compile(r'^(\d+(?:p\d+)?)mOsm(?:L|/L)?(?:-(.+))?$', re.IGNORECASE)
_FLAG_RE = re.compile(r'^([+-])([A-Za-z].*)$')
_REP_RE = re.compile(r'^rep(\d+)$', re.IGNORECASE)
_UNIT_TO_UM = {'nm': 1e-3, 'um': 1.0, 'mm': 1e3}


# ---------------------------------------------------------------------------
# Rules I/O
# ---------------------------------------------------------------------------

def load_rules(path: Path | None = None) -> dict:
    """The user's rules (default RULES_PATH) merged over DEFAULT_RULES
    (missing keys -> default)."""
    rules = copy.deepcopy(DEFAULT_RULES)
    try:
        user = json.loads(Path(path or RULES_PATH).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return rules
    if not isinstance(user, dict):
        return rules
    for name, cfg in (user.get('builtin') or {}).items():
        if name in rules['builtin'] and isinstance(cfg, dict):
            rules['builtin'][name].update(cfg)
    if isinstance(user.get('words'), list):
        rules['words'] = [w for w in user['words'] if _valid_word_rule(w)]
    if isinstance(user.get('ignore'), list):
        rules['ignore'] = [str(t) for t in user['ignore']]
    return rules


def save_rules(rules: dict, path: Path | None = None):
    path = Path(path or RULES_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rules, indent=2), encoding='utf-8')


def _valid_word_rule(w) -> bool:
    return (isinstance(w, dict) and isinstance(w.get('column'), str) and w['column'].strip()
            and isinstance(w.get('values'), dict))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@dataclass
class ProposedColumn:
    name: str
    kind: str                 # TEXT / NUMBER / CHECKBOX
    source: str               # which rule produced it, for display / provenance
    values: dict = field(default_factory=dict)   # sample -> str

    @property
    def n_filled(self) -> int:
        return sum(1 for v in self.values.values() if v != '')


@dataclass
class ParseResult:
    columns: list             # [ProposedColumn], first-seen order
    leftovers: dict           # sample -> [unrecognised tokens]
    recognised: dict          # sample -> [(token, column name)]

    def leftover_counts(self) -> dict:
        counts: dict = {}
        for toks in self.leftovers.values():
            for t in set(toks):
                counts[t] = counts.get(t, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower())))


def fmt_number(x: float) -> str:
    """Numbers as the table stores them: '%.10g' (2.38, 4, 0.5)."""
    return '%.10g' % x


def _num(text: str) -> float:
    return float(text.lower().replace('p', '.'))


class _Row:
    """One sample's parsed values, keeping columns' first-seen order globally."""

    def __init__(self, parser: 'NameParser', sample: str):
        self.parser, self.sample = parser, sample
        self.values: dict = {}
        self.recognised: list = []

    def slot(self, bases: list) -> str:
        """'' or '_2', '_3', ... : the first suffix free for all `bases`."""
        k = 1
        while True:
            suffix = '' if k == 1 else f'_{k}'
            if all(f'{b}{suffix}' not in self.values for b in bases):
                return suffix
            k += 1

    def put(self, column: str, value, kind: str, source: str, token: str):
        text = fmt_number(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)
        self.values[column] = text
        self.parser._declare(column, kind, source)
        self.recognised.append((token, column))


class NameParser:
    def __init__(self, rules: dict | None = None):
        self.rules = rules if rules is not None else load_rules()
        self._columns: dict = {}   # name -> ProposedColumn (insertion ordered)
        b = self.rules['builtin']
        self._b = b
        self._ignore = {t.lower() for t in self.rules.get('ignore', [])}
        # synonym -> (word rule, canonical value)
        self._words: dict = {}
        for rule in self.rules.get('words', []):
            for value, synonyms in rule['values'].items():
                for syn in [value, *synonyms]:
                    self._words.setdefault(str(syn).lower(), (rule, value))

    def _declare(self, column: str, kind: str, source: str):
        if column not in self._columns:
            self._columns[column] = ProposedColumn(column, kind, source)

    # -- one token ------------------------------------------------------------

    def _match(self, row: _Row, token: str) -> bool:
        """Apply the first rule recognising `token`; False if none does."""
        low = token.lower()
        if low in self._ignore:
            return True
        b = self._b

        if b['time']['enabled'] and (m := _TIME_RE.match(token)):
            value = float(m.group(1) + ('.' + m.group(2) if m.group(2) else ''))
            col = b['time']['column']
            row.put(col + row.slot([col]), value, NUMBER, 'time (<n>p<frac>h)', token)
            return True

        if b['dose']['enabled'] and (m := _DOSE_RE.match(token)):
            drug_col, dose_col = b['dose']['drug_column'], b['dose']['dose_column']
            suffix = row.slot([drug_col, dose_col])
            row.put(drug_col + suffix, m.group(3), TEXT, 'dose (<n>uM-<drug>)', token)
            row.put(dose_col + suffix, _num(m.group(1)) * _UNIT_TO_UM[m.group(2).lower()],
                    NUMBER, 'dose (<n>uM-<drug>)', token)
            return True

        if b['osmolarity']['enabled'] and (m := _OSM_RE.match(token)):
            col = b['osmolarity']['column']
            suffix = row.slot([col])
            row.put(col + suffix, _num(m.group(1)), NUMBER, 'osmolarity (<n>mOsmL)', token)
            if m.group(2):
                row.put(b['osmolarity']['osmolyte_column'] + suffix, m.group(2), TEXT,
                        'osmolarity (<n>mOsmL-<osmolyte>)', token)
            return True

        if b['rep']['enabled'] and (m := _REP_RE.match(token)):
            col = b['rep']['column']
            row.put(col + row.slot([col]), int(m.group(1)), NUMBER, 'replicate (rep<n>)', token)
            return True

        if low in self._words:
            rule, value = self._words[low]
            col = rule['column']
            if rule.get('checkbox'):
                row.put(col, 'yes', CHECKBOX, f'word list ({col})', token)
            else:
                also = rule.get('also') or {}
                suffix = row.slot([col, *also])
                row.put(col + suffix, value, TEXT, f'word list ({col})', token)
                for other, other_value in also.items():
                    kind = NUMBER if isinstance(other_value, (int, float)) else TEXT
                    row.put(other + suffix, other_value, kind, f'word list ({col})', token)
            return True

        if b['flag']['enabled'] and (m := _FLAG_RE.match(token)):
            row.put(m.group(2), 'yes' if m.group(1) == '+' else 'no', CHECKBOX,
                    'flag (+name / -name)', token)
            return True
        return False

    # -- names ----------------------------------------------------------------

    def parse(self, names: list) -> ParseResult:
        self._columns = {}
        rows, leftovers = {}, {}
        for name in names:
            row = _Row(self, name)
            left = []
            for token in (t for t in name.split('_') if t):
                if self._match(row, token):
                    continue
                parts = [p for p in token.split('-') if p]
                if len(parts) > 1 and not token.startswith(('+', '-')):
                    unmatched = [p for p in parts if not self._match(row, p)]
                    # Nothing matched piecewise: keep the token whole ('after-act').
                    left.extend([token] if len(unmatched) == len(parts) else unmatched)
                else:
                    left.append(token)
            rows[name], leftovers[name] = row, left

        columns = list(self._columns.values())
        for col in columns:
            for name in names:
                v = rows[name].values.get(col.name, '')
                if col.kind == CHECKBOX and v == '':
                    v = 'no'   # absent token: the flag is off
                col.values[name] = v
        return ParseResult(columns, leftovers, {n: rows[n].recognised for n in names})


def parse_names(names: list, rules: dict | None = None) -> ParseResult:
    return NameParser(rules).parse(names)


# ---------------------------------------------------------------------------
# Rule edits from the leftovers panel
# ---------------------------------------------------------------------------

def add_word_column(rules: dict, column: str, tokens: list, *, checkbox: bool = False) -> dict:
    """Rules with `tokens` becoming values of `column` (each token its own
    value), or — checkbox — a yes/no column that is 'yes' where any appears."""
    rules = copy.deepcopy(rules)
    column = column.strip()
    tokens = [t for t in tokens if t]
    existing = next((w for w in rules['words']
                     if w['column'] == column and bool(w.get('checkbox')) == checkbox), None)
    if existing is None:
        existing = {'column': column, 'values': {}}
        if checkbox:
            existing['checkbox'] = True
        rules['words'].append(existing)
    if checkbox:
        existing['values'].setdefault('yes', [])
        existing['values']['yes'] = sorted(set(existing['values']['yes']) | set(tokens))
    else:
        for t in tokens:
            existing['values'].setdefault(t, [t])
    return rules


def add_ignored(rules: dict, tokens: list) -> dict:
    rules = copy.deepcopy(rules)
    rules['ignore'] = sorted(set(rules.get('ignore', [])) | set(tokens), key=str.lower)
    return rules
