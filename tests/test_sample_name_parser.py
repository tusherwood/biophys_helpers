"""sample_name_parser: tokenising and the default rules on real sample-name conventions."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sample_name_parser as snp  # noqa: E402


def _parse(names, rules=None):
    r = snp.parse_names(names, rules if rules is not None else snp.DEFAULT_RULES)
    return {c.name: c for c in r.columns}, r


class DefaultRulesTests(unittest.TestCase):
    def test_full_name(self):
        cols, r = _parse(['3p84h_starving_300mOsmL_4uM-wnk463_rep2'])
        row = {k: c.values['3p84h_starving_300mOsmL_4uM-wnk463_rep2'] for k, c in cols.items()}
        self.assertEqual(row, {'time_h': '3.84', 'state': 'starving', 'osmolarity_mOsmL': '300',
                               'drug': 'wnk463', 'dose_uM': '4', 'rep': '2'})
        self.assertEqual(r.leftover_counts(), {})

    def test_fields_are_not_positional_and_dmso_is_dose_zero(self):
        names = ['2p38h_prolif_300mOsmL-RPMI+150mM-sucrose_dmso_rep1', '2p70h_300mOsmL_dmso_rep2']
        cols, _ = _parse(names)
        self.assertEqual(cols['state'].values, {names[0]: 'proliferating', names[1]: ''})
        self.assertEqual(cols['osmolyte'].values[names[0]], 'RPMI+150mM-sucrose')
        self.assertEqual(cols['drug'].values[names[1]], 'dmso')
        self.assertEqual(cols['dose_uM'].values[names[1]], '0')

    def test_units_and_dashed_drug_names(self):
        cols, _ = _parse(['500nM-wnk-in-11', '1p5mM-x', '72h'])
        self.assertEqual(cols['drug'].values['500nM-wnk-in-11'], 'wnk-in-11')
        self.assertEqual(cols['dose_uM'].values['500nM-wnk-in-11'], '0.5')
        self.assertEqual(cols['dose_uM'].values['1p5mM-x'], '1500')
        self.assertEqual(cols['time_h'].values['72h'], '72')

    def test_flags_and_leftovers(self):
        names = ['16p59h_after-act_+l-glut_+pyruvate', '16p81h_after-act_-l-glut_-pyruvate']
        cols, r = _parse(names)
        self.assertEqual(cols['l-glut'].kind, snp.CHECKBOX)
        self.assertEqual(cols['pyruvate'].values, {names[0]: 'yes', names[1]: 'no'})
        self.assertEqual(r.leftover_counts(), {'after-act': 2})

    def test_second_drug_goes_to_suffixed_columns(self):
        cols, _ = _parse(['1uM-a_2uM-b'])
        self.assertEqual(cols['drug_2'].values['1uM-a_2uM-b'], 'b')
        self.assertEqual(cols['dose_uM_2'].values['1uM-a_2uM-b'], '2')

    def test_dash_fallback_matches_pieces(self):
        cols, r = _parse(['starving-tm1'])
        self.assertEqual(cols['state'].values['starving-tm1'], 'starving')
        self.assertEqual(r.leftovers['starving-tm1'], ['tm1'])


class RuleEditTests(unittest.TestCase):
    def test_word_column_checkbox_and_ignore(self):
        names = ['2p67h_starving_dmso_tm1_rep1', '2p99h_starving_dmso_tru_rep3', '3p00h_dmso_short']
        rules = snp.add_word_column(snp.DEFAULT_RULES, 'operator', ['tm1', 'tru'])
        rules = snp.add_word_column(rules, 'short_exposure', ['short'], checkbox=True)
        cols, r = _parse(names, rules)
        self.assertEqual(cols['operator'].values, {names[0]: 'tm1', names[1]: 'tru', names[2]: ''})
        self.assertEqual(cols['short_exposure'].values,
                         {names[0]: 'no', names[1]: 'no', names[2]: 'yes'})
        self.assertEqual(r.leftover_counts(), {})
        _, r = _parse(['a_tm9'], snp.add_ignored(snp.DEFAULT_RULES, ['tm9']))
        self.assertEqual(r.leftover_counts(), {'a': 1})
        self.assertNotIn('operator', json.dumps(snp.DEFAULT_RULES))   # defaults untouched

    def test_save_load_merges_over_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rules.json'
            self.assertEqual(snp.load_rules(path), snp.DEFAULT_RULES)   # no file
            path.write_text(json.dumps({'builtin': {'time': {'column': 'time_hours'}}}))
            rules = snp.load_rules(path)
            self.assertEqual(rules['builtin']['time'], {'enabled': True, 'column': 'time_hours'})
            self.assertEqual(rules['words'], snp.DEFAULT_RULES['words'])
            snp.save_rules(snp.add_ignored(rules, ['x']), path)
            self.assertEqual(snp.load_rules(path)['ignore'], ['x'])


if __name__ == '__main__':
    unittest.main()
