"""Read-only polygon validity and geography preflight; no repairs or area totals."""

import json
from pathlib import Path

import pyogrio
import pyproj
import shapely


def main():
    home = Path(__file__).resolve().parents[1]
    gdb = home / "working/padus-4.1/PADUS4_1Geodatabase.gdb"
    report = {"date": "2026-10-21", "purpose": "Original geometry inspection; no repair or runtime preparation",
              "shapely": shapely.__version__, "pyproj": pyproj.__version__, "layers": {}}
    for layer, kind in pyogrio.list_layers(gdb):
        if not kind or "Combined_" in layer:
            continue
        info = pyogrio.read_info(gdb, layer=layer, force_feature_count=True)
        crs = pyproj.CRS(info["crs"])
        if not crs.is_projected:
            raise RuntimeError(f"Unexpected nonprojected source CRS: {layer}")
        data = {"features": int(info["features"]), "crs_authority": crs.to_authority(),
                "axis_units": [a.unit_name for a in crs.axis_info],
                "read": 0, "null": 0, "empty": 0, "invalid": 0, "invalid_examples": []}
        for start in range(0, data["features"], 10000):
            frame = pyogrio.read_dataframe(gdb, layer=layer, columns=["Unit_Nm", "Own_Type", "Pub_Access"],
                                          skip_features=start, max_features=10000, fid_as_index=True)
            geometries = frame.geometry.array
            null = shapely.is_missing(geometries)
            empty = shapely.is_empty(geometries)
            valid = shapely.is_valid(geometries)
            bad = ~valid & ~null
            data["read"] += len(frame)
            data["null"] += int(null.sum())
            data["empty"] += int(empty.sum())
            data["invalid"] += int(bad.sum())
            for fid, row in frame.loc[bad].head(max(0, 10 - len(data["invalid_examples"]))).iterrows():
                data["invalid_examples"].append({"fid": int(fid), "name": str(row["Unit_Nm"]),
                                                  "owner": str(row["Own_Type"]), "access": str(row["Pub_Access"]),
                                                  "reason": shapely.is_valid_reason(row.geometry)})
            print(json.dumps({"layer": layer, "read": data["read"], "features": data["features"],
                              "invalid": data["invalid"], "null": data["null"]}), flush=True)
        if data["read"] != data["features"]:
            raise RuntimeError("Geometry row accounting mismatch")
        report["layers"][layer] = data
    counties = home / "source/census-2021/tl_2021_us_county.zip"
    frame = pyogrio.read_dataframe(counties, columns=["GEOID", "NAME", "STATEFP", "ALAND", "AWATER"])
    core = json.loads((home.parents[1] / "data/prepared/counties.geojson").read_text(encoding="utf-8"))
    keys = {f["properties"]["county_fips"] for f in core["features"]}
    report["counties"] = {"features": len(frame), "crs": str(frame.crs),
                          "duplicate_fips": int(frame["GEOID"].duplicated().sum()),
                          "missing_core_fips": sorted(keys - set(frame["GEOID"])),
                          "invalid": int((~shapely.is_valid(frame.geometry.array)).sum()),
                          "covered_core_counties": int(frame["GEOID"].isin(keys).sum())}
    report["data_issue_requires_review"] = any(d["invalid"] or d["null"] or d["empty"] for d in report["layers"].values()) or bool(report["counties"]["missing_core_fips"]) or bool(report["counties"]["invalid"])
    destination = home / "verification/original-geometry-preflight.json"
    destination.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(destination), "data_issue_requires_review": report["data_issue_requires_review"],
                      "county_coverage": report["counties"]}), flush=True)


if __name__ == "__main__":
    main()
