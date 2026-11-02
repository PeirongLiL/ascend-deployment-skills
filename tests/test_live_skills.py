"""Functional regression tests. HTTP fixture is a mock, never a real model benchmark."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1] / 'skills'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = load('live_benchmark', ROOT / 'ascend-llm-benchmark/scripts/benchmark.py')
diag = load('live_diagnostics', ROOT / 'ascend-server-diagnostics/scripts/diagnose.py')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/metrics':
            payload = b'vllm:prefix_cache_queries_total{model_name="fixture"} 100\nvllm:prefix_cache_hits_total{model_name="fixture"} 0\n'
            content_type = 'text/plain'
        else:
            payload = json.dumps({'data': [{'id': 'fixture'}]} if self.path == '/v1/models' else {}).encode()
            content_type = 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/tokenize':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'tokens': [1, 2, 3, 4, 5, 6, 7]}).encode())
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        chat = 'messages' in body
        delay = getattr(self.server, 'delay', .003)
        n = body['max_tokens'] + int(getattr(self.server, 'wrong_output', False))
        count_in = len(body['prompt']) if not chat else 16
        def send(payload):
            self.wfile.write(('data: ' + json.dumps(payload) + '\n\n').encode())
            self.wfile.flush()
        try:
            if chat:
                send({'choices': [{'delta': {'reasoning_content': 'reason'}, 'finish_reason': None}]})
                time.sleep(delay)
            for i in range(body['max_tokens']):
                send({'choices': [({'delta': {'content': 'x'}} if chat else {'text': 'x'})]})
                time.sleep(delay)
            send({'choices': [{'finish_reason': 'length'}]})
            send({'usage': {'prompt_tokens': count_in, 'completion_tokens': n}})
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class PerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.wrong_output = False
        self.server.delay = .003

    def config(self):
        return {'base_url': self.base, 'model': 'fixture', 'cache_mode': 'isolated', 'cache_salt_supported': True,
                'warmup_requests': 2, 'warmup_output_tokens': 4, 'maximum_runtime_seconds': 10,
                'strict_output': True, 'cases': [{'input_tokens': 16, 'output_tokens': 4, 'concurrency': 2, 'requests': 4}]}

    def test_current_inspection(self):
        result = bench.inspect(self.config())
        self.assertEqual(result['models']['data'][0]['id'], 'fixture')
        self.assertEqual(result['metrics']['queries'], 100)

    def test_real_http_raw_end_to_end_and_recompute(self):
        c = self.config()
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            out = Path(temp) / 'run'
            self.assertEqual(bench.live_run(c, out), 0)
            summary = json.loads((out / 'summary.json').read_text())
            rows = [json.loads(s) for s in (out / 'requests.jsonl').read_text().splitlines()]
            self.assertEqual(summary['state'], 'completed')
            self.assertEqual(len(rows), 6)
            self.assertEqual(sum(r['phase'] == 'warmup' for r in rows), 2)
            self.assertEqual(summary['rows'][0]['成功请求数'], 4)
            self.assertIsNone(summary['rows'][0]['Goodput(QPS)'])
            case = summary['cases'][0]
            rebuilt = bench.summarize(c, c['cases'][0], [r for r in rows if r['phase'] == 'formal'], case['row']['测试总时长(s)'])
            self.assertEqual(rebuilt['row'], case['row'])
            self.assertGreater(case['row']['单token平均延迟(s)'], 0)

    def test_chat_reasoning_and_content_timing(self):
        c = self.config()
        c.update(endpoint='chat', prompt_text='fixture prompt')
        r = bench.one_request(c, c['cases'][0], None, 1, 'run', time.monotonic() + 5, 4)
        self.assertTrue(r['success'], r)
        self.assertGreater(r['ttfc_seconds'], r['ttft_seconds'])

    def test_wrong_output_prevents_formal_test(self):
        self.server.wrong_output = True
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            out = Path(temp) / 'run'
            self.assertEqual(bench.live_run(self.config(), out), 2)
            result = json.loads((out / 'summary.json').read_text())
            self.assertEqual(result['state'], 'incomplete')
            self.assertFalse(result['rows'])
            self.assertIn('warmup failed', result['error'])

    def test_run_budget_interrupts_request(self):
        self.server.delay = .08
        c = self.config()
        r = bench.one_request(c, c['cases'][0], [1] * 16, 1, 'run', time.monotonic() + .025, 4)
        self.assertFalse(r['success'])
        self.assertLess(r['latency_seconds'], 1.0)

    def test_exact_input_guard(self):
        c = self.config()
        r = bench.one_request(c, c['cases'][0], [1] * 15, 1, 'run', time.monotonic() + 5, 4)
        self.assertFalse(r['success'])
        self.assertIn('input length mismatch', r['error'])

    def test_context_and_secrets_validation(self):
        c = self.config()
        c['service_max_model_len'] = 18
        with self.assertRaises(ValueError): bench.validate(c)
        for url in ['https://u:secret@host', 'https://host?key=secret']:
            with self.assertRaises(ValueError): bench.valid_url(url)

    def test_no_silent_cache_claim(self):
        c = self.config()
        c['cache_salt_supported'] = False
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError): bench.live_run(c, Path(temp) / 'run')

    def test_historical_matrix_profile_counts(self):
        c = json.loads((ROOT / 'ascend-llm-benchmark/assets/deepseek-curves.json').read_text())
        plan = bench.plan(c)
        self.assertEqual(plan['cases'], 15)
        self.assertEqual(plan['formal_requests'], 1539)

    def test_requires_execute_without_network(self):
        p = subprocess.run([sys.executable, str(ROOT / 'ascend-llm-benchmark/scripts/benchmark.py'), 'run', '--config', '/not/read', '--output-dir', '/not/write'], capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('requires --execute', p.stderr)


def section(text, rc=0):
    return {'text': text, 'returncode': rc, 'raw_start_line': 10}


def snapshot(sections):
    return {'host': 'fixture-host', 'captured_at': '2026-11-02T00:00:00Z', 'transport_returncode': 0, 'sections': sections}


class DiagnosticsTests(unittest.TestCase):
    def test_health_ok_does_not_hide_link_and_ecc_fault(self):
        s = snapshot({'health@3': section('Health Status : OK'),
                      'ecc@3': section('HBM Double Bit Error Count : 2\nHBM Double Bit Aggregate Total Err Cnt : 99'),
                      'roce-link@3': section('link status: DOWN'),
                      'roce-health@3': section('net health status: Receive timeout'),
                      'roce-optical@3': section('Rx LoL Flag : 0x2')})
        out = diag.analyze(s, expected=[3])
        self.assertEqual(out['cards']['3']['health'], 'OK')
        self.assertTrue(any(f['category'] == 'hbm' and f['state'] == '确认异常' for f in out['findings']))
        self.assertTrue(any(f['category'] == 'roce' and f['state'] == '确认异常' for f in out['findings']))
        self.assertEqual(out['stress_tests']['hbm_readback'], '未测试')

    def test_unused_link_down_is_not_confirmed_business_fault(self):
        out = diag.analyze(snapshot({'roce-link@3': section('link status: DOWN')}))
        self.assertFalse(any(f['category'] == 'roce' and f['state'] == '确认异常' for f in out['findings']))

    def test_network_init_is_not_reported_as_hardware_fault(self):
        out = diag.analyze(snapshot({'roce-health@3': section('net health status: Init')}))
        self.assertFalse(any(f['category'] == 'roce' for f in out['findings']))
        self.assertTrue(any(f['state'] == '未测试' for f in out['findings']))

    def test_missing_tool_does_not_pass(self):
        out = diag.analyze(snapshot({'ecc@3': section('not found', 127)}))
        self.assertTrue(any(f['state'] == '未测试' for f in out['findings']))

    def test_smartctl_error_status_does_not_hide_disk_fault(self):
        out = diag.analyze(snapshot({'smart@nvme0': section('SMART overall-health self-assessment test result: FAILED!\nCritical Warning: 0x04', 8)}))
        self.assertTrue(any(f['category'] == 'storage' and f['state'] == '确认异常' for f in out['findings']))

    def test_later_chip_health_does_not_hide_warning(self):
        out = diag.analyze(snapshot({'health@0': section('Health : OK\nChip ID : 0\nHealth : Warning\nChip ID : 1')}))
        self.assertTrue(any(f['category'] == 'npu' and f['state'] == '确认异常' for f in out['findings']))

    def test_duplicate_chip_ecc_does_not_hide_prior_error(self):
        self.assertEqual(diag.numbers('HBM Double Bit Error Count : 2\nHBM Double Bit Error Count : 0')['HBM Double Bit Error Count'], 2)

    def test_unknown_process_output_is_not_idle(self):
        out = diag.analyze(snapshot({'processes@0': section('unknown format')}))
        self.assertTrue(any('不能判断设备空闲' in f['message'] for f in out['findings']))

    def test_unparseable_success_output_is_not_health_pass(self):
        out = diag.analyze(snapshot({'health@3': section('unexpected driver output')}))
        self.assertIsNone(out['cards']['3']['health'])
        self.assertTrue(any(f['state'] == '未测试' for f in out['findings']))

    def test_collector_shell_syntax(self):
        script = diag.collector_script([0, 3])
        proc = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(' -reset', script)
        self.assertNotIn('systemctl restart', script)

    def test_parse_exitcodes_and_line_evidence(self):
        raw = '\n@@BEGIN health@3\nHealth Status : OK\n@@EXIT 0\n@@END\n@@BEGIN ecc@3\nnot installed\n@@EXIT 127\n@@END\n'
        sections = diag.parse_sections(raw)
        self.assertEqual(sections['ecc@3']['returncode'], 127)
        self.assertEqual(sections['health@3']['raw_start_line'], 2)

    def test_input_injection_rejected(self):
        for host in ['host;reboot', '-oProxyCommand=x', 'host foo']:
            with self.assertRaises(ValueError): diag.safe_host(host)
        with self.assertRaises(ValueError): diag.devices_arg('0;reboot')

    def test_real_collect_path_via_mock_transport(self):
        raw = '@@BEGIN health@0\nHealth Status : OK\n@@EXIT 0\n@@END\n'
        with tempfile.TemporaryDirectory() as temp, patch.object(diag.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, raw, '')) as transport:
            args = argparse_namespace(host='fixture-host', local=False, jump=None, devices='0', expected_roce_devices=None,
                                      command_timeout=2, maximum_seconds=20, output_dir=Path(temp)/'out')
            with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(diag.collect(args), 0)
            self.assertTrue((args.output_dir/'report.md').is_file())
            self.assertIn('ssh', transport.call_args.args[0])
            self.assertIn('npu-smi info', transport.call_args.kwargs['input'])

    def test_replacement_prevents_ecc_delta(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for label, ident, errors in [('before', 'OLD', 20), ('after', 'NEW', 0)]:
                d = root/label
                d.mkdir()
                s = snapshot({'vdie@3': section('VDie ID : '+ident), 'ecc@3': section('HBM Double Bit Error Count : '+str(errors))})
                diag.save(d/'snapshot.json', s)
            a = argparse_namespace(before=root/'before', after=root/'after', output_dir=root/'comparison')
            diag.compare(a)
            result = json.loads((a.output_dir/'comparison.json').read_text())['changes'][0]
            self.assertTrue(result['identity_changed'])
            self.assertIsNone(result['ecc_delta_same_reported_identity_only'])

    def test_stress_requires_execute_before_torch_import(self):
        for script, args in [('hbm_check.py', ['--device', '0', '--max-gib', '1']), ('hccl_microbench.py', ['--output', '/not/write'])]:
            p = subprocess.run([sys.executable, str(ROOT/'ascend-server-diagnostics/scripts'/script), *args], capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('requires --execute', p.stderr)


def argparse_namespace(**kwargs):
    import argparse
    return argparse.Namespace(**kwargs)


if __name__ == '__main__':
    unittest.main(verbosity=2)
