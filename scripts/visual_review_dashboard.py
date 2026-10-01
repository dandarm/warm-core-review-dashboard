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
import html
import json
import mimetypes
import os
import re
import sys
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_CSV = Path("examples/samples.csv")
DEFAULT_DATA_ROOT = Path("examples/images")
DEFAULT_OUTPUT_DIR = Path("reviews/demo")


@dataclass
class AppState:
    repo_root: Path
    input_csv: Path
    data_root: Path
    output_dir: Path
    samples: list[dict[str, Any]] = field(default_factory=list)
    missing_images: list[str] = field(default_factory=list)


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


def sample_key(sample_id: str) -> tuple[str, str, str, str]:
    parts = Path(sample_id).parts
    if len(parts) < 2:
        raise ValueError(f"sample_id does not look like Track*/file.npz: {sample_id}")

    track = parts[0]
    stem = Path(sample_id).stem
    key = stem.removeprefix("WP2300_Output_").removeprefix("Thermodynamical_Microphysical_")
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


def load_samples(state: AppState) -> None:
    if not state.input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {state.input_csv}")
    if not state.data_root.exists():
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
        for index, row in enumerate(reader):
            sample_id = row["sample_id"]
            images = map_images(state.data_root, sample_id)
            if not images:
                missing_images.append(sample_id)
            image_paths = [display_path(path, state.repo_root) for path in images]
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
        return {row["sample_id"]: row for row in csv.DictReader(handle)}


def write_reviewer_rows(path: Path, rows_by_sample: dict[str, dict[str, str]]) -> None:
    fieldnames = [
        "timestamp_utc",
        "reviewer",
        "sample_id",
        "original_label",
        "reviewed_label",
        "decision",
        "notes",
        "unlabelled",
        "source_csv",
        "image_paths",
    ]
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in sorted(rows_by_sample.values(), key=lambda item: item["sample_id"]):
            writer.writerow({field: row.get(field, "") for field in fieldnames})
    tmp_path.replace(path)


def append_audit_log(state: AppState, row: dict[str, str]) -> None:
    path = state.output_dir / "review_events.csv"
    fieldnames = [
        "timestamp_utc",
        "reviewer",
        "sample_id",
        "original_label",
        "reviewed_label",
        "decision",
        "notes",
        "unlabelled",
        "source_csv",
        "image_paths",
    ]
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in fieldnames})


def save_review(state: AppState, payload: dict[str, Any]) -> dict[str, Any]:
    reviewer = str(payload.get("reviewer") or "").strip()
    sample_id = str(payload.get("sample_id") or "").strip()
    action = str(payload.get("action") or "").strip().lower()
    notes = str(payload.get("notes") or "").strip()

    sample = next((item for item in state.samples if item["sample_id"] == sample_id), None)
    if not sample:
        raise ValueError(f"Unknown sample_id: {sample_id}")

    if action == "discard":
        reviewed_label = int(payload.get("reviewed_label", sample["label"]))
        decision = "discard"
    else:
        reviewed_label = int(payload.get("reviewed_label"))
        if reviewed_label not in (0, 1):
            raise ValueError("reviewed_label must be 0 or 1.")
        decision = "keep" if reviewed_label == sample["label"] else "switch"

    row = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reviewer": reviewer,
        "sample_id": sample_id,
        "original_label": str(sample["label"]),
        "reviewed_label": str(reviewed_label),
        "decision": decision,
        "notes": notes,
        "unlabelled": str(sample["unlabelled"]),
        "source_csv": display_path(state.input_csv, state.repo_root),
        "image_paths": "|".join(sample["images"]),
    }

    path = reviewer_file(state, reviewer)
    rows = read_reviewer_rows(path)
    rows[sample_id] = row
    write_reviewer_rows(path, rows)
    append_audit_log(state, row)
    return {"ok": True, "decision": decision, "reviewer_file": display_path(path, state.repo_root)}


def labels_for_reviewer(state: AppState, reviewer: str) -> dict[str, dict[str, str]]:
    path = reviewer_file(state, reviewer)
    return read_reviewer_rows(path)


def reviews_for_sample(state: AppState, sample_id: str, current_reviewer: str = "") -> list[dict[str, str]]:
    current_slug = reviewer_slug(current_reviewer) if current_reviewer.strip() else ""
    rows: list[dict[str, str]] = []
    for path in sorted(state.output_dir.glob("reviewer_*.csv")):
        reviewer_from_file = path.stem.removeprefix("reviewer_")
        if current_slug and reviewer_from_file == current_slug:
            continue
        reviewer_rows = read_reviewer_rows(path)
        row = reviewer_rows.get(sample_id)
        if not row:
            continue
        item = {
            "reviewer": row.get("reviewer") or reviewer_from_file,
            "reviewed_label": row.get("reviewed_label", ""),
            "decision": row.get("decision", ""),
            "notes": row.get("notes", ""),
            "timestamp_utc": row.get("timestamp_utc", ""),
        }
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
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div>
        <h1>Warm Core Visual Review</h1>
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
      <div class="sample-list" id="sampleList" aria-label="Samples"></div>
    </aside>
    <section class="viewer">
      <div class="sample-head">
        <div>
          <div class="sample-title" id="sampleTitle"></div>
          <div class="badges" id="badges"></div>
        </div>
        <div class="nav">
          <button id="showOtherReviews">Show other reviewers</button>
          <button id="jumpUnreviewed">Next unreviewed sample</button>
        </div>
      </div>
      <div class="reviewer-panel" id="otherReviewsPanel"></div>
      <div class="image-grid" id="images"></div>
    </section>
  </main>
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
};

function setStatus(text, cls = "") {
  els.status.textContent = text;
  els.status.className = "status" + (cls ? " " + cls : "");
}

function controlsDisabled() {
  return !state.reviewerConfirmed;
}

function updateControls() {
  const disabled = controlsDisabled();
  els.keep.disabled = disabled;
  els.switch.disabled = disabled;
  els.discard.disabled = disabled;
  els.notes.disabled = disabled;
  els.confirmReviewer.textContent = state.reviewerConfirmed ? "Change" : "OK";
}

function currentList() {
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
  const list = currentList();
  els.sampleList.innerHTML = "";
  list.forEach((sample, i) => {
    const button = document.createElement("button");
    button.className = "sample-row" + (i === state.index ? " active" : "") + (state.reviews[sample.sample_id] ? " reviewed" : "");
    button.innerHTML = `<i class="dot"></i><span>${sample.sample_id}</span><small>${sample.images.length} img</small>`;
    button.addEventListener("click", () => {
      state.index = i;
      render();
      refreshOtherReviewsIfVisible();
    });
    els.sampleList.appendChild(button);
  });
}

function render() {
  if (!currentList().length) {
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
    els.position.textContent = "0 / 0";
    els.sampleTitle.textContent = "No samples";
    els.badges.innerHTML = "";
    els.images.innerHTML = "";
    return;
  }
  state.index = Math.max(0, Math.min(state.index, list.length - 1));
  const sample = currentSample();
  const review = state.reviews[sample.sample_id];
  const reviewedInLabel = list.filter(item => state.reviews[item.sample_id]).length;

  els.tab0.classList.toggle("active", state.label === 0);
  els.tab1.classList.toggle("active", state.label === 1);
  els.position.textContent = `${state.index + 1} / ${list.length}`;
  els.reviewedCount.textContent = `${reviewedInLabel} of ${list.length} reviewed`;
  els.meter.style.width = `${list.length ? (reviewedInLabel / list.length) * 100 : 0}%`;
  els.sampleTitle.textContent = sample.sample_id;
  els.notes.value = review ? review.notes || "" : "";

  let reviewedBadge = `<span class="badge">Not reviewed</span>`;
  if (review) {
    reviewedBadge = review.decision === "discard"
      ? `<span class="badge saved">Discarded</span>`
      : `<span class="badge saved">Saved: ${review.reviewed_label}</span>`;
  }
  els.badges.innerHTML = `
    <span class="badge label${sample.label}">Original label ${sample.label}</span>
    <span class="badge">unlabelled ${sample.unlabelled}</span>
    ${reviewedBadge}
  `;

  els.images.innerHTML = sample.images.map(path => `
    <article class="image-panel">
      <h2>${imageTitle(path)}</h2>
      <div class="image-wrap"><img src="/image?path=${encodeURIComponent(path)}" alt="${imageTitle(path)}"></div>
    </article>
  `).join("");
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
  window.alert(`Review complete: all ${state.samples.length} samples have been reviewed.`);
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

function next(delta) {
  const list = currentList();
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
  const savedReviewer = localStorage.getItem("visualReview.reviewer") || "";
  els.reviewer.value = savedReviewer;
  const payload = await fetchJson("/api/samples");
  state.samples = payload.samples;
  state.config = payload.config;
  state.byLabel = {
    0: state.samples.filter(sample => sample.label === 0),
    1: state.samples.filter(sample => sample.label === 1),
  };
  els.source.textContent = `${payload.config.input_csv} · ${state.samples.length} samples`;

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
  els.keep.addEventListener("click", () => save(currentSample().label));
  els.switch.addEventListener("click", () => save(currentSample().label === 0 ? 1 : 0));
  els.discard.addEventListener("click", () => save(currentSample().label, "discard"));
  els.showOtherReviews.addEventListener("click", toggleOtherReviews);
  els.jumpUnreviewed.addEventListener("click", jumpUnreviewed);
  window.addEventListener("keydown", event => {
    if (event.target.matches("input, textarea")) return;
    if (event.key === "ArrowLeft") next(-1);
    if (event.key === "ArrowRight") next(1);
    if (!controlsDisabled() && event.key.toLowerCase() === "k") save(currentSample().label);
    if (!controlsDisabled() && event.key.toLowerCase() === "s") save(currentSample().label === 0 ? 1 : 0);
  });

  state.reviewerConfirmed = false;
  setStatus("Enter a reviewer name and press OK to start.");
  render();
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
            if parsed.path == "/":
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
    )
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
