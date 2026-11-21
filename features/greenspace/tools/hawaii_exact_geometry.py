"""Source-specific exact intersections and oriented-cycle reconstruction.

Fractions represent the original binary-float coordinates exactly. Only new
intersection nodes are rounded; every original coordinate is retained.
"""

from fractions import Fraction
import math

import numpy as np
import shapely

from investigate_rings import cycles
from investigate_upper_snake import require


def cross(a,b):
    return a[0]*b[1]-a[1]*b[0]


def sub(a,b):
    return a[0]-b[0],a[1]-b[1]


def exact_area(coords):
    xy=[tuple(Fraction.from_float(float(v)) for v in p[:2]) for p in coords]
    origin=xy[0]
    xy=[sub(p,origin) for p in xy]
    return sum((cross(a,b) for a,b in zip(xy,xy[1:])),Fraction(0))/2


def reconstruct(source,candidate=None):
    rings=[list(r.coords) for p in source.geoms for r in [p.exterior,*p.interiors]]
    require(len(rings)==3228 and len(source.geoms)==2944,'Specific Hawaii source ring structure differs')
    edges=[]
    owners=[]
    for ri,ring in enumerate(rings):
        for si,(a,b) in enumerate(zip(ring,ring[1:])):
            if a!=b:
                edges.append((tuple(a[:2]),tuple(b[:2])))
                owners.append((ri,si))
    exact_cache={p:tuple(Fraction.from_float(float(v)) for v in p) for edge in edges for p in edge}
    tree=shapely.STRtree([shapely.LineString(edge) for edge in edges])
    inserts=[{} for _ in edges]
    crossings=[]
    exact_nodes={}
    for i,(af,bf) in enumerate(edges):
        a,b=exact_cache[af],exact_cache[bf]
        r=sub(b,a)
        for raw in tree.query(shapely.LineString([af,bf])):
            j=int(raw)
            if j<=i:
                continue
            cf,df=edges[j]
            c,d=exact_cache[cf],exact_cache[df]
            s,q=sub(d,c),sub(c,a)
            denominator=cross(r,s)
            if denominator:
                t,u=cross(q,s)/denominator,cross(q,r)/denominator
                if not (0<=t<=1 and 0<=u<=1):
                    continue
                point=a[0]+t*r[0],a[1]+t*r[1]
                if 0<t<1:
                    inserts[i][t]=point
                if 0<u<1:
                    inserts[j][u]=point
                if 0<t<1 or 0<u<1:
                    exact_nodes[point]=tuple(float(v) for v in point)
                if 0<t<1 and 0<u<1:
                    crossings.append({'edge_a':list(owners[i]),'edge_b':list(owners[j]),
                                      'exact_xy':[str(v) for v in point],'rounded_xy':tuple(float(v) for v in point)})
            elif cross(q,r)==0:
                axis=0 if r[0] else 1
                for p in (c,d):
                    t=(p[axis]-a[axis])/r[axis]
                    if 0<t<1:
                        inserts[i][t]=p
                axis=0 if s[0] else 1
                for p in (a,b):
                    u=(p[axis]-c[axis])/s[axis]
                    if 0<u<1:
                        inserts[j][u]=p
    insert_by_owner={owner:entry for owner,entry in zip(owners,inserts)}
    enriched=[]
    for ri,ring in enumerate(rings):
        output=[]
        for si,a in enumerate(ring[:-1]):
            output.append(tuple(a[:2]))
            output.extend(tuple(float(v) for v in p) for _,p in sorted(insert_by_owner.get((ri,si),{}).items()))
        output.append(tuple(ring[-1][:2]))
        enriched.append(output)
    decomposed=[c for r in enriched for c in cycles(r)]
    signed=[exact_area(c) for c in decomposed]
    require(all(v!=0 for v in signed),'Zero-area source cycle needs explicit non-area treatment')
    require(all(shapely.Polygon(c).is_valid for c in decomposed),'Exact-noded source cycle still invalid')
    shells=[c for c,v in zip(decomposed,signed) if v<0]
    holes=[c for c,v in zip(decomposed,signed) if v>0]
    noded=shapely.node(shapely.MultiLineString(enriched))
    faces=list(shapely.polygonize(shapely.get_parts(noded)).geoms)
    winding=exact_winding_rings(enriched,faces)
    original_winding=exact_winding_rings(rings,faces)
    require(winding==original_winding,'Exact original/enriched face witness classifications disagree')
    result=shapely.union_all([face for face,w in zip(faces,winding) if w>0])
    excluded=shapely.union_all([face for face,w in zip(faces,winding) if w<0])
    # Every generated boundary node must be from the exact source arrangement;
    # no new intersection from shell/hole union may appear silently.
    allowed={tuple(p[:2]) for r in enriched for p in r}
    require(all(tuple(p[:2]) in allowed for p in shapely.get_coordinates(noded)),'Noding introduced an unproved boundary node')
    require(all(tuple(p[:2]) in allowed for p in shapely.get_coordinates(result)),'Union introduced an unproved boundary node')
    require(result.is_valid and not result.is_empty and result.intersection(excluded).area==0,'Source-supported/excluded regions conflict')
    errors=[sum(abs(Fraction.from_float(v)-q) for v,q in zip(f,p)) for p,f in exact_nodes.items()]
    max_error=max(errors,default=Fraction(0))
    candidate_proof={}
    if candidate is not None:
        odd=shapely.union_all([face for face,w in zip(faces,winding) if w%2])
        require(candidate.symmetric_difference(odd).is_empty,'Prior candidate differs from exact-classified odd region')
        original_points=set(exact_cache)
        node_list=list(exact_nodes)
        node_tree=shapely.STRtree([shapely.Point(exact_nodes[p]) for p in node_list])
        candidate_errors=[]
        for coord in {tuple(p[:2]) for p in shapely.get_coordinates(candidate)}-original_points:
            index=int(node_tree.nearest(shapely.Point(coord)))
            exact=node_list[index]
            candidate_errors.append(sum(abs(Fraction.from_float(v)-q) for v,q in zip(coord,exact)))
        candidate_max=max(candidate_errors,default=Fraction(0))
        require(candidate_max<Fraction(1,1000000),'Prior candidate contains an unproved numerical node')
        candidate_proof={'candidate_vs_exact_classified_odd_region_symmetric_difference_m2':0.0,
                         'candidate_new_nodes_checked':len(candidate_errors),
                         'maximum_candidate_node_l1_error_upper_m':math.nextafter(float(candidate_max),math.inf)}
    original_exact=-sum((exact_area(r) for r in rings),Fraction(0))
    enriched_exact=-sum((exact_area(r) for r in enriched),Fraction(0))
    report={'source_rings':len(rings),'source_segments':len(edges),'exact_proper_crossings':crossings,
            'new_exact_arrangement_nodes':len(exact_nodes),'original_vertices_moved':0,
            'maximum_node_rounding_l1_distance_upper_m':math.nextafter(float(max_error),math.inf),
            'source_cycles_after_exact_noding':len(decomposed),'clockwise_shells':len(shells),
            'counterclockwise_cycles':len(holes),'negative_winding_faces':sum(w<0 for w in winding),
            'exact_ray_witnesses_checked':len(faces),'exact_original_and_enriched_witnesses_agree':True,
            'face_counts_by_exact_winding':{str(w):winding.count(w) for w in sorted(set(winding))},
            'source_signed_area_exact_rational_m2':str(original_exact),
            'enriched_signed_area_exact_rational_m2':str(enriched_exact),
            'signed_integral_rounding_change_m2':float(abs(enriched_exact-original_exact)),
            'independent_region_area_m2':result.area,'excluded_negative_area_m2':excluded.area,
            'independent_region_parts':len(list(shapely.get_parts(result))),
            'source_linework_length_m':shapely.MultiLineString(rings).length,
            'all_output_nodes_from_exact_source_arrangement':True,**candidate_proof}
    return result,excluded,noded,enriched,report


def exact_polygon_area(geometry):
    if geometry.is_empty:
        return Fraction(0)
    if geometry.geom_type in {'Point','LineString','LinearRing'}:
        return Fraction(0)
    if geometry.geom_type=='Polygon':
        return abs(exact_area(list(geometry.exterior.coords)))-sum((abs(exact_area(list(r.coords))) for r in geometry.interiors),Fraction(0))
    require(hasattr(geometry,'geoms'),'Nonpolygon geometry in area bound')
    return sum((exact_polygon_area(p) for p in geometry.geoms),Fraction(0))


def county_allowance(source,candidate,replacement,publisher_area,proof):
    # L-infinity tubes have exact area 2*r*(abs(dx)+abs(dy))+4*r*r per
    # segment. Sum without cancellation. The homotopy from each original
    # segment to its noded representation stays in this tube because every
    # inserted point is within r of its exact intersection and no source
    # vertex moves. Winding cannot change outside that homotopy support.
    radius=Fraction(1,1000000)
    require(proof['maximum_node_rounding_l1_distance_upper_m']<float(radius)
            and proof['maximum_candidate_node_l1_error_upper_m']<float(radius),
            'Numerical source reconstruction exceeds proved guard')
    rings=[list(r.coords) for p in source.geoms for r in [p.exterior,*p.interiors]]
    l1=Fraction(0)
    segments=0
    for ring in rings:
        for a,b in zip(ring,ring[1:]):
            if a!=b:
                segments+=1
                l1+=sum(abs(Fraction.from_float(float(x))-Fraction.from_float(float(y))) for x,y in zip(a[:2],b[:2]))
    tube=2*radius*l1+4*radius*radius*segments
    change=candidate.symmetric_difference(replacement)
    exact_change=exact_polygon_area(change)
    publisher_extra=abs(exact_polygon_area(replacement)-Fraction.from_float(float(publisher_area)))
    # Include both representation tubes, the whole candidate coverage change,
    # and the complete publisher-area disagreement as additional caution.
    total=2*tube+exact_change+publisher_extra
    upper=math.nextafter(float(total),math.inf)
    require(Fraction.from_float(upper)>=total,'Allowance conversion not outward rounded')
    return {'guard_radius_m':float(radius),'guard_radius_exact_m':str(radius),
            'source_segments':segments,'source_total_l1_length_exact_m':str(l1),
            'one_representation_tube_exact_m2':str(tube),'both_representation_tubes_m2':float(2*tube),
            'candidate_symmetric_difference_exact_m2':str(exact_change),
            'candidate_symmetric_difference_m2':float(exact_change),
            'additional_publisher_discrepancy_exact_m2':str(publisher_extra),
            'additional_publisher_discrepancy_m2':float(publisher_extra),
            'total_exact_m2':str(total),'total_outward_upper_m2':upper,
            'scope':'Mapped source coverage and numerical representation; publisher discrepancy is extra caution, not a bound on surveyed accuracy.'}


def exact_winding_checks(source,faces):
    """Exact directed rays at all face witnesses, using y-range filtering only."""
    rings=[list(r.coords) for p in source.geoms for r in [p.exterior,*p.interiors]]
    return exact_winding_rings(rings,faces)


def exact_winding_rings(rings,faces):
    edges=np.concatenate([np.column_stack([np.asarray(r)[:-1,:2],np.asarray(r)[1:,:2]]) for r in rings])
    x0,y0,x1,y1=edges.T
    checked=[]
    for face in faces:
        p=face.representative_point()
        px,py=Fraction.from_float(p.x),Fraction.from_float(p.y)
        selected=np.flatnonzero(((y0<=p.y)&(y1>p.y))|((y1<=p.y)&(y0>p.y)))
        winding=0
        for i in selected:
            ax,ay,bx,by=(Fraction.from_float(float(v)) for v in edges[i])
            left=(bx-ax)*(py-ay)-(px-ax)*(by-ay)
            require(left!=0,'Witness lies on exact source line')
            if y0[i]<=p.y<y1[i] and left>0:
                winding-=1
            elif y1[i]<=p.y<y0[i] and left<0:
                winding+=1
        checked.append(winding)
    return checked
