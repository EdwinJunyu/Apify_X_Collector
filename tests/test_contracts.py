import json
import unittest
from pathlib import Path
from collector.config import load_config
from collector.validation import check, load_schema, ValidationError


ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def test_frozen_defaults(self):
        config = load_config(ROOT / 'config.example.json')
        self.assertFalse(config['schedule_enabled'])
        self.assertEqual(config['per_run_limit_usd'], '0.50')
        self.assertEqual(config['pilot_limit_usd'], '10.00')
        self.assertEqual(config['active_limit_bytes'], 50 * 1024 ** 3)
        self.assertEqual(config['min_free_bytes'], 10 * 1024 ** 3)
        self.assertEqual(config['schedule_local_time'], '09:00')
        self.assertEqual(config['mode'], 'OFFLINE')

    def test_synthetic_fixtures_match_contracts(self):
        fixture = json.loads((ROOT / 'fixtures/offline.json').read_bytes())
        check('account', fixture['account'])
        check('context', fixture['context'])
        for policy in fixture['policies']:
            check('policy', policy)

    def test_candidates_can_have_unknown_external_id(self):
        fixture = json.loads((ROOT / 'fixtures/offline.json').read_bytes())
        account = fixture['account']
        account.update({'external_user_id': None, 'verification_status': 'PENDING', 'source_policy_id': None})
        check('account', account)

    def test_no_schema_silently_allows_new_fields(self):
        for path in (ROOT / 'collector/schemas').glob('*.json'):
            schema = load_schema(path.stem)
            self.assertEqual(schema['$schema'], 'https://json-schema.org/draft/2020-12/schema')
            self.assertFalse(schema['additionalProperties'])


if __name__ == '__main__':
    unittest.main()
