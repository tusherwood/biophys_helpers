"""
user_state.py

Small per-user settings store shared by the analysis wizard and the tools it
embeds: one JSON object in ~/.biophys_helpers/wizard_state.json (the file
name predates the other users).

Keys currently written:
    last_experiment       the wizard's experiment directory
    recent_experiments    most-recent-first list of experiment directories
    window                {'geometry': 'WxH+X+Y', 'maximized': bool}
    wizard_sidebar_width  width (px) of the wizard's step sidebar
    baseline_sidebar_width  width (px) of the baseline page's category sidebar
    per_sample_rfreq      the baseline page's per-sample reference-frequency box
    last_calibration      the calibration JSON of the last successful baseline run
    notify_sound          imaging-step notification sound on/off

Every read tolerates a missing or corrupt file (-> defaults), and every write
is best-effort: settings must never break the tool using them.
"""
from __future__ import annotations

import json
from pathlib import Path

STATE_PATH = Path.home() / '.biophys_helpers' / 'wizard_state.json'
MAX_RECENT = 10


def load() -> dict:
    try:
        state = json.loads(Path(STATE_PATH).read_text(encoding='utf-8'))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def get(key: str, default=None):
    return load().get(key, default)


def update(**updates):
    """Merge `updates` into the saved state (re-read first, so separate
    writers don't clobber each other's keys)."""
    state = load()
    state.update(updates)
    try:
        path = Path(STATE_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding='utf-8')
    except OSError:
        pass


def _norm(path) -> str:
    return str(Path(path)).rstrip('\\/').lower()


def recent_experiments(existing_only: bool = True) -> list[str]:
    items = [str(p) for p in (get('recent_experiments') or []) if isinstance(p, str)]
    if existing_only:
        items = [p for p in items if Path(p).is_dir()]
    return items


def add_recent_experiment(path) -> list[str]:
    """Move `path` to the front of the recent list (deduplicated, capped)."""
    path = str(path)
    items = [p for p in recent_experiments(existing_only=False) if _norm(p) != _norm(path)]
    items = [path] + items
    items = items[:MAX_RECENT]
    update(recent_experiments=items, last_experiment=path)
    return items
