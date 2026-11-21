"""Conservative county allocation of preparation review allowances and risk."""

import math
import hashlib

import pyogrio
import shapely

ACRE=4046.8564224


class CountyReview:
    def __init__(self,home,percentage_only=False):
        self.percentage_only=percentage_only
        source=home/"source/census-2021/tl_2021_us_county.zip"
        with source.open("rb") as stream:
            actual=hashlib.file_digest(stream,"sha256").hexdigest()
        if actual!="26890ff92275ff1392cd205c7021ba510b7f63f2ff985ca327b9f5fd5e9ecc90":
            raise RuntimeError("Preserved Census county source changed")
        self.source_sha256=actual
        self.frame=pyogrio.read_dataframe(source,columns=["GEOID","NAME","ALAND"]).to_crs("ESRI:102039")
        self.tree=shapely.STRtree(self.frame.geometry.array)
        self.totals={}

    def footprint(self,original,derivative):
        a,b=original.bounds,derivative.bounds
        return shapely.box(min(a[0],b[0]),min(a[1],b[1]),max(a[2],b[2]),max(a[3],b[3]))

    def allocate(self,original,derivative,amount):
        if not math.isfinite(amount) or amount<0:
            raise RuntimeError("Unassessable county review allowance")
        if amount==0:
            return []
        box=self.footprint(original,derivative)
        allocations=[]
        for index in self.tree.query(box,predicate="intersects"):
            county=self.frame.iloc[index]
            code=str(county["GEOID"])
            denominator=min(float(county["ALAND"]),county.geometry.area)
            if denominator<=0:
                raise RuntimeError("Missing conservative county sensitivity denominator")
            entry=self.totals.setdefault(code,{"county_fips":code,"name":str(county["NAME"]),"review_allowance_m2":0.0,
                                               "conservative_sensitivity_denominator_m2":denominator})
            entry["review_allowance_m2"]+=amount
            entry["review_allowance_acres"]=entry["review_allowance_m2"]/ACRE
            entry["percentage_point_sensitivity"]=entry["review_allowance_m2"]/denominator*100
            allocations.append({"county_fips":code,"allocated_review_allowance_m2":amount})
        if not allocations:
            if not self.percentage_only:
                raise RuntimeError("Nonzero source review allowance cannot be assigned to Census coverage")
            # No reviewed county intersects even the complete linear source /
            # derivative envelope (for example remote offshore polygons).
            # Such prepared geometries cannot alter a county intersection.
        return allocations

    def material(self):
        return [v for v in self.totals.values() if (not self.percentage_only and v["review_allowance_m2"]>ACRE) or v["percentage_point_sensitivity"]>0.01]

    def unresolved_risk(self,original,derivative):
        box=self.footprint(original,derivative)
        rows=[]
        for index in self.tree.query(box,predicate="intersects"):
            county=self.frame.iloc[index]
            code=str(county["GEOID"])
            denominator=min(float(county["ALAND"]),county.geometry.area)
            possible=county.geometry.intersection(box).area
            existing=self.totals.get(code,{}).get("review_allowance_m2",0)
            rows.append({"county_fips":code,"name":str(county["NAME"]),"unresolved_footprint_upper_bound_m2":possible,
                         "unresolved_footprint_upper_bound_acres":possible/ACRE,
                         "existing_review_allowance_m2":existing,"percentage_point_upper_sensitivity":possible/denominator*100,
                         "material_change_cannot_be_excluded":possible+existing>ACRE or (possible+existing)/denominator*100>0.01})
        return {"counties":rows,"material_change_cannot_be_excluded":not rows or any(r["material_change_cannot_be_excluded"] for r in rows),
                "interpretation":"Entire source/derivative envelope intersected with counties bounds unresolved effects; this is a deliberately broad risk screen, not measured lost land or a county total."}
