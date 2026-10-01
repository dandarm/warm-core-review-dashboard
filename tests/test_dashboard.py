import csv
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "dashboard", ROOT / "scripts/visual_review_dashboard.py"
)
dashboard = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dashboard
spec.loader.exec_module(dashboard)


class StandaloneTests(unittest.TestCase):
    def test_external_dataset_and_review_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            external = Path(tmp)
            shutil.copytree(ROOT / "examples", external / "dataset")
            state = dashboard.AppState(
                ROOT, external / "dataset/samples.csv",
                external / "dataset/images", external / "reviews"
            )
            dashboard.load_samples(state)
            self.assertEqual(len(state.samples), 4)
            self.assertEqual({s['label'] for s in state.samples}, {0, 1})
            self.assertEqual(state.missing_images, [])
            sample = state.samples[0]
            for label, action, expected in (
                (sample['label'], '', 'keep'),
                (1 - sample['label'], '', 'switch'),
                (sample['label'], 'discard', 'discard'),
            ):
                result = dashboard.save_review(state, {
                    'reviewer': 'Test', 'sample_id': sample['sample_id'],
                    'reviewed_label': label, 'action': action,
                })
                self.assertEqual(result['decision'], expected)
            rows = dashboard.labels_for_reviewer(state, 'Test')
            self.assertEqual(rows[sample['sample_id']]['decision'], 'discard')
            with (state.output_dir / 'review_events.csv').open(newline='') as f:
                self.assertEqual(len(list(csv.DictReader(f))), 3)

            dashboard.DashboardHandler.state = state
            server = dashboard.ThreadingHTTPServer(
                ('127.0.0.1', 0), dashboard.DashboardHandler
            )
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(base + '/api/samples') as response:
                    self.assertEqual(len(json.load(response)['samples']), 4)
                for item in state.samples:
                    for image in item['images']:
                        with urlopen(base + '/image?path=' + quote(image)) as response:
                            self.assertTrue(response.headers['Content-Type'].startswith('image/'))
                            self.assertEqual(response.read(), Path(image).read_bytes())
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + '/image?path=' + quote(str(state.input_csv)))
                self.assertEqual(error.exception.code, 403)
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
