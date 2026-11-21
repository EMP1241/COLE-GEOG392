"""Load only the individually investigated, hash-bound geometry replacements.

The Kremmling, Upper Snake, excluded NJ and Eastern Canal decisions are record-specific.
"""

import hashlib
import json
import sqlite3

import shapely


FILES = {
    "verification/kremmling-resolution.json": "b216a5c1b3e825b8bb995a7b0fca25f6aa478a6a088e1b0e9bfec9e2c3a55773",
    "verification/kremmling-resolution-audit.json": "c44885b5036e4ca54d6b21a3ba9fcfaa1e9ce77210b3164416e2450665d522f4",
    "working/source-checked-repair-2026-10-21/repair_audit.sqlite3": "c4efe7103c22ac9520c5e180e0c80b1b5759998b99da428f0f6e15ec931ca42e",
    "working/kremmling-investigation-2026-10-21/accepted-conservative-replacement.wkb": "6a62fd1a380b07dbaf524dd49cba9887da584289777e7bff733f714a39bcab77",
    "working/kremmling-investigation-2026-10-21/independent-source-shell-minus-holes.wkb": "2fe792b44eafc9c7e18aaef3ddef9514bc983b2e56a5fccbf08c479970b36edc",
    "working/kremmling-investigation-2026-10-21/excluded-unsupported-fragment.wkb": "86780a2ba5f4e3dd3cf927d7a1f65bd468ea3c85ebe7da179d857ac92ec20c2b",
    "verification/upper-snake-resolution.json": "95c13469263232f4a7868dbe0f45bce0064d65da1841ad98223f60683a275d1d",
    "verification/upper-snake-resolution-audit.json": "7fbff8ee60f8438bd25643fc8384d55401e5add51641c90e05a1bc3b7900834d",
    "verification/upper-snake-diagnosis.json": "b571b1883f1df6b1d4c74a587030cf5c6d63c3e64a18940e099f0d63f22e2416",
    "verification/upper-snake-overlap-diagnosis.json": "e9a6d8b549a45c737878bc616fb3c33cf29fc7bcdd86c7bf02057630d9b9a18c",
    "working/national-repair-2026-10-21/repair_audit.sqlite3": "6d62bb98a9a01afcdc059cf42392470a32cd41d51758ca7a43168305b0eaf215",
    "working/upper-snake-investigation-2026-10-21/accepted-source-union-replacement.wkb": "7b190cebfcb524f4412d287495bed8fe2e1e1db20c9310fb47a6e89bd8a89720",
    "working/upper-snake-investigation-2026-10-21/source-valid-parts-union.wkb": "7b190cebfcb524f4412d287495bed8fe2e1e1db20c9310fb47a6e89bd8a89720",
    "working/upper-snake-investigation-2026-10-21/source-positive-winding-region.wkb": "2d2fa40f1bdf0cb2de380df6884e0d3ed450cdb126712d36411f6316b28f344f",
    "working/upper-snake-investigation-2026-10-21/source-polygon-overlap.wkb": "4252da17bb0e5bab03c38f9cc22f1207e74bb2c881e7f4b6151335bc2338a8d3",
    "verification/nj41938-resolution.json": "e155a069df72f51edb1b5d2e281d60ef70b3702ed947a7c4a2d8ed9fb7f05964",
    "verification/nj41938-resolution-audit.json": "150c2de1b3402ec612ed205eb1e818beb4c32b669951d7e709ba897e11125c54",
    "working/national-repair-upper-snake-2026-10-21/repair_audit.sqlite3": "d1793aab8a9878796b4630e6b34cad30c0ae70b67cab8b39da213114880fa033",
    "working/nj41938-resolution-2026-10-21/accepted-polygon-replacement.wkb": "44fe8231879d87286fd4cc18e7237e6c8b5af3949ab567e4f2a4d74fe112cf93",
    "working/nj41938-resolution-2026-10-21/accepted-full-replacement.wkb": "7df409a3cc2586842e537edd8cd779d39156b2b1641815b2b196b12951655163",
    "verification/eastern-canal-resolution.json": "f97f97c419767731e187a9ecb269650b6168bc26e5e2c025b7319b7c35cda7ba",
    "verification/eastern-canal-resolution-audit.json": "e5afc4d03e035db136766104a67948bde398733e8c42abf2a2fafb87ff57ffb6",
    "working/national-prep-local-exclusions-2026-10-22/repair_audit.sqlite3": "76f8ae2050a030f5e4208dd28ef94cf5e77330f1c84f2095654ee2b0123d8f81",
    "working/eastern-canal-resolution-2026-10-22/accepted-source-supported-replacement.wkb": "cc0008c59136c55576d4b213d882a368aea479c1b603d81d951562b68a7e5653",
    "working/eastern-canal-resolution-2026-10-22/excluded-unconfirmed-lobe.wkb": "e123928253bf6233d6b74d2074f9eac8d2f9bfd38a1eb5456fffb7c85673ac43",
    "verification/hawaii-resolution.json": "e24a35011df37c39d05aa7de5f064ab7edf28957acf7e19095384d39e45e0276",
    "verification/hawaii-resolution-audit.json": "8c06408f0d4eccec3492463761606a663ba8787deb274846147210e073859542",
    "working/national-prep-eastern-canal-2026-10-22/repair_audit.sqlite3": "8be0006ae997706efd15682646a949c2cd13afe9a86c4a94fa713f36034db178",
}
EXPECTED_IDENTITIES = {("PADUS4_1Fee", 117), ("PADUS4_1Fee", 163), ("PADUS4_1Fee", 41938), ("PADUS4_1Fee", 88623), ("PADUS4_1Fee",199053)}


def approved_inputs(home):
    registry = dict(FILES)
    for receipt in ("verification/nj41938-resolution.json", "verification/eastern-canal-resolution.json", "verification/hawaii-resolution.json"):
        body = (home / receipt).read_bytes()
        if hashlib.sha256(body).hexdigest() != FILES[receipt]:
            raise RuntimeError(f"Acceptance receipt changed: {receipt}")
        # Paths and hashes come only from exact pinned receipts. Resolve paths
        # inside the feature before any read, including historical subset checks.
        for relative, expected in json.loads(body)["input_artifacts"].items():
            if not (home / relative).resolve().is_relative_to(home.resolve()):
                raise RuntimeError("Accepted input path escapes feature directory")
            if relative in registry and registry[relative] != expected["sha256"]:
                raise RuntimeError("Conflicting accepted input hash")
            registry[relative] = expected["sha256"]
        if receipt == "verification/hawaii-resolution.json":
            for name,item in json.loads(body)["accepted_artifacts"].items():
                relative="working/hawaii-resolution-2026-10-22/"+name
                if not (home/relative).resolve().is_relative_to(home.resolve()):
                    raise RuntimeError("Hawaii artifact escapes feature")
                registry[relative]=item["sha256"]
    return registry


def verify_inputs(home, expected_inputs=None):
    # Historical audits verify their original pinned subset, so adding another
    # accepted record does not make an unchanged historical run unauditable.
    registry = approved_inputs(home)
    selected = registry if expected_inputs is None else expected_inputs
    if not selected or any(registry.get(path) != value for path, value in selected.items()):
        raise RuntimeError("Unknown or unapproved adjudication input manifest")
    for relative, expected in selected.items():
        with (home / relative).open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise RuntimeError(f"Accepted adjudication input changed: {relative}")
    return dict(selected)


def load(home, layer, fid, original, candidate, attributes, archive_sha256):
    if (layer,fid)==("PADUS4_1Fee",199053):
        return load_hawaii(home,original,candidate,attributes,archive_sha256)
    if (layer, fid) == ("PADUS4_1Fee", 88623):
        return load_eastern_canal(home, layer, fid, original, candidate, attributes, archive_sha256)
    if (layer, fid) == ("PADUS4_1Fee", 41938):
        return load_nj41938(home, layer, fid, original, candidate, attributes, archive_sha256)
    if (layer, fid) == ("PADUS4_1Fee", 163):
        return load_upper_snake(home, layer, fid, original, candidate, attributes, archive_sha256)
    if (layer, fid) != ("PADUS4_1Fee", 117):
        return None
    verify_inputs(home)
    receipt = json.loads((home / "verification/kremmling-resolution.json").read_text(encoding="utf-8"))
    audit = json.loads((home / "verification/kremmling-resolution-audit.json").read_text(encoding="utf-8"))
    digest = lambda geometry: hashlib.sha256(shapely.to_wkb(geometry, byte_order=1)).hexdigest()
    if (receipt["decision"] != "accepted_conservative_replacement_previous_candidate_rejected"
            or receipt["source_layer"] != layer or receipt["source_fid"] != fid
            or receipt["source_archive_sha256"] != archive_sha256
            or receipt["source_original_wkb_sha256"] != digest(original)
            or receipt["previous_candidate_wkb_sha256"] != digest(candidate)
            or audit["status"] != "passed" or audit["discrepancies"]):
        raise RuntimeError("Kremmling source/candidate identity or accepted decision differs")
    prior = home / "working/source-checked-repair-2026-10-21/repair_audit.sqlite3"
    with sqlite3.connect(prior.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT attributes_json FROM repairs WHERE layer=? AND source_fid=?", (layer, fid)).fetchone()
    if row is None or json.loads(row[0]) != attributes:
        raise RuntimeError("Kremmling attributes differ from the investigated original")
    root = home / "working/kremmling-investigation-2026-10-21"
    replacement = shapely.from_wkb((root / "accepted-conservative-replacement.wkb").read_bytes())
    independent = shapely.from_wkb((root / "independent-source-shell-minus-holes.wkb").read_bytes())
    excluded = shapely.from_wkb((root / "excluded-unsupported-fragment.wkb").read_bytes())
    if (not replacement.is_valid or replacement.is_empty or not independent.is_valid
            or replacement.geom_type != "MultiPolygon" or len(replacement.geoms) != 267
            or replacement.symmetric_difference(independent).area != 0
            or replacement.difference(candidate).area != 0
            or replacement.intersection(excluded).area != 0
            or candidate.difference(replacement).symmetric_difference(excluded).area != 0
            or excluded.area != receipt["excluded_fragment_area_m2"]
            or replacement.area != receipt["replacement_area_m2"]):
        raise RuntimeError("Kremmling replacement no longer satisfies the accepted geometry checks")
    expected_parts = receipt["retained_component_sha256"]
    if [digest(part) for part in replacement.geoms] != expected_parts:
        raise RuntimeError("Kremmling retained components changed")
    return replacement, independent, {
        "method": "specific_previously_accepted_conservative_replacement",
        "receipt": "verification/kremmling-resolution.json",
        "input_sha256": dict(FILES), "credit": "Margaret",
        "excluded_fragment_area_m2": excluded.area,
        "comparison_area_m2": independent.area,
        "replacement_vs_independent_symmetric_difference_m2": 0.0,
        "area_added_m2": 0.0,
        "review_absolute_difference_m2": abs(replacement.area - independent.area) + excluded.area,
        "scope": "PADUS4_1Fee FID 117 with exact source/archive/candidate/attribute identity only",
    }


def load_upper_snake(home, layer, fid, original, candidate, attributes, archive_sha256):
    verify_inputs(home)
    receipt = json.loads((home / "verification/upper-snake-resolution.json").read_text(encoding="utf-8"))
    audit = json.loads((home / "verification/upper-snake-resolution-audit.json").read_text(encoding="utf-8"))
    digest = lambda geometry: hashlib.sha256(shapely.to_wkb(geometry, byte_order=1)).hexdigest()
    if (receipt["decision"] != "accepted_source_union_replacement_previous_even_odd_candidate_rejected"
            or receipt["source_layer"] != layer or receipt["source_fid"] != fid
            or receipt["source_archive_sha256"] != archive_sha256
            or receipt["source_original_wkb_sha256"] != digest(original)
            or receipt["previous_candidate_wkb_sha256"] != digest(candidate)
            or receipt["source_attributes"] != attributes
            or audit["status"] != "passed" or audit["discrepancies"]
            or audit["acceptance_receipt_sha256"] != FILES["verification/upper-snake-resolution.json"]):
        raise RuntimeError("Upper Snake source/candidate/attributes or accepted decision differs")
    prior = home / "working/national-repair-2026-10-21/repair_audit.sqlite3"
    with sqlite3.connect(prior.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT attributes_json FROM repairs WHERE layer=? AND source_fid=?", (layer, fid)).fetchone()
    if row is None or json.loads(row[0]) != attributes:
        raise RuntimeError("Upper Snake attributes differ from the investigated original")
    root = home / "working/upper-snake-investigation-2026-10-21"
    replacement = shapely.from_wkb((root / "accepted-source-union-replacement.wkb").read_bytes())
    independent = shapely.from_wkb((root / "source-positive-winding-region.wkb").read_bytes())
    source_union = shapely.from_wkb((root / "source-valid-parts-union.wkb").read_bytes())
    overlap = shapely.from_wkb((root / "source-polygon-overlap.wkb").read_bytes())
    added = replacement.difference(candidate)
    if (not replacement.is_valid or replacement.is_empty or not independent.is_valid
            or replacement.geom_type != "MultiPolygon" or len(replacement.geoms) != 1090
            or replacement.symmetric_difference(independent).area != 0
            or replacement.symmetric_difference(source_union).area != 0
            or candidate.difference(replacement).area != 0
            or added.symmetric_difference(overlap).area != 0
            or added.area != receipt["area_added_to_previous_candidate_m2"]
            or replacement.area != receipt["replacement_area_m2"]
            or digest(replacement) != receipt["accepted_artifact"]["sha256"]):
        raise RuntimeError("Upper Snake replacement no longer satisfies accepted geometry checks")
    source_rings = [list(r.coords) for p in original.geoms for r in [p.exterior, *p.interiors]]
    if replacement.boundary.difference(shapely.node(shapely.MultiLineString(source_rings))).length != 0:
        raise RuntimeError("Upper Snake replacement boundary absent from original source linework")
    return replacement, independent, {
        "method": "specific_previously_accepted_source_union_replacement",
        "receipt": "verification/upper-snake-resolution.json",
        "input_sha256": dict(FILES), "credit": "Margaret",
        "comparison_area_m2": independent.area,
        "replacement_vs_independent_symmetric_difference_m2": 0.0,
        "area_added_m2": added.area, "source_supported_overlap_counted_once": True,
        "review_absolute_difference_m2": abs(replacement.area - independent.area) + added.area,
        "scope": "PADUS4_1Fee FID 163 with exact source/archive/candidate/attribute identity only",
    }


def load_nj41938(home, layer, fid, original, candidate, attributes, archive_sha256):
    from osgeo import gdal

    inputs = verify_inputs(home)
    receipt = json.loads((home / "verification/nj41938-resolution.json").read_text(encoding="utf-8"))
    audit = json.loads((home / "verification/nj41938-resolution-audit.json").read_text(encoding="utf-8"))
    digest = lambda geometry: hashlib.sha256(shapely.to_wkb(geometry, byte_order=1)).hexdigest()
    if (receipt["decision"] != "accepted_specific_bounded_native_curve_replacement_previous_candidate_rejected"
            or receipt["source_layer"] != layer or receipt["source_fid"] != fid
            or receipt["source_archive_sha256"] != archive_sha256
            or receipt["source_original_default_linear_wkb_sha256"] != digest(original)
            or receipt["previous_full_candidate_wkb_sha256"] != digest(candidate)
            or receipt["source_attributes"] != attributes or attributes["Own_Type"] != "NGO"
            or receipt["ownership_eligible_for_greenspace"] is not False
            or audit["status"] != "passed" or audit["discrepancies"]
            or audit["acceptance_receipt_sha256"] != FILES["verification/nj41938-resolution.json"]):
        raise RuntimeError("NJ source/candidate/attributes or accepted decision differs")
    archive = home / "source/padus-4.1/PADUS4_1Geodatabase.zip"
    gdal.UseExceptions()
    dataset = gdal.OpenEx("/vsizip/" + archive.resolve().as_posix() + "/PADUS4_1Geodatabase.gdb",
                          gdal.OF_VECTOR | gdal.OF_READONLY, allowed_drivers=["OpenFileGDB"])
    feature = dataset.GetLayerByName(layer).GetFeature(fid)
    native = feature.GetGeometryRef()
    if (hashlib.sha256(bytes(native.ExportToWkb())).hexdigest() != receipt["source_native_wkb_sha256"]
            or any((None if feature.GetField(k) is None else str(feature.GetField(k))) != v for k,v in attributes.items())):
        raise RuntimeError("Original-ZIP NJ native geometry/attributes differ")
    root = home / "working/nj41938-resolution-2026-10-21"
    full = shapely.from_wkb((root / "accepted-full-replacement.wkb").read_bytes())
    polygon = shapely.from_wkb((root / "accepted-polygon-replacement.wkb").read_bytes())
    fine = home / "working/nj41938-curves-2026-10-21"
    independent = shapely.from_wkb((fine / "0.01-degrees-positive-region.wkb").read_bytes())
    native_check = shapely.from_wkb((home / "working/nj41938-native-region-2026-10-21/independently-sampled-positive-region.wkb").read_bytes())
    noded = shapely.from_wkb((fine / "0.01-degrees-noded-linework.wkb").read_bytes())
    zero = shapely.from_wkb((fine / "0.01-degrees-zero-winding-region.wkb").read_bytes())
    lines = [g for g in full.geoms if g.geom_type == "LineString"]
    # The acceptance receipt measured polygon-to-polygon overlay. Including
    # zero-area lines changes floating summation at ~1e-18 m², so recover and
    # hash-check the exact prior polygon bytes rather than weakening equality.
    pending, candidate_parts = [candidate], []
    while pending:
        item = pending.pop()
        if item.geom_type == "Polygon":
            candidate_parts.append(item)
        elif hasattr(item, "geoms"):
            pending.extend(reversed(list(item.geoms)))
    candidate_polygon = (candidate_parts[0] if len(candidate_parts)==1 else shapely.MultiPolygon(candidate_parts))
    if digest(candidate_polygon) != receipt["previous_polygon_candidate_wkb_sha256"]:
        raise RuntimeError("NJ prior polygon bytes differ from accepted source investigation")
    difference = polygon.symmetric_difference(candidate_polygon).area
    native_difference = polygon.symmetric_difference(native_check).area
    if (not full.is_valid or not polygon.is_valid or polygon.is_empty
            or polygon.symmetric_difference(independent).area != 0
            or native_difference != receipt["independent_native_region_symmetric_difference_m2"]
            or native_difference >= receipt["diagnostic_area_budget_m2"]
            or receipt["diagnostic_area_budget_m2"] != 1e-5
            or receipt["curve_area_envelope_bound_m2"] >= 1e-5
            or polygon.boundary.difference(noded).length != 0 or polygon.intersection(zero).area != 0
            or difference != receipt["candidate_symmetric_difference_m2"]
            or polygon.difference(candidate_polygon).area != receipt["candidate_area_added_m2"]
            or candidate_polygon.difference(polygon).area != receipt["candidate_area_removed_m2"]
            or [digest(g) for g in lines] != receipt["retained_collapsed_line_wkb_sha256"]
            or len(lines) != 3 or full.area != polygon.area or polygon.area != receipt["replacement_area_m2"]):
        raise RuntimeError("NJ replacement no longer satisfies native-curve/line/exclusion checks")
    return full, independent, {
        "method": "specific_previously_accepted_bounded_native_curve_representation",
        "receipt": "verification/nj41938-resolution.json", "input_sha256": inputs, "credit": "Margaret",
        "comparison_area_m2": independent.area, "replacement_vs_independent_symmetric_difference_m2": 0.0,
        "independent_native_region_symmetric_difference_m2": native_difference,
        "area_added_m2": receipt["candidate_area_added_m2"],
        "area_removed_m2": receipt["candidate_area_removed_m2"],
        "curve_area_envelope_m2": receipt["curve_area_envelope_bound_m2"],
        "review_absolute_difference_m2": difference + receipt["curve_area_envelope_bound_m2"],
        "approximation_not_exact_native_arc_geometry": True, "ngo_ownership_remains_excluded": True,
        "scope": "PADUS4_1Fee FID 41938 with exact native/default source/archive/candidate/attribute identities only",
    }


def load_eastern_canal(home, layer, fid, original, candidate, attributes, archive_sha256):
    import math
    from osgeo import gdal
    from eastern_canal_geometry import reconstruct

    inputs = verify_inputs(home)
    receipt_path = "verification/eastern-canal-resolution.json"
    receipt = json.loads((home / receipt_path).read_text(encoding="utf-8"))
    audit = json.loads((home / "verification/eastern-canal-resolution-audit.json").read_text(encoding="utf-8"))
    digest = lambda geometry: hashlib.sha256(shapely.to_wkb(geometry, byte_order=1)).hexdigest()
    if (receipt["decision"] != "accepted_specific_source_supported_replacement_unconfirmed_lobe_excluded_county_effect_bounded"
            or receipt["source_layer"] != layer or receipt["source_fid"] != fid
            or receipt["source_archive_sha256"] != archive_sha256
            or receipt["source_original_wkb_sha256"] != digest(original)
            or receipt["previous_candidate_wkb_sha256"] != digest(candidate)
            or receipt["source_attributes"] != attributes
            or audit["status"] != "passed" or audit["discrepancies"]
            or audit["acceptance_receipt_sha256"] != FILES[receipt_path]):
        raise RuntimeError("Eastern Canal source/candidate/attributes or accepted decision differs")
    gdal.UseExceptions()
    archive = home / "source/padus-4.1/PADUS4_1Geodatabase.zip"
    dataset = gdal.OpenEx("/vsizip/" + archive.resolve().as_posix() + "/PADUS4_1Geodatabase.gdb",
                          gdal.OF_VECTOR | gdal.OF_READONLY, allowed_drivers=["OpenFileGDB"])
    feature = dataset.GetLayerByName(layer).GetFeature(fid)
    native = feature.GetGeometryRef()
    if (native.HasCurveGeometry(1)
            or hashlib.sha256(bytes(native.ExportToWkb())).hexdigest() != receipt["source_native_wkb_sha256"]
            or any((None if feature.GetField(k) is None else str(feature.GetField(k))) != v for k,v in attributes.items())):
        raise RuntimeError("Original-ZIP Eastern Canal native geometry/attributes differ")
    root = home / "working/eastern-canal-resolution-2026-10-22"
    replacement = shapely.from_wkb((root / "accepted-source-supported-replacement.wkb").read_bytes())
    excluded = shapely.from_wkb((root / "excluded-unconfirmed-lobe.wkb").read_bytes())
    independent, lobe, exact_proof = reconstruct(original)
    investigated = home / "working/eastern-canal-investigation-2026-10-22"
    positive = shapely.from_wkb((investigated / "source-positive-region.wkb").read_bytes())
    noded = shapely.from_wkb((investigated / "source-noded-linework.wkb").read_bytes())
    removed = candidate.difference(replacement)
    if (not replacement.is_valid or replacement.is_empty or len(replacement.geoms) != 10
            or digest(independent) != digest(replacement) or digest(lobe) != digest(excluded)
            or json.loads(json.dumps(exact_proof)) != receipt["independent_exact_segment_proof"]
            or not replacement.symmetric_difference(positive).is_empty
            or not replacement.difference(candidate).is_empty
            or not removed.symmetric_difference(excluded).is_empty
            or replacement.intersection(excluded).area != 0
            or replacement.boundary.difference(noded).length != 0
            or removed.area != receipt["candidate_area_removed_m2"]
            or replacement.area != receipt["replacement_area_m2"]):
        raise RuntimeError("Eastern Canal replacement differs from exact source reconstruction")
    rings = [list(r.coords) for p in original.geoms for r in [p.exterior, *p.interiors]]
    length = shapely.MultiLineString(rings).length
    guard = receipt["county_boundary_guard_m"]
    tube = 2*guard*(length+1) + len(rings)*math.pi*guard**2
    publisher_difference = abs(replacement.area - float(attributes["SHAPE_Area"]))
    allowance = tube + lobe.area + publisher_difference
    if (guard != 0.001 or length != receipt["source_linework_length_m"]
            or tube != receipt["numerical_tube_allowance_m2"]
            or publisher_difference != receipt["additional_publisher_discrepancy_allowance_m2"]
            or allowance != receipt["total_county_effect_allowance_m2"]
            or allowance != 75.31112267463192
            or [c["county_fips"] for c in receipt["county_bounds"]] != ["04013"]):
        raise RuntimeError("Eastern Canal accepted county allowance differs")
    return replacement, independent, {
        "method": "specific_previously_accepted_source_supported_county_bounded_replacement",
        "receipt": receipt_path, "input_sha256": inputs, "credit": "Margaret",
        "comparison_area_m2": independent.area,
        "replacement_vs_independent_symmetric_difference_m2": 0.0,
        "area_added_m2": 0.0, "area_removed_m2": removed.area,
        "review_absolute_difference_m2": allowance,
        "county_effect_bounded": True, "county_bounds": receipt["county_bounds"],
        "unsupported_lobe_remains_excluded": True,
        "scope": "PADUS4_1Fee FID 88623 with exact native/default source/archive/candidate/attribute identities only; county caps required",
    }


def load_hawaii(home,original,candidate,attributes,archive_sha256):
    inputs=verify_inputs(home)
    receipt=json.loads((home/"verification/hawaii-resolution.json").read_text())
    audit=json.loads((home/"verification/hawaii-resolution-audit.json").read_text())
    digest=lambda g:hashlib.sha256(shapely.to_wkb(g,byte_order=1)).hexdigest()
    if (receipt["decision"]!="accepted_specific_exact_source_winding_replacement_with_county_bound"
            or receipt["source_archive_sha256"]!=archive_sha256
            or receipt["source_original_wkb_sha256"]!=digest(original)
            or receipt["previous_candidate_wkb_sha256"]!=digest(candidate)
            or receipt["source_attributes"]!=attributes
            or audit["status"]!="passed" or audit["discrepancies"]
            or audit["acceptance_receipt_sha256"]!=FILES["verification/hawaii-resolution.json"]):
        raise RuntimeError("Hawaii source/candidate/attributes or pinned acceptance differs")
    # The pinned passing audit proves exact construction for these exact source
    # bytes. Reuse that certificate; do not redo the investigation at each load.
    root=home/"working/hawaii-resolution-2026-10-22"
    replacement=shapely.from_wkb((root/"accepted-source-winding-replacement.wkb").read_bytes())
    independent=shapely.from_wkb((home/"working/hawaii-coverage-2026-10-22/source-positive-region.wkb").read_bytes())
    excluded=shapely.from_wkb((root/"excluded-negative-winding-lobes.wkb").read_bytes())
    if (not replacement.is_valid or len(replacement.geoms)!=2952
            or replacement.symmetric_difference(independent).area!=0
            or replacement.intersection(excluded).area!=0
            or candidate.difference(replacement).symmetric_difference(excluded).area!=0
            or replacement.difference(candidate).area!=receipt["candidate_area_added_m2"]):
        raise RuntimeError("Hawaii accepted region/excluded lobes differ")
    return replacement,independent,{
        "method":"specific_previously_audited_exact_source_winding_county_bounded_replacement",
        "receipt":"verification/hawaii-resolution.json","input_sha256":inputs,"credit":"Margaret",
        "comparison_area_m2":independent.area,"replacement_vs_independent_symmetric_difference_m2":0.0,
        "review_absolute_difference_m2":receipt["county_allowance_proof"]["total_outward_upper_m2"],
        "county_effect_bounded":True,"county_bounds":receipt["county_bounds"],
        "negative_lobes_remain_excluded":True,"source_supported_overlap_counted_once":True}
