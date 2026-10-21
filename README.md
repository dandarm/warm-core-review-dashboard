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

The browser opens http://127.0.0.1:8765. Choose **Open local folder** to
review a folder of track images, or **Connect to atlas** to use the public site.
Neither option needs an input CSV listing images or labels. Enter a reviewer
name and press OK to enable decisions. Stop the server with Ctrl+C.
Add `--no-browser` to skip opening the browser or `--port 8766` to change ports.

## Optional CSV mode

Use a CSV only when you already have a labelled sample selection to review:

```bash
python3 scripts/visual_review_dashboard.py /path/to/samples.csv \
  --data-root /path/to/images \
  --output-dir /path/to/reviews/dataset
```

You can also use `--csv /path/to/samples.csv`. Relative paths are resolved
from this project's root directory, regardless of the current working directory.
Use a separate output directory for each dataset.

The CSV must contain `sample_id,label` or `sample_id,true_label` headers.
Labels must be 0 or 1. The optional `unlabelled` column defaults to 0.

```csv
sample_id,label
Track123/WP2300_Output_18-Nov-2017_1219_NPP.npz,1
```

NPZ files are not required: their names identify the corresponding images.
Supported prefixes are `WP2300_Output_` and
`Thermodynamical_Microphysical_`, followed by date, time, and platform.
The dashboard searches `--data-root/Track123/Images/` when that directory
exists, otherwise it searches directly within `--data-root`.
Image names must contain the date, time, and platform in that order, for example
`TBanomaly_18-Nov-2017_1219UTC_NPP.png`.
Supported extensions are lowercase `.jpg`, `.jpeg`, and `.png`.
All matching images are displayed. The terminal reports the number of samples
without matching images. Arbitrary image filenames are not supported.

## Local track folders (no CSV)

Start the dashboard as shown in Quick start, then click **Open local folder** next to
**Connect to atlas**. Use **Browse** to select a folder on the computer running
the dashboard, or enter its absolute path if the native picker is unavailable.
The folder may contain `track00000841/MEDICANES-WC-DC/Images/` or
`Track0841/Images/` directories. The track list is loaded first; image files
are read only when you select a track. Image filenames must contain a date,
four-digit time, and satellite name as described above. Local reviews are saved
separately under `reviews/local/` for each selected folder.

The `.npz` sample ID used in review CSVs is generated from image names in both
local and public-atlas modes for compatibility with existing reviews; no NPZ
file is read or required. The dashboard displays the date, time, and satellite
instead of that ID.

## Public atlas images

### Label directly from the atlas (no CSV)

```bash
python3 scripts/visual_review_dashboard.py --atlas
```

Press **Connect to atlas** to request only the list of track names. The left
column displays tracks from ID 841 onward without fetching their image directories. Click a
track to load its samples; the server requests only that track's image index.
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

Reviews are saved to `reviews/atlas/` in this mode, including when connecting
from a CSV session. New labels are saved as `0` or `1`; discarded samples use
`discard`.
Reopening a track reloads the selected reviewer's saved decisions.
The dataset is shared by clients connected to the same local server;
use one browser session per server.

### Use a CSV selection with online images

Use the same input CSV and load images directly from the IFREMER public atlas:

```bash
python3 scripts/visual_review_dashboard.py examples/samples.csv \
  --remote-base-url https://data-cersat.ifremer.fr/data/storms/medicanes/storm-atlas/climatology/ \
  --output-dir reviews/atlas
```

Replace `examples/samples.csv` with your dataset CSV. In this mode
`--data-root` is ignored. For example, `Track0841` maps to
`track00000841/MEDICANES-WC-DC/Images/`. Images are matched by date,
overpass time and satellite, including names with or without `UTC`.
Only tracks referenced in the CSV are queried; each directory listing is
fetched once at startup. The browser loads matching images directly from the
public site as samples are displayed. An internet connection is required.
Images are not copied into the project; review CSVs remain local and do not
record image URLs. Missing directories are reported as warnings; connection
failures stop startup with the failing URL so they are not mistaken for
missing images. Restart the dashboard to refresh the directory listings.

## Review files

`reviewer_<name>.csv` stores each reviewer's latest decision with exactly two
columns: `sample_id,label`. Labels are `0`, `1`, or `discard`.
`review_events.csv` records the history with `reviewer,sample_id,label`.
Optional notes are stored separately in `reviewer_notes.json`.
Existing review files are migrated automatically when the dashboard loads.
Keep confirms the label, Switch inverts it, and Discard does not delete images
or change the input CSV: dataset export code must exclude samples labelled
`discard`.

Show other reviewers reads `reviewer_*.csv` from the same output directory.
To resume existing reviews, copy those files into your chosen `--output-dir`
and use matching `sample_id` identifiers. The display remains enabled while
navigating within the session.

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
