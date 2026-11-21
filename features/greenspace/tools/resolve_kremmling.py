"""Accept a conservative Kremmling derivative only after original-source checks.

The previous linework candidate remains unaccepted. This creates a separate
replacement that excludes its one component wholly outside source outer shells.
"""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from osgeo import gdal
import shapely

from investigate_rings import cycles, signed_area


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    home = Path(__file__).resolve().parents[1]
    out = home / "working/kremmling-investigation-2026-10-21"
    require(not out.exists(), "Refuse to replace an existing source investigation")
    archive = home / "source/padus-4.1/PADUS4_1Geodatabase.zip"
    receipt = json.loads((archive.parent / "full-inventory-acquisition.json").read_text(encoding="utf-8"))
    original_archive_hash = file_hash(archive)
    require(original_archive_hash == receipt["sha256"], "Original USGS archive changed")
    audit = home / "working/source-checked-repair-2026-10-21/repair_audit.sqlite3"
    audit_hash = file_hash(audit)
    with sqlite3.connect(audit.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        record = dict(connection.execute("SELECT * FROM repairs WHERE layer='PADUS4_1Fee' AND source_fid=117").fetchone())
    source = shapely.from_wkb(record["original_wkb"])
    previous = shapely.from_wkb(record["repaired_wkb"])
    gdal.UseExceptions()
    dataset = gdal.OpenEx("/vsizip/" + archive.resolve().as_posix() + "/PADUS4_1Geodatabase.gdb",
                          gdal.OF_VECTOR | gdal.OF_READONLY, allowed_drivers=["OpenFileGDB"])
    feature = dataset.GetLayerByName("PADUS4_1Fee").GetFeature(117)
    source_bytes = bytes(feature.GetGeometryRef().GetLinearGeometry().ExportToWkb())
    require(hashlib.sha256(source_bytes).hexdigest() == record["original_sha256"], "Direct archive geometry differs")
    require(not feature.GetGeometryRef().HasCurveGeometry(1), "Unresolved nonlinear source boundary")
    attributes = json.loads(record["attributes_json"])
    require(all((None if feature.GetField(key) is None else str(feature.GetField(key))) == value
                for key, value in attributes.items()), "Source attributes differ")
    diagnosis = json.loads((home / "verification/kremmling-ring-diagnosis.json").read_text(encoding="utf-8"))
    grid_size = 1 / diagnosis["source_precision"]["XYScale"]
    overlay_area_allowance = grid_size ** 2
    rings = [list(ring.coords) for polygon in source.geoms for ring in [polygon.exterior, *polygon.interiors]]
    loops = [loop for ring in rings for loop in cycles(ring)]
    cw = [loop for loop in loops if signed_area(loop) < 0]
    ccw = [loop for loop in loops if signed_area(loop) > 0]
    require(len(cw) + len(ccw) == len(loops), "Zero-area source cycle needs review")
    shells = [shapely.Polygon(loop) for loop in cw]
    holes = [shapely.Polygon(loop) for loop in ccw]
    require(all(g.is_valid for g in shells + holes), "Invalid decomposed source cycle")
    shell_union = shapely.union_all(shells)
    hole_union = shapely.union_all(holes)
    # Independently define supported area using source outer shells and holes.
    # An orphan hole cannot establish public area outside an outer shell.
    supported = shell_union.difference(hole_union)
    excluded = []
    retained = []
    for index, polygon in enumerate(previous.geoms):
        outside_area = polygon.difference(shell_union).area
        if outside_area > overlay_area_allowance:
            inside_area = polygon.intersection(shell_union).area
            require(inside_area == 0, "Candidate component partly inside public shell; no automatic removal")
            excluded.append((index, polygon, outside_area, inside_area))
        else:
            retained.append(polygon)
    require(len(excluded) == 1 and excluded[0][1].area < 1, "Unexpected or substantial source exclusion")
    replacement = shapely.MultiPolygon(retained)
    require(replacement.is_valid and not replacement.is_empty, "Replacement not valid polygon area")
    # Copy existing supported components unchanged: no snapping or node movement.
    retained_hashes = [hashlib.sha256(shapely.to_wkb(g, byte_order=1)).hexdigest() for g in retained]
    output_hashes = [hashlib.sha256(shapely.to_wkb(g, byte_order=1)).hexdigest() for g in replacement.geoms]
    require(retained_hashes == output_hashes, "Retained component coordinates changed")
    additions = replacement.difference(previous).area
    difference = replacement.symmetric_difference(supported).area
    hole_overlap = replacement.intersection(hole_union).area
    residual_outside = replacement.difference(shell_union).area
    noded_source = shapely.node(shapely.MultiLineString(rings))
    unsupported_boundary_length = replacement.boundary.difference(noded_source).length
    require(additions == 0, "Replacement adds area")
    require(difference == 0, "Replacement differs from independent source shell-minus-hole region")
    require(hole_overlap == 0, "Replacement fills a source hole")
    require(residual_outside <= overlay_area_allowance, "Replacement has unsupported exterior area")
    require(unsupported_boundary_length == 0, "Replacement introduces a boundary outside original source linework")
    publisher_area = float(feature.GetField("SHAPE_Area"))
    publisher_difference = replacement.area - publisher_area
    require(abs(publisher_difference) <= 4046.8564224 and abs(publisher_difference) / publisher_area * 100 <= 0.01,
            "Stored publisher area comparison exceeds existing thresholds")
    require(file_hash(archive) == original_archive_hash and file_hash(audit) == audit_hash,
            "Original archive or earlier audit changed")
    out.mkdir(parents=True)
    artifacts = {}
    for name, geometry in [("accepted-conservative-replacement.wkb", replacement),
                           ("independent-source-shell-minus-holes.wkb", supported),
                           ("excluded-unsupported-fragment.wkb", excluded[0][1])]:
        body = shapely.to_wkb(geometry, byte_order=1)
        (out / name).write_bytes(body)
        artifacts[name] = {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    report = {
        "date": "2026-10-21", "created_utc": datetime.now(timezone.utc).isoformat(),
        "decision": "accepted_conservative_replacement_previous_candidate_rejected",
        "source_layer": "PADUS4_1Fee", "source_fid": 117, "source_name": feature.GetField("Unit_Nm"),
        "source_archive_sha256": original_archive_hash, "source_original_wkb_sha256": record["original_sha256"],
        "previous_candidate_wkb_sha256": record["repaired_sha256"], "source_attributes_match_audit": True,
        "credit": "Margaret", "source_simple_clockwise_shells": len(cw), "source_simple_counterclockwise_holes": len(ccw),
        "previous_candidate_parts": len(previous.geoms), "replacement_parts": len(replacement.geoms),
        "excluded_candidate_part_index": excluded[0][0], "excluded_fragment_area_m2": excluded[0][1].area,
        "excluded_fragment_area_cm2": excluded[0][1].area * 10000,
        "excluded_fragment_intersection_source_shell_area_m2": excluded[0][3],
        "replacement_area_m2": replacement.area, "previous_candidate_area_m2": previous.area,
        "area_added_m2": additions, "comparison_area_from_symmetric_difference_m2": difference,
        "replacement_overlap_source_holes_m2": hole_overlap,
        "replacement_outside_source_shell_residual_m2": residual_outside,
        "numeric_overlay_area_allowance_m2": overlay_area_allowance,
        "numeric_allowance_basis": "One cell of the published 0.0001 m source coordinate grid; only an overlay numerical-residue allowance, not ground-truth accuracy or a snapping operation.",
        "replacement_boundary_not_in_noded_original_source_length_m": unsupported_boundary_length,
        "all_retained_components_byte_identical": retained_hashes == output_hashes,
        "retained_component_sha256": retained_hashes,
        "publisher_shape_area_m2": publisher_area, "replacement_minus_publisher_area_m2": publisher_difference,
        "replacement_minus_publisher_percent": publisher_difference / publisher_area * 100,
        "original_archive_unchanged": True, "previous_audit_unchanged": True,
        "previous_audit_sha256": audit_hash, "artifacts": artifacts,
        "interpretation": "The source contains an orphan counterclockwise sliver crossing an outer shell. The previous even-odd repair fills a disconnected fragment outside every source shell. The replacement excludes that unsupported fragment under the already agreed unconfirmed-eligibility exclusion; it neither adds area nor changes retained coordinates.",
        "limits": "This establishes a conservative source-supported representation, not the source author's intended treatment of the orphan sliver or surveyed boundary truth. No assumption that the fragment is privately owned is made; another independently eligible source record could cover it.",
        "scope": "Specific derived geometry for this record only. All private/military/restricted/closed and unconfirmed-eligibility exclusions still apply before county aggregation. No national repair restart or runtime integration in this investigation."
    }
    destination = home / "verification/kremmling-resolution.json"
    require(not destination.exists(), "Refuse to replace prior decision")
    destination.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ["decision", "excluded_fragment_area_m2", "excluded_fragment_area_cm2",
                     "replacement_parts", "area_added_m2", "comparison_area_from_symmetric_difference_m2",
                     "replacement_boundary_not_in_noded_original_source_length_m", "all_retained_components_byte_identical",
                     "replacement_outside_source_shell_residual_m2", "replacement_minus_publisher_area_m2"]}), flush=True)


if __name__ == "__main__":
    main()
