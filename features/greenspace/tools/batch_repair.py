"""Automated source reconciliation and conservative county-scale review bounds."""

from decimal import Decimal,localcontext
from fractions import Fraction
import hashlib
import itertools
import math

from osgeo import gdal,ogr
import shapely

from batch_exact_geometry import reconstruct,exact_polygon_area
from investigate_nj41938_curves import angle_minus_sine


def circle_parameters(points):
    # Native CircularString triples may describe long arcs or a full circle.
    # The NJ-specific helper deliberately rejects those; calculate their circle
    # directly and measure the actual GDAL chord angles instead.
    with localcontext() as context:
        context.prec=60
        a,b,c=[[Decimal.from_float(float(v)) for v in p[:2]] for p in points]
        if a==c:
            ux,uy=(b[0]-a[0])/2,(b[1]-a[1])/2
        else:
            bx,by=b[0]-a[0],b[1]-a[1]
            cx,cy=c[0]-a[0],c[1]-a[1]
            determinant=2*(bx*cy-by*cx)
            if not determinant:
                return None
            ux=((bx*bx+by*by)*cy-(cx*cx+cy*cy)*by)/determinant
            uy=(bx*(cx*cx+cy*cy)-cx*(bx*bx+by*by))/determinant
        return {'center_offset_from_first_control_m':[float(ux),float(uy)],
                'radius_m':math.sqrt(float(ux*ux+uy*uy))}


def curve_bound(native):
    total=0.0
    arcs=0
    stack=[native]
    while stack:
        g=stack.pop()
        if g.GetGeometryName()=='CIRCULARSTRING':
            controls=g.GetPoints()
            for i in range(0,len(controls)-2,2):
                points=controls[i:i+3]
                info=circle_parameters(points)
                if info is None or info['radius_m']==0:
                    continue
                curve=ogr.Geometry(ogr.wkbCircularString)
                for p in points:curve.AddPoint_2D(*p[:2])
                linear=curve.GetLinearGeometry().GetPoints()
                center=[points[0][k]+info['center_offset_from_first_control_m'][k] for k in range(2)]
                radius=info['radius_m']
                angles=[]
                for a,b in zip(linear,linear[1:]):
                    u=[a[k]-center[k] for k in range(2)]
                    v=[b[k]-center[k] for k in range(2)]
                    angles.append(abs(math.atan2(u[0]*v[1]-u[1]*v[0],u[0]*v[0]+u[1]*v[1])))
                residue=max(abs(math.hypot(p[0]-center[0],p[1]-center[1])-radius) for p in linear)
                guard=residue+1e-6+64*max(math.ulp(radius),*(math.ulp(v) for v in center))
                area=radius*radius/2*math.fsum(abs(angle_minus_sine(a) if a<0.1 else a-math.sin(a)) for a in angles)
                total+=area+4*radius*math.fsum(angles)*guard+4*len(angles)*math.pi*guard*guard
                arcs+=1
        else:
            stack.extend(g.GetGeometryRef(i) for i in range(g.GetGeometryCount()))
    return math.nextafter(total,math.inf) if arcs else 0.0,arcs


class BatchRepair:
    def __init__(self,home):
        self.home=home
        gdal.UseExceptions()
        archive=home/'source/padus-4.1/PADUS4_1Geodatabase.zip'
        self.dataset=gdal.OpenEx('/vsizip/'+archive.resolve().as_posix()+'/PADUS4_1Geodatabase.gdb',gdal.OF_VECTOR|gdal.OF_READONLY,allowed_drivers=['OpenFileGDB'])

    def reconcile(self,layer,fid,original,candidate,attributes):
        feature=self.dataset.GetLayerByName(layer).GetFeature(fid)
        native=feature.GetGeometryRef()
        default=bytes(native.GetLinearGeometry().ExportToWkb())
        if hashlib.sha256(default).digest()!=hashlib.sha256(shapely.to_wkb(original,byte_order=1)).digest():
            raise RuntimeError('Batch direct original native/default geometry differs')
        if any((None if feature.GetField(k) is None else str(feature.GetField(k)))!=v for k,v in attributes.items()):
            raise RuntimeError('Batch original attributes differ')
        region,negative,noded,enriched,proof=reconstruct(original,candidate)
        # Exact arrangement and original/enriched witness agreement prove source
        # coverage. Both original and candidate node guards are explicitly checked.
        radius=Fraction(1,1000000)
        rings=[list(r.coords) for p in original.geoms for r in [p.exterior,*p.interiors]]
        l1=Fraction(0)
        count=0
        for ring in rings:
            for a,b in zip(ring,ring[1:]):
                if a!=b:
                    count+=1
                    l1+=sum(abs(Fraction.from_float(float(x))-Fraction.from_float(float(y))) for x,y in zip(a[:2],b[:2]))
        tube=2*radius*l1+4*radius*radius*count
        delta=candidate.symmetric_difference(region)
        changed=exact_polygon_area(delta)
        curves,arc_count=curve_bound(native)
        bound=math.nextafter(float(2*tube+changed)+curves,math.inf)
        return region,{
            'method':'batch_exact_source_winding_and_county_effect_bound','credit':'Margaret',
            'source_native_sha256':hashlib.sha256(bytes(native.ExportToWkb())).hexdigest(),
            'source_default_sha256':hashlib.sha256(default).hexdigest(),
            'exact_source_proof':proof,'candidate_symmetric_difference_m2':float(changed),
            'area_added_m2':region.difference(candidate).area,'area_removed_m2':candidate.difference(region).area,
            'both_numeric_tubes_exact_m2':str(2*tube),'native_arc_count':arc_count,
            'native_curve_area_envelope_m2':curves,'review_absolute_difference_m2':bound,
            'publisher_area_diagnostic_m2':attributes.get('SHAPE_Area'),
            'policy':'Positive source winding once; negative/zero coverage excluded; original vertices retained. Complete changed area plus numerical and native-curve allowances; county cumulative percentage-point cap only.'}
