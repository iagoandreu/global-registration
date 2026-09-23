"""The registration itself. Runs OUTSIDE Fusion, under py -3.10 with numpy/scipy.

    py -3.10 gr_engine.py job.json out.json

Fusion's bundled Python has no numpy, and an ICP on a 200 000-triangle scan is not
something to write in stdlib loops, so the same split Mesh to Part uses applies
here: the add-in reads the geometry, this does the arithmetic.

Everything in and out is in CENTIMETRES, Fusion's internal unit. Reported figures
are in millimetres, because that is what the user reads.

WHY IT IS THREE STAGES AND NOT JUST ICP
    ICP only ever finds the nearest local minimum. Started from a gross
    misorientation - which is exactly the case this is for - it converges
    confidently to nonsense. So:

    1. Centre of gravity. Removes the translation entirely, and costs nothing.
       For a closed body the VOLUME centroid is the real centre of gravity; for
       an open one only the area centroid exists. Neither is the mean of the
       vertices, which is a measure of where the tessellation is dense.

    2. Principal axes. The inertia axes of the two point sets, aligned. An
       eigenvector has no sign, so there are four proper rotations that match the
       axes, and a near-symmetric part will genuinely prefer the wrong one - so
       all four are tried and scored, and each is then refined by a few ICP
       iterations before scoring again. Scoring the raw candidates instead picks
       the wrong basin on anything whose axes are close in magnitude.

    3. ICP proper, from the best candidate.

POINT-TO-PLANE, AND WHY IT IS THE DEFAULT
    Point-to-point ICP slides to a halt on a smooth surface: every correspondence
    pulls towards a specific vertex, and on a flat region those pulls fight each
    other instead of letting the surfaces slide. Point-to-plane only penalises
    motion ALONG the surface normal, so the surfaces slide freely and lock in the
    directions that matter. On the femur here it converges in a third the
    iterations.

TRIMMING
    A fixed distance threshold has to be guessed. Keeping the best fraction of
    correspondences instead adapts as the alignment improves, which is what makes
    partial overlap work: set the overlap to what you believe is common to both.
"""

import json
import math
import sys
import time

import numpy as np
from scipy.spatial import cKDTree


# --------------------------------------------------------------------------- #
# geometry
# --------------------------------------------------------------------------- #

def triangles(coords, idx):
    v = np.asarray(coords, float).reshape(-1, 3)
    f = np.asarray(idx, np.int64).reshape(-1, 3)
    return v, f


def face_areas_and_centroids(v, f):
    a = v[f[:, 0]]
    b = v[f[:, 1]]
    c = v[f[:, 2]]
    cross = np.cross(b - a, c - a)
    area = 0.5 * np.linalg.norm(cross, axis=1)
    return area, (a + b + c) / 3.0, cross


def centre_of_gravity(v, f, kind='auto'):
    """(point, which). 'volume' when the mesh is closed, else 'surface'.

    The volume centroid comes from the signed tetrahedron sum against the origin,
    which is exact for any closed oriented mesh and needs no decomposition. It is
    only meaningful if the mesh really is closed - a hole makes the signed sum
    drift - so it is checked and abandoned rather than trusted.
    """
    area, cent, cross = face_areas_and_centroids(v, f)
    surface = (cent * area[:, None]).sum(axis=0) / max(area.sum(), 1e-30)
    if kind == 'surface':
        return surface, 'surface'

    a = v[f[:, 0]]
    b = v[f[:, 1]]
    c = v[f[:, 2]]
    vol6 = np.einsum('ij,ij->i', a, np.cross(b, c))      # 6 * signed tet volume
    total = vol6.sum()
    if kind == 'volume' or abs(total) > 1e-12:
        moment = ((a + b + c) * vol6[:, None]).sum(axis=0) / 4.0
        if abs(total) > 1e-12:
            return moment / total, 'volume'
    return surface, 'surface'


def sample_surface(v, f, n, seed=0):
    """n points spread by AREA over the mesh, with the normal at each.

    Area-weighted rather than "every vertex", because a scan's vertex density
    follows its curvature and its decimation history, not its shape - fitting to
    vertices weights a finely tessellated condyle far above a flat shaft.
    """
    area, _c, cross = face_areas_and_centroids(v, f)
    total = area.sum()
    if total <= 0:
        raise RuntimeError('the mesh has no area')
    rng = np.random.default_rng(seed)
    n = int(min(n, 4_000_000))
    pick = rng.choice(len(f), size=n, p=area / total)
    r1 = np.sqrt(rng.random(n))
    r2 = rng.random(n)
    a = v[f[pick, 0]]
    b = v[f[pick, 1]]
    c = v[f[pick, 2]]
    pts = (1.0 - r1)[:, None] * a + (r1 * (1.0 - r2))[:, None] * b + (r1 * r2)[:, None] * c
    nrm = cross[pick]
    nrm = nrm / np.maximum(np.linalg.norm(nrm, axis=1), 1e-30)[:, None]
    return pts, nrm


def principal_frame(pts, weights=None):
    """Weighted principal axes, columns ordered by DESCENDING spread."""
    w = np.ones(len(pts)) if weights is None else np.asarray(weights, float)
    c = (pts * w[:, None]).sum(axis=0) / w.sum()
    d = pts - c
    cov = (d * w[:, None]).T @ d / w.sum()
    val, vec = np.linalg.eigh(cov)
    order = np.argsort(val)[::-1]
    R = vec[:, order]
    if np.linalg.det(R) < 0:
        R[:, 2] *= -1.0
    return c, R, val[order]


# --------------------------------------------------------------------------- #
# the two solvers
# --------------------------------------------------------------------------- #

def kabsch(P, Q, w=None):
    """Rigid transform taking P onto Q, minimising the squared point distance."""
    w = np.ones(len(P)) if w is None else np.asarray(w, float)
    sw = w.sum()
    pc = (P * w[:, None]).sum(axis=0) / sw
    qc = (Q * w[:, None]).sum(axis=0) / sw
    H = ((P - pc) * w[:, None]).T @ (Q - qc)
    U, _S, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    R = Vt.T @ np.diag([1.0, 1.0, d]) @ U.T
    return R, qc - R @ pc


def point_to_plane(P, Q, N, w=None):
    """Rigid transform minimising the distance ALONG the fixed surface normal.

    Linearised about the identity: valid while the residual rotation is small,
    which after the principal-axis stage it is. The rotation is re-orthogonalised
    rather than used raw, so an iteration can never introduce shear or scale.
    """
    w = np.ones(len(P)) if w is None else np.asarray(w, float)
    sw = np.sqrt(w)[:, None]
    A = np.hstack([np.cross(P, N), N]) * sw
    b = -np.einsum('ij,ij->i', P - Q, N)[:, None] * sw
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    a = sol[:3, 0]
    t = sol[3:, 0]
    # exponential map of the small rotation, so the result is orthogonal exactly
    theta = np.linalg.norm(a)
    K = np.array([[0.0, -a[2], a[1]], [a[2], 0.0, -a[0]], [-a[1], a[0], 0.0]])
    if theta < 1e-12:
        R = np.eye(3) + K
        U, _s, Vt = np.linalg.svd(R)
        R = U @ Vt
    else:
        k = K / theta
        R = np.eye(3) + math.sin(theta) * k + (1.0 - math.cos(theta)) * (k @ k)
    return R, t


# --------------------------------------------------------------------------- #
# ICP
# --------------------------------------------------------------------------- #

def icp(src, tree, fixed_pts, fixed_nrm, R0, t0, metric='plane', iters=60,
        overlap=1.0, max_dist=0.0, tol=1.0e-7, log=None):
    """Returns (R, t, stats). src is the moving sample; R0/t0 the starting pose."""
    R = np.array(R0, float)
    t = np.array(t0, float)
    prev = None
    used = 0
    rms = float('inf')
    it = 0
    for it in range(1, int(iters) + 1):
        P = src @ R.T + t
        d, j = tree.query(P, k=1, workers=-1)
        keep = np.ones(len(P), bool)
        if max_dist > 0.0:
            keep &= d <= max_dist
        if overlap < 1.0:
            k = max(3, int(round(overlap * keep.sum())))
            if k < keep.sum():
                thr = np.partition(d[keep], k - 1)[k - 1]
                keep &= d <= thr
        if keep.sum() < 3:
            break
        used = int(keep.sum())
        rms = float(np.sqrt((d[keep] ** 2).mean()))

        Pk = P[keep]
        Qk = fixed_pts[j[keep]]
        if metric == 'plane' and fixed_nrm is not None:
            dR, dt = point_to_plane(Pk, Qk, fixed_nrm[j[keep]])
        else:
            dR, dt = kabsch(Pk, Qk)
        R = dR @ R
        t = dR @ t + dt

        if log and (it % 10 == 0 or it == 1):
            log('  iter %3d  rms %.5f mm over %d points' % (it, rms * 10.0, used))
        if prev is not None and abs(prev - rms) < tol:
            break
        prev = rms
    return R, t, {'iterations': it, 'pairs': used, 'rms_mm': rms * 10.0}


def residuals(P, tree, fixed_pts=None, fixed_nrm=None):
    """Distance from each moving point to the fixed surface, both ways of saying it.

    Point-to-point has a FLOOR that has nothing to do with alignment: the fixed
    cloud is a finite sample of a curved surface, so even a perfect alignment
    leaves every moving point about half the cloud spacing from the nearest fixed
    point. On the synthetic blob that floor is 0.15 mm and a perfect registration
    reports exactly 0.15 mm, which looks like error and is not.

    The distance to the TANGENT PLANE at that nearest point removes almost all of
    it, and is the honest reading of how far the surfaces are apart. Both are
    reported, with the floor, so the point-to-point figure stays interpretable.
    """
    d, j = tree.query(P, k=1, workers=-1)
    out = {'rms_mm': float(np.sqrt((d ** 2).mean()) * 10.0),
           'mean_mm': float(d.mean() * 10.0),
           'p95_mm': float(np.percentile(d, 95) * 10.0),
           'max_mm': float(d.max() * 10.0)}
    if fixed_pts is not None and fixed_nrm is not None:
        dp = np.abs(np.einsum('ij,ij->i', P - fixed_pts[j], fixed_nrm[j]))
        out.update({'plane_rms_mm': float(np.sqrt((dp ** 2).mean()) * 10.0),
                    'plane_p95_mm': float(np.percentile(dp, 95) * 10.0),
                    'plane_max_mm': float(dp.max() * 10.0)})
    return out


# --------------------------------------------------------------------------- #
# the job
# --------------------------------------------------------------------------- #

def register(job, log):
    t0 = time.time()
    fv, ff = triangles(job['fixed']['coords'], job['fixed']['idx'])
    mv, mf = triangles(job['moving']['coords'], job['moving']['idx'])
    log('fixed  %d verts %d tris' % (len(fv), len(ff)))
    log('moving %d verts %d tris' % (len(mv), len(mf)))

    gc_kind = job.get('gc', 'auto')
    fc, fk = centre_of_gravity(fv, ff, gc_kind)
    mc, mk = centre_of_gravity(mv, mf, gc_kind)
    log('centre of gravity: fixed %s, moving %s' % (fk, mk))
    log('  fixed  %.4f %.4f %.4f mm' % tuple(fc * 10.0))
    log('  moving %.4f %.4f %.4f mm' % tuple(mc * 10.0))

    n_fixed = int(job.get('fixed_points', 120000))
    n_move = int(job.get('sample_points', 40000))
    fixed_pts, fixed_nrm = sample_surface(fv, ff, n_fixed, seed=11)
    # the fixed cloud carries the vertices too: a sampled cloud alone leaves the
    # sharpest features under-represented, and those are what pin the rotation
    fixed_pts = np.vstack([fixed_pts, fv])
    fixed_nrm = np.vstack([fixed_nrm, vertex_normals(fv, ff)])
    src, _sn = sample_surface(mv, mf, n_move, seed=7)
    tree = cKDTree(fixed_pts)

    # what a perfect alignment would still report, from the cloud spacing alone
    f_area = float(face_areas_and_centroids(fv, ff)[0].sum()) * 100.0
    floor = 0.5 * math.sqrt(f_area / max(len(fixed_pts), 1))
    before = residuals(src, tree, fixed_pts, fixed_nrm)
    log('point-to-point floor from the fixed cloud spacing: %.4f mm' % floor)
    log('before: rms %.4f max %.4f mm' % (before['rms_mm'], before['max_mm']))

    # ---- stage 1: centre of gravity -------------------------------------- #
    R_gc = np.eye(3)
    t_gc = fc - mc
    candidates = [('centre of gravity', R_gc, t_gc)]

    # ---- stage 2: principal axes, all four proper rotations -------------- #
    if job.get('principal_axes', True):
        _fcp, Rf, fval = principal_frame(fixed_pts)
        _mcp, Rm, mval = principal_frame(src)
        log('principal spread fixed  %s mm' % np.round(np.sqrt(fval) * 10.0, 3))
        log('principal spread moving %s mm' % np.round(np.sqrt(mval) * 10.0, 3))
        for sx in (1.0, -1.0):
            for sy in (1.0, -1.0):
                S = np.diag([sx, sy, sx * sy])        # det = +1 always
                R = Rf @ S @ Rm.T
                candidates.append(('principal axes %+d%+d' % (sx, sy), R,
                                   fc - R @ mc))

    # ---- score the candidates, each after a short refinement ------------- #
    probe_iters = int(job.get('probe_iters', 12))
    scored = []
    for name, R, t in candidates:
        Rr, tr, st = icp(src, tree, fixed_pts, fixed_nrm, R, t,
                         metric=job.get('metric', 'plane'), iters=probe_iters,
                         overlap=float(job.get('overlap', 1.0)),
                         max_dist=float(job.get('max_distance_mm', 0.0)) * 0.1,
                         tol=float(job.get('tolerance_mm', 1e-6)) * 0.1)
        scored.append((st['rms_mm'], name, Rr, tr))
        log('  seed %-22s -> rms %.5f mm after %d probe iterations'
            % (name, st['rms_mm'], st['iterations']))
    scored.sort(key=lambda x: x[0])
    _best_rms, best_name, R, t = scored[0]
    log('seed chosen: %s' % best_name)

    # ---- stage 3: the real run ------------------------------------------- #
    R, t, st = icp(src, tree, fixed_pts, fixed_nrm, R, t,
                   metric=job.get('metric', 'plane'),
                   iters=int(job.get('iterations', 60)),
                   overlap=float(job.get('overlap', 1.0)),
                   max_dist=float(job.get('max_distance_mm', 0.0)) * 0.1,
                   tol=float(job.get('tolerance_mm', 1e-6)) * 0.1,
                   log=log)

    # measured on a DIFFERENT sample from the one that was fitted, so the number
    # is not the objective congratulating itself
    check, _cn = sample_surface(mv, mf, min(n_move, 60000), seed=999)
    after = residuals(check @ R.T + t, tree, fixed_pts, fixed_nrm)
    log('after:  rms %.5f mean %.5f p95 %.5f max %.5f mm  (floor %.5f)'
        % (after['rms_mm'], after['mean_mm'], after['p95_mm'], after['max_mm'],
           floor))
    log('        to the tangent plane: rms %.5f p95 %.5f max %.5f mm'
        % (after['plane_rms_mm'], after['plane_p95_mm'], after['plane_max_mm']))

    # Re-orthogonalise before handing it over. R is the product of dozens of
    # per-iteration rotations and drifts by a few ulp; Fusion's defineAsFreeMove
    # rejects anything that is not orthogonal, and would be right to.
    U, _s, Vt = np.linalg.svd(R)
    R = U @ Vt
    if np.linalg.det(R) < 0:
        U[:, 2] *= -1.0
        R = U @ Vt

    M = np.eye(4)
    M[:3, :3] = R
    M[:3, 3] = t
    ang = math.degrees(math.acos(max(-1.0, min(1.0, (np.trace(R) - 1.0) / 2.0))))
    return {
        'ok': True,
        'matrix': M.tolist(),
        'seed': best_name,
        'gc_kind': {'fixed': fk, 'moving': mk},
        'gc_fixed_mm': (fc * 10.0).tolist(),
        'gc_moving_mm': (mc * 10.0).tolist(),
        'rotation_deg': ang,
        'translation_mm': (t * 10.0).tolist(),
        'before': before,
        'after': after,
        'sampling_floor_mm': floor,
        'iterations': st['iterations'],
        'pairs': st['pairs'],
        'fixed_points': int(len(fixed_pts)),
        'moving_points': int(len(src)),
        'secs': round(time.time() - t0, 2),
    }


def vertex_normals(v, f):
    n = np.zeros_like(v)
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    cr = np.cross(b - a, c - a)
    for k in range(3):
        np.add.at(n, f[:, k], cr)
    ln = np.linalg.norm(n, axis=1)
    ln[ln == 0.0] = 1.0
    return n / ln[:, None]


def main():
    job = json.load(open(sys.argv[1], encoding='utf-8'))
    lines = []

    def log(msg):
        lines.append(msg)
        print(msg, flush=True)

    try:
        res = register(job, log)
        res['log'] = lines
    except Exception:
        import traceback
        res = {'ok': False, 'error': traceback.format_exc(), 'log': lines}
    with open(sys.argv[2], 'w', encoding='utf-8') as fh:
        json.dump(res, fh)


if __name__ == '__main__':
    main()
