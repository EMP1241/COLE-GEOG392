"""Install the exact scalar database plus a versioned runtime manifest."""
import hashlib
import json
from pathlib import Path
import shutil

HOME = Path(__file__).resolve().parents[1]
ROOT = HOME.parents[1]


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    source = HOME / "data"
    receipt = json.loads((HOME / "verification/county-database-preparation-2026-10-22.json").read_text())
    manifest = json.loads((source / "manifest.json").read_text())
    database = source / "greenspace.sqlite3"
    revision_path = HOME / "verification/attribution-revision-2026-10-22.json"
    if revision_path.exists():
        revision = json.loads(revision_path.read_text(encoding="utf-8"))
        for name in ("greenspace.sqlite3", "manifest.json"):
            key = (source / name).relative_to(ROOT).as_posix()
            if revision["status"] != "passed" or revision["after"][key]["sha256"] != sha(source / name):
                raise RuntimeError("Attribution revision differs from its receipt")
        receipt = {**receipt, "manifest_sha256": sha(source / "manifest.json"), "database": manifest["database"]}
    if (receipt["status"] != "passed" or not receipt["source_archive_extraction_and_repair_database_unchanged"] or
            receipt["manifest_sha256"] != sha(source / "manifest.json") or
            receipt["database"] != manifest["database"] or sha(database) != manifest["database"]["sha256"]):
        raise RuntimeError("Prepared Greenspace release differs from its acceptance receipt")
    destination = ROOT / "src/cole/features/greenspace/data"
    destination.mkdir(parents=True, exist_ok=True)
    copies = {"greenspace.sqlite3": database, "preparation-manifest.json": source / "manifest.json"}
    runtime = {**manifest, "feature_version": "1.0.0", "preparation_manifest_sha256": receipt["manifest_sha256"]}
    runtime_text = json.dumps(runtime, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    for name, original in copies.items():
        target = destination / name
        if target.exists() and sha(target) != sha(original):
            raise RuntimeError(f"Refuse to replace a different runtime artifact: {target}")
    manifest_target = destination / "manifest.json"
    if manifest_target.exists() and manifest_target.read_text(encoding="utf-8") != runtime_text:
        raise RuntimeError("Refuse to replace a different runtime manifest")
    for name, original in copies.items():
        target = destination / name
        if not target.exists():
            shutil.copy2(original, target)
        if sha(target) != sha(original):
            raise RuntimeError(f"Runtime copy differs: {target}")
    if not manifest_target.exists():
        manifest_target.write_text(runtime_text, encoding="utf-8")
    print(json.dumps({"status": "installed_exact_database_copy", "directory": str(destination), "database": manifest["database"],
                      "runtime_manifest_sha256": sha(manifest_target)}))


if __name__ == "__main__":
    main()
