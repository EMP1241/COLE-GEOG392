"""Diagnose Upper Snake against the original USGS ZIP without changing sources."""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from osgeo import gdal
import shapely

from investigate_rings import boundary_segments, cycles, ring_hash, signed_area


def digest_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    home = Path(__file__).resolve().parents[1]
    out = home / "working/upper-snake-investigation-2026-10-21"
    destination = home / "verification/upper-snake-diagnosis.json"
    require(not out.exists() and not destination.exists(), "Refuse to overwrite investigation")
    archive = home / "source/padus-4.1/PADUS4_1Geodatabase.zip"
    receipt = json.loads((archive.parent / "full-inventory-acquisition.json").read_text(encoding="utf-8"))
    archive_hash = digest_file(archive)
    require(archive_hash == receipt["sha256"], "Original archive differs")
    audit = home / "working/national-repair-2026-10-21/repair_audit.sqlite3"
    audit_hash = digest_file(audit)
    require(audit_hash == "6d62bb98a9a01afcdc059cf42392470a32cd41d51758ca7a43168305b0eaf215", "Previous audit differs")
    with sqlite3.connect(audit.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        record = dict(connection.execute("SELECT * FROM repairs WHERE layer='PADUS4_1Fee' AND source_fid=163").fetchone())
    gdal.UseExceptions()
    dataset = gdal.OpenEx("/vsizip/" + archive.resolve().as_posix() + "/PADUS4_1Geodatabase.gdb",
                          gdal.OF_VECTOR | gdal.OF_READONLY, allowed_drivers=["OpenFileGDB"])
    feature = dataset.GetLayerByName("PADUS4_1Fee").GetFeature(163)
    native = feature.GetGeometryRef()
    body = bytes(native.GetLinearGeometry().ExportToWkb())
    require(hashlib.sha256(body).hexdigest() == record["original_sha256"], "Original ZIP geometry differs from audit")
    attributes = json.loads(record["attributes_json"])
    require(all((None if feature.GetField(key) is None else str(feature.GetField(key))) == value
                for key, value in attributes.items()), "Original ZIP attributes differ from audit")
    require(not native.HasCurveGeometry(1), "Actual nonlinear source segments require review")
    source = shapely.from_wkb(body)
    candidate = shapely.from_wkb(record["polygon_wkb"])
    rings = [list(ring.coords) for polygon in source.geoms for ring in [polygon.exterior, *polygon.interiors]]
    simple = [cycle for ring in rings for cycle in cycles(ring)]
    outer = [ring for ring in simple if signed_area(ring) < 0]
    inner = [ring for ring in simple if signed_area(ring) > 0]
    require(len(outer) + len(inner) == len(simple), "Zero-area source cycle")
    shells = [shapely.Polygon(ring) for ring in outer]
    holes = [shapely.Polygon(ring) for ring in inner]
    require(all(g.is_valid for g in shells + holes), "Invalid individual source cycle")
    tree = shapely.STRtree(shells)
    assigned = [[] for _ in shells]
    assignments = []
    for index, hole in enumerate(holes):
        containing = [int(i) for i in tree.query(hole) if shells[int(i)].covers(hole)]
        require(containing, "Source hole lacks containing shell")
        target = min(containing, key=lambda i: shells[i].area)
        assigned[target].append(index)
        assignments.append({"hole": index, "shell": target, "containing_shells": containing})
    reconstructed_parts = [shapely.Polygon(ring, [inner[j] for j in assigned[i]]) for i, ring in enumerate(outer)]
    reconstructed = shapely.MultiPolygon(reconstructed_parts)
    bad_parts = [{"shell_index": i, "reason": shapely.is_valid_reason(p), "holes": assigned[i],
                  "area_m2": p.area, "bounds": list(p.bounds)}
                 for i, p in enumerate(reconstructed_parts) if not p.is_valid]
    shell_interactions = []
    for i, shell in enumerate(shells):
        for raw in tree.query(shell, predicate="intersects"):
            j = int(raw)
            if j <= i:
                continue
            overlap = shell.intersection(shells[j])
            shared = shell.boundary.intersection(shells[j].boundary)
            shell_interactions.append({"a": i, "b": j, "intersection_area_m2": overlap.area,
                                       "shared_boundary_length_m": shared.length, "intersection_type": overlap.geom_type,
                                       "relation": shell.relate(shells[j])})
    hole_interactions = []
    for shell_index, indices in enumerate(assigned):
        for pos, i in enumerate(indices):
            for j in indices[pos + 1:]:
                if holes[i].intersects(holes[j]):
                    overlap = holes[i].intersection(holes[j])
                    shared = holes[i].boundary.intersection(holes[j].boundary)
                    hole_interactions.append({"shell": shell_index, "a": i, "b": j,
                                              "intersection_area_m2": overlap.area, "shared_boundary_length_m": shared.length,
                                              "intersection_type": overlap.geom_type, "relation": holes[i].relate(holes[j])})
    # Independently interpret source outer/interior roles. Comparison only;
    # this diagnosis does not automatically accept either geometry.
    shell_union = shapely.union_all(shells)
    hole_union = shapely.union_all(holes)
    supported = shell_union.difference(hole_union)
    output_rings = [list(ring.coords) for p in candidate.geoms for ring in [p.exterior, *p.interiors]]
    source_segments = boundary_segments(rings)
    candidate_segments = boundary_segments(output_rings)
    noded = shapely.node(shapely.MultiLineString(rings))
    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(), "decision": "diagnosis_only_not_accepted",
        "credit": "Margaret", "source_layer": "PADUS4_1Fee", "source_fid": 163,
        "source_name": feature.GetField("Unit_Nm"), "attributes": attributes,
        "source_archive_sha256": archive_hash, "previous_audit_sha256": audit_hash,
        "original_wkb_sha256": record["original_sha256"], "candidate_wkb_sha256": record["polygon_sha256"],
        "direct_archive_geometry_and_attributes_match_audit": True, "actual_nonlinear_segments": False,
        "source_rings": len(rings), "source_simple_cycles": len(simple),
        "clockwise_shells": len(shells), "counterclockwise_holes": len(holes),
        "all_individual_source_cycles_valid": True,
        "reconstructed_valid": bool(reconstructed.is_valid), "reconstructed_reason": shapely.is_valid_reason(reconstructed),
        "invalid_reconstructed_parts": bad_parts, "source_shell_interactions": shell_interactions,
        "assigned_hole_interactions": hole_interactions, "hole_assignments": assignments,
        "signed_source_ring_area_m2": -math.fsum(signed_area(ring) for ring in rings),
        "decoded_area_m2": source.area, "candidate_area_m2": candidate.area,
        "publisher_shape_area_m2": feature.GetField("SHAPE_Area"),
        "source_shell_minus_holes_valid": bool(supported.is_valid),
        "candidate_vs_source_shell_minus_holes_symmetric_difference_m2": candidate.symmetric_difference(supported).area,
        "candidate_outside_source_shell_area_m2": candidate.difference(shell_union).area,
        "candidate_overlap_source_hole_area_m2": candidate.intersection(hole_union).area,
        "candidate_boundary_absent_from_noded_source_length_m": candidate.boundary.difference(noded).length,
        "oriented_cycle_multisets_equal": Counter(ring_hash(r) for r in simple) == Counter(ring_hash(r) for r in output_rings),
        "exact_segment_multiplicities_equal": source_segments == candidate_segments,
        "source_only_segment_instances": sum((source_segments - candidate_segments).values()),
        "candidate_only_segment_instances": sum((candidate_segments - source_segments).values()),
        "repairs_performed": False,
    }
    require(digest_file(archive) == archive_hash and digest_file(audit) == audit_hash, "Originals or prior audit changed")
    report["original_archive_and_previous_audit_unchanged"] = True
    out.mkdir(parents=True)
    artifacts = {}
    for name, geometry in [("source-ring-reconstruction.wkb", reconstructed), ("source-shell-minus-holes.wkb", supported)]:
        data = shapely.to_wkb(geometry, byte_order=1)
        (out / name).write_bytes(data)
        artifacts[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    report["artifacts"] = artifacts
    destination.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"hole_assignments", "source_shell_interactions", "assigned_hole_interactions"}}), flush=True)
    print(json.dumps({"source_shell_interaction_count": len(shell_interactions),
                      "assigned_hole_interaction_count": len(hole_interactions)}), flush=True)


if __name__ == "__main__":
    main()
