import csv
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from io import BytesIO
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
    def test_full_atlas_order_and_failure_preserves_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = dashboard.AppState(root, root / "none.csv", root, root / "reviews")
            def fetch(track):
                return [dict(sample_id=track, label=None, index=0)]
            with patch.object(dashboard, "atlas_tracks", return_value=["track00000001", "track00000002"]), patch.object(dashboard, "fetch_atlas_track", side_effect=fetch):
                dashboard.load_full_atlas(state)
            self.assertEqual([s["sample_id"] for s in state.samples], ["track00000001", "track00000002"])
            self.assertEqual([s["index"] for s in state.samples], [0, 1])
            self.assertEqual(state.atlas_progress["status"], "ready")
            previous = state.samples
            with patch.object(dashboard, "atlas_tracks", return_value=["track00000003"]), patch.object(dashboard, "fetch_atlas_track", side_effect=TimeoutError("offline")):
                dashboard.load_full_atlas(state)
            self.assertEqual(state.atlas_progress["status"], "error")
            self.assertIs(state.samples, previous)

    def test_atlas_discovery_and_unlabelled_decisions(self):
        listing = b'<a href="TBanomaly_30-Dec-1999_0630_NOAA15.jpg">A</a><a href="CTH_30-Dec-1999_0630UTC_NOAA15.jpg">B</a>'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = dashboard.AppState(root, root / "absent.csv", root / "absent", root / "reviews")
            with patch.object(dashboard.urllib.request, "urlopen", return_value=BytesIO(listing)):
                dashboard.load_atlas_track(state, "track00000841")
            self.assertEqual(len(state.samples), 1)
            sample = state.samples[0]
            self.assertIsNone(sample["label"])
            self.assertEqual(len(sample["images"]), 2)
            for label in (0, 1):
                result = dashboard.save_review(state, dict(
                    reviewer="Test", sample_id=sample["sample_id"], reviewed_label=label
                ))
                self.assertEqual(result["decision"], "label")
                row = dashboard.labels_for_reviewer(state, "Test")[sample["sample_id"]]
                self.assertEqual(row["original_label"], "")
                self.assertEqual(row["reviewed_label"], str(label))
            dashboard.save_review(state, dict(reviewer="Test", sample_id=sample["sample_id"], action="discard"))
            row = dashboard.labels_for_reviewer(state, "Test")[sample["sample_id"]]
            self.assertEqual(row["reviewed_label"], "")
            self.assertEqual(row["decision"], "discard")

    def test_remote_mapping_and_listing_cache(self):
        base = "https://example.org/atlas/"
        directory = base + "track00000841/MEDICANES-WC-DC/Images/"
        name = "TBanomaly_FourChannels_30-Dec-1999_0630UTC_NOAA15.jpg"
        listing = (
            f'<a href="{name}">image</a>'
            f'<a href="{name}">duplicate</a>'
            f'<a href="https://elsewhere.org/{name}">external</a>'
            f'<a href="../{name}">parent</a>'
            '<a href="TBanomaly_30-Dec-1999_0630_NOAA150.jpg">wrong satellite</a>'
        ).encode()
        cache = {}
        with patch.object(dashboard.urllib.request, "urlopen", return_value=BytesIO(listing)) as fetch:
            for _ in range(2):
                images = dashboard.map_remote_images(
                    base, "Track0841/WP2300_Output_30-Dec-1999_0630_NOAA15.npz", cache
                )
                self.assertEqual(images, [directory + name])
            fetch.assert_called_once_with(directory, timeout=30)
        self.assertEqual(dashboard.map_remote_images(
            base, "Track0841/WP2300_Output_31-Dec-1999_0630_NOAA15.npz", cache
        ), [])

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
