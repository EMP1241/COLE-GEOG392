"""Inspect original PAD-US ownership/access attributes before area processing.

Run with the installed ArcGIS Pro Python environment (pyogrio/pandas available).
This prepares no runtime database and calculates no eligible area.
"""

import hashlib
import json
import platform
import shutil
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

import pyogrio


def main():
    home = Path(__file__).resolve().parents[1]
    archive = home / "source/padus-4.1/PADUS4_1Geodatabase.zip"
    receipt = json.loads((archive.parent / "full-inventory-acquisition.json").read_text(encoding="utf-8"))
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != receipt["sha256"]:
        raise RuntimeError("Preserved source changed")
    root = (home / "working/padus-4.1").resolve()
    gdb = root / "PADUS4_1Geodatabase.gdb"
    marker = root / "extraction.json"
    if not marker.exists():
        if root.exists():
            raise RuntimeError("Unrecorded extraction directory exists; preserve for review")
        with ZipFile(archive) as source:
            members = [i for i in source.infolist() if i.filename.replace("\\", "/").startswith("PADUS4_1Geodatabase.gdb/")]
            for member in members:
                target = (root / member.filename.replace("\\", "/")).resolve()
                if not target.is_relative_to(root):
                    raise RuntimeError("Unsafe source archive member")
            root.mkdir(parents=True)
            print(json.dumps({"phase": "extract", "members": len(members)}), flush=True)
            for member in members:
                target = root / member.filename.replace("\\", "/")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(member) as incoming, target.open("xb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
            marker.write_text(json.dumps({"archive_sha256": digest, "members": len(members)}, indent=2) + "\n", encoding="utf-8")
    elif json.loads(marker.read_text(encoding="utf-8"))["archive_sha256"] != digest:
        raise RuntimeError("Extraction belongs to another archive")
    result = {"source_sha256": digest, "date": "2026-10-21", "purpose": "Attribute/source preflight; no area estimates",
              "python": platform.python_version(), "pyogrio": pyogrio.__version__,
              "gdal": pyogrio.__gdal_version_string__, "layers": {}}
    for layer, geometry_type in pyogrio.list_layers(gdb):
        if not geometry_type or "Combined_" in layer:
            continue
        info = pyogrio.read_info(gdb, layer=layer, force_feature_count=True, force_total_bounds=True)
        fields = [str(f) for f in info["fields"]]
        wanted = [f for f in ["Category", "Own_Type", "Own_Name", "Mang_Type", "Mang_Name", "Des_Tp", "Pub_Access", "State_Nm", "Unit_Nm", "GIS_Acres"] if f in fields]
        print(json.dumps({"phase": "attributes", "layer": layer, "features": info["features"]}), flush=True)
        frame = pyogrio.read_dataframe(gdb, layer=layer, columns=wanted, read_geometry=False, fid_as_index=True)
        counts = {field: {str(key): int(value) for key, value in frame[field].fillna("<NULL>").value_counts(dropna=False).items()}
                  for field in wanted if field not in {"Unit_Nm", "GIS_Acres"}}
        pairs = []
        if "Own_Type" in frame and "Pub_Access" in frame:
            pairs = [{"owner": str(owner), "access": str(access), "records": int(count)}
                     for (owner, access), count in frame.groupby(["Own_Type", "Pub_Access"], dropna=False).size().items()]
        public = frame["Own_Type"].isin(["FED", "STAT", "LOC", "DIST", "TERR"]) if "Own_Type" in frame else None
        eligible_examples = []
        if public is not None:
            candidates = frame[public & frame["Pub_Access"].eq("OA")]
            eligible_examples = [{"fid": int(fid), **{k: (None if value != value else str(value)) for k, value in row.items()}}
                                 for fid, row in candidates.head(8).iterrows()]
        result["layers"][layer] = {"features": int(info["features"]), "geometry_type": geometry_type,
                                   "crs": info["crs"], "bounds": list(info["total_bounds"]), "fields": fields,
                                   "attribute_counts": counts, "owner_access_record_counts": pairs,
                                   "candidate_examples_not_validated": eligible_examples}
    destination = home / "verification/original-polygon-attributes.json"
    destination.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(destination), "layers": {k: v["features"] for k, v in result["layers"].items()}}), flush=True)


if __name__ == "__main__":
    main()
