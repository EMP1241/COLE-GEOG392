"""Independent ESRI source-ring reconstruction for a flagged area diagnostic."""

from collections import Counter
import hashlib
import math

import shapely

from investigate_rings import boundary_segments, cycles, ring_hash, signed_area


def reconcile(original, repaired, publisher_area):
    report = {"resolved": False, "method": "exact_source_ring_reconstruction",
              "publisher_shape_area_m2": publisher_area if math.isfinite(publisher_area) else None}
    if not math.isfinite(publisher_area) or publisher_area <= 0:
        report["issue"] = "publisher_area_missing_or_not_positive"
        return report, None
    if original.geom_type not in {"Polygon", "MultiPolygon"}:
        report["issue"] = "unsupported_source_geometry"
        return report, None
    try:
        polygons = [original] if original.geom_type == "Polygon" else list(original.geoms)
        source_rings = [list(ring.coords) for polygon in polygons for ring in [polygon.exterior, *polygon.interiors]]
        source_cycles = [cycle for ring in source_rings for cycle in cycles(ring)]
        outer = [ring for ring in source_cycles if signed_area(ring) < 0]
        inner = [ring for ring in source_cycles if signed_area(ring) > 0]
        if len(outer) + len(inner) != len(source_cycles) or not outer:
            raise ValueError("zero_area_cycle_or_no_clockwise_shell")
        shells = [shapely.Polygon(ring) for ring in outer]
        if any(not polygon.is_valid for polygon in shells):
            raise ValueError("source_cycles_not_simple")
        tree = shapely.STRtree(shells)
        holes = [[] for _ in shells]
        for ring in inner:
            hole = shapely.Polygon(ring)
            if not hole.is_valid:
                raise ValueError("source_hole_not_simple")
            candidates = [int(i) for i in tree.query(hole) if shells[int(i)].covers(hole)]
            if not candidates:
                raise ValueError("counterclockwise_ring_has_no_containing_shell")
            target = min(candidates, key=lambda i: shells[i].area)
            holes[target].append(ring)
        reconstructed = shapely.MultiPolygon([shapely.Polygon(ring, holes[i]) for i, ring in enumerate(outer)])
        if not reconstructed.is_valid or not repaired.is_valid:
            raise ValueError("reconstruction_or_repair_not_valid")
        if repaired.geom_type == "Polygon":
            repaired_polygons = [repaired]
        elif repaired.geom_type == "MultiPolygon":
            repaired_polygons = list(repaired.geoms)
        else:
            raise ValueError("repair_not_polygonal")
        output_rings = [list(ring.coords) for polygon in repaired_polygons for ring in [polygon.exterior, *polygon.interiors]]
        cycle_equal = Counter(ring_hash(ring) for ring in source_cycles) == Counter(ring_hash(ring) for ring in output_rings)
        segments_equal = boundary_segments(source_rings) == boundary_segments(output_rings)
        difference = reconstructed.symmetric_difference(repaired).area
        signed = -math.fsum(signed_area(ring) for ring in source_rings)
        publisher_difference = repaired.area - publisher_area
        publisher_ok = (math.isfinite(publisher_area) and publisher_area > 0
                        and abs(publisher_difference) <= 4046.8564224
                        and (abs(publisher_difference) <= 1 or abs(publisher_difference) / publisher_area * 100 <= 0.01))
        report.update(source_rings=len(source_rings), source_simple_cycles=len(source_cycles),
                      clockwise_shells=len(outer), counterclockwise_holes=len(inner),
                      signed_source_area_m2=signed, reconstructed_area_m2=reconstructed.area,
                      repaired_area_m2=repaired.area,
                      reconstruction_vs_repair_symmetric_difference_m2=difference,
                      oriented_cycles_exactly_equal=cycle_equal,
                      boundary_segment_multiplicities_exactly_equal=segments_equal,
                      repair_minus_publisher_area_m2=publisher_difference,
                      publisher_area_within_existing_thresholds=publisher_ok,
                      comparison_area_m2=reconstructed.area,
                      reconstructed_wkb_sha256=hashlib.sha256(shapely.to_wkb(reconstructed, byte_order=1)).hexdigest())
        report["resolved"] = bool(difference == 0 and cycle_equal and segments_equal and publisher_ok
                                  and abs(signed - repaired.area) <= 0.01)
        if not report["resolved"]:
            report["issue"] = "source_ring_or_area_evidence_incomplete"
        return report, reconstructed
    except (ValueError, RuntimeError, shapely.errors.GEOSException) as error:
        report["issue"] = str(error)
        return report, None
