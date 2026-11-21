"""Preserve the official PAD-US polygon archive; never replace an original."""

import argparse
import hashlib
import json
import shutil
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile


def check_archive(path):
    with ZipFile(path) as archive:
        members = archive.namelist()
        if not members or not any(".gdb/" in name.replace("\\", "/").lower() for name in members):
            raise RuntimeError("Archive does not contain a geodatabase")
        print(json.dumps({"phase": "verify_archive_crc", "members": len(members)}), flush=True)
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"ZIP CRC error: {bad}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-archive", type=Path, help="Original publisher ZIP downloaded manually")
    args = parser.parse_args()
    home = Path(__file__).resolve().parents[1]
    metadata = json.loads((home / "research/2026-10-21/652d4fc5d34e44db0e2ee45e.json").read_text(encoding="utf-8"))
    entry = next(f for f in metadata["files"] if f["name"] == "PADUS4_1Geodatabase.zip")
    destination = home / "source/padus-4.1" / entry["name"]
    partial = destination.with_suffix(".zip.part")
    expected_size = entry["size"]
    final_url = None
    archive_checked = False
    if args.local_archive and not destination.exists():
        incoming = args.local_archive.resolve(strict=True)
        if incoming.stat().st_size != expected_size:
            raise RuntimeError("Local archive length differs from the recorded publisher edition; source review required")
        if partial.exists():
            raise RuntimeError("Partial destination already exists; preserve it for explicit review")
        with ZipFile(incoming) as archive:
            if not any(".gdb/" in name.replace("\\", "/").lower() for name in archive.namelist()):
                raise RuntimeError("Local file does not contain the expected geodatabase structure")
        shutil.copyfile(incoming, partial)
    if not destination.exists():
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > expected_size:
            raise RuntimeError("Partial archive exceeds publisher length")
        if offset < expected_size:
            # The publisher's manager URL is a JavaScript download page. Its
            # public client resolves the file through this read-only query.
            query = {"query": "query GetDownloadByCuid($cuid: String!) { file(cuid: $cuid) { downloadUri } }",
                     "variables": {"cuid": entry["cuid"]}}
            api_request = urllib.request.Request("https://api.sciencebase.gov/graphql",
                                                 data=json.dumps(query).encode("utf-8"),
                                                 headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(api_request, timeout=45) as api_response:
                result = json.load(api_response)
            if result.get("errors"):
                raise RuntimeError("Public ScienceBase download resolution failed")
            resolved_url = (result.get("data", {}).get("file") or {}).get("downloadUri")
            if not resolved_url or urllib.parse.urlparse(resolved_url).scheme != "https":
                raise RuntimeError("Publisher did not return an HTTPS archive URL")
            request = urllib.request.Request(resolved_url, headers={"User-Agent": "COLE research source acquisition"})
            if offset:
                request.add_header("Range", f"bytes={offset}-")
            with urllib.request.urlopen(request, timeout=45) as response:
                final_url = response.url
                if offset and (response.status != 206 or not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-")):
                    raise RuntimeError("Server did not honor resume range; partial preserved")
                if not offset and response.status != 200:
                    raise RuntimeError(f"Unexpected HTTP status {response.status}")
                if response.headers.get_content_type() in {"text/html", "application/json"}:
                    raise RuntimeError("Archive endpoint returned a document instead of ZIP")
                count = offset
                last_notice = time.monotonic()
                with partial.open("ab" if offset else "xb") as output:
                    while block := response.read(1024 * 1024):
                        count += len(block)
                        if count > expected_size:
                            raise RuntimeError("Download exceeds publisher length")
                        output.write(block)
                        if time.monotonic() - last_notice > 15:
                            print(json.dumps({"phase": "download", "bytes": count, "expected_bytes": expected_size}), flush=True)
                            last_notice = time.monotonic()
            if count != expected_size:
                raise RuntimeError(f"Download incomplete: {count}/{expected_size}; partial preserved")
        check_archive(partial)
        archive_checked = True
        partial.rename(destination)
    if destination.stat().st_size != expected_size:
        raise RuntimeError("Preserved archive has unexpected length")
    with destination.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    receipt = home / "source/padus-4.1/full-inventory-acquisition.json"
    if receipt.exists():
        previous = json.loads(receipt.read_text(encoding="utf-8"))
        if previous["sha256"] != digest:
            raise RuntimeError("Preserved source differs from its receipt")
        print(json.dumps(previous), flush=True)
        return
    if not archive_checked:
        check_archive(destination)
    result = {"item_id": metadata["id"], "filename": entry["name"],
              "url": entry["downloadUri"], "retrieved_utc": datetime.now(timezone.utc).isoformat(),
              "bytes": expected_size, "sha256": digest, "zip_crc_checked": True,
              "publisher_checksum": entry.get("checksum"),
              "final_url_host": urllib.parse.urlparse(final_url).hostname if final_url else None,
              "acquisition_method": "user_download_local_import" if args.local_archive else "publisher_download_api",
              "local_source_path": str(args.local_archive.resolve()) if args.local_archive else None}
    receipt.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
