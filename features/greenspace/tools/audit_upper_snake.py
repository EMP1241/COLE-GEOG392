"""Recheck accepted Upper Snake evidence read-only; never repair an input."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from osgeo import gdal
import shapely

from investigate_rings import cycles, signed_area
from investigate_upper_snake import digest_file, require
from repair_geometry import source_tree


def main():
    home = Path(__file__).resolve().parents[1]
    destination = home / "verification/upper-snake-resolution-audit.json"
    require(not destination.exists(), "Refuse to replace data audit")
    receipt_path = home / "verification/upper-snake-resolution.json"
    receipt_hash = digest_file(receipt_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    errors = []

    def check(condition, message):
        if not condition:
            errors.append(message)

    root = home / "working/upper-snake-investigation-2026-10-21"
    for name, expected in receipt["investigation_artifacts"].items():
        check(digest_file(root / name) == expected["sha256"], f"Artifact changed: {name}")
    check(digest_file(home / "verification/upper-snake-diagnosis.json") == receipt["source_diagnosis_sha256"], "Source diagnosis changed")
    check(digest_file(home / "verification/upper-snake-overlap-diagnosis.json") == receipt["overlap_diagnosis_sha256"], "Overlap diagnosis changed")
    accepted_path = home / receipt["accepted_artifact"]["path"]
    check(digest_file(accepted_path) == receipt["accepted_artifact"]["sha256"], "Accepted geometry changed")
    archive = home / "source/padus-4.1/PADUS4_1Geodatabase.zip"
    check(digest_file(archive) == receipt["source_archive_sha256"], "Original archive changed")
    gdal.UseExceptions()
    dataset = gdal.OpenEx("/vsizip/" + archive.resolve().as_posix() + "/PADUS4_1Geodatabase.gdb",
                          gdal.OF_VECTOR | gdal.OF_READONLY, allowed_drivers=["OpenFileGDB"])
    feature = dataset.GetLayerByName("PADUS4_1Fee").GetFeature(163)
    body = bytes(feature.GetGeometryRef().GetLinearGeometry().ExportToWkb())
    check(hashlib.sha256(body).hexdigest() == receipt["source_original_wkb_sha256"], "Direct original geometry differs")
    check(all((None if feature.GetField(k) is None else str(feature.GetField(k))) == v
              for k,v in receipt["source_attributes"].items()), "Source attributes differ")
    check(not feature.GetGeometryRef().HasCurveGeometry(1), "Unresolved nonlinear geometry")
    source = shapely.from_wkb(body)
    accepted = shapely.from_wkb(accepted_path.read_bytes())
    positive = shapely.from_wkb((root / "source-positive-winding-region.wkb").read_bytes())
    overlap = shapely.from_wkb((root / "source-polygon-overlap.wkb").read_bytes())
    audit = home / "working/national-repair-2026-10-21/repair_audit.sqlite3"
    with sqlite3.connect(audit.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        candidate_body = connection.execute("SELECT polygon_wkb FROM repairs WHERE layer='PADUS4_1Fee' AND source_fid=163").fetchone()[0]
    check(hashlib.sha256(candidate_body).hexdigest() == receipt["previous_candidate_wkb_sha256"], "Previous candidate differs")
    candidate = shapely.from_wkb(candidate_body)
    rings = [list(r.coords) for p in source.geoms for r in [p.exterior, *p.interiors]]
    simple = [c for r in rings for c in cycles(r)]
    shells = [shapely.Polygon(c) for c in simple if signed_area(c) < 0]
    holes = [shapely.Polygon(c) for c in simple if signed_area(c) > 0]
    check(len(shells) == 1091 and len(holes) == 205 and all(p.is_valid for p in shells+holes), "Source cycles differ/invalid")
    tree = shapely.STRtree(shells)
    assignments = [[] for _ in shells]
    for hole in holes:
        choices = [int(i) for i in tree.query(hole) if shells[int(i)].covers(hole)]
        require(choices, "Audit cannot assign original hole")
        assignments[min(choices, key=lambda i: shells[i].area)].append(list(hole.exterior.coords))
    parts = [shapely.Polygon(list(p.exterior.coords), assignments[i]) for i,p in enumerate(shells)]
    check(all(p.is_valid for p in parts), "Reconstructed individual polygon invalid")
    source_union = shapely.union_all(parts)
    source_overlap = parts[437].intersection(parts[441])
    noded = shapely.node(shapely.MultiLineString(rings))
    faces = list(shapely.polygonize(shapely.get_parts(noded)).geoms)
    hole_tree = shapely.STRtree(holes)
    counted, uncounted, face_counts = [], [], {}
    for face in faces:
        point = face.representative_point()
        winding = len(tree.query(point, predicate="within")) - len(hole_tree.query(point, predicate="within"))
        face_counts[str(winding)] = face_counts.get(str(winding), 0) + 1
        (counted if winding > 0 else uncounted).append(face)
    winding_region = shapely.union_all(counted)
    check(face_counts == receipt["source_winding_face_counts"], "Source winding faces differ")
    check(accepted.is_valid and len(accepted.geoms) == 1090, "Accepted validity/part count differs")
    check(accepted.symmetric_difference(source_union).area == 0, "Accepted differs from original source union")
    check(accepted.symmetric_difference(winding_region).area == 0 and accepted.symmetric_difference(positive).area == 0, "Independent source winding region differs")
    check(accepted.intersection(shapely.union_all(uncounted)).area == 0, "Accepted fills zero/negative winding area")
    check(accepted.boundary.difference(noded).length == 0, "Accepted boundary not on source linework")
    check(candidate.difference(accepted).area == 0, "Accepted removes candidate area")
    check(accepted.difference(candidate).symmetric_difference(source_overlap).area == 0, "Accepted addition not exact source overlap")
    check(source_overlap.symmetric_difference(overlap).area == 0, "Saved overlap not exact source overlap")
    check(accepted.area == receipt["replacement_area_m2"], "Accepted area differs")
    check(abs((source.area-candidate.area) - receipt["explained_decoded_difference_m2"]) < 0.01, "Original decoded discrepancy not explained")
    tree_before = json.loads((home / "working/national-repair-2026-10-21/source-extraction-before.json").read_text(encoding="utf-8"))
    check(source_tree(home / "working/padus-4.1/PADUS4_1Geodatabase.gdb") == tree_before, "Original extraction changed")
    previous = {
        "working/controlled-repair-2026-10-21/repair_audit.sqlite3": "ca6c54ba464da4e6db7104715b56e549fd4f0f0b54db5c0962b12d0e90e151f9",
        "working/source-checked-repair-2026-10-21/repair_audit.sqlite3": "c4efe7103c22ac9520c5e180e0c80b1b5759998b99da428f0f6e15ec931ca42e",
        "working/national-repair-2026-10-21/repair_audit.sqlite3": receipt["previous_audit_sha256"],
    }
    for relative, expected in previous.items():
        check(digest_file(home / relative) == expected, f"Previous audit changed: {relative}")
    check(digest_file(archive) == receipt["source_archive_sha256"] and digest_file(receipt_path) == receipt_hash, "Original archive or acceptance receipt changed")
    report = {"created_utc": datetime.now(timezone.utc).isoformat(), "status": "passed" if not errors else "failed",
              "discrepancies": errors, "source_layer": "PADUS4_1Fee", "source_fid": 163, "credit": "Margaret",
              "accepted_geometry_sha256": receipt["accepted_artifact"]["sha256"], "acceptance_receipt_sha256": receipt_hash,
              "accepted_geometry_valid": bool(accepted.is_valid), "accepted_parts": len(accepted.geoms),
              "independent_source_union_symmetric_difference_m2": accepted.symmetric_difference(source_union).area,
              "independent_winding_region_symmetric_difference_m2": accepted.symmetric_difference(winding_region).area,
              "source_supported_addition_m2": accepted.difference(candidate).area,
              "source_overlap_counted_once": True, "original_archive_extraction_and_all_previous_audits_unchanged": not errors,
              "repairs_performed_in_audit": False, "runtime_database_built": False}
    destination.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report), flush=True)
    require(not errors, "Upper Snake data audit failed")


if __name__ == "__main__":
    main()
