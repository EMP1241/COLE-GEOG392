"""Diagnose NJ Fee FID 41938 from the preserved original ZIP; no acceptance."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from osgeo import gdal
import shapely

from investigate_rings import cycles, signed_area
from investigate_upper_snake import digest_file, require
from repair_geometry import parts


def main():
    home = Path(__file__).resolve().parents[1]
    out = home / "working/nj41938-investigation-2026-10-21"
    destination = home / "verification/nj41938-diagnosis.json"
    require(not out.exists() and not destination.exists(), "Refuse to overwrite investigation")
    archive = home / "source/padus-4.1/PADUS4_1Geodatabase.zip"
    acquisition = json.loads((archive.parent / "full-inventory-acquisition.json").read_text(encoding="utf-8"))
    archive_hash = digest_file(archive)
    require(archive_hash == acquisition["sha256"], "Original archive changed")
    audit = home / "working/national-repair-upper-snake-2026-10-21/repair_audit.sqlite3"
    audit_hash = digest_file(audit)
    require(audit_hash == "d1793aab8a9878796b4630e6b34cad30c0ae70b67cab8b39da213114880fa033", "Previous audit changed")
    with sqlite3.connect(audit.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        record = dict(connection.execute("SELECT * FROM repairs WHERE layer='PADUS4_1Fee' AND source_fid=41938").fetchone())
    gdal.UseExceptions()
    dataset = gdal.OpenEx("/vsizip/" + archive.resolve().as_posix() + "/PADUS4_1Geodatabase.gdb",
                          gdal.OF_VECTOR | gdal.OF_READONLY, allowed_drivers=["OpenFileGDB"])
    feature = dataset.GetLayerByName("PADUS4_1Fee").GetFeature(41938)
    native = feature.GetGeometryRef()
    body = bytes(native.GetLinearGeometry().ExportToWkb())
    require(hashlib.sha256(body).hexdigest() == record["original_sha256"], "Original geometry differs from audit")
    attributes = json.loads(record["attributes_json"])
    require(all((None if feature.GetField(k) is None else str(feature.GetField(k))) == v
                for k,v in attributes.items()), "Original attributes differ from audit")
    # Curved source is investigated and preserved separately. This diagnostic
    # refers explicitly to its recorded default linearization, not exact arcs.
    source = shapely.from_wkb(body)
    candidate = shapely.from_wkb(record["repaired_wkb"])
    polygon = shapely.from_wkb(record["polygon_wkb"])
    rings = [list(r.coords) for p in source.geoms for r in [p.exterior, *p.interiors]]
    loops = [c for r in rings for c in cycles(r)]
    outer = [c for c in loops if signed_area(c) < 0]
    inner = [c for c in loops if signed_area(c) > 0]
    zero = [c for c in loops if signed_area(c) == 0]
    shells = [shapely.Polygon(c) for c in outer]
    holes = [shapely.Polygon(c) for c in inner]
    require(not zero and shells and all(p.is_valid for p in shells+holes), "Invalid or zero-area source cycle")
    shell_tree, hole_tree = shapely.STRtree(shells), shapely.STRtree(holes)
    assigned = [[] for _ in shells]
    containment = []
    for i,hole in enumerate(holes):
        choices = [int(j) for j in shell_tree.query(hole) if shells[int(j)].covers(hole)]
        containment.append({"hole": i, "containing_shells": choices})
        if choices:
            assigned[min(choices,key=lambda j:shells[j].area)].append(i)
    reconstructed_parts = [shapely.Polygon(r, [inner[j] for j in assigned[i]]) for i,r in enumerate(outer)]
    reconstruction = shapely.MultiPolygon(reconstructed_parts)
    interactions = []
    for i, indices in enumerate(assigned):
        for j in indices:
            shared = shells[i].boundary.intersection(holes[j].boundary)
            interactions.append({"shell": i, "hole": j, "shared_boundary_type": shared.geom_type,
                                 "shared_boundary_length_m": shared.length, "shared_boundary_wkt": shared.wkt})
    noded = shapely.node(shapely.MultiLineString(rings))
    faces = list(shapely.polygonize(shapely.get_parts(noded)).geoms)
    positive, negative, empty_faces, counts = [], [], [], {}
    for face in faces:
        point = face.representative_point()
        winding = len(shell_tree.query(point,predicate="within")) - len(hole_tree.query(point,predicate="within"))
        counts[str(winding)] = counts.get(str(winding),0) + 1
        (positive if winding > 0 else negative if winding < 0 else empty_faces).append(face)
    positive_region = shapely.union_all(positive)
    # This independent shell-minus-hole set is only compared for this record;
    # it must not silently erase nested islands or be generalized nationally.
    shell_union, hole_union = shapely.union_all(shells), shapely.union_all(holes)
    shell_minus_holes = shell_union.difference(hole_union)
    polygon_parts, line_parts, point_parts = parts(candidate)
    lines = shapely.union_all(line_parts)
    surface_boundaries = polygon.boundary
    source_non_surface = noded.difference(surface_boundaries)
    promoted_area = math.fsum(signed_area(list(p.exterior.coords)) for p in source.geoms
                              if signed_area(list(p.exterior.coords)) > 0)
    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(), "decision": "diagnosis_only_not_accepted",
        "credit": "Margaret", "source_layer": "PADUS4_1Fee", "source_fid": 41938,
        "source_attributes": attributes, "source_archive_sha256": archive_hash,
        "previous_audit_sha256": audit_hash, "original_wkb_sha256": record["original_sha256"],
        "candidate_wkb_sha256": record["repaired_sha256"], "candidate_polygon_wkb_sha256": record["polygon_sha256"],
        "direct_original_geometry_and_attributes_match": True, "actual_nonlinear_segments": bool(native.HasCurveGeometry(1)),
        "linearized_source_scope": "Recorded GDAL default GetLinearGeometry; native curve evidence is separate and must be resolved before acceptance",
        "decoded_polygon_parts": len(source.geoms), "source_rings": len(rings), "source_cycles": len(loops),
        "clockwise_shells": len(shells), "counterclockwise_holes": len(holes), "source_hole_containment": containment,
        "source_cycle_signed_areas_m2": [signed_area(c) for c in loops],
        "reconstruction_valid": bool(reconstruction.is_valid), "reconstruction_validity_reason": shapely.is_valid_reason(reconstruction),
        "reconstructed_part_validity": [{"index":i,"valid":bool(p.is_valid),"reason":shapely.is_valid_reason(p)} for i,p in enumerate(reconstructed_parts)],
        "shell_hole_boundary_interactions": interactions, "source_winding_face_counts": counts,
        "negative_winding_area_m2": math.fsum(p.area for p in negative),
        "positive_winding_region_valid": bool(positive_region.is_valid),
        "positive_winding_vs_candidate_polygon_symmetric_difference_m2": positive_region.symmetric_difference(polygon).area,
        "shell_minus_holes_vs_candidate_polygon_symmetric_difference_m2": shell_minus_holes.symmetric_difference(polygon).area,
        "candidate_overlap_zero_winding_faces_m2": polygon.intersection(shapely.union_all(empty_faces)).area,
        "candidate_boundary_absent_from_original_noded_linework_m": surface_boundaries.difference(noded).length,
        "source_linework_absent_from_candidate_boundary_and_lines_m": noded.difference(shapely.union_all([surface_boundaries,lines])).length,
        "candidate_lines_absent_from_original_noded_linework_m": lines.difference(noded).length,
        "source_non_surface_linework_vs_candidate_lines_symmetric_difference_length_m": source_non_surface.symmetric_difference(lines).length,
        "candidate_polygon_parts":len(polygon_parts), "candidate_line_parts":len(line_parts), "candidate_point_parts":len(point_parts),
        "candidate_lines_wkt": lines.wkt, "candidate_line_length_m":lines.length,
        "decoded_area_m2": source.area, "candidate_area_m2":polygon.area,
        "signed_source_ring_area_m2":-math.fsum(signed_area(r) for r in rings),
        "exterior_slot_counterclockwise_area_m2":promoted_area,
        "decoded_minus_candidate_area_m2":source.area-polygon.area,
        "twice_promoted_area_m2":2*promoted_area,
        "unexplained_decoded_area_difference_m2":source.area-polygon.area-2*promoted_area,
        "publisher_shape_area_m2":float(feature.GetField("SHAPE_Area")),
        "source_ownership_remains_excluded":attributes["Own_Type"]=="NGO", "repairs_performed":False,
        "artifacts":{},
    }
    require(digest_file(archive)==archive_hash and digest_file(audit)==audit_hash,"Preserved input changed")
    out.mkdir(parents=True)
    for name,geometry in [("source-ring-reconstruction.wkb",reconstruction), ("source-positive-winding-region.wkb",positive_region),
                          ("source-shell-minus-holes.wkb",shell_minus_holes),("source-noded-linework.wkb",noded),
                          ("candidate-collapsed-lines.wkb",lines)]:
        data=shapely.to_wkb(geometry,byte_order=1)
        (out/name).write_bytes(data)
        report["artifacts"][name]={"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()}
    report["original_archive_and_previous_audit_unchanged"]=True
    destination.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps(report),flush=True)


if __name__ == "__main__":
    main()
