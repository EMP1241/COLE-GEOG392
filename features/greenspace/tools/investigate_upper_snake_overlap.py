"""Explain the specific Upper Snake reconstruction failure; no acceptance."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

import shapely

from investigate_rings import cycles, signed_area
from investigate_upper_snake import digest_file, require


def main():
    home = Path(__file__).resolve().parents[1]
    out = home / "working/upper-snake-investigation-2026-10-21"
    dest = home / "verification/upper-snake-overlap-diagnosis.json"
    require(not dest.exists(), "Refuse to overwrite overlap diagnosis")
    diagnosis = json.loads((home / "verification/upper-snake-diagnosis.json").read_text(encoding="utf-8"))
    audit = home / "working/national-repair-2026-10-21/repair_audit.sqlite3"
    require(digest_file(audit) == diagnosis["previous_audit_sha256"], "Previous audit changed")
    with sqlite3.connect(audit.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT original_wkb,polygon_wkb FROM repairs WHERE layer='PADUS4_1Fee' AND source_fid=163").fetchone()
    source, candidate = map(shapely.from_wkb, row)
    reconstruction = shapely.from_wkb((out / "source-ring-reconstruction.wkb").read_bytes())
    parts = list(reconstruction.geoms)
    require(all(p.is_valid for p in parts), "Invalid individual reconstructed source polygon")
    tree = shapely.STRtree(parts)
    overlaps = []
    overlap_shapes = []
    for i, part in enumerate(parts):
        for raw in tree.query(part, predicate="intersects"):
            j = int(raw)
            if j <= i:
                continue
            common = part.intersection(parts[j])
            shared = part.boundary.intersection(parts[j].boundary)
            if common.area > 0 or shared.length > 0:
                overlaps.append({"a": i, "b": j, "intersection_area_m2": common.area,
                                 "shared_boundary_length_m": shared.length, "intersection_wkt": common.wkt,
                                 "part_a_area_m2": part.area, "part_b_area_m2": parts[j].area,
                                 "part_a_minus_candidate_m2": part.difference(candidate).area,
                                 "part_b_minus_candidate_m2": parts[j].difference(candidate).area})
                if common.area > 0:
                    overlap_shapes.append(common)
    union = shapely.union_all(parts)
    overlap_union = shapely.union_all(overlap_shapes)
    candidate_only = candidate.difference(union)
    union_only = union.difference(candidate)
    # Independent planar faces classified by signed source winding coverage,
    # including positive islands nested inside holes of a larger shell.
    rings = [list(r.coords) for p in source.geoms for r in [p.exterior, *p.interiors]]
    simple = [c for r in rings for c in cycles(r)]
    shells = [shapely.Polygon(c) for c in simple if signed_area(c) < 0]
    holes = [shapely.Polygon(c) for c in simple if signed_area(c) > 0]
    shell_tree, hole_tree = shapely.STRtree(shells), shapely.STRtree(holes)
    noded = shapely.node(shapely.MultiLineString(rings))
    faces = list(shapely.polygonize(shapely.get_parts(noded)).geoms)
    positives, negatives, zero_count, winding_counts = [], [], 0, {}
    for face in faces:
        point = face.representative_point()
        winding = len(shell_tree.query(point, predicate="within")) - len(hole_tree.query(point, predicate="within"))
        winding_counts[str(winding)] = winding_counts.get(str(winding), 0) + 1
        if winding > 0:
            positives.append(face)
        elif winding < 0:
            negatives.append(face)
        else:
            zero_count += 1
    positive_region = shapely.union_all(positives)
    report = {"created_utc": datetime.now(timezone.utc).isoformat(), "decision": "diagnosis_only_not_accepted",
              "credit": "Margaret", "source_layer": "PADUS4_1Fee", "source_fid": 163,
              "actual_source_polygon_overlaps": overlaps,
              "all_individual_source_polygons_valid": True,
              "source_parts_union_valid": bool(union.is_valid), "source_parts_union_area_m2": union.area,
              "signed_source_area_m2": diagnosis["signed_source_ring_area_m2"],
              "candidate_area_m2": candidate.area,
              "union_minus_candidate_area_m2": union_only.area,
              "candidate_minus_union_area_m2": candidate_only.area,
              "source_polygon_overlap_union_area_m2": overlap_union.area,
              "union_only_vs_source_overlap_symmetric_difference_m2": union_only.symmetric_difference(overlap_union).area,
              "candidate_contains_source_overlap_area_m2": candidate.intersection(overlap_union).area,
              "union_boundary_absent_from_noded_source_length_m": union.boundary.difference(noded).length,
              "candidate_boundary_absent_from_noded_source_length_m": candidate.boundary.difference(noded).length,
              "polygonized_source_faces": len(faces), "source_winding_face_counts": winding_counts,
              "negative_winding_area_m2": math.fsum(p.area for p in negatives),
              "source_positive_winding_valid": bool(positive_region.is_valid),
              "source_parts_union_vs_positive_winding_symmetric_difference_m2": union.symmetric_difference(positive_region).area,
              "publisher_shape_area_m2": float(diagnosis["publisher_shape_area_m2"]),
              "union_minus_publisher_area_m2": union.area - float(diagnosis["publisher_shape_area_m2"]),
              "repairs_performed": False, "artifacts": {}}
    for name, geom in [("source-valid-parts-union.wkb", union), ("source-positive-winding-region.wkb", positive_region),
                       ("source-polygon-overlap.wkb", overlap_union), ("union-minus-candidate.wkb", union_only)]:
        path = out / name
        require(not path.exists(), "Refuse to overwrite derivative")
        data = shapely.to_wkb(geom, byte_order=1)
        path.write_bytes(data)
        report["artifacts"][name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    require(digest_file(audit) == diagnosis["previous_audit_sha256"], "Previous audit changed")
    dest.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
