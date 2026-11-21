"""Read-only diagnosis of Kremmling rings directly from the original USGS ZIP."""

import hashlib
import json
import math
from pathlib import Path
import sqlite3
import xml.etree.ElementTree as ET

import shapely
from osgeo import gdal

from investigate_rings import cycles, signed_area


def main():
    home = Path(__file__).resolve().parents[1]
    archive = home / "source/padus-4.1/PADUS4_1Geodatabase.zip"
    acquisition = json.loads((archive.parent / "full-inventory-acquisition.json").read_text(encoding="utf-8"))
    with archive.open("rb") as stream:
        archive_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    if archive_hash != acquisition["sha256"]:
        raise RuntimeError("Original source changed")
    audit = home / "working/source-checked-repair-2026-10-21/repair_audit.sqlite3"
    with sqlite3.connect(audit.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(connection.execute("SELECT * FROM repairs WHERE layer='PADUS4_1Fee' AND source_fid=117").fetchone())
    original, repaired = shapely.from_wkb(row["original_wkb"]), shapely.from_wkb(row["repaired_wkb"])
    gdal.UseExceptions()
    dataset = gdal.OpenEx("/vsizip/" + archive.resolve().as_posix() + "/PADUS4_1Geodatabase.gdb",
                          gdal.OF_VECTOR | gdal.OF_READONLY, allowed_drivers=["OpenFileGDB"])
    feature = dataset.GetLayerByName("PADUS4_1Fee").GetFeature(117)
    native = feature.GetGeometryRef()
    wkb = bytes(native.GetLinearGeometry().ExportToWkb())
    if hashlib.sha256(wkb).hexdigest() != row["original_sha256"]:
        raise RuntimeError("Direct original-ZIP geometry differs from audit")
    attributes = json.loads(row["attributes_json"])
    attributes_match = all((None if feature.GetField(key) is None else str(feature.GetField(key))) == value
                           for key, value in attributes.items())
    if not attributes_match:
        raise RuntimeError("Source attributes differ from audit")
    definition = dataset.ExecuteSQL("GetLayerDefinition PADUS4_1Fee")
    xml = definition.GetNextFeature().GetField(0)
    precision = {node.tag.split("}")[-1]: float(node.text) for node in ET.fromstring(xml).iter()
                 if node.tag.split("}")[-1] in {"XOrigin", "YOrigin", "XYScale", "XYTolerance"}}
    dataset.ReleaseResultSet(definition)
    rings = [list(ring.coords) for polygon in original.geoms for ring in [polygon.exterior, *polygon.interiors]]
    decomposed = [cycle for ring in rings for cycle in cycles(ring)]
    outer = [ring for ring in decomposed if signed_area(ring) < 0]
    inner = [ring for ring in decomposed if signed_area(ring) > 0]
    shells = [shapely.Polygon(ring) for ring in outer]
    tree = shapely.STRtree(shells)
    failures = []
    containment_counts = []
    for index, ring in enumerate(inner):
        hole = shapely.Polygon(ring)
        candidates = [int(value) for value in tree.query(hole)]
        covered = [value for value in candidates if shells[value].covers(hole)]
        containment_counts.append(len(covered))
        if covered:
            continue
        diagnostics = []
        for candidate in candidates:
            shell = shells[candidate]
            outside = hole.difference(shell)
            coordinates = shapely.get_coordinates(outside)
            max_vertex_distance = float(shapely.distance(shapely.points(coordinates), shell).max()) if len(coordinates) else 0.0
            diagnostics.append({"shell_index": candidate, "shell_area_m2": shell.area,
                                "hole_minus_shell_area_m2": outside.area,
                                "hole_minus_shell_length_m": outside.length,
                                "outside_type": outside.geom_type,
                                "outside_bounds": list(outside.bounds) if not outside.is_empty else None,
                                "max_outside_vertex_distance_to_shell_m": max_vertex_distance,
                                "relation": hole.relate(shell),
                                "outside_wkt": outside.wkt if len(outside.wkt) < 3000 else None})
        failures.append({"hole_index": index, "hole_area_m2": hole.area, "hole_valid": bool(hole.is_valid),
                         "hole_bounds": list(hole.bounds), "hole_vertices": len(ring), "candidate_diagnostics": diagnostics})
    report = {"date": "2026-10-21", "source_archive_sha256": archive_hash, "layer": "PADUS4_1Fee", "fid": 117,
              "name": feature.GetField("Unit_Nm"), "source_geometry_matches_audit": True,
              "source_attributes_match_audit": attributes_match,
              "actual_nonlinear_source_segments": bool(native.HasCurveGeometry(1)),
              "source_precision": precision, "source_rings": len(rings), "simple_cycles": len(decomposed),
              "clockwise_shells": len(outer), "counterclockwise_holes": len(inner),
              "invalid_source_simple_shells": sum(not shell.is_valid for shell in shells),
              "containment_failures": failures, "contained_holes": sum(value > 0 for value in containment_counts),
              "signed_source_area_m2": -math.fsum(signed_area(ring) for ring in rings),
              "decoded_area_m2": original.area, "repaired_area_m2": repaired.area,
              "publisher_shape_area_m2": feature.GetField("SHAPE_Area"),
              "repairs_performed": False, "decision": "diagnosis_only_not_accepted"}
    destination = home / "verification/kremmling-ring-diagnosis.json"
    if destination.exists():
        raise RuntimeError("Refuse to replace existing diagnosis")
    destination.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
