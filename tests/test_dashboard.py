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
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "dashboard", ROOT / "scripts/visual_review_dashboard.py"
)
dashboard = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dashboard
spec.loader.exec_module(dashboard)


class StandaloneTests(unittest.TestCase):
    def test_flat_image_folder_keeps_independent_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'loose images'
            folder.mkdir()
            for name in ('image10.jpg', 'image2.PNG', 'image1.jpeg'):
                (folder / name).write_bytes(b'image')
            (folder / 'notes.txt').write_text('not an image')
            (folder / 'nested').mkdir()
            (folder / 'nested' / 'image3.jpg').write_bytes(b'nested')
            (root / 'outside.jpg').write_bytes(b'outside')
            state = dashboard.AppState(root, root / 'none.csv', root, root / 'reviews')
            self.assertEqual(dashboard.connect_flat_folder(state, str(folder)), 3)
            self.assertEqual([sample['sample_id'] for sample in state.samples],
                             ['image1.jpeg', 'image2.PNG', 'image10.jpg'])
            self.assertTrue(state.flat_mode)
            self.assertFalse(state.atlas_mode)
            self.assertEqual({sample['label'] for sample in state.samples}, {None})
            for sample_id, label in (('image1.jpeg', 0), ('image2.PNG', 1)):
                dashboard.save_review(state, {'reviewer': 'Test', 'sample_id': sample_id,
                                              'reviewed_label': label})
            review_path = dashboard.reviewer_file(state, 'Test')
            with review_path.open(newline='') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows, [{'sample_id': 'image1.jpeg', 'label': '0'},
                                    {'sample_id': 'image2.PNG', 'label': '1'}])
            output_dir = state.output_dir
            dashboard.DashboardHandler.state = state
            server = dashboard.ThreadingHTTPServer(('127.0.0.1', 0), dashboard.DashboardHandler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                request = Request(base + '/api/flat/connect',
                                  data=json.dumps({'folder': str(folder)}).encode(),
                                  headers={'Content-Type': 'application/json'}, method='POST')
                with urlopen(request) as response:
                    self.assertEqual(json.load(response)['count'], 3)
                with urlopen(base + '/api/samples') as response:
                    self.assertTrue(json.load(response)['config']['flat_mode'])
                with urlopen(base + '/image?path=' + quote(str(folder / 'image2.PNG'))) as response:
                    self.assertEqual(response.read(), b'image')
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + '/image?path=' + quote(str(root / 'outside.jpg')))
                self.assertEqual(error.exception.code, 403)
            finally:
                server.shutdown()
                thread.join()
                server.server_close()
            dashboard.connect_flat_folder(state, str(folder))
            self.assertEqual(state.output_dir, output_dir)
            self.assertEqual(dashboard.labels_for_reviewer(state, 'Test')['image2.PNG']['reviewed_label'], '1')
            track_root = root / 'tracks'
            (track_root / 'Track0841').mkdir(parents=True)
            dashboard.connect_local_folder(state, str(track_root))
            self.assertFalse(state.flat_mode)
            self.assertTrue(state.atlas_mode)
            dashboard.connect_flat_folder(state, str(folder))
            with patch.object(dashboard, 'atlas_tracks', return_value=['track00000841']):
                dashboard.connect_atlas(state)
            self.assertFalse(state.flat_mode)
            self.assertEqual(state.output_dir, root / 'reviews/atlas')
            empty = root / 'empty'
            empty.mkdir()
            with self.assertRaisesRegex(ValueError, 'No JPG or PNG'):
                dashboard.connect_flat_folder(state, str(empty))
            self.assertEqual(state.output_dir, root / 'reviews/atlas')

    def test_local_folder_tracks_and_reviews(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'downloaded'
            for track, image_dir in (
                ('track00000841', folder / 'track00000841' / 'MEDICANES-WC-DC' / 'Images'),
                ('Track0842', folder / 'Track0842' / 'Images'),
            ):
                image_dir.mkdir(parents=True)
                (image_dir / 'TBanomaly_30-Dec-1999_0630_NOAA15.jpg').write_bytes(b'image')
            (folder / 'unrelated').mkdir()
            state = dashboard.AppState(root, root / 'none.csv', root, root / 'reviews')
            self.assertEqual(dashboard.connect_local_folder(state, str(folder)),
                             ['track00000841', 'Track0842'])
            self.assertEqual(state.samples, [])
            self.assertTrue(state.local_mode)
            self.assertNotEqual(state.output_dir, root / 'reviews/atlas')
            dashboard.load_atlas_track(state, 'track00000841')
            self.assertEqual(len(state.samples), 1)
            sample = state.samples[0]
            self.assertEqual(sample['sample_id'], 'Track0841/WP2300_Output_30-Dec-1999_0630_NOAA15.npz')
            self.assertEqual(sample['images'], [str(folder / 'track00000841' / 'MEDICANES-WC-DC' /
                                                       'Images' / 'TBanomaly_30-Dec-1999_0630_NOAA15.jpg')])
            dashboard.save_review(state, {'reviewer': 'Test', 'sample_id': sample['sample_id'],
                                          'reviewed_label': 1})
            dashboard.load_atlas_track(state, 'Track0842')
            self.assertEqual(len(state.samples), 1)
            self.assertEqual(dashboard.labels_for_reviewer(state, 'Test')[sample['sample_id']]['reviewed_label'], '1')
            dashboard.DashboardHandler.state = state
            server = dashboard.ThreadingHTTPServer(('127.0.0.1', 0), dashboard.DashboardHandler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with urlopen(f'http://127.0.0.1:{server.server_port}/image?path=' +
                             quote(state.samples[0]['images'][0])) as response:
                    self.assertEqual(response.read(), b'image')
            finally:
                server.shutdown()
                thread.join()
                server.server_close()
            with patch.object(dashboard, 'atlas_tracks', return_value=['track00000841']):
                dashboard.connect_atlas(state)
            self.assertFalse(state.local_mode)
            self.assertEqual(state.output_dir, root / 'reviews/atlas')

    def test_atlas_listing_starts_at_track_841(self):
        listing = ('<a href="track00000840/">840</a>'
                   '<a href="track00000842/">842</a>'
                   '<a href="track00000841/">841</a>')
        with patch.object(dashboard, 'read_atlas_index', return_value=listing):
            self.assertEqual(dashboard.atlas_tracks(), ['track00000841', 'track00000842'])

    def test_index_retries_timeout_and_not_found_is_not_retried(self):
        with patch.object(dashboard.urllib.request, "urlopen", side_effect=[
            dashboard.urllib.error.URLError(TimeoutError("handshake timed out")),
            BytesIO(b"ok"),
        ]) as fetch, patch.object(dashboard.time, "sleep"):
            self.assertEqual(dashboard.read_atlas_index("https://example.org/"), "ok")
            self.assertEqual(fetch.call_count, 2)
        with patch.object(dashboard.urllib.request, "urlopen", side_effect=TimeoutError("offline")) as fetch, patch.object(dashboard.time, "sleep"):
            with self.assertRaises(TimeoutError):
                dashboard.read_atlas_index("https://example.org/")
            self.assertEqual(fetch.call_count, 3)
        error = dashboard.urllib.error.HTTPError("https://example.org/", 404, "missing", {}, None)
        with patch.object(dashboard.urllib.request, "urlopen", side_effect=error) as fetch:
            with self.assertRaises(dashboard.urllib.error.HTTPError):
                dashboard.read_atlas_index("https://example.org/")
            self.assertEqual(fetch.call_count, 1)
        with patch.object(dashboard.urllib.request, "urlopen", side_effect=dashboard.urllib.error.URLError("unknown url type: https")) as fetch:
            with self.assertRaisesRegex(RuntimeError, "cannot open HTTPS"):
                dashboard.read_atlas_index("https://example.org/", attempts=1)
            self.assertEqual(fetch.call_count, 1)

    def test_atlas_connect_and_load_only_selected_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = dashboard.AppState(root, root / "none.csv", root, root / "reviews")
            tracks = ["track00000841", "track00000842", "track00000843"]
            with patch.object(dashboard, "atlas_tracks", return_value=tracks) as listing, patch.object(
                dashboard, "fetch_atlas_track", return_value=[
                    dict(sample_id="Track0842/WP2300_Output_05-Jan-2000_0555_NOAA15.npz",
                         label=None, index=0, images=["https://example.org/image.jpg"], unlabelled=1)
                ]
            ) as fetch:
                self.assertEqual(dashboard.connect_atlas(state), tracks)
                self.assertEqual(state.samples, [])
                self.assertEqual(state.atlas_tracks, tracks)
                fetch.assert_not_called()
                dashboard.load_atlas_track(state, tracks[1])
                self.assertEqual(state.atlas_track, tracks[1])
                self.assertEqual(len(state.samples), 1)
                sample_id = state.samples[0]["sample_id"]
                dashboard.save_review(state, dict(reviewer="Test", sample_id=sample_id, reviewed_label=1))
                dashboard.load_atlas_track(state, tracks[0])
                self.assertEqual(state.samples, state.atlas_cache[tracks[0]])
                dashboard.load_atlas_track(state, tracks[1])
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(fetch.call_args_list[0].args, (tracks[1],))
                self.assertEqual(dashboard.labels_for_reviewer(state, "Test")[sample_id]["reviewed_label"], "1")
                listing.assert_called_once()
                with self.assertRaisesRegex(ValueError, "Unknown atlas track"):
                    dashboard.load_atlas_track(state, "track00009999")
                self.assertEqual(fetch.call_count, 2)

    def test_failed_track_keeps_previous_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = dashboard.AppState(root, root / "none.csv", root, root / "reviews")
            state.atlas_tracks = ["track00000841", "track00000842"]
            state.atlas_cache["track00000841"] = [
                dict(sample_id="Track0841/WP2300_Output_30-Dec-1999_0630_NOAA15.npz",
                     label=None, index=0, images=[], unlabelled=1)
            ]
            dashboard.load_atlas_track(state, "track00000841")
            previous = state.samples
            with patch.object(dashboard, "fetch_atlas_track", side_effect=TimeoutError("handshake")):
                with self.assertRaises(TimeoutError):
                    dashboard.load_atlas_track(state, "track00000842")
            self.assertEqual(state.atlas_track, "track00000841")
            self.assertIs(state.samples, previous)
            self.assertNotIn("track00000842", state.atlas_cache)
    def test_atlas_discovery_and_unlabelled_decisions(self):
        listing = b'<a href="TBanomaly_30-Dec-1999_0630_NOAA15.jpg">A</a><a href="CTH_30-Dec-1999_0630UTC_NOAA15.jpg">B</a>'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = dashboard.AppState(root, root / "absent.csv", root / "absent", root / "reviews")
            state.atlas_tracks = ["track00000841"]
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
            dashboard.save_review(state, {
                'reviewer': 'Test', 'sample_id': sample['sample_id'],
                'reviewed_label': sample['label'], 'notes': 'unclear',
            })
            self.assertEqual(dashboard.labels_for_reviewer(state, 'Test')[sample['sample_id']]['notes'], 'unclear')
            self.assertEqual(dashboard.reviews_for_sample(state, sample['sample_id'])[0]['notes'], 'unclear')
            with (state.output_dir / 'reviewer_Test.csv').open(newline='') as f:
                reader = csv.DictReader(f)
                self.assertEqual(reader.fieldnames, ['sample_id', 'label'])
                self.assertEqual(list(reader)[0]['label'], str(sample['label']))
            with (state.output_dir / 'review_events.csv').open(newline='') as f:
                reader = csv.DictReader(f)
                self.assertEqual(reader.fieldnames, ['reviewer', 'sample_id', 'label'])
                self.assertEqual([row['label'] for row in reader], [str(sample['label']),
                                 str(1 - sample['label']), 'discard', str(sample['label'])])

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

    def test_legacy_review_migration_preserves_labels_and_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / 'reviews'
            output_dir.mkdir()
            sample_id = 'Track0001/WP2300_Output_01-Jan-2000_0100_NPP.npz'
            fields = ['timestamp_utc', 'reviewer', 'sample_id', 'original_label',
                      'reviewed_label', 'decision', 'notes', 'unlabelled', 'source_csv', 'image_paths']
            rows = [dict(reviewer='Leo', sample_id=sample_id, reviewed_label='1',
                         decision='switch', notes='inspect', image_paths='https://example.org/image.jpg'),
                    dict(reviewer='Leo', sample_id='other', reviewed_label='0',
                         decision='discard', image_paths='https://example.org/other.jpg')]
            for name in ('reviewer_Leo.csv', 'review_events.csv'):
                with (output_dir / name).open('w', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)
            dashboard.migrate_reviews(output_dir)
            dashboard.migrate_reviews(output_dir)
            with (output_dir / 'reviewer_Leo.csv').open(newline='') as f:
                reader = csv.DictReader(f)
                self.assertEqual(reader.fieldnames, ['sample_id', 'label'])
                self.assertEqual({row['sample_id']: row['label'] for row in reader},
                                 {sample_id: '1', 'other': 'discard'})
            with (output_dir / 'review_events.csv').open(newline='') as f:
                reader = csv.DictReader(f)
                self.assertEqual(reader.fieldnames, ['reviewer', 'sample_id', 'label'])
                self.assertEqual([row['label'] for row in reader], ['1', 'discard'])
            self.assertEqual(json.loads((output_dir / 'reviewer_notes.json').read_text()),
                             {'Leo': {sample_id: 'inspect'}})
            state = dashboard.AppState(root, root / 'none.csv', root, output_dir)
            state.samples = [dict(sample_id=sample_id, label=0)]
            self.assertEqual(dashboard.labels_for_reviewer(state, 'Leo')[sample_id],
                             {'reviewed_label': '1', 'decision': 'switch', 'notes': 'inspect'})
            self.assertEqual(dashboard.reviews_for_sample(state, sample_id)[0]['notes'], 'inspect')


if __name__ == '__main__':
    unittest.main()
