"""Log operation timings for a slow county without writing accepted results."""

import json
import argparse
import time

import shapely
import aggregate_counties as preparation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fips", default="02090")
    parser.add_argument("--tag", default="timing")
    args = parser.parse_args()
    code = args.fips
    frame, core, _ = preparation.counties()
    county = frame.loc[frame.GEOID == code].iloc[0]
    summary = json.loads((preparation.REPAIR / "repair-summary.json").read_text(encoding="utf-8"))
    target = preparation.RUN / f"overlay-performance-{code}-{args.tag}.jsonl"
    stream = target.open("x", encoding="utf-8")
    def log(value):
        text = json.dumps(value)
        stream.write(text + "\n")
        stream.flush()
        print(text, flush=True)
    for name in ("union", "intersect", "subtract"):
        original = getattr(preparation, name)
        def wrapper(*args, original=original, name=name):
            inputs = args[0] if name == "union" else args
            coords = sum(shapely.get_num_coordinates(g) for g in inputs)
            started = time.monotonic()
            if name == "union" or coords > 100000:
                log({"phase": "start", "operation": name, "inputs": len(inputs), "coordinates": int(coords)})
            result = original(*args)
            elapsed = time.monotonic() - started
            if name == "union" or coords > 100000 or elapsed > 1:
                log({"phase": "end", "operation": name, "seconds": elapsed, "output_coordinates": int(shapely.get_num_coordinates(result))})
            return result
        setattr(preparation, name, wrapper)
    started = time.monotonic()
    try:
        result = preparation.calculate_county(county, core, summary["county_review_allowances"])
    except Exception as error:
        cursor = error.__traceback__
        while cursor:
            local = cursor.tb_frame.f_locals
            if "residual" in local and "eligible" in local:
                directory = preparation.RUN / f"accounting-diagnostic-{code}-{args.tag}"
                directory.mkdir()
                diagnostic = {"county_fips": code, "code_sha256": preparation.sha(preparation.Path(preparation.__file__)),
                              "error": str(error), "residual_m2": local["residual"],
                              "numeric_tolerance_m2": local["numeric_tolerance"], "denominator_m2": local["denominator"], "operands": {}}
                for key in ("boundary", "public", "before", "eligible", "excluded", "shared"):
                    geometry = local[key]
                    filename = directory / (key + ".wkb")
                    filename.write_bytes(shapely.to_wkb(geometry))
                    diagnostic["operands"][key] = {"area_m2": geometry.area, "valid": geometry.is_valid, "sha256": preparation.sha(filename)}
                diagnostic["outside_public_m2"] = local["eligible"].difference(local["public"]).area
                diagnostic["excluded_contact_m2"] = local["eligible"].intersection(local["excluded"]).area
                preparation.write(directory / "diagnosis.json", diagnostic)
                log({"phase": "accounting_diagnosis", **diagnostic})
            cursor = cursor.tb_next
        stream.close()
        raise
    log({"phase": "complete", "county_fips": code, "seconds": time.monotonic()-started,
         "eligible_percent": result[0][9]})
    stream.close()


if __name__ == "__main__":
    main()
