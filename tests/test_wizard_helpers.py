"""Pure helpers behind the analysis wizard: per-user state, the analysis log,
the sample-name check, calibration auto-find and the newest-experiment pick."""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analysis_log  # noqa: E402
import sample_name_parser as snp  # noqa: E402
import user_state  # noqa: E402


class UserStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        patcher = mock.patch.object(user_state, 'STATE_PATH', self.tmp / 'state.json')
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_update_merges_and_survives_corruption(self):
        user_state.update(a=1)
        user_state.update(b=2)
        self.assertEqual(user_state.load(), {'a': 1, 'b': 2})
        (self.tmp / 'state.json').write_text('{not json')
        self.assertEqual(user_state.load(), {})
        self.assertEqual(user_state.get('a', 'default'), 'default')

    def test_recent_list_dedupes_caps_and_skips_missing(self):
        dirs = []
        for i in range(12):
            d = self.tmp / f'exp{i}'
            d.mkdir()
            dirs.append(d)
            user_state.add_recent_experiment(d)
        user_state.add_recent_experiment(str(dirs[3]).upper())   # same dir, other case
        recent = user_state.recent_experiments()
        self.assertEqual(len(recent), user_state.MAX_RECENT)
        self.assertEqual(recent[0].lower(), str(dirs[3]).lower())
        self.assertEqual(sum(1 for p in recent if p.lower() == str(dirs[3]).lower()), 1)
        self.assertEqual(user_state.get('last_experiment').lower(), str(dirs[3]).lower())
        dirs[11].rmdir()
        self.assertNotIn(str(dirs[11]), user_state.recent_experiments())


class AnalysisLogTests(unittest.TestCase):
    def test_header_session_and_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp) / '2026-01-01_exp'
            sd.mkdir()
            analysis_log._sessions.clear()
            t = datetime(2026, 1, 2, 9, 15)
            analysis_log.append(sd, 'Baseline density', 'calibration x.json', ['wrote a.csv', ''],
                                now=t)
            analysis_log.append(sd, 'Gating finalized', 'all\nsamples', now=t)
            text = analysis_log.log_path(sd).read_text(encoding='utf-8')
            self.assertTrue(text.startswith('# Analysis log — 2026-01-01_exp'))
            self.assertEqual(text.count('## Session 2026-01-02 09:15'), 1)
            self.assertIn('- **09:15 Baseline density** — calibration x.json\n  - wrote a.csv\n', text)
            self.assertIn('- **09:15 Gating finalized** — all samples\n', text)
            self.assertIsNone(analysis_log.append(Path(tmp) / 'missing', 'x'))


class NameCheckTests(unittest.TestCase):
    def test_leftovers_typos_and_value_typos(self):
        names = ['3h_prolif_rep1', '3h_prolfi_rep2', '3h_starving_4uM-wnk463_rep1',
                 '3h_starving_4uM-wnk436_rep2', '3h_starving_4uM-wnk463_rep3',
                 '3h_starving_300mOsmL-RPMI+150mM-sucrose_rep1',
                 '3h_starving_300mOsmL-RPMI+180mM-sucrose_rep1',
                 'd2373_x_rep1', 'd2374_x_rep1']
        check = snp.check_names(names, snp.DEFAULT_RULES)
        self.assertFalse(check.ok)
        self.assertEqual(check.typos, {'prolfi': 'prolif'})
        self.assertIn(('drug', 'wnk436', 'wnk463'), check.value_typos)
        # different numbers are different values, not typos
        self.assertFalse(any('150mM' in a or '150mM' in b for _c, a, b in check.value_typos))
        self.assertNotIn('d2374', check.typos)

    def test_clean_names(self):
        check = snp.check_names(['3h_starving_rep1', '6h_starving_rep2'], snp.DEFAULT_RULES)
        self.assertTrue(check.ok)
        self.assertEqual(check.columns, ['time_h', 'state', 'rep'])


class CalibrationAndNewestTests(unittest.TestCase):
    def test_find_experiment_calibration(self):
        import calculate_baseline_density as cbd
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            self.assertIsNone(cbd.find_experiment_calibration(sd))
            for name in ('20260819_density_baseline_calibration.json',
                         '20261104_density_baseline_calibration.json',
                         '._20261201_density_baseline_calibration.json',
                         '20261104_8um_mass_calibration.json'):
                (sd / name).write_text('{}')
            self.assertEqual(cbd.find_experiment_calibration(sd).name,
                             '20261104_density_baseline_calibration.json')

    def test_newest_raw_experiment(self):
        import analysis_wizard as aw
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('2026-11-04_calibrations', '2026-11-03_b', '2026-10-30_a', 'zzz_scratch'):
                (root / name).mkdir()
            (root / '2027-01-01_file.txt').write_text('')
            self.assertEqual(aw.newest_raw_experiment(root).name, '2026-11-03_b')


class CopySkipTests(unittest.TestCase):
    def test_copy_trees_leaves_out_skipped_files(self):
        import queue
        import threading
        import new_analysis_dir as nad
        with tempfile.TemporaryDirectory() as tmp:
            src, dest = Path(tmp) / 'x_compiled', Path(tmp) / 'out'
            src.mkdir()
            (src / 'experiment_data.xlsx').write_text('a')
            for n in nad.VQVAE_CACHE_FILES:
                (src / n).write_text('b')
            self.assertEqual(len(nad.vqvae_cache_files(src)), 2)
            msgs = queue.Queue()
            nad.copy_trees([(src, dest)], msgs, threading.Event(),
                           skip=nad.vqvae_cache_files(src))
            self.assertEqual(sorted(p.name for p in dest.iterdir()), ['experiment_data.xlsx'])


if __name__ == '__main__':
    unittest.main()
