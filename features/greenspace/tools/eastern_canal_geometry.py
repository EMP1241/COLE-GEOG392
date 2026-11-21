"""Independent exact-segment reconstruction for the specific canal record."""

from fractions import Fraction
import math

import shapely

from investigate_rings import signed_area
from investigate_upper_snake import require


def cross(a,b):
    return a[0]*b[1]-a[1]*b[0]


def intersection(a,b,c,d):
    """Proper intersection of exact binary-float rational source coordinates."""
    a,b,c,d=[tuple(Fraction.from_float(float(v)) for v in p[:2]) for p in [a,b,c,d]]
    r=(b[0]-a[0],b[1]-a[1])
    s=(d[0]-c[0],d[1]-c[1])
    q=(c[0]-a[0],c[1]-a[1])
    denominator=cross(r,s)
    if not denominator:
        return None
    t,u=cross(q,s)/denominator,cross(q,r)/denominator
    if not (0<t<1 and 0<u<1):
        return None
    return (a[0]+t*r[0],a[1]+t*r[1]),t,u


def exact_area(coords):
    xy=[tuple(v if isinstance(v,Fraction) else Fraction.from_float(float(v)) for v in p[:2]) for p in coords]
    ox,oy=xy[0]
    xy=[(x-ox,y-oy) for x,y in xy]
    return sum((cross(a,b) for a,b in zip(xy,xy[1:])),Fraction(0))/2


def reconstruct(source):
    rings=[list(r.coords) for p in source.geoms for r in [p.exterior,*p.interiors]]
    bad=[i for i,r in enumerate(rings) if not shapely.Polygon(r).is_valid]
    require(bad==[8] and len(rings)==11,"Specific source ring structure differs")
    ring=rings[8]
    edges=[shapely.LineString([a,b]) for a,b in zip(ring,ring[1:])]
    tree=shapely.STRtree(edges)
    crossings=[]
    for i,edge in enumerate(edges):
        for raw in tree.query(edge):
            j=int(raw)
            if j<=i+1 or (i==0 and j==len(edges)-1):
                continue
            found=intersection(ring[i],ring[i+1],ring[j],ring[j+1])
            if found is not None:
                crossings.append((i,j,*found))
    require(len(crossings)==1,"Unexpected number of proper source crossings")
    i,j,point,t,u=crossings[0]
    floating=tuple(float(v) for v in point)
    loop_a=[floating,*ring[i+1:j+1],floating]
    loop_b=[floating,*ring[j+1:-1],*ring[:i+1],floating]
    exact_loops=[[point,*ring[i+1:j+1],point],[point,*ring[j+1:-1],*ring[:i+1],point]]
    require(all(shapely.Polygon(r).is_valid for r in [loop_a,loop_b]),"Split source loop remains invalid")
    all_rings=[r for k,r in enumerate(rings) if k!=8]+[loop_a,loop_b]
    shells=[r for r in all_rings if signed_area(r)<0]
    holes=[r for r in all_rings if signed_area(r)>0]
    require(len(shells)==10 and len(holes)==2,"Specific oriented loop roles differ")
    polygons=[shapely.Polygon(r) for r in shells]
    assignments=[[] for _ in shells]
    uncontained=[]
    hole_log=[]
    for ring in holes:
        hole=shapely.Polygon(ring)
        targets=[k for k,shell in enumerate(polygons) if shell.covers(hole)]
        if targets:
            target=min(targets,key=lambda k:polygons[k].area)
            assignments[target].append(ring)
            hole_log.append({"area_m2":hole.area,"assigned_shell":target})
        else:
            uncontained.append(hole)
            hole_log.append({"area_m2":hole.area,"assigned_shell":None})
    require(len(uncontained)==1 and uncontained[0].area<0.01,"Uncontained source area exceeds targeted tiny lobe")
    reconstructed=shapely.MultiPolygon([shapely.Polygon(r,assignments[k]) for k,r in enumerate(shells)])
    require(reconstructed.is_valid,"Independent reconstructed source region invalid")
    require(uncontained[0].intersection(reconstructed).area==0,"Unconfirmed lobe overlaps supported source coverage")
    exact_signed=[exact_area(r) for r in exact_loops]
    clockwise_index=next(k for k,v in enumerate(exact_signed) if v<0)
    exact_supported_loop=exact_loops[clockwise_index]
    rounded_point=tuple(Fraction.from_float(v) for v in floating)
    rounding_l1=sum(abs(a-b) for a,b in zip(point,rounded_point))
    neighbor_a=exact_supported_loop[1]
    neighbor_b=exact_supported_loop[-2]
    def triangle_bound(neighbor):
        neighbor=tuple(v if isinstance(v,Fraction) else Fraction.from_float(float(v)) for v in neighbor[:2])
        return abs(cross((rounded_point[0]-point[0],rounded_point[1]-point[1]),(neighbor[0]-point[0],neighbor[1]-point[1])))/2
    rounding_area_bound=triangle_bound(neighbor_a)+triangle_bound(neighbor_b)
    exact_positive=-sum((exact_area(r) for r in rings),Fraction(0))+next(v for v in exact_signed if v>0)
    reconstructed_exact=sum((abs(exact_area(list(p.exterior.coords)))-sum((abs(exact_area(list(r.coords))) for r in p.interiors),Fraction(0)) for p in reconstructed.geoms),Fraction(0))
    require(abs(reconstructed_exact-exact_positive)<=rounding_area_bound,"Exact source area exceeds rounding-triangle bound")
    crossing_geometry=edges[i].intersection(edges[j])
    require(crossing_geometry.geom_type=="Point","GEOS crossing result is not one point")
    error=shapely.Point(floating).distance(crossing_geometry)
    original_ring=rings[8]
    report={"self_crossing_ring":8,"source_segment_indices":[i,j],"source_segment_endpoints":[original_ring[i],original_ring[i+1],original_ring[j],original_ring[j+1]],
            "exact_rational_intersection_xy":[str(v) for v in point],"rounded_intersection_xy":floating,
            "exact_intersection_parameters_t_u":[str(t),str(u)],"geos_intersection_xy":[crossing_geometry.x,crossing_geometry.y],
            "geos_vs_correctly_rounded_exact_intersection_distance_m":error,
            "exact_split_signed_areas_rational_m2":[str(v) for v in exact_signed],
            "exact_split_signed_areas_m2":[float(v) for v in exact_signed],"holes":hole_log,
            "exact_source_positive_area_rational_m2":str(exact_positive),"exact_source_positive_area_m2":float(exact_positive),
            "accepted_float_polygon_exact_area_rational_m2":str(reconstructed_exact),"exact_area_rounding_difference_m2":float(abs(reconstructed_exact-exact_positive)),
            "intersection_rounding_l1_distance_upper_bound_m":math.nextafter(float(rounding_l1),math.inf),
            "intersection_rounding_symmetric_difference_triangle_bound_m2":math.nextafter(float(rounding_area_bound),math.inf),
            "original_vertices_moved":0,"new_intersection_nodes":1,"supported_shells":10,"uncontained_counterclockwise_loops":1,
            "independent_reconstruction_area_m2":reconstructed.area,"excluded_unconfirmed_lobe_area_m2":uncontained[0].area}
    return reconstructed,uncontained[0],report
