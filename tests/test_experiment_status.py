"""experiment_status: stage detection on a synthetic experiment dir, and the resume rule;
compile_experiment._annotations_from_workbook seeding."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import experiment_status as es  # noqa: E402


def _touch(path: Path, text: str = ''):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sup = Path(self._tmp.name) / '2026-01-01_exp'
        for s in ('s1_rep1', 's2_rep1'):
            _touch(self.sup / s / '20260101.120000_mass_results' / 'm.csv', 'mass_pg\n1\n')
        (self.sup / '20260101.130000_compiled_notes').mkdir(parents=True)   # not a sample

    def tearDown(self):
        self._tmp.cleanup()

    def test_samples_and_initial_state(self):
        self.assertEqual([d.name for d in es.sample_dirs(self.sup)], ['s1_rep1', 's2_rep1'])
        st = es.scan(self.sup)
        self.assertEqual(st['mass'].state, es.DONE)
        self.assertEqual(st['baseline'].state, es.TODO)
        self.assertEqual(st['gating'].state, es.TODO)
        self.assertEqual(st['compile'].state, es.TODO)
        self.assertEqual(es.first_unfinished(st), 'baseline')

    def test_baseline_categories_gating_and_stale_compile(self):
        _touch(self.sup / '20260101_140000_baseline_density.csv', 'sample\n')
        self.assertEqual(es.scan(self.sup)['baseline'].state, es.PARTIAL)
        _touch(self.sup / 'density_categories.json', json.dumps({'categories': [
            {'name': 'A', 'skip': False, 'samples': ['s1_rep1'], 'mean_density_g_per_mL': 1.0},
            {'name': 'Skip', 'skip': True, 'samples': ['s2_rep1'], 'mean_density_g_per_mL': None}]}))
        st = es.scan(self.sup)
        self.assertEqual(st['baseline'].state, es.DONE)
        # s1 has no image binaries -> nothing to analyse -> category done
        self.assertEqual(st['imaging'].state, es.DONE)

        _touch(self.sup / 's1_rep1' / '20260101_150000_gating' / 's1_rep1_gate.yaml', 'gates: {}\n')
        self.assertEqual(es.scan(self.sup)['gating'].state, es.PARTIAL)
        _touch(self.sup / 's2_rep1' / '20260101_150000_gating' / 's2_rep1_gate.yaml', 'gates: {}\n')
        self.assertEqual(es.scan(self.sup)['gating'].state, es.DONE)

        stamp = time.strftime('%Y%m%d.%H%M%S', time.localtime(time.time() + 60))
        (self.sup / f'{stamp}_compiled').mkdir()
        self.assertEqual(es.scan(self.sup)['compile'].state, es.DONE)
        gate = self.sup / 's1_rep1' / '20260101_150000_gating' / 's1_rep1_gate.yaml'
        later = time.time() + 3600
        os.utime(gate, (later, later))
        st = es.scan(self.sup)
        self.assertEqual(st['compile'].state, es.STALE)
        self.assertIn('gates', st['compile'].detail)

    def test_resume_goes_past_the_furthest_done_stage(self):
        S = es.StageStatus
        st = {'baseline': S('baseline', es.PARTIAL, ''), 'imaging': S('imaging', es.TODO, ''),
              'gating': S('gating', es.DONE, ''), 'compile': S('compile', es.STALE, ''),
              'new_dir': S('new_dir', es.TODO, '')}
        self.assertEqual(es.first_unfinished(st), 'compile')
        st['compile'] = S('compile', es.DONE, '')
        st['new_dir'] = S('new_dir', es.DONE, '')
        self.assertIsNone(es.first_unfinished(st))

    def test_fingerprint_changes_with_gate_files(self):
        a = es.records_fingerprint(self.sup)
        self.assertEqual(a, es.records_fingerprint(self.sup))
        _touch(self.sup / 's1_rep1' / '20260101_150000_gating' / 's1_rep1_gate.yaml', 'gates: {}\n')
        self.assertNotEqual(a, es.records_fingerprint(self.sup))


class AnnotationSeedTests(unittest.TestCase):
    def test_metadata_sheet_seeds_columns(self):
        import pandas as pd
        import compile_experiment as ce
        with tempfile.TemporaryDirectory() as tmp:
            xlsx = Path(tmp) / 'experiment_data.xlsx'
            pd.DataFrame({'sample_name': ['b', 'a'], 'sheet_name': ['b', 'a'],
                          'meta_time_h': [2.5, 3.0], 'meta_flag': ['yes', 'no'],
                          'meta_drug': ['dmso', None], 'coulter_column': ['', 'C1']}) \
                .to_excel(xlsx, sheet_name='metadata', index=False)
            state = ce._annotations_from_workbook(xlsx)
        self.assertEqual(state['custom_cols'], ['time_h', 'flag', 'drug'])
        self.assertEqual(state['checkbox_cols'], ['flag'])
        self.assertEqual(state['order'], ['b', 'a'])
        self.assertEqual(state['values']['b'], {'time_h': '2.5', 'flag': 'yes', 'drug': 'dmso'})
        self.assertEqual(state['values']['a'], {'time_h': '3', 'flag': '', 'drug': ''})
        self.assertEqual(state['coulter'], {'a': 'C1'})


if __name__ == '__main__':
    unittest.main()
