"""Reproduce Bingqian's hazard data and COLE's independent feature manifest."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from cole.features.hazards.tools.prepare_hazards import main as prepare_hazards
from cole.features.hazards.tools.prepare_hazards import read_cole_counties


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nri", type=Path, required=True)
    parser.add_argument("--counties", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    prepare_hazards(["--nri", str(args.nri), "--counties", str(args.counties), "--out", str(args.out)])
    report = json.loads((args.out / "hazards_report.json").read_text(encoding="utf-8"))
    county_keys = "\n".join(sorted(read_cole_counties(args.counties))) + "\n"
    manifest = {
        "feature_version": "1.0.0",
        "contributor": "Bingqian",
        "submitted_report_author": "@Bingqian",
        "submission_url": "https://drive.google.com/file/d/1InFE8eIpBuEM8EVUBcAnMVFbu_P4k5Xd/view",
        "submission_sha256": "cc294cadc89dd9e11cd0571f198ed58b420c6ac35ea29db710ca292a3acb2d1c",
        "county_keys_sha256": hashlib.sha256(county_keys.encode()).hexdigest(),
        "nri_version": report["nri_version"],
        "coverage": report["coverage"],
        "source": report["manifest_entries"]["sources.fema_nri"],
        "outputs": {
            name: {
                "bytes": (args.out / name).stat().st_size,
                "sha256": hashlib.sha256((args.out / name).read_bytes()).hexdigest(),
            }
            for name in ("hazards.csv", "hazards_report.json")
        },
        "ranking_universe_sizes": report["ranking_universe_sizes"],
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
