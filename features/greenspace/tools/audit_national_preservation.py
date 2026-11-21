"""Fingerprint the seven immutable checkpoints preceding Eastern Canal continuation."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from accepted_adjudications import FILES
from repair_geometry import fingerprint_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-hawaii",action="store_true",help="Check the eight checkpoints pinned by Hawaii's passing audit")
    args = parser.parse_args()
    home = Path(__file__).resolve().parents[1]
    destination = args.output.resolve()
    if destination.exists() or not destination.is_relative_to((home / "verification").resolve()):
        raise RuntimeError("Preservation audit output must be new and inside feature verification")
    baseline = home / ("verification/hawaii-resolution-audit.json" if args.include_hawaii else "verification/eastern-canal-resolution-audit.json")
    body = baseline.read_bytes()
    if hashlib.sha256(body).hexdigest() != FILES[baseline.relative_to(home).as_posix()]:
        raise RuntimeError("Pinned prior preservation evidence changed")
    previous = json.loads(body)["preserved_prior_audits"]
    errors, records = [], []
    expected_count=8 if args.include_hawaii else 7
    if len(previous) != expected_count or len({item["run"] for item in previous}) != expected_count:
        raise RuntimeError("Prior checkpoint identities differ")
    for item in previous:
        path = (home / "working" / item["run"] / "repair_audit.sqlite3").resolve()
        if not path.is_relative_to((home / "working").resolve()):
            raise RuntimeError("Prior audit path escapes feature working directory")
        actual = fingerprint_file(path)
        unchanged = actual["sha256"] == item["sha256"]
        records.append({"run": item["run"], **actual, "unchanged": unchanged})
        if not unchanged:
            errors.append(f"Prior checkpoint changed: {item['run']}")
    report = {"created_utc": datetime.now(timezone.utc).isoformat(), "credit": "Margaret",
              "status": "passed" if not errors else "failed", "discrepancies": errors,
              "baseline": baseline.relative_to(home).as_posix(),
              "baseline_sha256": hashlib.sha256(body).hexdigest(),
              "preserved_prior_audits": records, "read_only": True}
    with destination.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps(report), flush=True)
    if errors:
        raise RuntimeError("Prior checkpoint preservation failed")


if __name__ == "__main__":
    main()
