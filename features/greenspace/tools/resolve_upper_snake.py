"""Accept only the investigated Upper Snake source-parts union, with proofs."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from osgeo import gdal
import shapely

from investigate_rings import signed_area
from investigate_upper_snake import digest_file, require


def main():
    home = Path(__file__).resolve().parents[1]
    directory = home / "working/upper-snake-investigation-2026-10-21"
    destination = home / "verification/upper-snake-resolution.json"
    accepted_path = directory / "accepted-source-union-replacement.wkb"
    require(not destination.exists() and not accepted_path.exists(), "Refuse to replace accepted evidence")
    diagnosis_path = home / "verification/upper-snake-diagnosis.json"
    overlap_path = home / "verification/upper-snake-overlap-diagnosis.json"
    diagnosis = json.loads(diagnosis_path.read_text(encoding="utf-8"))
    overlap = json.loads(overlap_path.read_text(encoding="utf-8"))
    archive = home / "source/padus-4.1/PADUS4_1Geodatabase.zip"
    audit = home / "working/national-repair-2026-10-21/repair_audit.sqlite3"
    require(digest_file(archive) == diagnosis["source_archive_sha256"], "Original archive changed")
    require(digest_file(audit) == diagnosis["previous_audit_sha256"], "Previous audit changed")
    artifacts = {}
    for receipt in (diagnosis, overlap):
        for filename, expected in receipt["artifacts"].items():
            path = directory / filename
            require(path.stat().st_size == expected["bytes"] and digest_file(path) == expected["sha256"], "Investigation geometry differs")
            artifacts[filename] = expected
    with sqlite3.connect(audit.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM repairs WHERE layer='PADUS4_1Fee' AND source_fid=163").fetchone()
        source = shapely.from_wkb(row["original_wkb"])
        candidate = shapely.from_wkb(row["polygon_wkb"])
    gdal.UseExceptions()
    dataset = gdal.OpenEx("/vsizip/" + archive.resolve().as_posix() + "/PADUS4_1Geodatabase.gdb",
                          gdal.OF_VECTOR | gdal.OF_READONLY, allowed_drivers=["OpenFileGDB"])
    feature = dataset.GetLayerByName("PADUS4_1Fee").GetFeature(163)
    require(hashlib.sha256(bytes(feature.GetGeometryRef().GetLinearGeometry().ExportToWkb())).hexdigest() == row["original_sha256"], "Direct original ZIP geometry differs")
    attributes = json.loads(row["attributes_json"])
    require(all((None if feature.GetField(key) is None else str(feature.GetField(key))) == value
                for key, value in attributes.items()), "Direct original ZIP attributes differ")
    require(not feature.GetGeometryRef().HasCurveGeometry(1), "Nonlinear boundary unresolved")
    reconstruction = shapely.from_wkb((directory / "source-ring-reconstruction.wkb").read_bytes())
    replacement = shapely.from_wkb((directory / "source-valid-parts-union.wkb").read_bytes())
    positive = shapely.from_wkb((directory / "source-positive-winding-region.wkb").read_bytes())
    overlap_shape = shapely.from_wkb((directory / "source-polygon-overlap.wkb").read_bytes())
    parts = list(reconstruction.geoms)
    require(len(parts) == 1091 and all(p.is_valid for p in parts), "Unexpected/invalid source parts")
    require(len(overlap["actual_source_polygon_overlaps"]) == 1, "Unexpected overlap accounting")
    interaction = overlap["actual_source_polygon_overlaps"][0]
    require((interaction["a"], interaction["b"]) == (437, 441), "Different source polygon overlap")
    exact_overlap = parts[437].intersection(parts[441])
    require(exact_overlap.symmetric_difference(overlap_shape).area == 0, "Saved overlap differs from source parts")
    require(overlap_shape.area < 1, "Substantial source overlap requires further review")
    require(replacement.is_valid and not replacement.is_empty and positive.is_valid, "Invalid replacement or independent region")
    require(replacement.symmetric_difference(shapely.union_all(parts)).area == 0, "Replacement differs from source-part union")
    require(replacement.symmetric_difference(positive).area == 0, "Independent positive-winding region differs")
    require(overlap["negative_winding_area_m2"] == 0, "Unresolved negative source winding")
    require(candidate.difference(replacement).area == 0, "Replacement loses candidate area")
    added = replacement.difference(candidate)
    require(added.symmetric_difference(exact_overlap).area == 0, "Added area is not exactly the source-supported overlap")
    rings = [list(r.coords) for p in source.geoms for r in [p.exterior, *p.interiors]]
    noded = shapely.node(shapely.MultiLineString(rings))
    boundary_missing = replacement.boundary.difference(noded).length
    require(boundary_missing == 0, "Boundary absent from original source linework")
    promoted_area = math.fsum(signed_area(list(p.exterior.coords)) for p in source.geoms
                              if signed_area(list(p.exterior.coords)) > 0)
    decoded_candidate = source.area - candidate.area
    explanation = 2 * promoted_area + 2 * exact_overlap.area
    require(abs(decoded_candidate - explanation) < 0.01, "Decoded/candidate discrepancy not fully explained")
    signed_source = -math.fsum(signed_area(r) for r in rings)
    require(abs(signed_source - replacement.area - exact_overlap.area) < 0.01, "Signed-source overlap accounting differs")
    publisher = float(feature.GetField("SHAPE_Area"))
    require(abs(replacement.area - publisher) <= 4046.8564224
            and abs(replacement.area - publisher) / publisher * 100 <= 0.01, "Stored publisher area differs significantly")
    require(digest_file(archive) == diagnosis["source_archive_sha256"] and digest_file(audit) == diagnosis["previous_audit_sha256"], "Preserved input changed")
    body = shapely.to_wkb(replacement, byte_order=1)
    accepted_path.write_bytes(body)
    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(), "local_date": "2026-10-21",
        "decision": "accepted_source_union_replacement_previous_even_odd_candidate_rejected",
        "credit": "Margaret", "source_layer": "PADUS4_1Fee", "source_fid": 163,
        "source_name": feature.GetField("Unit_Nm"), "source_attributes": attributes,
        "source_archive_sha256": diagnosis["source_archive_sha256"],
        "source_original_wkb_sha256": row["original_sha256"], "previous_candidate_wkb_sha256": row["polygon_sha256"],
        "previous_audit_sha256": diagnosis["previous_audit_sha256"],
        "direct_original_zip_geometry_and_attributes_match": True, "actual_nonlinear_segments": False,
        "source_diagnosis_sha256": digest_file(diagnosis_path), "overlap_diagnosis_sha256": digest_file(overlap_path),
        "source_valid_polygon_parts": len(parts), "replacement_polygon_parts": len(replacement.geoms),
        "replacement_valid": True, "overlapping_source_parts": [437,441],
        "overlap_region_area_m2": exact_overlap.area, "source_overlap_counted_once": True,
        "area_added_to_previous_candidate_m2": added.area,
        "candidate_area_removed_m2": candidate.difference(replacement).area,
        "added_region_vs_source_overlap_symmetric_difference_m2": 0.0,
        "replacement_vs_source_part_union_symmetric_difference_m2": 0.0,
        "replacement_vs_source_positive_winding_symmetric_difference_m2": 0.0,
        "replacement_boundary_absent_from_noded_original_source_length_m": boundary_missing,
        "source_winding_face_counts": overlap["source_winding_face_counts"],
        "sum_exterior_slot_counterclockwise_area_m2": promoted_area,
        "decoded_minus_candidate_area_m2": decoded_candidate,
        "explained_decoded_difference_m2": explanation,
        "unexplained_decoded_difference_m2": decoded_candidate - explanation,
        "signed_source_area_m2": signed_source, "replacement_area_m2": replacement.area,
        "publisher_shape_area_m2": publisher, "replacement_minus_publisher_area_m2": replacement.area - publisher,
        "original_archive_and_previous_audit_unchanged": True,
        "accepted_artifact": {"path": accepted_path.relative_to(home).as_posix(), "bytes": len(body),
                              "sha256": hashlib.sha256(body).hexdigest()},
        "investigation_artifacts": artifacts,
        "method": "Exact repeated-vertex source cycles; clockwise shells/counterclockwise holes assigned to smallest containing shell; union of individually valid source polygons, independently checked against positive signed-winding planar faces.",
        "limits": "Specific source-supported area representation, not surveyed truth or final ownership/access eligibility. Nested positive source islands remain included; a global subtraction of all hole polygons would wrongly remove them. Intersection nodes lie on original source segments; no snapping, simplification, buffer or make_valid is used in this replacement.",
        "scope": "Only PADUS4_1Fee FID 163 under exact original/archive/attribute/receipt hashes. Originals and private/military/restricted/closed/unconfirmed exclusions remain intact; national preparation and runtime integration are incomplete.",
        "primary_documentation": ["https://doc.esri.com/en/arcgis-pro/latest/arcpy/get-started/reading-geometries.html",
                                  "https://gdal.org/en/stable/doxygen/classOGRGeometryFactory.html",
                                  "https://shapely.readthedocs.io/en/stable/manual.html"],
    }
    destination.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k,v in report.items() if k not in {"source_attributes", "investigation_artifacts"}}), flush=True)


if __name__ == "__main__":
    main()
