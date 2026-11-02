"""Pilot tests require only the Python standard library."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from core import investigator as inv

FINDING = dict(rule_id='CHK-CFG-007', severity='high', object_id='EmpEmployment.assignmentIdExternal',
               source_value='100', target_value='32', affected_field='max_length',
               reason='Target maximum length is smaller.', recommended_correction='Profile lengths before changing target.',
               detection_method='deterministic_rule')


class InvestigatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'tasks.sqlite3'
        self.payload = {'quality_analysis': {'findings': [copy.deepcopy(FINDING)]}}

    def add(self):
        return inv.ingest(self.db, 'synthetic:A-to-B', self.payload)

    def test_deduplicates_across_restarts(self):
        self.assertEqual(self.add(), self.add())
        self.assertEqual(len(inv.tasks(self.db)), 1)

    def test_scope_and_changed_evidence_create_new_tasks(self):
        self.add()
        inv.ingest(self.db, 'synthetic:B-to-A', self.payload)
        self.payload['quality_analysis']['findings'][0]['target_value'] = '16'
        self.add()
        self.assertEqual(len(inv.tasks(self.db)), 3)

    def test_validation_is_atomic(self):
        self.payload['quality_analysis']['findings'].append({})
        with self.assertRaises(ValueError):
            self.add()
        self.assertEqual(inv.tasks(self.db), [])

    def test_rejects_non_deterministic_evidence(self):
        self.payload['quality_analysis']['findings'][0]['detection_method'] = 'ai'
        with self.assertRaises(ValueError):
            self.add()

    def test_offline_review_preserves_evidence(self):
        self.add()
        inv.run_one(self.db)
        task = inv.tasks(self.db)[0]
        result = json.loads(task['result'])
        self.assertEqual(task['state'], 'needs_review')
        self.assertFalse(result['applied'])
        self.assertEqual(result['evidence'], FINDING)
        self.assertIsNone(inv.run_one(self.db))

    def test_concurrent_workers_claim_once(self):
        self.add()
        with ThreadPoolExecutor(max_workers=4) as pool:
            claimed = list(pool.map(lambda _: inv.run_one(self.db), range(4)))
        self.assertEqual(sum(x is not None for x in claimed), 1)

    def test_invalid_ai_evidence_fails_without_overwriting_evidence(self):
        self.add()
        inv.run_one(self.db, lambda _: dict(hypothesis='x', proposed_fix='y',
                     verification_steps=['z'], evidence_ids=['invented']))
        task = inv.tasks(self.db)[0]
        self.assertEqual(task['state'], 'failed')
        self.assertEqual(json.loads(task['evidence']), FINDING)
        self.assertIsNone(task['result'])

    def test_valid_ai_result_is_still_unapproved(self):
        self.add()
        inv.run_one(self.db, lambda e: dict(hypothesis='x', proposed_fix='y',
                     verification_steps=['z'], evidence_ids=[e['rule_id']]))
        result = json.loads(inv.tasks(self.db)[0]['result'])
        self.assertEqual(result['mode'], 'ai_assisted')
        self.assertTrue(result['human_review_required'])
        self.assertFalse(result['applied'])

    def test_retry_limit_and_error_redaction(self):
        self.add()
        def fail(_):
            raise RuntimeError('secret-token-not-for-logs')
        for attempt in range(3):
            inv.run_one(self.db, fail)
            if attempt < 2:
                inv.retry(self.db, 1)
        with self.assertRaises(ValueError):
            inv.retry(self.db, 1)
        self.assertNotIn('secret-token', json.dumps(inv.tasks(self.db)))

    def test_interrupted_requires_explicit_recovery(self):
        self.add()
        inv.claim(self.db)
        with self.assertRaises(ValueError):
            inv.retry(self.db, 1)
        inv.retry(self.db, 1, recover=True)
        inv.run_one(self.db)
        self.assertEqual(inv.tasks(self.db)[0]['attempts'], 2)

    def test_export_contains_history(self):
        self.add()
        inv.run_one(self.db)
        target = Path(self.tmp.name) / 'review.json'
        inv.export(self.db, target)
        report = json.loads(target.read_text())
        self.assertEqual([e['state'] for e in report['events']], ['queued', 'running', 'needs_review'])

    def test_sdk_contract_with_mock(self):
        sdk = MagicMock()
        sdk.OpenAI.return_value.chat.completions.create.return_value.choices[0].message.content = '{"test":true}'
        with patch.dict('sys.modules', {'openai': sdk}), patch.dict('os.environ', {
                'OPENAI_API_KEY': 'fake-test-key', 'OPENAI_MODEL': 'configured-model'}):
            self.assertEqual(inv.ai_proposal(FINDING), {'test': True})
        sdk.OpenAI.assert_called_once_with(timeout=45, max_retries=0)


if __name__ == '__main__':
    unittest.main()
