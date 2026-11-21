"""Reconcile one flagged repair with original USGS rings; no source edits."""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import struct
import xml.etree.ElementTree as ET

import numpy as np
from osgeo import gdal
import shapely


def signed_area(coordinates):
    xy = np.asarray(coordinates, dtype="float64")
    x, y = xy[:, 0] - xy[0, 0], xy[:, 1] - xy[0, 1]
    return math.fsum((x[:-1] * y[1:] - x[1:] * y[:-1]).tolist()) / 2


def cycles(coordinates):
    """Split a closed walk at repeated vertices without moving coordinates."""
    walk, location, result = [], {}, []
    for raw in coordinates:
        point = tuple(map(float, raw[:2]))
        if walk and point == walk[-1]:
            continue
        if point in location:
            start = location[point]
            cycle = walk[start:] + [point]
            if len(cycle) >= 4:
                result.append(cycle)
            for removed in walk[start + 1:]:
                del location[removed]
            walk = walk[:start + 1]
        else:
            location[point] = len(walk)
            walk.append(point)
    if len(walk) != 1:
        raise RuntimeError("Ring walk did not close after exact-vertex splitting")
    return result


def ring_hash(coordinates):
    points = []
    for point in coordinates:
        value = tuple(map(float, point[:2]))
        if not points or value != points[-1]:
            points.append(value)
    if points and points[-1] == points[0]:
        points.pop()
    if len(set(points)) != len(points):
        raise RuntimeError("Canonical ring still has repeated vertices")
    start = min(range(len(points)), key=points.__getitem__)
    points = points[start:] + points[:start]
    return hashlib.sha256(b"".join(struct.pack("<dd", *point) for point in points)).hexdigest()


def boundary_segments(rings):
    counts = Counter()
    for coordinates in rings:
        points = [tuple(map(float, point[:2])) for point in coordinates]
        for a, b in zip(points, points[1:]):
            if a != b:
                counts[tuple(sorted((a, b)))] += 1
    return counts


def main():
    home = Path(__file__).resolve().parents[1]
    archive = home / "source/padus-4.1/PADUS4_1Geodatabase.zip"
    acquisition = json.loads((archive.parent / "full-inventory-acquisition.json").read_text(encoding="utf-8"))
    with archive.open("rb") as source:
        source_hash = hashlib.file_digest(source, "sha256").hexdigest()
    if source_hash != acquisition["sha256"]:
        raise RuntimeError("Original archive differs from recorded source")
    audit = home / "working/controlled-repair-2026-10-21/repair_audit.sqlite3"
    with sqlite3.connect(audit.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        record = dict(connection.execute("SELECT * FROM repairs WHERE layer='PADUS4_1Fee' AND source_fid=78").fetchone())
    original = shapely.from_wkb(record["original_wkb"])
    repaired = shapely.from_wkb(record["repaired_wkb"])
    gdal.UseExceptions()
    gdal.SetConfigOption("OGR_ORGANIZE_POLYGONS", None)
    path = "/vsizip/" + archive.resolve().as_posix() + "/PADUS4_1Geodatabase.gdb"
    dataset = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_READONLY, allowed_drivers=["OpenFileGDB"])
    layer = dataset.GetLayerByName("PADUS4_1Fee")
    feature = layer.GetFeature(78)
    source_geometry = feature.GetGeometryRef()
    linear_geometry = source_geometry.GetLinearGeometry()
    raw_wkb = bytes(linear_geometry.ExportToWkb())
    raw_hash = hashlib.sha256(raw_wkb).hexdigest()
    if raw_hash != record["original_sha256"]:
        raise RuntimeError("Direct original-ZIP geometry differs from audited input")
    publisher_area = float(feature.GetField("SHAPE_Area"))
    source_rings, promoted_holes = [], []
    for index, polygon in enumerate(original.geoms):
        exterior = list(polygon.exterior.coords)
        source_rings.append(exterior)
        area = signed_area(exterior)
        if area > 0:
            promoted_holes.append({"decoded_polygon_index": index, "area_m2": area,
                                   "ring_sha256": ring_hash(exterior)})
        source_rings.extend(list(hole.coords) for hole in polygon.interiors)
    simple_cycles = [cycle for ring in source_rings for cycle in cycles(ring)]
    outer = [cycle for cycle in simple_cycles if signed_area(cycle) < 0]
    inner = [cycle for cycle in simple_cycles if signed_area(cycle) > 0]
    zero = [cycle for cycle in simple_cycles if signed_area(cycle) == 0]
    shells = [shapely.Polygon(coordinates) for coordinates in outer]
    if any(not polygon.is_valid for polygon in shells) or zero:
        raise RuntimeError("Exact source-ring decomposition has invalid/zero cycles")
    tree = shapely.STRtree(shells)
    holes = [[] for _ in shells]
    assignments = []
    for index, ring in enumerate(inner):
        hole = shapely.Polygon(ring)
        if not hole.is_valid:
            raise RuntimeError("Exact source hole remains invalid")
        candidates = [int(candidate) for candidate in tree.query(hole)
                      if shells[int(candidate)].covers(hole)]
        if not candidates:
            raise RuntimeError("Counterclockwise source hole has no containing shell")
        candidate = min(candidates, key=lambda value: shells[value].area)
        holes[candidate].append(ring)
        assignments.append({"hole_index": index, "outer_index": candidate, "candidate_shells": len(candidates)})
    reconstructed = shapely.MultiPolygon([shapely.Polygon(ring, holes[index]) for index, ring in enumerate(outer)])
    reconstructed_wkb = shapely.to_wkb(reconstructed, byte_order=1)
    output_rings = [list(ring.coords) for polygon in repaired.geoms for ring in [polygon.exterior, *polygon.interiors]]
    source_cycle_hashes = Counter(ring_hash(ring) for ring in simple_cycles)
    output_hashes = Counter(ring_hash(ring) for ring in output_rings)
    source_segments, output_segments = boundary_segments(source_rings), boundary_segments(output_rings)
    symdiff = reconstructed.symmetric_difference(repaired).area if reconstructed.is_valid else None
    promoted_area = math.fsum(value["area_m2"] for value in promoted_holes)
    signed_source_area = -math.fsum(signed_area(ring) for ring in source_rings)
    explained_difference = 2 * promoted_area
    observed_difference = original.area - repaired.area
    rings_equal = source_cycle_hashes == output_hashes
    segments_equal = source_segments == output_segments
    report = {
        "investigated_utc": datetime.now(timezone.utc).isoformat(), "source_archive_sha256": source_hash,
        "source_item_id": acquisition["item_id"], "source_layer": "PADUS4_1Fee", "source_fid": 78,
        "source_name": feature.GetField("Unit_Nm"), "source_attributes_unchanged": True,
        "gdal_reading_zip": gdal.VersionInfo("--version"), "shapely": shapely.__version__,
        "direct_zip_original_wkb_sha256": raw_hash, "matches_original_repair_audit": True,
        "source_geometry_type": source_geometry.GetGeometryName(),
        "source_contains_actual_nonlinear_segments": bool(source_geometry.HasCurveGeometry(1)),
        "original_decoded_polygon_parts": len(original.geoms), "original_decoded_holes": sum(len(p.interiors) for p in original.geoms),
        "all_source_rings": len(source_rings), "exterior_slots_with_hole_winding": len(promoted_holes),
        "promoted_holes": promoted_holes, "sum_promoted_hole_areas_m2": promoted_area,
        "twice_promoted_hole_area_m2": explained_difference,
        "observed_decoded_vs_repaired_difference_m2": observed_difference,
        "unexplained_difference_m2": observed_difference - explained_difference,
        "signed_source_ring_area_m2": signed_source_area, "repaired_area_m2": repaired.area,
        "publisher_shape_area_m2": publisher_area, "repair_minus_publisher_area_m2": repaired.area - publisher_area,
        "source_simple_cycles": len(simple_cycles), "clockwise_shells": len(outer), "counterclockwise_holes": len(inner),
        "zero_area_cycles": len(zero), "hole_assignments": assignments,
        "reconstructed_valid": bool(reconstructed.is_valid), "reconstructed_area_m2": reconstructed.area,
        "reconstructed_vs_existing_repair_symmetric_difference_m2": symdiff,
        "all_oriented_cycle_hashes_match_existing_repair": rings_equal,
        "all_exact_undirected_boundary_segment_multiplicities_match": segments_equal,
        "source_boundary_segments": sum(source_segments.values()), "repaired_boundary_segments": sum(output_segments.values()),
        "source_only_segment_instances": sum((source_segments - output_segments).values()),
        "repaired_only_segment_instances": sum((output_segments - source_segments).values()),
        "reconstruction_wkb_sha256": hashlib.sha256(reconstructed_wkb).hexdigest(),
        "geometry_reconstruction_method": "Split exact repeated vertices; clockwise shells/counterclockwise holes; assign holes to smallest containing source shell. No make_valid, buffer, snapping or coordinate changes.",
        "source_docs": ["https://doc.esri.com/en/arcgis-pro/latest/arcpy/get-started/reading-geometries.html",
                        "https://gdal.org/en/stable/doxygen/classOGRGeometryFactory.html"],
        "acceptance_scope": "Only this existing flagged geometry; no blanket acceptance of other repairs or ownership/access correctness.",
        "decision": "unresolved",
    }
    if (reconstructed.is_valid and repaired.is_valid and symdiff == 0 and rings_equal and segments_equal
            and abs(observed_difference - explained_difference) < 0.01
            and abs(signed_source_area - repaired.area) < 0.01
            and abs(repaired.area - publisher_area) < 4046.8564224
            and abs(repaired.area - publisher_area) / publisher_area * 100 < 0.01):
        report["decision"] = "flagged_repair_reconciled_against_source_rings"
    destination = home / "verification/ring-investigation-fid78.json"
    if destination.exists():
        raise RuntimeError("Refuse to replace an existing investigation receipt")
    destination.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    (home / "working/controlled-repair-2026-10-21/fid78-source-ring-reconstruction.wkb").write_bytes(reconstructed_wkb)
    print(json.dumps({key: report[key] for key in ["decision", "exterior_slots_with_hole_winding", "unexplained_difference_m2",
                      "source_contains_actual_nonlinear_segments", "source_simple_cycles", "clockwise_shells", "counterclockwise_holes",
                      "reconstructed_valid", "reconstructed_vs_existing_repair_symmetric_difference_m2",
                      "all_oriented_cycle_hashes_match_existing_repair", "all_exact_undirected_boundary_segment_multiplicities_match",
                      "source_only_segment_instances", "repaired_only_segment_instances"]}), flush=True)


if __name__ == "__main__":
    main()
