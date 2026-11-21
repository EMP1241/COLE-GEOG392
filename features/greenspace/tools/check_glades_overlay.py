"""Independent high-precision area check for near-coincident Glades curves."""

from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json
import math
from pathlib import Path

import shapely

from investigate_upper_snake import digest_file, require


def decimal_area(geometry):
    with localcontext() as context:
        context.prec=60
        def ring_area(ring):
            coordinates=list(ring.coords)
            ox,oy=[Decimal.from_float(float(v)) for v in coordinates[0][:2]]
            xy=[(Decimal.from_float(float(x))-ox,Decimal.from_float(float(y))-oy) for x,y,*_ in coordinates]
            return sum((a[0]*b[1]-b[0]*a[1] for a,b in zip(xy,xy[1:])),Decimal(0))/2
        pending=[geometry]
        polygons=[]
        while pending:
            part=pending.pop()
            if part.geom_type=="Polygon":
                polygons.append(part)
            elif hasattr(part,"geoms"):
                pending.extend(part.geoms)
        return sum((abs(ring_area(p.exterior))-sum((abs(ring_area(r)) for r in p.interiors),Decimal(0)) for p in polygons),Decimal(0))


def main():
    home=Path(__file__).resolve().parents[1]
    dest=home/"verification/glades-overlay-check.json"
    require(not dest.exists(),"Refuse to overwrite overlay evidence")
    root=home/"working/glades-curves-2026-10-21"
    a_path=root/"0.01-degrees-positive-region.wkb"
    b_path=root/"independent-positive-region.wkb"
    a=shapely.from_wkb(a_path.read_bytes())
    b=shapely.from_wkb(b_path.read_bytes())
    da,db=decimal_area(a),decimal_area(b)
    print(json.dumps({"decimal_a_m2":str(da),"decimal_b_m2":str(db),"decimal_area_difference_m2":str(da-db)}),flush=True)
    operations={}
    for name,operation in [("a_minus_b",lambda:a.difference(b)),("b_minus_a",lambda:b.difference(a)),
                           ("symmetric_difference",lambda:a.symmetric_difference(b))]:
        geometry=operation()
        operations[name]={"area_m2":geometry.area,"decimal_area_m2":str(decimal_area(geometry)),"valid":bool(geometry.is_valid),
                          "geometry_type":geometry.geom_type}
        print(json.dumps({name:operations[name]}),flush=True)
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"decision":"diagnostic_only_not_accepted","credit":"Margaret",
            "source_fid":77431,"inputs":{p.relative_to(home).as_posix():digest_file(p) for p in [a_path,b_path]},
            "gdal_fine_area_m2":a.area,"independent_area_m2":b.area,
            "gdal_fine_decimal_area_m2":str(da),"independent_decimal_area_m2":str(db),
            "absolute_decimal_area_difference_m2":str(abs(da-db)),"operations":operations,
            "symmetric_difference_at_least_area_difference":Decimal(operations["symmetric_difference"]["decimal_area_m2"])>=abs(da-db),
            "decimal_signed_overlay_change_m2":str(Decimal(operations["a_minus_b"]["decimal_area_m2"])-Decimal(operations["b_minus_a"]["decimal_area_m2"])),
            "no_precision_grid_snapping_or_coordinate_changes":True,"repairs_performed":False}
    report["earlier_diagnostic_attempt"]="Initial Decimal area helper did not handle non-area overlay components; it failed before writing any receipt. Corrected to recurse polygon components and retain zero area for lines. No source or candidate geometry was changed."
    dest.write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps(report),flush=True)


if __name__=="__main__":
    main()
