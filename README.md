# Warm Core Review Dashboard

A local dashboard for inspecting scientific images and reviewing binary labels:
negatives (0) and warm-core positives (1). Includes navigation, notes,
Keep / Switch / Discard decisions, and optional comparison with other reviewers.

## Quick start

Requires Python 3.10 or later and a browser. No third-party packages are needed.
From the project directory:

```bash
python3 scripts/visual_review_dashboard.py
```

For the current local installation, the complete launch command is:

```bash
cd /media/fenrir/disk1/danieleda/wc-detection/warm-core-review-dashboard
python3 scripts/visual_review_dashboard.py
```

The browser opens http://127.0.0.1:8765 with the bundled examples.
Enter a reviewer name and press OK to enable decisions.
Demo reviews are saved to `reviews/demo/`. Stop the server with Ctrl+C.
Add `--no-browser` to skip opening the browser or `--port 8766` to change ports.

## Custom datasets

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

## Review files

`reviewer_<name>.csv` stores each reviewer's latest decision for each sample.
`review_events.csv` records the history of saved decisions.
Keep confirms the label, Switch inverts it, and Discard records
`decision=discard`. Discard does not delete images or change the input CSV:
dataset export code must exclude samples with that decision.

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
