"""Acquire COLE's selected public sources without overwriting prior source bytes.

Run from any directory: python scripts/fetch_sources.py [--check-remote]
Existing files are checked against sources.json. --check-remote also verifies that
the publisher still serves identical bytes; changed editions require review.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "data" / "source"
MAX_DOWNLOAD = 100 * 1024 * 1024
SOURCES = {
    "epi": {"filename": "fbc_data_2026.xlsx", "url": "https://files.epi.org/uploads/fbc_data_2026.xlsx", "kind": "xlsx", "publisher": "Economic Policy Institute", "edition_year": 2026, "cost_year": 2025, "license": "CC BY-NC-SA 4.0", "reference_url": "https://www.epi.org/resources/budget/"},
    "counties": {"filename": "cb_2021_us_county_5m.zip", "url": "https://www2.census.gov/geo/tiger/GENZ2021/shp/cb_2021_us_county_5m.zip", "kind": "shapefile", "publisher": "U.S. Census Bureau", "geography_vintage": 2021, "reference_url": "https://www2.census.gov/geo/tiger/GENZ2021/shp/"},
    "states": {"filename": "cb_2021_us_state_5m.zip", "url": "https://www2.census.gov/geo/tiger/GENZ2021/shp/cb_2021_us_state_5m.zip", "kind": "shapefile", "publisher": "U.S. Census Bureau", "geography_vintage": 2021, "reference_url": "https://www2.census.gov/geo/tiger/GENZ2021/shp/"},
    "places": {"filename": "national_place_by_county2020.txt", "url": "https://www2.census.gov/geo/docs/reference/codes2020/national_place_by_county2020.txt", "kind": "places", "publisher": "U.S. Census Bureau", "geography_vintage": 2020, "reference_url": "https://www2.census.gov/geo/docs/reference/codes2020/"},
    "zctas": {"filename": "tab20_zcta520_county20_natl.txt", "url": "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt", "kind": "zctas", "publisher": "U.S. Census Bureau", "geography_vintage": 2020, "reference_url": "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/"},
    "noaa_temperature": {
        "filename": "climdiv-norm-tmpccy-v1.0.0-20260904",
        "url": "https://www.ncei.noaa.gov/monitoring-content/data/us/climdiv/monthly/current/climdiv-norm-tmpccy-v1.0.0-20260904",
        "kind": "noaa_county_normals",
        "publisher": "NOAA National Centers for Environmental Information",
        "normal_period_start": 1991,
        "normal_period_end": 2020,
        "release_date": "2026-09-04",
        "units": "degrees Fahrenheit",
        "reference_url": "https://www.ncei.noaa.gov/monitoring-content/data/us/climdiv/monthly/current/",
    },
    "noaa_county_readme": {
        "filename": "noaa-county-readme.txt",
        "url": "https://www.ncei.noaa.gov/monitoring-content/data/us/climdiv/monthly/current/county-readme.txt",
        "kind": "noaa_county_readme",
        "publisher": "NOAA National Centers for Environmental Information",
        "reference_url": "https://www.ncei.noaa.gov/monitoring-content/data/us/climdiv/monthly/current/county-readme.txt",
    },
    "noaa_normals_readme": {
        "filename": "noaa-normals-readme.txt",
        "url": "https://www.ncei.noaa.gov/monitoring-content/data/us/climdiv/monthly/current/normals-readme.txt",
        "kind": "noaa_normals_readme",
        "publisher": "NOAA National Centers for Environmental Information",
        "reference_url": "https://www.ncei.noaa.gov/monitoring-content/data/us/climdiv/monthly/current/normals-readme.txt",
    },
}


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def validate_archive(path: Path, kind: str) -> None:
    """Check archive structure without extracting any archive member."""
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > 1000 or sum(m.file_size for m in members) > 250 * 1024 * 1024:
            raise ValueError(f"Archive exceeds expected resource limits: {path.name}")
        names = []
        for member in members:
            normalized = member.filename.replace("\\", "/")
            p = PurePosixPath(normalized)
            if p.is_absolute() or ".." in p.parts or ":" in normalized:
                raise ValueError(f"Unsafe archive member: {member.filename}")
            if member.flag_bits & 1:
                raise ValueError("Encrypted archives are not supported")
            names.append(normalized)
        if len(set(names)) != len(names):
            raise ValueError("Archive contains duplicate members")
        if kind == "xlsx" and not {"[Content_Types].xml", "xl/workbook.xml"}.issubset(names):
            raise ValueError("Downloaded ZIP is not an Excel workbook")
        if kind == "shapefile":
            # Temporary file names do not retain the publisher stem.
            shapes = [n[:-4] for n in names if n.endswith(".shp")]
            if len(shapes) != 1 or not all(shapes[0] + suffix in names for suffix in (".dbf", ".shx", ".prj")):
                raise ValueError("Shapefile archive must have one complete shp/dbf/shx/prj set")
        if archive.testzip() is not None:
            raise ValueError("Archive CRC validation failed")


def validate_source(path: Path, kind: str) -> None:
    if kind in ("xlsx", "shapefile"):
        validate_archive(path, kind)
        return
    if kind == "noaa_county_normals":
        with path.open("r", encoding="ascii", newline="") as handle:
            first = handle.readline().rstrip("\r\n")
        if (len(first) < 95 or first[95:].strip() or not first[:11].isdigit()
                or first[5:7] != "02" or first[7:11] not in {"0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009", "0010", "0031", "0032"}):
            raise ValueError(f"Unexpected NOAA county normal record: {first[:180]}")
        return
    with path.open("rb") as handle:
        raw = handle.read(16384)
    if b"<html" in raw.lower():
        raise ValueError(f"Unexpected HTML returned for {kind}")
    if kind in ("noaa_county_readme", "noaa_normals_readme"):
        text = raw.decode("cp1252")
        markers = (
            ("nClimDiv", "COUNTY", "STATE-CODE", "DIVISION-NUMBER", "YEAR")
            if kind == "noaa_county_readme"
            else ("nClimDiv", "NORMALS VALUES FOR SPECIAL PERIODS", "1991-2020", "0010")
        )
        if not all(marker in text for marker in markers):
            raise ValueError(f"Unexpected {kind} documentation")
        return
    header = raw.splitlines()[0].decode("utf-8-sig")
    required = ("STATEFP", "PLACEFP", "COUNTYFP") if kind == "places" else ("GEOID_ZCTA5_20", "GEOID_COUNTY_20")
    if not set(required).issubset(header.split("|")):
        raise ValueError(f"Unexpected {kind} text header: {header[:180]}")


def atomic_json(path: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as handle:
        temp = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def fetch(check_remote: bool = False) -> dict:
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = SOURCE_DIR / "sources.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"schema_version": "1.0", "sources": {}}
    for key, spec in SOURCES.items():
        destination = SOURCE_DIR / spec["filename"]
        prior = manifest["sources"].get(key)
        if destination.exists():
            validate_source(destination, spec["kind"])
            if not prior or prior["url"] != spec["url"] or sha256(destination) != prior["sha256"]:
                raise ValueError(f"Untracked or changed local source: {destination}. Preserve and review it before acquiring again.")
            if not check_remote:
                print(f"verified local {key}: {destination.stat().st_size:,} bytes")
                continue
        temp = None
        try:
            request = Request(spec["url"], headers={"User-Agent": "COLE academic data preparation/1.0", "Accept-Encoding": "identity"})
            with urlopen(request, timeout=45) as response:
                final_url = response.geturl()
                if urlparse(final_url).scheme != "https" or urlparse(final_url).hostname != urlparse(spec["url"]).hostname:
                    raise ValueError(f"Unexpected source redirect: {final_url}")
                if "text/html" in response.headers.get("Content-Type", "").lower():
                    raise ValueError("Publisher returned HTML instead of the requested data")
                metadata = {"final_url": final_url, "content_type": response.headers.get("Content-Type"), "etag": response.headers.get("ETag"), "last_modified": response.headers.get("Last-Modified")}
                with tempfile.NamedTemporaryFile("wb", dir=SOURCE_DIR, delete=False, suffix=".download") as handle:
                    temp = Path(handle.name)
                    size = 0
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_DOWNLOAD:
                            raise ValueError("Download exceeds expected 100 MiB limit")
                        handle.write(chunk)
            validate_source(temp, spec["kind"])
            digest = sha256(temp)
            if prior and digest != prior["sha256"]:
                raise ValueError(f"Publisher bytes changed for {key}; saved source was preserved. Review the new release explicitly.")
            now = datetime.now(timezone.utc).isoformat()
            if destination.exists():
                prior["last_remote_verified_utc"] = now
            else:
                temp.replace(destination)
                manifest["sources"][key] = {**spec, **metadata, "retrieved_utc": now, "sha256": digest, "bytes": size}
            atomic_json(manifest_path, manifest)
            print(f"verified remote {key}: {size:,} bytes SHA256 {digest}")
        finally:
            if temp:
                temp.unlink(missing_ok=True)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-remote", action="store_true")
    args = parser.parse_args()
    fetch(args.check_remote)
