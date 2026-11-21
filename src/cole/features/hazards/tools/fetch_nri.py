# Original preparation/download contribution: Bingqian.
"""Download FEMA's NRI county table and record its provenance.

Writes the zip plus a sidecar JSON (<zip name>.download.json) holding the
requested and final URLs, retrieval time, size and SHA-256. prepare_hazards.py
reads that sidecar, so the manifest records when the data was actually
downloaded rather than when a file was last copied.

The default URL and fingerprint pin NRI v1.20 (December 2025). A newer
release is a deliberate change: pass --url and --expected-sha256 (or
--allow-new-release to record whatever is downloaded).

Usage:
  python tools/fetch_nri.py --out data/raw/nri
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

NRI_URL = "https://www.fema.gov/about/reports-and-data/openfema/nri/v120/NRI_Table_Counties.zip"
NRI_V120_SHA256 = "b99dae90a317b16630d7f9b1faa4a2784d5ca458761c1d3ab9646f046df9d072"
NRI_CSV_NAME = "NRI_Table_Counties.csv"


def download(url: str, target: Path) -> tuple[str, str]:
    """Save url to target; return (final URL, content type).

    fema.gov's edge network refuses Python's own HTTP client (HTTP 403) but
    serves curl, which ships with macOS and Windows 10+. Python is the
    fallback where curl is absent.
    """
    curl = shutil.which("curl")
    if curl:
        # --url keeps the address from ever being read as a curl option.
        result = subprocess.run([curl, "-fsSL", "--retry", "2", "--proto", "=https", "--proto-redir", "=https",
                                 "-o", str(target),
                                 "-w", "%{url_effective}\n%{content_type}", "--url", url],
                                capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise SystemExit(f"curl could not download {url}: {result.stderr.strip()}")
        final_url, _, content_type = result.stdout.partition("\n")
        return final_url.strip(), content_type.strip()
    request = urllib.request.Request(url, headers={"User-Agent": "COLE-data-preparation"})
    with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as stream:
        if urllib.parse.urlsplit(response.geturl()).scheme != "https":
            raise SystemExit(f"The download was redirected away from https: {response.geturl()}")
        shutil.copyfileobj(response, stream, 1024 * 1024)
        return response.geturl(), response.headers.get("Content-Type", "")


def sha256_file(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="directory for the zip and its sidecar")
    parser.add_argument("--url", default=NRI_URL)
    parser.add_argument("--expected-sha256", default=NRI_V120_SHA256)
    parser.add_argument("--allow-new-release", action="store_true", help="record a download whose fingerprint differs")
    args = parser.parse_args(argv)

    parts = urllib.parse.urlsplit(args.url)
    if parts.scheme != "https" or not parts.netloc:
        raise SystemExit("Use an https:// URL for the NRI table.")
    filename = Path(parts.path).name
    if not filename.endswith(".zip"):
        raise SystemExit(f"The URL must name a .zip file, not {filename!r}.")

    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / filename
    partial = target.with_suffix(".part")
    retrieved = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        final_url, content_type = download(args.url, partial)
        sha, size = sha256_file(partial)
        pinned = sha == args.expected_sha256
        # A matching fingerprint already proves the bytes; the label only
        # matters for an unpinned release.
        if not pinned and "zip" not in content_type:
            raise SystemExit(f"Expected a zip archive but received {content_type or 'an unknown type'} from {final_url}. "
                             "FEMA may have moved the file; check the OpenFEMA NRI page.")
        if not pinned and not args.allow_new_release:
            raise SystemExit(f"Downloaded file fingerprint {sha} differs from the pinned release {args.expected_sha256}. "
                             "Review the new release, then rerun with --allow-new-release.")
        try:
            with zipfile.ZipFile(partial) as archive:
                if NRI_CSV_NAME not in archive.namelist():
                    raise SystemExit(f"The archive has no {NRI_CSV_NAME}.")
        except zipfile.BadZipFile as exc:
            raise SystemExit(f"The download is not a valid zip archive: {exc}") from exc
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)

    sidecar = {"filename": target.name, "url": args.url, "final_url": final_url,
               "retrieved_utc": retrieved.isoformat(), "bytes": size, "sha256": sha}
    record = Path(f"{target}.download.json")
    temporary = record.with_suffix(".tmp")
    temporary.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
    temporary.replace(record)
    print(f"Saved {target} ({size} bytes, sha256 {sha}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
