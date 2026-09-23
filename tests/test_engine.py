"""Offline tests: the registration itself, against transforms applied on purpose.

    py -3.10 tests/test_engine.py

Needs numpy, scipy and trimesh. Does not need Fusion. Every answer here is exact
by construction - a known rigid transform is applied and the engine has to undo
it - so a pass means the arithmetic is right, not that it matched last time.
"""

import json
import math
import os
import subprocess
import sys
import tempfile

import numpy as np
import trimesh

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import gr_engine as E                                       # noqa: E402

ENGINE = os.path.join(ROOT, 'gr_engine.py')
FAILED = []


def check(name, cond, detail=''):
    print('  %s %s%s' % ('ok  ' if cond else 'FAIL', name,
                         '' if cond else '   %s' % (detail,)))
    if not cond:
        FAILED.append(name)


def rot(axis, deg):
    a = np.asarray(axis, float)
    a = a / np.linalg.norm(a)
    th = math.radians(deg)
    K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]], float)
    return np.eye(3) + math.sin(th) * K + (1 - math.cos(th)) * (K @ K)


def blob():
    """Three clearly different principal axes, and no symmetry end to end."""
    m = trimesh.creation.icosphere(subdivisions=4, radius=1.0)
    v = np.asarray(m.vertices).copy()
    v[:, 0] *= 3.0
    v[:, 1] *= 1.7
    d = np.linalg.norm(v - np.array([2.6, 0.0, 0.6]), axis=1)
    v += m.vertex_normals * np.exp(-(d ** 2) * 2.0)[:, None] * 0.7
    return trimesh.Trimesh(vertices=v, faces=np.asarray(m.faces), process=True)


def run(fixed, moving, **kw):
    job = {'fixed': {'coords': fixed.vertices.ravel().tolist(),
                     'idx': fixed.faces.ravel().tolist()},
           'moving': {'coords': moving.vertices.ravel().tolist(),
                      'idx': moving.faces.ravel().tolist()}}
    job.update(kw)
    d = tempfile.mkdtemp()
    jp, op = os.path.join(d, 'j.json'), os.path.join(d, 'o.json')
    json.dump(job, open(jp, 'w'))
    subprocess.run([sys.executable, ENGINE, jp, op], capture_output=True, text=True)
    return json.load(open(op))


# --------------------------------------------------------------------------- #
print('centre of gravity')
# --------------------------------------------------------------------------- #

# a box 2 x 4 x 6 centred on the origin: both centroids are the origin exactly
box = trimesh.creation.box(extents=(2.0, 4.0, 6.0))
v, f = np.asarray(box.vertices), np.asarray(box.faces)
c_vol, k_vol = E.centre_of_gravity(v, f, 'volume')
c_sur, k_sur = E.centre_of_gravity(v, f, 'surface')
check('the volume centroid of a centred box is the origin',
      np.allclose(c_vol, 0.0, atol=1e-12) and k_vol == 'volume', c_vol)
check('so is the area centroid', np.allclose(c_sur, 0.0, atol=1e-12)
      and k_sur == 'surface', c_sur)

# shift it and both must follow exactly
shift = np.array([3.0, -7.0, 11.0])
c2, _k = E.centre_of_gravity(v + shift, f, 'volume')
check('and it follows a translation exactly', np.allclose(c2, shift, atol=1e-12),
      c2)

# a cone: the volume centroid is at a quarter of the height, the area one is not.
# This is the case where using the vertex mean, or the wrong centroid, shows up.
cone = trimesh.creation.cone(radius=1.0, height=4.0, sections=256)
cv, cf = np.asarray(cone.vertices), np.asarray(cone.faces)
c_vol, _k = E.centre_of_gravity(cv, cf, 'volume')
check('a cone volume centroid sits at h/4', abs(c_vol[2] - 1.0) < 2e-3, c_vol[2])
# the area centroid of the same cone is a DIFFERENT and equally exact number:
# the slant surface has area pi*r*l with its centroid at h/3, the base disc has
# area pi*r^2 at z = 0, so zbar = (l*h/3) / (l + r). Asserting the analytic value
# rather than "it is different" is what makes this a test of the formula.
c_sur, _k = E.centre_of_gravity(cv, cf, 'surface')
slant = math.sqrt(1.0 + 16.0)
want = (slant * 4.0 / 3.0) / (slant + 1.0)
check('and its area centroid is (l*h/3)/(l+r), a different place',
      abs(c_sur[2] - want) < 2e-3, '%.4f vs %.4f' % (c_sur[2], want))
check('the vertex mean is different again from both',
      abs(float(cv[:, 2].mean()) - c_vol[2]) > 0.1, cv[:, 2].mean())

# --------------------------------------------------------------------------- #
print('the solvers, on exact correspondences')
# --------------------------------------------------------------------------- #

rng = np.random.default_rng(3)
P = rng.normal(size=(200, 3))
R_true = rot([0.3, -0.7, 0.2], 37.0)
t_true = np.array([1.5, -2.0, 0.75])
Q = P @ R_true.T + t_true

R, t = E.kabsch(P, Q)
check('Kabsch recovers a rotation exactly',
      np.allclose(R, R_true, atol=1e-12), np.abs(R - R_true).max())
check('and its translation', np.allclose(t, t_true, atol=1e-12),
      np.abs(t - t_true).max())

# a reflection must never be returned, however the points are arranged
Pm = P.copy()
Pm[:, 0] *= -1.0
Rm, _tm = E.kabsch(Pm, Q)
check('and never returns a reflection', np.linalg.det(Rm) > 0.99,
      np.linalg.det(Rm))

# point-to-plane on a small perturbation, with the normals of the target
N = rng.normal(size=(200, 3))
N /= np.linalg.norm(N, axis=1)[:, None]
small = rot([0.1, 0.2, -0.3], 1.5)
P2 = (Q - np.array([0.02, -0.01, 0.03])) @ small
Rp, tp = E.point_to_plane(P2, Q, N)
check('point-to-plane returns an orthogonal rotation',
      np.allclose(Rp @ Rp.T, np.eye(3), atol=1e-12),
      np.abs(Rp @ Rp.T - np.eye(3)).max())
before = np.abs(np.einsum('ij,ij->i', P2 - Q, N)).mean()
after = np.abs(np.einsum('ij,ij->i', P2 @ Rp.T + tp - Q, N)).mean()
check('and reduces the along-normal residual', after < before * 0.02,
      '%.3e -> %.3e' % (before, after))

# --------------------------------------------------------------------------- #
print('end to end, on transforms applied on purpose')
# --------------------------------------------------------------------------- #

base = blob()
CASES = [('30 deg about z', rot([0, 0, 1], 30.0), np.array([5.0, -2.0, 1.0])),
         ('120 deg about (1,1,1)', rot([1, 1, 1], 120.0), np.array([-8.0, 4.0, 3.0])),
         ('180 deg about y', rot([0, 1, 0], 180.0), np.array([2.0, 2.0, -6.0]))]

for label, R_t, t_t in CASES:
    moved = base.copy()
    moved.vertices = np.asarray(base.vertices) @ R_t.T + t_t
    res = run(base, moved, sample_points=15000, fixed_points=50000)
    if not res.get('ok'):
        check(label, False, res['error'].strip().splitlines()[-1][:70])
        continue
    M = np.array(res['matrix'])
    R_tot = M[:3, :3] @ R_t
    t_tot = M[:3, :3] @ t_t + M[:3, 3]
    ang = math.degrees(math.acos(max(-1, min(1, (np.trace(R_tot) - 1) / 2))))
    check('%s: the applied rotation is undone' % label, ang < 0.02,
          '%.4f deg left' % ang)
    check('%s: and the translation' % label,
          np.linalg.norm(t_tot) * 10.0 < 0.02,
          '%.4f mm left' % (np.linalg.norm(t_tot) * 10.0))
    check('%s: the residual is at the sampling floor' % label,
          res['after']['rms_mm'] < res['sampling_floor_mm'] * 1.3,
          '%.4f vs floor %.4f mm' % (res['after']['rms_mm'],
                                     res['sampling_floor_mm']))

# partial overlap: only part of the moving body exists on the fixed one
R_t, t_t = rot([0, 0, 1], 25.0), np.array([3.0, 1.0, -2.0])
moved = base.copy()
moved.vertices = np.asarray(base.vertices) @ R_t.T + t_t
keep = np.asarray(base.vertices)[:, 0] > -0.5
half = trimesh.Trimesh(vertices=moved.vertices,
                       faces=np.asarray(base.faces)[keep[base.faces].all(axis=1)],
                       process=True)
res = run(base, half, sample_points=15000, fixed_points=50000, overlap=0.6,
          gc='surface')
if res.get('ok'):
    R_tot = np.array(res['matrix'])[:3, :3] @ R_t
    ang = math.degrees(math.acos(max(-1, min(1, (np.trace(R_tot) - 1) / 2))))
    check('a 57 %% fragment still registers', ang < 0.2, '%.4f deg left' % ang)
else:
    check('a 57 %% fragment still registers', False, res['error'][-80:])

# the reported transform must be a rigid one, not a general matrix
M = np.array(res['matrix'])
check('the returned matrix is rigid: no scale, no shear',
      np.allclose(M[:3, :3] @ M[:3, :3].T, np.eye(3), atol=1e-9)
      and abs(np.linalg.det(M[:3, :3]) - 1.0) < 1e-9
      and np.allclose(M[3], [0, 0, 0, 1]))

print()
if FAILED:
    print('%d FAILED: %s' % (len(FAILED), ', '.join(FAILED)))
    raise SystemExit(1)
print('all engine tests passed')
