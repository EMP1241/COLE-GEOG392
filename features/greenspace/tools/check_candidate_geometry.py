"""Check a bounded sample of potentially eligible areas without repairing them."""

import json
from pathlib import Path

import pyogrio
import shapely


def main():
    home = Path(__file__).resolve().parents[1]
    gdb = home / "working/padus-4.1/PADUS4_1Geodatabase.gdb"
    where = "Own_Type IN ('FED','STAT','LOC','DIST','TERR') AND Pub_Access = 'OA' AND Own_Name <> 'DOD' AND Mang_Name <> 'DOD' AND Des_Tp <> 'MIL'"
    frame = pyogrio.read_dataframe(gdb, layer="PADUS4_1Fee", where=where, max_features=100,
                                  columns=["Unit_Nm", "Own_Type", "Own_Name", "Mang_Name", "Des_Tp", "Pub_Access"], fid_as_index=True)
    invalid = ~shapely.is_valid(frame.geometry.array)
    examples = [{"fid": int(fid), **{key: str(value) for key, value in row.items() if key != "geometry"},
                 "reason": shapely.is_valid_reason(row.geometry)} for fid, row in frame.loc[invalid].head(10).iterrows()]
    result = {"date": "2026-10-21", "layer": "PADUS4_1Fee", "sample_limit": 100,
              "where": where, "sampled": len(frame), "invalid_in_sample": int(invalid.sum()),
              "examples": examples, "repairs_performed": False,
              "limit": "Bounded candidate sample, not an estimate of national invalid-candidate prevalence; exclusions/overlaps not yet processed."}
    print(json.dumps(result), flush=True)
    if examples:
        try:
            import arcpy
            arcpy.env.overwriteOutput = False
            ids = [example["fid"] for example in examples[:3]]
            selection = "OBJECTID IN (" + ",".join(map(str, ids)) + ")"
            source = str(gdb / "PADUS4_1Fee")
            layer = arcpy.management.MakeFeatureLayer(source, "cole_greenspace_geometry_sample", selection).getOutput(0)
            table = home / "working/candidate_geometry_ogc.dbf"
            if table.exists():
                raise RuntimeError("Validation output already exists; preserve it")
            arcpy.management.CheckGeometry(layer, str(table), "OGC")
            result["arcpy_ogc_check"] = {"selected_fids": ids, "problems": [dict(zip(["feature_id", "problem"], values))
                                          for values in arcpy.da.SearchCursor(str(table), ["FEATURE_ID", "PROBLEM"])]}
            arcpy.management.Delete(layer)
        except Exception as error:
            result["arcpy_ogc_check"] = {"completed": False, "error": str(error)[:1000]}
    destination = home / "verification/candidate-geometry-examples.json"
    destination.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"receipt": str(destination), "arcpy_ogc_check": result.get("arcpy_ogc_check")}), flush=True)


if __name__ == "__main__":
    main()
