import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('pipeline', Path(__file__).with_name('pipeline.py'))
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root_patch = patch.object(pipeline, 'ROOT', Path(self.temp.name))
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.operation = 'original-production'
        self.target = {
            'action': 'SETTLE', 'environment_id': 'real-uat-prod',
            'config_revision': 9, 'settles_operation': self.operation,
            'settles_run': 'original-run', 'retain': True,
            'repositories': [{'repository_id': s, 'sha': 'a' * 40} for s in sorted(pipeline.SERVICES)],
        }
        self.actual = {
            'environment_id': 'real-uat-prod', 'environment_url': 'http://test:80',
            'repositories': self.target['repositories'],
            'services': [{'service_id': s, 'healthy': True, 'artifact_ref': 'sha256:' + 'b' * 64,
                          'repositories': [{'repository_id': s, 'sha': 'a' * 40}]}
                         for s in sorted(pipeline.SERVICES)],
        }
        self.saved = {
            'target': {**self.target, 'action': 'DEPLOY_PROD'},
            'observation': {'deployment': self.actual},
        }
        self.save()
        self.docker = Mock(side_effect=self.docker_result)
        self.docker_patch = patch.object(pipeline, 'docker', self.docker)
        self.docker_patch.start()
        self.addCleanup(self.docker_patch.stop)
        self.checkpoint_patch = patch.object(pipeline, 'checkpoint')
        self.checkpoint_patch.start()
        self.addCleanup(self.checkpoint_patch.stop)

    def save(self):
        (pipeline.ROOT / f'{self.operation}.json').write_text(json.dumps(self.saved))

    def docker_result(self, *args):
        if args[0] == 'ps':
            return 'owned-container'
        if args[:2] == ('network', 'ls'):
            return 'owned-network'
        if args[0] == 'inspect':
            return json.dumps([{'Config': {'Labels': {pipeline.LABEL: self.operation}}}])
        if args[:2] == ('network', 'inspect'):
            return json.dumps([{'Labels': {pipeline.LABEL: self.operation}, 'Containers': {}}])
        return ''

    def test_preserves_production_when_retention_is_explicit(self):
        with patch.object(pipeline, 'observe', return_value=self.actual):
            pipeline.settle(self.target)
        writes = [c for c in self.docker.call_args_list
                  if c.args[0] == 'rm' or c.args[:2] == ('network', 'rm')]
        self.assertEqual(writes, [])

    def test_reports_actual_artifacts_when_production_is_retained(self):
        with patch.object(pipeline, 'observe', return_value=self.actual):
            actual = pipeline.settle(self.target)
        self.assertEqual(actual['services'], [
            {**s, 'disposition': 'RETAINED', 'active': True, 'settled': True}
            for s in self.actual['services']
        ])

    def test_rejects_production_when_retention_is_missing_or_invalid(self):
        for flag in (None, False, 'true', 1):
            with self.subTest(flag=flag):
                target = {**self.target, 'retain': flag}
                with self.assertRaises(RuntimeError):
                    pipeline.settle(target)

    def test_rejects_retention_when_original_action_is_not_production(self):
        self.saved['target']['action'] = 'DEPLOY_CANDIDATE'
        self.save()
        with self.assertRaises(RuntimeError):
            pipeline.settle(self.target)

    def test_rejects_retention_when_fixed_revisions_differ(self):
        target = copy.deepcopy(self.target)
        target['repositories'][0]['sha'] = 'c' * 40
        with self.assertRaises(RuntimeError):
            pipeline.settle(target)

    def test_rejects_retention_when_live_health_check_fails(self):
        with patch.object(pipeline, 'observe', side_effect=RuntimeError('unhealthy')):
            with self.assertRaisesRegex(RuntimeError, 'unhealthy'):
                pipeline.settle(self.target)

    def test_rejects_retention_when_original_deployment_is_missing(self):
        (pipeline.ROOT / f'{self.operation}.json').unlink()
        with self.assertRaises(RuntimeError):
            pipeline.settle(self.target)

    def test_rejects_retention_when_saved_artifacts_are_incomplete(self):
        self.saved['observation']['deployment']['services'].pop()
        self.save()
        with self.assertRaises(RuntimeError):
            pipeline.settle(self.target)

    def test_observes_original_images_when_production_is_retained(self):
        with patch.object(pipeline, 'observe', return_value=self.actual) as observe:
            pipeline.settle(self.target)
        observe.assert_called_once_with(
            self.target, self.operation,
            {s: 'sha256:' + 'b' * 64 for s in sorted(pipeline.SERVICES)},
        )

    def test_removes_owned_test_resources_when_test_environment_is_settled(self):
        self.target.update(environment_id='real-uat-test', retain=False)
        self.saved['target'].update(environment_id='real-uat-test', action='DEPLOY_CANDIDATE')
        self.save()
        pipeline.settle(self.target)
        self.docker.assert_any_call('rm', '--force', '--volumes', 'owned-container')

    def test_rejects_test_retention_when_test_resources_should_be_removed(self):
        self.target['environment_id'] = 'real-uat-test'
        self.saved['target']['environment_id'] = 'real-uat-test'
        self.save()
        with self.assertRaises(RuntimeError):
            pipeline.settle(self.target)


if __name__ == '__main__':
    unittest.main()
