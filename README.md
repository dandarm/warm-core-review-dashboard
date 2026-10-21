# Warm Core Review Dashboard

A local dashboard for inspecting scientific images and reviewing binary labels:
negatives (0) and warm-core positives (1). Includes navigation, notes,
Keep / Switch / Discard decisions, and optional comparison with other reviewers.

## Quick start

Requires Python 3.8 or later and a browser. No third-party packages are needed.
From the project directory:

```bash
python3 scripts/visual_review_dashboard.py --atlas
```

The browser opens http://127.0.0.1:8765. Choose **Open local folder** for
track folders, **Open image folder** for loose images, or **Connect to atlas**
to use the public site.
None of these options needs an input CSV listing images or labels. Enter a reviewer
name and press OK to enable decisions. Stop the server with Ctrl+C.
Add `--no-browser` to skip opening the browser or `--port 8766` to change ports.

## Local track folders

Start the dashboard as shown in Quick start, then click **Open local folder** next to
**Connect to atlas**. Use **Browse** to select a folder on the computer running
the dashboard, or enter its absolute path if the native picker is unavailable.
The folder may contain `track00000841/MEDICANES-WC-DC/Images/` or
`Track0841/Images/` directories. The track list is loaded first; image files
are read only when you select a track. Image filenames must contain a date,
four-digit time, and satellite name, for example
`TBanomaly_18-Nov-2017_1219UTC_NPP.png`. JPG and PNG files are supported.
Local reviews are saved separately under `reviews/local/` for each folder.

Some saved sample IDs end in `.npz` for compatibility with older reviews, but
no NPZ files are needed. The dashboard shows the date, time, and satellite.

## Loose image folders

Click **Open image folder** and select a folder containing JPG or PNG files.
Only images directly in that folder are listed. Each image appears separately,
in natural filename order, and receives its own label. Use the list or the
previous/next buttons to navigate. Reviews are saved under `reviews/flat/`,
separately for each folder.

## Public atlas images

Press **Connect to atlas** to see tracks from ID 841 onward. Click a track to
load its samples; images are requested only for the selected track.
The sample selector above the images jumps within the chosen track. The
previous/next buttons move between samples and request the adjacent track
only when crossing a track boundary. Empty tracks show "No images in this
track"; the arrow can then move to the next or previous track.

Samples are grouped by date, time and satellite, with no initial label.
Confirm your reviewer name, then assign **Negative** (0), **Warm core** (1),
or **Discard**. Completion refers to the selected track. The sidebar uses a
virtualized list so its scrollbar spans the full track list. Images scroll
independently, leaving the sample title and review controls visible.

Track image indexes are cached in the server for the session, including empty
tracks, so revisiting a track makes no new site request. A failed request
leaves the current track visible; select the desired track again after the
site recovers. Image files are fetched by the browser only when displayed.
Each index request has a 15-second timeout and is attempted once. Restarting
the server clears the in-memory cache.

On Windows, `unknown url type: https` normally means the selected Python
installation lacks working SSL support. Check it with:

```powershell
py -c "import ssl; print(ssl.OPENSSL_VERSION)"
```

If that fails, install a complete Python build with SSL support and use that
interpreter to run the dashboard. The dashboard reports this problem directly
instead of retrying it as a network timeout.

Reviews are saved to `reviews/atlas/` in this mode. New labels are saved as
`0` or `1`; discarded samples use `discard`.
Reopening a track reloads the selected reviewer's saved decisions.
The dataset is shared by clients connected to the same local server;
use one browser session per server.

## Review files

`reviewer_<name>.csv` stores each reviewer's latest decision with exactly two
columns: `sample_id,label`. Labels are `0`, `1`, or `discard`.
`review_events.csv` records the history with `reviewer,sample_id,label`.
Optional notes are stored separately in `reviewer_notes.json`.
Existing review files are migrated automatically when the dashboard loads.
Discard does not delete images. Samples labelled `discard` should be excluded
when exporting a reviewed dataset.

Show other reviewers reads `reviewer_*.csv` from the same output directory.
To resume a review, select the same local folder or reconnect to the atlas
and enter the same reviewer name. The display remains enabled while navigating.

The dashboard is intended for local use, without authentication.
The server listens on localhost by default.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Repository contents

This project runs independently of the training repository.
`scripts/` contains the dashboard, `examples/` contains four samples and
18 images, and `tests/` contains integration checks.
`reviews/` and `data/` are excluded from Git. Personal reviews are not bundled.

The example images originate from the scientific dataset described in
`examples/README.md`. No redistribution license is granted by including them
here. Choose a code license and confirm image redistribution rights before
making this repository public.
