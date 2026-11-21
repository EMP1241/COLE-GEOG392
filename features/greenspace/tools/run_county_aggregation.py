"""Resume unchanged county calculations with operation/failure timing logs."""

import faulthandler
import inspect
import json
import threading
import time
import traceback

import shapely
import aggregate_counties as preparation


def main():
    tag = str(time.time_ns())
    stream = (preparation.RUN / f"operation-timing-{tag}.jsonl").open("x", encoding="utf-8")
    traces = (preparation.RUN / f"thread-progress-{tag}.txt").open("x", encoding="utf-8")
    lock = threading.Lock()
    state = threading.local()
    def log(value):
        with lock:
            stream.write(json.dumps({"monotonic": time.monotonic(), "county_fips": getattr(state, "fips", None), **value}) + "\n")
            stream.flush()
    original_county = preparation.calculate_county
    def county(*args):
        state.fips = str(args[0].GEOID)
        log({"phase": "county_start"})
        try:
            result = original_county(*args)
            log({"phase": "county_complete"})
            return result
        except BaseException as error:
            context = {}
            cursor = error.__traceback__
            while cursor:
                values = cursor.tb_frame.f_locals
                for key in ("residual", "numeric_tolerance", "excluded_contact", "outside_public", "outside_county", "denominator", "combined_sensitivity"):
                    if key in values:
                        context[key] = values[key]
                cursor = cursor.tb_next
            log({"phase": "county_error", "error": str(error), "context": context})
            print(json.dumps({"phase": "county_error", "fips": state.fips, "error": str(error), "context": context}), flush=True)
            raise
    preparation.calculate_county = county
    for name in ("union", "intersect", "subtract"):
        original = getattr(preparation, name)
        def operation(*args, name=name, original=original):
            inputs = args[0] if name == "union" else args
            coordinates = sum(shapely.get_num_coordinates(g) for g in inputs)
            large = coordinates > 100000
            caller = inspect.currentframe().f_back
            line = caller.f_lineno
            started = time.monotonic()
            if large:
                log({"phase": "operation_start", "operation": name, "line": line, "coordinates": int(coordinates)})
            result = original(*args)
            elapsed = time.monotonic()-started
            if large or elapsed > 10:
                log({"phase": "operation_end", "operation": name, "line": line, "seconds": elapsed})
            return result
        setattr(preparation, name, operation)
    faulthandler.dump_traceback_later(60, repeat=True, file=traces)
    try:
        preparation.aggregate()
    finally:
        faulthandler.cancel_dump_traceback_later()
        stream.close()
        traces.close()


if __name__ == "__main__":
    main()
