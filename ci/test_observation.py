import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('pipeline', Path(__file__).with_name('pipeline.py'))
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


class ObservationTests(unittest.TestCase):
    def test_reports_reachable_endpoints_when_public_host_is_configured(self):
        target = {
            'environment_id': 'real-uat-test', 'config_revision': 1,
            'public_host': '172.16.28.130',
            'repositories': [{'repository_id': name, 'sha': 'a' * 40}
                             for name in sorted(pipeline.SERVICES)],
        }
        container = {
            'Image': 'sha256:test', 'State': {'Running': True},
            'NetworkSettings': {'Ports': {'80/tcp': [{'HostPort': '18080'}]}},
        }
        image = {'Config': {'Labels': {'org.opencontainers.image.revision': 'a' * 40}}}
        with patch.object(pipeline, 'assert_owned', return_value=container), \
             patch.object(pipeline, 'docker', return_value=json.dumps([image])), \
             patch.object(pipeline, 'wait_http') as health:
            actual = pipeline.observe(target, 'test-operation',
                                      {name: 'sha256:test' for name in pipeline.SERVICES})

        self.assertEqual(actual['endpoints'], {
            'vote': 'http://172.16.28.130:18080',
            'result': 'http://172.16.28.130:18080',
        })
        self.assertEqual([call.args[0] for call in health.call_args_list],
                         ['http://172.16.28.130:18080'] * 2)

    def test_rejects_host_injection_when_public_host_is_not_an_ip_address(self):
        target = {'public_host': 'example.com/path'}

        with self.assertRaises(ValueError):
            pipeline.observe(target, 'test-operation', {})
