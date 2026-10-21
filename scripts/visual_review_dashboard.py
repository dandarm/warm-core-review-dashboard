#!/usr/bin/env python3
"""Local visual review dashboard for threshold-sweep samples.

The server reads a CSV containing sample_id and label columns, maps each sample
to the corresponding climatology images, and stores reviewer-specific labels.
It intentionally uses only the Python standard library so it can run in the
project environment without installing a web framework.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import mimetypes
import os
import re
import sys
import time
import threading
import urllib.parse
import urllib.request
import urllib.error
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_CSV = Path("examples/samples.csv")
DEFAULT_DATA_ROOT = Path("examples/images")
DEFAULT_OUTPUT_DIR = Path("reviews/demo")
ATLAS_URL = "https://data-cersat.ifremer.fr/data/storms/medicanes/storm-atlas/climatology/"
ATLAS_REQUEST_TIMEOUT = 15
ATLAS_FIRST_TRACK = 841


@dataclass
class AppState:
    repo_root: Path
    input_csv: Path
    data_root: Path
    output_dir: Path
    samples: list[dict[str, Any]] = field(default_factory=list)
    missing_images: list[str] = field(default_factory=list)
    remote_base_url: str = ""
    atlas_mode: bool = False
    atlas_lock: Any = field(default_factory=threading.Lock)
    review_lock: Any = field(default_factory=threading.Lock)
    atlas_cache: dict = field(default_factory=dict)
    atlas_tracks: list[str] = field(default_factory=list)
    atlas_track: str = ""
    local_mode: bool = False
    local_root: Path | None = None
    flat_mode: bool = False
    flat_root: Path | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve a local dashboard for manual visual review of warm-core samples."
    )
    parser.add_argument(
        "input_csv",
        nargs="?",
        type=Path,
        help="Input CSV with sample_id and label columns. Overrides --csv when provided.",
    )
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Input CSV with sample_id and label columns.")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
        help="Root containing Track*/Images folders, or a flat image directory.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory where reviewer CSV files are written.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host.")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port.")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically.")
    parser.add_argument("--atlas", action="store_true", help="Start with no CSV; connect to the public atlas from the dashboard.")
    parser.add_argument("--atlas-workers", type=int, choices=range(1, 7), default=1,
                        help="Compatibility option; atlas batches always load sequentially.")
    parser.add_argument(
        "--remote-base-url", default="",
        help="Public atlas URL; uses trackNNNNNNNN/MEDICANES-WC-DC/Images/ instead of local images.",
    )
    return parser.parse_args()


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_under_root(root: Path, path: Path) -> Path:
    path = path if path.is_absolute() else root / path
    return path.resolve()


def display_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def without_prefix(value: str, prefix: str) -> str:
    return value[len(prefix):] if value.startswith(prefix) else value


def sample_key(sample_id: str) -> tuple[str, str, str, str]:
    parts = Path(sample_id).parts
    if len(parts) < 2:
        raise ValueError(f"sample_id does not look like Track*/file.npz: {sample_id}")

    track = parts[0]
    stem = Path(sample_id).stem
    key = without_prefix(without_prefix(stem, "WP2300_Output_"), "Thermodynamical_Microphysical_")
    fields = key.split("_")
    if len(fields) < 3:
        raise ValueError(f"Cannot extract date, time and platform from sample_id: {sample_id}")
    date = fields[0]
    overpass_time = fields[1]
    platform = "_".join(fields[2:])
    return track, date, overpass_time, platform


def image_sort_key(path: Path) -> tuple[int, str]:
    name = path.name
    priority = 9
    if "TBanomaly_FourChannels" in name and "3Dplot" not in name:
        priority = 0
    elif "TBanomaly_3Dplot_FourChannels" in name:
        priority = 1
    elif "VertCrossSection_AlongTrack" in name:
        priority = 2
    elif "VertCrossSection_CrossTrack" in name:
        priority = 3
    return priority, name


def map_images(data_root: Path, sample_id: str) -> list[Path]:
    track, date, overpass_time, platform = sample_key(sample_id)
    image_dir = data_root / track / "Images"
    search_dirs = [image_dir] if image_dir.exists() else [data_root]
    patterns = []
    for extension in ("jpg", "jpeg", "png"):
        patterns.append(f"*{date}*{overpass_time}*{platform}*.{extension}")
        patterns.append(f"*{date}*{overpass_time}UTC*{platform}*.{extension}")
    images: list[Path] = []
    for directory in search_dirs:
        for pattern in patterns:
            images.extend(directory.glob(pattern))
    return sorted(set(images), key=image_sort_key)


class ImageLinks(HTMLParser):
    def __init__(self, directory: str):
        super().__init__()
        self.directory = directory
        self.images: set[str] = set()

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        url = urllib.parse.urljoin(self.directory, href)
        parsed = urllib.parse.urlsplit(url)
        base = urllib.parse.urlsplit(self.directory)
        relative = without_prefix(parsed.path, base.path)
        if (
            parsed.scheme == base.scheme and parsed.netloc == base.netloc
            and parsed.path.startswith(base.path) and "/" not in relative
            and not parsed.query and not parsed.fragment
            and Path(relative).suffix.lower() in (".jpg", ".jpeg", ".png")
        ):
            self.images.add(url)


class TrackLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tracks = set()

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get("href") or ""
        if tag == "a" and re.fullmatch(r"track[0-9]+/", href):
            self.tracks.add(href.rstrip("/"))


def read_atlas_index(url, *, attempts=3, timeout=60):
    if url.startswith("https:") and not hasattr(urllib.request, "HTTPSHandler"):
        raise RuntimeError(
            "This Python installation cannot open HTTPS URLs. On Windows, run "
            "py -c \"import ssl; print(ssl.OPENSSL_VERSION)\". If that fails, "
            "install a complete Python build with SSL support."
        )
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code not in (408, 429, 500, 502, 503, 504):
                raise
            if attempt == attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if "unknown url type: https" in str(exc):
                raise RuntimeError(
                    "This Python installation cannot open HTTPS URLs. On Windows, run "
                    "py -c \"import ssl; print(ssl.OPENSSL_VERSION)\". If that fails, "
                    "install a complete Python build with SSL support."
                ) from exc
            if attempt == attempts - 1:
                raise
        except ValueError as exc:
            if "unknown url type: https" in str(exc):
                raise RuntimeError(
                    "This Python installation cannot open HTTPS URLs. On Windows, run "
                    "py -c \"import ssl; print(ssl.OPENSSL_VERSION)\". If that fails, "
                    "install a complete Python build with SSL support."
                ) from exc
            raise
        time.sleep(2 ** (attempt + 1))


def atlas_tracks():
    parser = TrackLinks()
    parser.feed(read_atlas_index(ATLAS_URL, attempts=1, timeout=ATLAS_REQUEST_TIMEOUT))
    return sorted(track for track in parser.tracks if int(track[5:]) >= ATLAS_FIRST_TRACK)


def samples_from_images(track, images):
    groups = {}
    for image in sorted(images):
        name = (image.name if isinstance(image, Path) else
                Path(urllib.parse.unquote(urllib.parse.urlsplit(image).path)).name)
        match = re.search(r"_([0-9]{2}-[A-Za-z]{3}-[0-9]{4})_([0-9]{4})(?:UTC)?_(.+)\.(?:jpg|jpeg|png)$", name, re.I)
        if match:
            date, hour, platform = match.groups()
            sample_id = f"Track{int(track[5:]):04d}/WP2300_Output_{date}_{hour}_{platform}.npz"
            groups.setdefault(sample_id, []).append(image)
    return [
        dict(index=i, sample_id=key, label=None, unlabelled=1,
             images=sorted(group, key=lambda image: image_sort_key(Path(str(image)))), metadata={})
        for i, (key, group) in enumerate(sorted(groups.items()))
    ]


def fetch_atlas_track(track):
    if not re.fullmatch(r"track[0-9]+", track):
        raise ValueError("Invalid atlas track.")
    directory = ATLAS_URL + track + "/MEDICANES-WC-DC/Images/"
    try:
        parser = ImageLinks(directory)
        parser.feed(read_atlas_index(directory, attempts=1, timeout=ATLAS_REQUEST_TIMEOUT))
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        parser = ImageLinks(directory)
    return samples_from_images(track, parser.images)


def local_tracks(root):
    return sorted((path.name for path in root.iterdir()
                   if path.is_dir() and re.fullmatch(r"track[0-9]+", path.name, re.I)),
                  key=lambda name: (int(name[5:]), name))


def natural_name_key(name):
    parts = re.split(r"(\d+)", name.casefold())
    return tuple((1, int(part)) if part.isdigit() else (0, part) for part in parts), name


def local_review_dir(state, kind, root):
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:10]
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", root.name).strip("._-") or "folder"
    return state.repo_root / "reviews" / kind / f"{name[:80]}-{digest}"


def fetch_local_track(root, track):
    image_dirs = [root / track / "MEDICANES-WC-DC" / "Images", root / track / "Images"]
    image_dir = next((path for path in image_dirs if path.is_dir()), None)
    if image_dir is None:
        return []
    images = [path for path in image_dir.iterdir()
              if path.is_file() and path.suffix.lower() in (".jpg", ".jpeg", ".png")]
    samples = samples_from_images(track, images)
    for sample in samples:
        sample["images"] = [str(path) for path in sample["images"]]
    return samples


def connect_local_folder(state, folder):
    if not folder.strip():
        raise ValueError("Choose a folder first.")
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Folder not found: {root}")
    tracks = local_tracks(root)
    if not tracks:
        raise ValueError("No Track* folders found in the selected folder.")
    state.local_root = root
    state.local_mode = True
    state.flat_mode = False
    state.flat_root = None
    state.data_root = root
    state.atlas_tracks = tracks
    state.atlas_cache = {}
    state.atlas_track = ""
    set_atlas_samples(state, [])
    return tracks


def connect_flat_folder(state, folder):
    if not folder.strip():
        raise ValueError("Choose a folder first.")
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Folder not found: {root}")
    images = sorted((path for path in root.iterdir()
                     if path.is_file() and not path.is_symlink()
                     and path.suffix.lower() in (".jpg", ".jpeg", ".png")),
                    key=lambda path: natural_name_key(path.name))
    if not images:
        raise ValueError("No JPG or PNG images found directly in this folder.")
    output_dir = local_review_dir(state, "flat", root)
    output_dir.mkdir(parents=True, exist_ok=True)
    migrate_reviews(output_dir)
    state.samples = [dict(index=index, sample_id=path.name, label=None, unlabelled=1,
                          images=[str(path)], metadata={}) for index, path in enumerate(images)]
    state.data_root = root
    state.output_dir = output_dir
    state.atlas_mode = False
    state.local_mode = False
    state.local_root = None
    state.flat_mode = True
    state.flat_root = root
    state.atlas_tracks = []
    state.atlas_cache = {}
    state.atlas_track = ""
    state.remote_base_url = ""
    state.missing_images = []
    return len(images)


def choose_local_folder():
    try:
        import tkinter as tk
        from tkinter import filedialog
        window = tk.Tk()
        window.withdraw()
        window.attributes("-topmost", True)
        try:
            return filedialog.askdirectory(parent=window, title="Select image folder")
        finally:
            window.destroy()
    except Exception as exc:
        raise RuntimeError("Folder picker unavailable; enter the folder path manually.") from exc


def set_atlas_samples(state, samples):
    for index, sample in enumerate(samples):
        sample["index"] = index
    state.samples = samples
    state.atlas_mode = True
    state.remote_base_url = "" if state.local_mode else ATLAS_URL
    if state.local_mode:
        state.output_dir = local_review_dir(state, "local", state.local_root)
    else:
        state.output_dir = state.repo_root / "reviews/atlas"
    state.output_dir.mkdir(parents=True, exist_ok=True)
    migrate_reviews(state.output_dir)
    state.missing_images = []


def load_atlas_track(state, track):
    if track not in state.atlas_tracks:
        raise ValueError(f"Unknown atlas track: {track}")
    if track not in state.atlas_cache:
        state.atlas_cache[track] = (fetch_local_track(state.local_root, track) if state.local_mode
                                    else fetch_atlas_track(track))
    set_atlas_samples(state, state.atlas_cache[track])
    state.atlas_track = track


def connect_atlas(state):
    if state.local_mode or not state.atlas_tracks:
        tracks = atlas_tracks()
        if not tracks:
            raise ValueError("No tracks found in the atlas.")
        state.atlas_tracks = tracks
    if state.local_mode:
        state.atlas_cache = {}
        state.atlas_track = ""
    was_local = state.local_mode
    state.local_mode = False
    state.local_root = None
    state.flat_mode = False
    state.flat_root = None
    if was_local or not state.atlas_mode:
        set_atlas_samples(state, [])
    state.atlas_mode = True
    return state.atlas_tracks


def map_remote_images(base_url: str, sample_id: str, cache: dict[str, list[str]]) -> list[str]:
    base = urllib.parse.urlsplit(base_url)
    if base.scheme not in ("https", "http") or not base.netloc or base.query or base.fragment:
        raise ValueError("Remote base URL must be an HTTP(S) directory URL without query or fragment.")
    track, date, overpass_time, platform = sample_key(sample_id)
    match = re.fullmatch(r"track(\d+)", track, re.IGNORECASE)
    if not match:
        raise ValueError(f"Cannot extract track number from {sample_id}")
    directory = (
        base_url.rstrip("/") + f"/track{int(match[1]):08d}/MEDICANES-WC-DC/Images/"
    )
    if directory not in cache:
        try:
            with urllib.request.urlopen(directory, timeout=30) as response:
                parser = ImageLinks(directory)
                parser.feed(response.read().decode("utf-8"))
                cache[directory] = sorted(parser.images)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise RuntimeError(f"Cannot read remote image directory {directory}: {exc}") from exc
            print(f"Warning: remote image directory not found: {directory}", file=sys.stderr)
            cache[directory] = []
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Cannot read remote image directory {directory}: {exc}") from exc
    pattern = re.compile(
        rf"_{re.escape(date)}_{re.escape(overpass_time)}(?:UTC)?_{re.escape(platform)}\.(?:jpg|jpeg|png)$",
        re.IGNORECASE,
    )
    return sorted(
        (url for url in cache[directory]
         if pattern.search(urllib.parse.unquote(urllib.parse.urlsplit(url).path))),
        key=lambda url: image_sort_key(Path(urllib.parse.urlsplit(url).path)),
    )


def load_samples(state: AppState) -> None:
    if not state.input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {state.input_csv}")
    if not state.remote_base_url and not state.data_root.exists():
        raise FileNotFoundError(f"Data root not found: {state.data_root}")

    with state.input_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "sample_id" not in reader.fieldnames:
            raise ValueError("Input CSV must contain at least a sample_id column.")
        label_column = "label" if "label" in reader.fieldnames else "true_label" if "true_label" in reader.fieldnames else ""
        if not label_column:
            raise ValueError("Input CSV must contain a label or true_label column.")

        samples: list[dict[str, Any]] = []
        missing_images: list[str] = []
        remote_cache: dict[str, list[str]] = {}
        for index, row in enumerate(reader):
            sample_id = row["sample_id"]
            if state.remote_base_url:
                image_paths = map_remote_images(state.remote_base_url, sample_id, remote_cache)
            else:
                images = map_images(state.data_root, sample_id)
                image_paths = [display_path(path, state.repo_root) for path in images]
            if not image_paths:
                missing_images.append(sample_id)
            samples.append(
                {
                    "index": index,
                    "sample_id": sample_id,
                    "label": int(row[label_column]),
                    "unlabelled": int(row.get("unlabelled") or 0),
                    "images": image_paths,
                    "metadata": row,
                }
            )

    state.samples = samples
    state.missing_images = missing_images
    state.output_dir.mkdir(parents=True, exist_ok=True)
    migrate_reviews(state.output_dir)


def reviewer_slug(reviewer: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", reviewer.strip()).strip("._-")
    if not slug:
        raise ValueError("Reviewer name is required.")
    return slug[:80]


def reviewer_file(state: AppState, reviewer: str) -> Path:
    return state.output_dir / f"reviewer_{reviewer_slug(reviewer)}.csv"


def read_reviewer_rows(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["sample_id"]: {"sample_id": row["sample_id"], "label": stored_label(row)}
                for row in csv.DictReader(handle)}


def stored_label(row: dict[str, str]) -> str:
    label = row.get("label") if "label" in row else (
        "discard" if row.get("decision") == "discard" else row.get("reviewed_label", "")
    )
    if label not in ("0", "1", "discard"):
        raise ValueError(f"Invalid saved label for {row.get('sample_id', '')}: {label!r}")
    return label


def write_reviewer_rows(path: Path, rows_by_sample: dict[str, dict[str, str]]) -> None:
    fieldnames = ["sample_id", "label"]
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in sorted(rows_by_sample.values(), key=lambda item: item["sample_id"]):
            writer.writerow({field: row.get(field, "") for field in fieldnames})
    tmp_path.replace(path)


def append_audit_log(state: AppState, row: dict[str, str]) -> None:
    path = state.output_dir / "review_events.csv"
    fieldnames = ["reviewer", "sample_id", "label"]
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in fieldnames})


def notes_file(output_dir: Path) -> Path:
    return output_dir / "reviewer_notes.json"


def read_notes(output_dir: Path) -> dict[str, dict[str, str]]:
    path = notes_file(output_dir)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_notes(output_dir: Path, notes: dict[str, dict[str, str]]) -> None:
    path = notes_file(output_dir)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(notes, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def migrate_reviews(output_dir: Path) -> None:
    notes = read_notes(output_dir)
    notes_changed = False
    for path in output_dir.glob("reviewer_*.csv"):
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames == ["sample_id", "label"]:
                continue
            rows = list(reader)
        slug = without_prefix(path.stem, "reviewer_")
        for row in rows:
            if row.get("notes"):
                notes.setdefault(slug, {})[row["sample_id"]] = row["notes"]
                notes_changed = True
        if notes_changed:
            write_notes(output_dir, notes)
            notes_changed = False
        write_reviewer_rows(path, {row["sample_id"]: {
            "sample_id": row["sample_id"], "label": stored_label(row)
        } for row in rows})

    audit_path = output_dir / "review_events.csv"
    if audit_path.exists():
        with audit_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames == ["reviewer", "sample_id", "label"]:
                return
            rows = list(reader)
        tmp_path = audit_path.with_suffix(".csv.tmp")
        with tmp_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["reviewer", "sample_id", "label"])
            writer.writeheader()
            for row in rows:
                writer.writerow({"reviewer": row["reviewer"], "sample_id": row["sample_id"],
                                 "label": stored_label(row)})
        tmp_path.replace(audit_path)


def save_review(state: AppState, payload: dict[str, Any]) -> dict[str, Any]:
    reviewer = str(payload.get("reviewer") or "").strip()
    sample_id = str(payload.get("sample_id") or "").strip()
    action = str(payload.get("action") or "").strip().lower()
    notes = str(payload.get("notes") or "").strip()

    sample = next((item for item in state.samples if item["sample_id"] == sample_id), None)
    if not sample:
        raise ValueError(f"Unknown sample_id: {sample_id}")

    if action == "discard":
        reviewed_label = sample["label"] if sample["label"] is not None else ""
        decision = "discard"
    else:
        reviewed_label = int(payload.get("reviewed_label"))
        if reviewed_label not in (0, 1):
            raise ValueError("reviewed_label must be 0 or 1.")
        decision = "label" if sample["label"] is None else "keep" if reviewed_label == sample["label"] else "switch"

    path = reviewer_file(state, reviewer)
    row = {"sample_id": sample_id, "label": "discard" if decision == "discard" else str(reviewed_label)}
    with state.review_lock:
        state.output_dir.mkdir(parents=True, exist_ok=True)
        migrate_reviews(state.output_dir)
        saved_notes = read_notes(state.output_dir)
        slug = reviewer_slug(reviewer)
        if notes:
            saved_notes.setdefault(slug, {})[sample_id] = notes
        elif slug in saved_notes:
            saved_notes[slug].pop(sample_id, None)
        if notes_file(state.output_dir).exists() or notes:
            write_notes(state.output_dir, saved_notes)
        rows = read_reviewer_rows(path)
        rows[sample_id] = row
        write_reviewer_rows(path, rows)
        append_audit_log(state, {"reviewer": reviewer, **row})
    return {"ok": True, "decision": decision, "reviewer_file": display_path(path, state.repo_root)}


def display_review(state: AppState, row: dict[str, str], notes: str = "") -> dict[str, str]:
    label = row["label"]
    original = next((item["label"] for item in state.samples if item["sample_id"] == row["sample_id"]), None)
    decision = "discard" if label == "discard" else (
        "label" if original is None else "keep" if int(label) == original else "switch"
    )
    return {"reviewed_label": "" if label == "discard" else label,
            "decision": decision, "notes": notes}


def labels_for_reviewer(state: AppState, reviewer: str) -> dict[str, dict[str, str]]:
    path = reviewer_file(state, reviewer)
    notes = read_notes(state.output_dir).get(reviewer_slug(reviewer), {})
    return {sample_id: display_review(state, row, notes.get(sample_id, ""))
            for sample_id, row in read_reviewer_rows(path).items()}


def reviews_for_sample(state: AppState, sample_id: str, current_reviewer: str = "") -> list[dict[str, str]]:
    current_slug = reviewer_slug(current_reviewer) if current_reviewer.strip() else ""
    rows: list[dict[str, str]] = []
    notes = read_notes(state.output_dir)
    for path in sorted(state.output_dir.glob("reviewer_*.csv")):
        reviewer_from_file = without_prefix(path.stem, "reviewer_")
        if current_slug and reviewer_from_file == current_slug:
            continue
        reviewer_rows = read_reviewer_rows(path)
        row = reviewer_rows.get(sample_id)
        if not row:
            continue
        item = {"reviewer": reviewer_from_file,
                **display_review(state, row, notes.get(reviewer_from_file, {}).get(sample_id, ""))}
        rows.append(item)
    return rows


HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Warm Core Visual Review</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #15202b;
      --muted: #657080;
      --line: #d9dee5;
      --accent: #12636f;
      --accent-ink: #ffffff;
      --warn: #9b3d19;
      --ok: #1f6f43;
      --shadow: 0 1px 3px rgba(16, 24, 40, 0.12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      letter-spacing: 0;
    }
    button, input, textarea, select { font: inherit; }
    button {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      min-height: 36px;
      padding: 0 12px;
      border-radius: 6px;
      cursor: pointer;
    }
    button:hover { border-color: #9aa8b7; }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.52;
    }
    button:disabled:hover { border-color: var(--line); }
    button.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: var(--accent-ink);
    }
    button.switch { border-color: #cf7f4b; color: var(--warn); }
    button.discard { border-color: #b8bec7; color: #475467; }
    button.icon { width: 38px; padding: 0; font-size: 18px; }
    input, textarea, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      background: #fff;
      color: var(--ink);
    }
    textarea { min-height: 70px; resize: vertical; }
    header {
      position: sticky;
      top: 0;
      z-index: 20;
      background: rgba(246, 247, 249, 0.96);
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(8px);
    }
    .topbar {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) minmax(260px, 360px) auto;
      gap: 16px;
      align-items: center;
      max-width: 1680px;
      margin: 0 auto;
      padding: 12px 18px;
    }
    h1 { margin: 0; font-size: 18px; line-height: 1.25; }
    .sub { color: var(--muted); font-size: 12px; margin-top: 3px; }
    .reviewer { display: grid; grid-template-columns: 84px minmax(120px, 1fr) auto; gap: 8px; align-items: center; }
    .reviewer label { color: var(--muted); font-size: 12px; }
    .tabs, .actions, .nav, .thumb-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .tabs {
      border-left: 1px solid var(--line);
      padding-left: 16px;
      margin-left: 4px;
    }
    .tabs button.active { background: #d7edf0; border-color: #9fc7ce; color: #083c44; }
    .source-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 6px; }
    dialog { border: 1px solid var(--line); border-radius: 8px; padding: 20px; width: min(520px, calc(100vw - 32px)); color: var(--ink); }
    dialog::backdrop { background: rgba(21, 32, 43, .45); }
    dialog h2 { margin: 0 0 16px; font-size: 17px; }
    .folder-field { display: flex; gap: 8px; margin: 8px 0 18px; }
    .folder-field input { min-width: 0; }
    .dialog-actions { display: flex; justify-content: flex-end; gap: 8px; }
    main {
      max-width: 1680px;
      margin: 0 auto;
      padding: 16px 18px 28px;
      display: grid;
      grid-template-columns: minmax(270px, 340px) 1fr;
      gap: 16px;
    }
    aside, .viewer, .image-panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    aside { padding: 14px; align-self: start; position: sticky; top: 76px; }
    .progress {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      align-items: center;
      margin-bottom: 12px;
    }
    .progress strong { font-size: 18px; }
    .meter { grid-column: 1 / -1; height: 8px; background: #e8ecf1; border-radius: 100px; overflow: hidden; }
    .meter div { height: 100%; background: var(--accent); width: 0%; }
    .sample-list {
      max-height: calc(100vh - 310px);
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfcfd;
    }
    .sample-row {
      width: 100%;
      display: grid;
      grid-template-columns: 34px 1fr auto;
      gap: 8px;
      align-items: center;
      border: 0;
      border-bottom: 1px solid var(--line);
      border-radius: 0;
      min-height: 38px;
      text-align: left;
      background: transparent;
    }
    .sample-row.active { background: #e6f3f5; }
    .sample-row.reviewed .dot { background: var(--ok); }
    .sample-row .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: #adb7c2;
      justify-self: center;
    }
    .sample-row span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .sample-row small { color: var(--muted); }
    .viewer { padding: 14px; }
    .sample-head {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: start;
      margin-bottom: 12px;
    }
    .sample-title {
      font-weight: 700;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .badges { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      border-radius: 999px;
      border: 1px solid var(--line);
      padding: 2px 9px;
      background: #f7f9fb;
      color: var(--muted);
      font-size: 12px;
    }
    .badge.label0 { color: #1d5b75; border-color: #a4cad9; background: #e9f5f9; }
    .badge.label1 { color: #7a3d08; border-color: #d8b177; background: #fff4df; }
    .badge.saved { color: var(--ok); border-color: #9fd0b2; background: #edf8f1; }
    .reviewer-panel {
      display: none;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfcfd;
      margin: 0 0 12px;
      padding: 10px 12px;
    }
    .reviewer-panel.visible { display: block; }
    .reviewer-panel h2 {
      margin: 0 0 8px;
      font-size: 13px;
      color: #344054;
    }
    .reviewer-votes {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 8px;
    }
    .reviewer-vote {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      padding: 8px 10px;
      min-width: 0;
    }
    .reviewer-vote strong,
    .reviewer-vote span {
      display: block;
      overflow-wrap: anywhere;
    }
    .reviewer-vote span { color: var(--muted); font-size: 12px; margin-top: 3px; }
    .image-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .image-panel { overflow: hidden; }
    .image-panel h2 {
      margin: 0;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      font-size: 13px;
      font-weight: 650;
      color: #344054;
      overflow-wrap: anywhere;
    }
    .image-wrap {
      background: #eef1f4;
      display: grid;
      place-items: center;
      min-height: 260px;
    }
    .image-wrap img {
      width: 100%;
      height: auto;
      display: block;
      object-fit: contain;
      max-height: 70vh;
      background: #fff;
    }
    .review-box {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      align-items: end;
      margin-top: 12px;
      border-top: 1px solid var(--line);
      padding-top: 12px;
    }
    .status { color: var(--muted); min-height: 20px; margin-top: 8px; }
    .status.error { color: #b42318; }
    .status.ok { color: var(--ok); }
    @media (max-width: 980px) {
      .topbar, main, .sample-head, .review-box { grid-template-columns: 1fr; }
      aside { position: static; }
      .image-grid { grid-template-columns: 1fr; }
      .sample-list { max-height: 280px; }
      .tabs {
        border-left: 0;
        border-top: 1px solid var(--line);
        margin-left: 0;
        padding-left: 0;
        padding-top: 10px;
      }
    }
    [hidden] { display: none !important; }
    html { height: 100%; overflow: hidden; }
    body { height: 100vh; height: 100dvh; display: flex; flex-direction: column; overflow: hidden; }
    header { flex: none; }
    main { width: 100%; flex: 1; min-height: 0; overflow: hidden; padding-bottom: 16px; }
    aside, .viewer { min-width: 0; min-height: 0; overflow: hidden; display: flex; flex-direction: column; }
    aside { position: static; align-self: stretch; }
    aside > :not(.sample-list), .sample-head { flex: none; }
    .sample-list { flex: 1; min-height: 0; max-height: none; overflow-y: auto; overflow-x: hidden; position: relative; overscroll-behavior: contain; }
    .sample-row { height: 40px; min-height: 40px; grid-template-columns: 18px minmax(0, 1fr) auto; }
    .sample-list-inner { position: relative; width: 100%; }
    .sample-list-inner .sample-row { position: absolute; left: 0; }
    .image-grid { flex: 1; min-height: 0; overflow-y: auto; align-content: start; grid-auto-rows: max-content; overscroll-behavior: contain; scrollbar-gutter: stable; }
    .reviewer-panel { flex: none; max-height: 25%; overflow: auto; }
    #samplePicker { width: min(220px, 100%); min-width: 120px; }
    .sub, .status { overflow-wrap: anywhere; }
    textarea { max-height: 120px; }
    @media (max-width: 980px) {
      main { grid-template-rows: minmax(160px, 38%) minmax(0, 1fr); padding: 8px; gap: 8px; }
      .topbar { padding: 8px; gap: 6px; }
      aside, .viewer { padding: 8px; }
      aside { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); grid-template-rows: auto auto auto minmax(0, 1fr); gap: 4px 8px; }
      aside .progress { margin: 0; }
      aside .sample-list { grid-column: 2; grid-row: 1 / 5; }
      aside .status { margin: 0; max-height: 40px; overflow: auto; }
      aside > div:has(textarea) { min-height: 0; margin: 0 !important; }
      aside textarea { height: 100%; min-height: 28px; resize: none; }
      .actions { gap: 4px; }
      .actions button { padding: 0 6px; }
      .sample-head { gap: 4px; margin-bottom: 6px; }
      .sample-head .nav { gap: 4px; }
      .sample-head button { min-height: 30px; font-size: 12px; }
      .reviewer { grid-template-columns: 64px minmax(0, 1fr) auto; }
    }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div>
        <h1>Warm Core Visual Review</h1>
        <div class="source-actions">
          <button id="connectAtlas">Connect to atlas</button>
          <button id="openLocalFolder">Open local folder</button>
          <button id="openFlatFolder">Open image folder</button>
        </div>
        <div class="sub" id="source"></div>
      </div>
      <div class="reviewer">
        <label for="reviewer">Reviewer</label>
        <input id="reviewer" autocomplete="name" placeholder="Reviewer name">
        <button id="confirmReviewer">OK</button>
      </div>
      <div class="tabs" role="tablist" aria-label="Label filter">
        <button id="tab0" data-label="0">Negatives</button>
        <button id="tab1" data-label="1">Positives (Warm core)</button>
      </div>
    </div>
  </header>
  <main>
    <aside>
      <div class="progress">
        <div>
          <strong id="position">0 / 0</strong>
          <div class="sub" id="reviewedCount">0 reviewed</div>
        </div>
        <div class="nav">
          <button class="icon" id="prev" title="Previous">‹</button>
          <button class="icon" id="next" title="Next">›</button>
        </div>
        <div class="meter"><div id="meter"></div></div>
      </div>
      <div class="actions">
        <button class="primary" id="keep">Keep</button>
        <button class="switch" id="switch">Switch</button>
        <button class="discard" id="discard">Discard</button>
      </div>
      <div class="status" id="status"></div>
      <div style="margin: 12px 0 8px;">
        <textarea id="notes" placeholder="Optional notes"></textarea>
      </div>
      <div class="sample-list" id="sampleList" aria-label="Samples or atlas tracks"></div>
    </aside>
    <section class="viewer">
      <div class="sample-head">
        <div>
          <div class="sample-title" id="sampleTitle"></div>
          <div class="badges" id="badges"></div>
        </div>
        <div class="nav">
          <select id="samplePicker" aria-label="Sample in selected track" hidden></select>
          <button id="showOtherReviews">Show other reviewers</button>
          <button id="jumpUnreviewed">Next unreviewed sample</button>
        </div>
      </div>
      <div class="reviewer-panel" id="otherReviewsPanel"></div>
      <div class="image-grid" id="images"></div>
    </section>
  </main>
  <dialog id="folderDialog">
    <h2 id="folderDialogTitle">Open local tracks</h2>
    <label id="folderDialogLabel" for="folderPath">Folder containing Track* directories</label>
    <div class="folder-field">
      <input id="folderPath" autocomplete="off" placeholder="Absolute folder path">
      <button id="browseFolder" type="button">Browse</button>
    </div>
    <div id="folderError" class="status error" role="alert"></div>
    <div class="dialog-actions">
      <button id="cancelFolder" type="button">Cancel</button>
      <button id="loadFolder" class="primary" type="button">Load</button>
    </div>
  </dialog>
<script>
const state = {
  samples: [],
  byLabel: {0: [], 1: []},
  label: Number(new URLSearchParams(location.search).get("label") || 0),
  index: 0,
  reviews: {},
  config: null,
  reviewerConfirmed: false,
  completionAlertShown: false,
  otherReviewsSampleId: "",
  otherReviewsVisible: false,
  tracks: [],
  trackIndex: -1,
  folderKind: "tracks",
};

const els = {
  source: document.getElementById("source"),
  reviewer: document.getElementById("reviewer"),
  confirmReviewer: document.getElementById("confirmReviewer"),
  tab0: document.getElementById("tab0"),
  tab1: document.getElementById("tab1"),
  position: document.getElementById("position"),
  reviewedCount: document.getElementById("reviewedCount"),
  meter: document.getElementById("meter"),
  prev: document.getElementById("prev"),
  next: document.getElementById("next"),
  keep: document.getElementById("keep"),
  switch: document.getElementById("switch"),
  discard: document.getElementById("discard"),
  status: document.getElementById("status"),
  notes: document.getElementById("notes"),
  sampleList: document.getElementById("sampleList"),
  sampleTitle: document.getElementById("sampleTitle"),
  badges: document.getElementById("badges"),
  showOtherReviews: document.getElementById("showOtherReviews"),
  otherReviewsPanel: document.getElementById("otherReviewsPanel"),
  images: document.getElementById("images"),
  jumpUnreviewed: document.getElementById("jumpUnreviewed"),
  samplePicker: document.getElementById("samplePicker"),
  folderDialog: document.getElementById("folderDialog"),
  folderDialogTitle: document.getElementById("folderDialogTitle"),
  folderDialogLabel: document.getElementById("folderDialogLabel"),
  folderPath: document.getElementById("folderPath"),
  folderError: document.getElementById("folderError"),
  browseFolder: document.getElementById("browseFolder"),
  loadFolder: document.getElementById("loadFolder"),
};

function sourceName() {
  if (state.config?.flat_mode) return "Images · " + state.config.flat_root;
  return state.config?.local_mode ? "Local · " + state.config.local_root : "Atlas";
}

function unlabelledMode() {
  return !!(state.config?.atlas_mode || state.config?.flat_mode);
}

function sampleDisplayName(sample) {
  const match = sample.sample_id.match(/(?:WP2300_Output_|Thermodynamical_Microphysical_)(\d{2}-[A-Za-z]{3}-\d{4})_(\d{4})_(.+)\.npz$/i);
  return match ? `${match[1]} · ${match[2]} · ${match[3]}` : sample.sample_id;
}

function setStatus(text, cls = "") {
  els.status.textContent = text;
  els.status.className = "status" + (cls ? " " + cls : "");
}

function controlsDisabled() {
  return !state.reviewerConfirmed || !currentSample() || state.loading;
}

function updateControls() {
  const disabled = controlsDisabled();
  els.keep.disabled = disabled;
  els.switch.disabled = disabled;
  els.discard.disabled = disabled;
  els.notes.disabled = disabled;
  els.keep.textContent = unlabelledMode() ? "Negative" : "Keep";
  els.switch.textContent = unlabelledMode() ? "Warm core" : "Switch";
  els.confirmReviewer.textContent = state.reviewerConfirmed ? "Change" : "OK";
}

function currentList() {
  if (unlabelledMode()) return state.samples;
  return state.byLabel[state.label] || [];
}

function currentSample() {
  return currentList()[state.index];
}

function imageTitle(path) {
  const name = path.split("/").pop();
  if (name.includes("TBanomaly_FourChannels") && !name.includes("3Dplot")) return "TB anomaly four channels";
  if (name.includes("TBanomaly_3Dplot_FourChannels")) return "TB anomaly 3D";
  if (name.includes("VertCrossSection_AlongTrack")) return "Vertical cross section along track";
  if (name.includes("VertCrossSection_CrossTrack")) return "Vertical cross section cross track";
  return name;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function hideOtherReviews() {
  state.otherReviewsVisible = false;
  state.otherReviewsSampleId = "";
  els.otherReviewsPanel.className = "reviewer-panel";
  els.otherReviewsPanel.innerHTML = "";
  els.showOtherReviews.textContent = "Show other reviewers";
}

function formatOtherReview(review) {
  const decision = escapeHtml(review.decision || "-");
  const label = escapeHtml(review.decision === "discard" ? "discard" : (review.reviewed_label || "-"));
  const notes = review.notes ? `<span>Notes: ${escapeHtml(review.notes)}</span>` : "";
  return `
    <div class="reviewer-vote">
      <strong>${escapeHtml(review.reviewer)}</strong>
      <span>Decision: ${decision}</span>
      <span>Label: ${label}</span>
      ${notes}
    </div>
  `;
}

async function showOtherReviews() {
  const sample = currentSample();
  if (!sample) return;
  const reviewer = els.reviewer.value.trim();
  try {
    const payload = await fetchJson(
      `/api/sample_reviews?sample_id=${encodeURIComponent(sample.sample_id)}&current_reviewer=${encodeURIComponent(reviewer)}`
    );
    const reviews = payload.reviews || [];
    state.otherReviewsVisible = true;
    state.otherReviewsSampleId = sample.sample_id;
    els.showOtherReviews.textContent = "Hide other reviewers";
    els.otherReviewsPanel.className = "reviewer-panel visible";
    if (!reviews.length) {
      els.otherReviewsPanel.innerHTML = "<h2>Other reviewer labels</h2><div class=\"sub\">No reviews found for this sample.</div>";
      return;
    }
    els.otherReviewsPanel.innerHTML = `
      <h2>Other reviewer labels</h2>
      <div class="reviewer-votes">${reviews.map(formatOtherReview).join("")}</div>
    `;
  } catch (error) {
    state.otherReviewsVisible = true;
    els.showOtherReviews.textContent = "Hide other reviewers";
    els.otherReviewsPanel.className = "reviewer-panel visible";
    els.otherReviewsPanel.innerHTML = `<h2>Other reviewer labels</h2><div class="status error">${error.message}</div>`;
  }
}

function toggleOtherReviews() {
  if (state.otherReviewsVisible) {
    hideOtherReviews();
    return;
  }
  showOtherReviews();
}

function refreshOtherReviewsIfVisible() {
  if (state.otherReviewsVisible) {
    showOtherReviews();
  }
}

function renderList() {
  const atlas = !!state.config?.atlas_mode;
  const list = atlas ? state.tracks : currentList();
  const rowHeight = 40;
  const start = Math.max(0, Math.floor(els.sampleList.scrollTop / rowHeight) - 6);
  const count = Math.ceil(els.sampleList.clientHeight / rowHeight) + 12;
  const inner = document.createElement("div");
  inner.className = "sample-list-inner";
  inner.style.height = (list.length * rowHeight) + "px";
  list.slice(start, start + count).forEach((item, offset) => {
    const i = start + offset;
    const button = document.createElement("button");
    const selected = atlas ? i === state.trackIndex : i === state.index;
    button.className = "sample-row" + (selected ? " active" : "") + (!atlas && state.reviews[item.sample_id] ? " reviewed" : "");
    button.style.top = (i * rowHeight) + "px";
    const name = atlas ? item : item.sample_id;
    button.title = name;
    button.setAttribute("aria-label", (i + 1) + ": " + name);
    const countText = atlas ? (state.config.atlas_track_counts?.[item] ?? "") : item.images.length;
    button.innerHTML = `<i class="dot"></i><span>${escapeHtml(name)}</span><small>${countText === "" ? "" : countText + (atlas ? " samples" : " img")}</small>`;
    button.addEventListener("click", () => {
      if (atlas) selectTrack(i);
      else { state.index = i; render(); refreshOtherReviewsIfVisible(); }
    });
    inner.appendChild(button);
  });
  els.sampleList.replaceChildren(inner);
}

function revealCurrentSample() {
  const selectedIndex = state.config?.atlas_mode ? state.trackIndex : state.index;
  if (selectedIndex < 0) return;
  const top = selectedIndex * 40;
  if (top < els.sampleList.scrollTop) els.sampleList.scrollTop = top;
  else if (top + 40 > els.sampleList.scrollTop + els.sampleList.clientHeight)
    els.sampleList.scrollTop = top + 40 - els.sampleList.clientHeight;
}

function render() {
  if (!unlabelledMode() && !currentList().length) {
    const fallbackLabel = Object.keys(state.byLabel).find(label => state.byLabel[label].length > 0);
    if (fallbackLabel !== undefined && Number(fallbackLabel) !== state.label) {
      state.label = Number(fallbackLabel);
      state.index = 0;
      history.replaceState(null, "", `?label=${state.label}`);
    }
  }
  updateControls();
  const list = currentList();
  if (!list.length) {
    els.position.textContent = state.config?.atlas_mode
      ? `${Math.max(0, state.trackIndex + 1)} / ${state.tracks.length}` : "0 / 0";
    els.sampleTitle.textContent = state.config?.atlas_mode
      ? (state.trackIndex < 0 ? "Select a track" : "No images in this track") : "No samples";
    els.badges.innerHTML = "";
    els.images.innerHTML = "";
    els.samplePicker.hidden = true;
    revealCurrentSample();
    renderList();
    els.reviewedCount.textContent = "0 reviewed";
    els.meter.style.width = "0%";
    return;
  }
  state.index = Math.max(0, Math.min(state.index, list.length - 1));
  const sample = currentSample();
  const review = state.reviews[sample.sample_id];
  const reviewedInLabel = list.filter(item => state.reviews[item.sample_id]).length;

  els.tab0.classList.toggle("active", state.label === 0);
  els.tab1.classList.toggle("active", state.label === 1);
  els.position.textContent = `${state.index + 1} / ${list.length}`;
  if (state.config?.atlas_mode) {
    els.samplePicker.hidden = false;
    if (els.samplePicker.options.length !== list.length || els.samplePicker.dataset.track !== state.config.atlas_track) {
      els.samplePicker.replaceChildren(...list.map((item, i) => new Option(`${i + 1}. ${sampleDisplayName(item)}`, String(i))));
      els.samplePicker.dataset.track = state.config.atlas_track;
    }
    els.samplePicker.value = String(state.index);
  } else els.samplePicker.hidden = true;
  els.reviewedCount.textContent = `${reviewedInLabel} of ${list.length} reviewed`;
  els.meter.style.width = `${list.length ? (reviewedInLabel / list.length) * 100 : 0}%`;
  els.sampleTitle.textContent = state.config?.atlas_mode ? sampleDisplayName(sample) : sample.sample_id;
  els.sampleTitle.title = sample.sample_id;
  els.notes.value = review ? review.notes || "" : "";

  let reviewedBadge = `<span class="badge">Not reviewed</span>`;
  if (review) {
    reviewedBadge = review.decision === "discard"
      ? `<span class="badge saved">Discarded</span>`
      : `<span class="badge saved">Saved: ${review.reviewed_label}</span>`;
  }
  els.badges.innerHTML = `
    <span class="badge label${sample.label}">${sample.label === null ? "Unlabelled" : "Original label " + sample.label}</span>
    <span class="badge">unlabelled ${sample.unlabelled}</span>
    ${reviewedBadge}
  `;

  els.images.innerHTML = sample.images.map(path => `
    <article class="image-panel">
      <h2>${imageTitle(path)}</h2>
      <div class="image-wrap"><img src="${escapeHtml(/^https?:\/\//.test(path) ? path : '/image?path=' + encodeURIComponent(path))}" alt="${escapeHtml(imageTitle(path))}"></div>
    </article>
  `).join("");
  revealCurrentSample();
  renderList();
  updateControls();
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok || payload.ok === false) throw new Error(payload.error || response.statusText);
  return payload;
}

async function loadReviews() {
  const reviewer = els.reviewer.value.trim();
  if (!reviewer) {
    state.reviews = {};
    state.reviewerConfirmed = false;
    render();
    return;
  }
  try {
    const payload = await fetchJson(`/api/reviews?reviewer=${encodeURIComponent(reviewer)}`);
    state.reviews = payload.reviews || {};
    state.reviewerConfirmed = true;
    setStatus(`Reviews loaded for ${reviewer}`);
  } catch (error) {
    state.reviews = {};
    state.reviewerConfirmed = false;
    setStatus(error.message, "error");
  }
  render();
}

async function confirmReviewer() {
  const reviewer = els.reviewer.value.trim();
  if (!reviewer) {
    state.reviewerConfirmed = false;
    setStatus("Enter a reviewer name and press OK.", "error");
    els.reviewer.focus();
    render();
    return;
  }
  localStorage.setItem("visualReview.reviewer", reviewer);
  state.completionAlertShown = false;
  await loadReviews();
  maybeShowCompletionMessage();
}

function maybeShowCompletionMessage() {
  if (state.completionAlertShown || !state.samples.length) return;
  const reviewedCount = state.samples.filter(sample => state.reviews[sample.sample_id]).length;
  if (reviewedCount !== state.samples.length) return;
  state.completionAlertShown = true;
  window.alert(`Review complete: all ${state.samples.length} samples${state.config.atlas_mode ? " in " + state.config.atlas_track : ""} have been reviewed.`);
}

async function save(reviewedLabel, action = "") {
  if (controlsDisabled()) {
    setStatus("Confirm the reviewer name with OK first.", "error");
    els.reviewer.focus();
    return;
  }
  const sample = currentSample();
  const reviewer = els.reviewer.value.trim();
  if (!reviewer) {
    setStatus("Enter a reviewer name before saving.", "error");
    els.reviewer.focus();
    return;
  }
  try {
    const payload = await fetchJson("/api/review", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        reviewer,
        sample_id: sample.sample_id,
        reviewed_label: reviewedLabel,
        action,
        notes: els.notes.value,
      }),
    });
    state.reviews[sample.sample_id] = {
      reviewed_label: String(reviewedLabel),
      decision: payload.decision,
      notes: els.notes.value,
    };
    setStatus(`Saved ${payload.decision} to ${payload.reviewer_file}`, "ok");
    maybeShowCompletionMessage();
    if (state.index < currentList().length - 1) state.index += 1;
    render();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

function setLabel(label) {
  state.label = label;
  state.index = 0;
  history.replaceState(null, "", `?label=${label}`);
  render();
  refreshOtherReviewsIfVisible();
}

async function next(delta) {
  if (state.loading) return;
  const list = currentList();
  if (state.config.atlas_mode && (state.index + delta < 0 || state.index + delta >= list.length)) {
    await selectTrack(state.trackIndex + delta, delta < 0);
    return;
  }
  state.index = Math.max(0, Math.min(state.index + delta, list.length - 1));
  render();
  refreshOtherReviewsIfVisible();
}

function jumpUnreviewed() {
  const list = currentList();
  const start = state.index + 1;
  const nextIndex = list.findIndex((sample, i) => i >= start && !state.reviews[sample.sample_id]);
  if (nextIndex >= 0) {
    state.index = nextIndex;
  } else {
    const wrapIndex = list.findIndex(sample => !state.reviews[sample.sample_id]);
    if (wrapIndex >= 0) state.index = wrapIndex;
  }
  render();
  refreshOtherReviewsIfVisible();
}

async function init() {
  let scrollFrame = 0;
  els.sampleList.addEventListener("scroll", () => {
    if (!scrollFrame) scrollFrame = requestAnimationFrame(() => { scrollFrame = 0; renderList(); });
  });
  new ResizeObserver(() => renderList()).observe(els.sampleList);
  const savedReviewer = localStorage.getItem("visualReview.reviewer") || "";
  els.reviewer.value = savedReviewer;
  const payload = await fetchJson("/api/samples");
  state.samples = payload.samples;
  state.config = payload.config;
  state.byLabel = {
    0: state.samples.filter(sample => sample.label === 0),
    1: state.samples.filter(sample => sample.label === 1),
  };
  els.source.textContent = payload.config.atlas_mode
    ? `${sourceName()} · ${payload.config.atlas_track || "no track selected"}`
    : payload.config.flat_mode ? `${sourceName()} · ${state.samples.length} images`
    : `${payload.config.input_csv} · ${state.samples.length} samples`;

  document.querySelectorAll(".tabs button").forEach(button => {
    button.addEventListener("click", () => setLabel(Number(button.dataset.label)));
  });
  els.confirmReviewer.addEventListener("click", confirmReviewer);
  els.reviewer.addEventListener("input", () => {
    state.reviewerConfirmed = false;
    updateControls();
    setStatus("Press OK to start reviewing with this name.");
  });
  els.reviewer.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault();
      confirmReviewer();
    }
  });
  els.prev.addEventListener("click", () => next(-1));
  els.next.addEventListener("click", () => next(1));
  els.samplePicker.addEventListener("change", () => {
    state.index = Number(els.samplePicker.value);
    render();
    refreshOtherReviewsIfVisible();
  });
  els.keep.addEventListener("click", () => save(unlabelledMode() ? 0 : currentSample().label));
  els.switch.addEventListener("click", () => save(unlabelledMode() ? 1 : currentSample().label === 0 ? 1 : 0));
  els.discard.addEventListener("click", () => save(currentSample().label, "discard"));
  els.showOtherReviews.addEventListener("click", toggleOtherReviews);
  els.jumpUnreviewed.addEventListener("click", jumpUnreviewed);
  window.addEventListener("keydown", event => {
    if (els.folderDialog.open) return;
    if (event.target.matches("input, textarea")) return;
    if (event.key === "ArrowLeft") next(-1);
    if (event.key === "ArrowRight") next(1);
    if (!controlsDisabled() && event.key.toLowerCase() === "k") save(unlabelledMode() ? 0 : currentSample().label);
    if (!controlsDisabled() && event.key.toLowerCase() === "s") save(unlabelledMode() ? 1 : currentSample().label === 0 ? 1 : 0);
  });

  state.reviewerConfirmed = false;
  document.getElementById("connectAtlas").addEventListener("click", connectAtlas);
  document.getElementById("openLocalFolder").addEventListener("click", () => openFolderDialog("tracks"));
  document.getElementById("openFlatFolder").addEventListener("click", () => openFolderDialog("images"));
  document.getElementById("cancelFolder").addEventListener("click", () => els.folderDialog.close());
  els.browseFolder.addEventListener("click", async () => {
    els.browseFolder.disabled = true;
    try {
      const payload = await fetchJson("/api/local/choose", {method: "POST"});
      if (payload.folder) els.folderPath.value = payload.folder;
    } catch (error) { els.folderError.textContent = error.message; }
    finally { els.browseFolder.disabled = false; }
  });
  els.loadFolder.addEventListener("click", connectLocalFolder);
  document.querySelector(".tabs").hidden = unlabelledMode();
  state.tracks = (await fetchJson("/api/atlas/tracks")).tracks;
  state.trackIndex = state.tracks.indexOf(state.config.atlas_track);
  setStatus("Enter a reviewer name and press OK to start.");
  render();
}

function openFolderDialog(kind) {
  if (state.folderKind !== kind) els.folderPath.value = "";
  state.folderKind = kind;
  els.folderDialogTitle.textContent = kind === "tracks" ? "Open local tracks" : "Open image folder";
  els.folderDialogLabel.textContent = kind === "tracks"
    ? "Folder containing Track* directories" : "Folder containing images";
  els.folderError.textContent = "";
  els.folderDialog.showModal();
}

async function connectAtlas() {
  const button = document.getElementById("connectAtlas");
  button.disabled = true;
  state.loading = true;
  updateControls();
  setStatus("Connecting to atlas...");
  try {
    const payload = await fetchJson("/api/atlas/connect", {method: "POST"});
    state.tracks = payload.tracks;
    const dataset = await fetchJson("/api/samples");
    state.samples = dataset.samples;
    state.config = dataset.config;
    state.index = 0;
    state.trackIndex = state.tracks.indexOf(state.config.atlas_track);
    state.completionAlertShown = false;
    document.querySelector(".tabs").hidden = true;
    els.source.textContent = "Atlas · " + state.tracks.length + " tracks";
    hideOtherReviews();
    state.reviews = {};
    if (state.reviewerConfirmed) await loadReviews();
    render();
    setStatus("Select a track to load its images.");
  } catch (error) { setStatus(error.message, "error"); }
  finally { button.disabled = false; state.loading = false; updateControls(); }
}

async function connectLocalFolder() {
  const folder = els.folderPath.value.trim();
  if (!folder) { els.folderError.textContent = "Choose a folder first."; els.folderPath.focus(); return; }
  els.folderError.textContent = "";
  els.loadFolder.disabled = true;
  state.loading = true;
  updateControls();
  try {
    const flat = state.folderKind === "images";
    const payload = await fetchJson(flat ? "/api/flat/connect" : "/api/local/connect", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({folder}),
    });
    state.tracks = payload.tracks || [];
    const dataset = await fetchJson("/api/samples");
    state.samples = dataset.samples;
    state.config = dataset.config;
    state.index = 0;
    state.trackIndex = -1;
    state.completionAlertShown = false;
    document.querySelector(".tabs").hidden = true;
    els.source.textContent = `${sourceName()} · ${flat ? state.samples.length + " images" : state.tracks.length + " tracks"}`;
    hideOtherReviews();
    state.reviews = {};
    if (state.reviewerConfirmed) await loadReviews();
    els.folderDialog.close();
    render();
    setStatus(flat ? `${state.samples.length} images loaded.` : "Select a track to load its images.");
  } catch (error) { els.folderError.textContent = error.message; }
  finally { els.loadFolder.disabled = false; state.loading = false; updateControls(); }
}

async function selectTrack(index, lastSample = false) {
  if (state.loading || index < 0 || index >= state.tracks.length) return;
  const track = state.tracks[index];
  state.loading = true;
  updateControls();
  setStatus("Loading " + track + "...");
  try {
    const payload = await fetchJson("/api/atlas/track", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({track}),
    });
    state.samples = payload.samples;
    state.config = payload.config;
    state.trackIndex = index;
    state.index = lastSample ? Math.max(0, state.samples.length - 1) : 0;
    els.source.textContent = `${sourceName()} · ${track} · ${state.samples.length} samples`;
    state.completionAlertShown = false;
    if (state.reviewerConfirmed) await loadReviews();
    render();
    refreshOtherReviewsIfVisible();
    setStatus(state.samples.length ? track + " loaded." : "No images in " + track + ".");
  } catch (error) { setStatus(error.message, "error"); }
  finally { state.loading = false; updateControls(); }
}
init().catch(error => setStatus(error.message, "error"));
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    state: AppState

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text: str, content_type: str = "text/html; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/api/atlas/tracks":
                self.send_json({"ok": True, "tracks": self.state.atlas_tracks})
            elif parsed.path == "/":
                self.send_text(HTML_PAGE)
            elif parsed.path == "/api/samples":
                self.handle_samples()
            elif parsed.path == "/api/reviews":
                self.handle_reviews(parsed)
            elif parsed.path == "/api/sample_reviews":
                self.handle_sample_reviews(parsed)
            elif parsed.path == "/image":
                self.handle_image(parsed)
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except Exception as exc:  # noqa: BLE001 - report local dashboard errors as JSON.
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/api/atlas/connect":
                with self.state.atlas_lock:
                    tracks = connect_atlas(self.state)
                self.send_json({"ok": True, "tracks": tracks})
                return
            if parsed.path == "/api/local/choose":
                self.send_json({"ok": True, "folder": choose_local_folder()})
                return
            if parsed.path == "/api/local/connect":
                length = int(self.headers.get("Content-Length") or "0")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                with self.state.atlas_lock:
                    tracks = connect_local_folder(self.state, str(payload.get("folder", "")))
                self.send_json({"ok": True, "tracks": tracks})
                return
            if parsed.path == "/api/flat/connect":
                length = int(self.headers.get("Content-Length") or "0")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                with self.state.atlas_lock:
                    count = connect_flat_folder(self.state, str(payload.get("folder", "")))
                self.send_json({"ok": True, "count": count})
                return
            if parsed.path == "/api/atlas/track":
                length = int(self.headers.get("Content-Length") or "0")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                with self.state.atlas_lock:
                    load_atlas_track(self.state, str(payload.get("track", "")))
                self.handle_samples()
                return
            if parsed.path != "/api/review":
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            result = save_review(self.state, payload)
            self.send_json(result)
        except Exception as exc:  # noqa: BLE001 - report local dashboard errors as JSON.
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def handle_samples(self) -> None:
        state = self.state
        self.send_json(
            {
                "ok": True,
                "samples": state.samples,
                "config": {
                    "input_csv": display_path(state.input_csv, state.repo_root),
                    "data_root": display_path(state.data_root, state.repo_root),
                    "output_dir": display_path(state.output_dir, state.repo_root),
                    "missing_images": state.missing_images,
                    "remote_base_url": state.remote_base_url,
                    "atlas_mode": state.atlas_mode,
                    "local_mode": state.local_mode,
                    "local_root": str(state.local_root) if state.local_root else "",
                    "flat_mode": state.flat_mode,
                    "flat_root": str(state.flat_root) if state.flat_root else "",
                    "atlas_track": state.atlas_track,
                    "atlas_track_counts": {track: len(samples) for track, samples in state.atlas_cache.items()},
                },
            }
        )

    def handle_reviews(self, parsed: urllib.parse.ParseResult) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        reviewer = query.get("reviewer", [""])[0]
        if not reviewer.strip():
            self.send_json({"ok": True, "reviews": {}})
            return
        self.send_json({"ok": True, "reviews": labels_for_reviewer(self.state, reviewer)})

    def handle_sample_reviews(self, parsed: urllib.parse.ParseResult) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        sample_id = query.get("sample_id", [""])[0]
        current_reviewer = query.get("current_reviewer", [""])[0]
        if not sample_id:
            self.send_json({"ok": False, "error": "Missing sample_id"}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json(
            {
                "ok": True,
                "reviews": reviews_for_sample(self.state, sample_id, current_reviewer),
            }
        )

    def handle_image(self, parsed: urllib.parse.ParseResult) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        rel_path = query.get("path", [""])[0]
        if not rel_path:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing path")
            return

        path = (self.state.repo_root / rel_path).resolve()
        data_root = self.state.data_root.resolve()
        if not str(path).startswith(str(data_root) + os.sep):
            self.send_error(HTTPStatus.FORBIDDEN, "Image path outside data root")
            return
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Image not found")
            return

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    args = parse_args()
    root = project_root()
    input_csv = args.input_csv or args.csv
    state = AppState(
        repo_root=root,
        input_csv=resolve_under_root(root, input_csv),
        data_root=resolve_under_root(root, args.data_root),
        output_dir=resolve_under_root(root, args.output_dir),
        remote_base_url=args.remote_base_url,
    )
    state.atlas_mode = args.atlas
    if not args.atlas:
        load_samples(state)

    DashboardHandler.state = state
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Loaded {len(state.samples)} samples from {display_path(state.input_csv, root)}")
    print(f"Missing image mappings: {len(state.missing_images)}")
    print(f"Saving reviews to {display_path(state.output_dir, root)}")
    print(f"Serving dashboard at {url}")
    if not args.no_browser:
        time.sleep(0.2)
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
