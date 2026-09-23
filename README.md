# Global Registration

A Fusion 360 add-in that aligns one body onto another **by shape alone** — no
landmarks, no common coordinate system — and carries any number of *move along*
entities with it on the same transform.

The 3-matic Global Registration workflow, in Fusion. Built for the case where a
scan, a segmentation and a design have drifted into different coordinate systems
and the plate has to stay where it was put relative to the bone.

**Mesh bodies and solids are interchangeable.** Fixed and moving may be one of
each; both are read as triangles, a mesh body directly and a solid by
tessellation.

## Measured

A distal femur that had been moved 1.3 m away and turned 90°, registered back
onto the CT mesh it came from:

| | before | after |
|---|---|---|
| rms | 1252.05 mm | **0.2927 mm** |
| p95 | — | 0.5053 mm |
| max | 1333.29 mm | 0.9378 mm |
| rms to the tangent plane | — | **0.0098 mm** |

Two seconds, 20 614 triangles each side. Because that pair happens to be the same
mesh rigidly moved, the exact answer is available by Procrustes on the vertex
correspondence — and the registration lands **0.00045° and 0.00074 mm** from it,
with a per-vertex residual of 0.000489 mm at worst.

### Why two "after" numbers, and which one to read

Point-to-point distance has a **floor that has nothing to do with alignment**. The
fixed surface is represented by a finite cloud of sample points, so even a perfect
alignment leaves every moving point about half the cloud spacing from the nearest
fixed point. On the femur above that floor is 0.2609 mm, and the registration
reports 0.2927 mm — which looks like a third of a millimetre of error and is
almost entirely discretisation.

The distance to the **tangent plane** at that nearest point removes it, and is the
honest reading of how far the two surfaces are apart. Both are reported, with the
floor, so neither number can mislead on its own.

## How it works — three stages, not just ICP

ICP only ever finds the nearest local minimum. Started from a gross
misorientation, which is exactly what this tool is for, it converges confidently
to nonsense. So:

**1. Centre of gravity.** Removes the translation entirely and costs nothing. For
a closed body the **volume** centroid is the real centre of gravity; for an open
one only the area centroid exists; the mode picks automatically or you force one.
Neither is the mean of the vertices, which measures where the tessellation is
dense rather than where the shape is — on a cone of height 4 the volume centroid
is at 1.000, the area centroid at 1.073 and the vertex mean somewhere else again.

**2. Principal axes.** The inertia axes of both point clouds, aligned. An
eigenvector has no sign, so four proper rotations match the axes, and a
near-symmetric part genuinely prefers the wrong one. All four are tried, each is
refined by a few ICP iterations, and *then* scored — scoring the raw candidates
picks the wrong basin whenever two axes are close in magnitude. On the femur the
four seeds scored 0.29, 5.43, 9.92 and 9.69 mm, so the choice was not close.

**3. ICP**, from the best seed.

## Parameters

| | what it does |
|---|---|
| **Centre of gravity** | volume if closed / always volume / always surface |
| **Also try the principal axes** | off makes it a local refinement from the current pose |
| **Metric** | point-to-plane (default) or point-to-point |
| **Max iterations** | ICP iteration cap |
| **Sample points (moving)** | how many points are fitted |
| **Sample points (fixed)** | how densely the target surface is represented — this sets the point-to-point floor |
| **Overlap used** | keep only the best *n*% of correspondences each iteration. This is what makes partial overlap work: a 57% fragment registers to 0.0007° with overlap at 60% |
| **Reject pairs beyond** | absolute distance cut, 0 for none |
| **Converged when rms changes less than** | |
| **Apply the transform** | off computes and reports without moving anything |
| **Keep the originals where they are** | duplicate first, move the copies |

**Point-to-plane is the default** because point-to-point ICP slides to a halt on a
smooth surface: every correspondence pulls towards one specific sample point, and
on a flat region those pulls fight each other instead of letting the surfaces
slide. Point-to-plane only penalises motion along the surface normal.

## Move along

Anything selected there gets the same transform: solids, mesh bodies, surfaces,
sketches, construction geometry, or whole components. Fixed and moving accept
mesh, solid and surface too, in any combination — a mesh may be the fixed entity
and a solid the moving one, since everything is read as triangles.

**The collection may not be mixed, and Fusion will not tell you.** Measured here:
a mesh body and a B-Rep body in one `ObjectCollection` moves the mesh and leaves
the solid exactly where it was, with `defineAsFreeMove` and `moveFeatures.add`
both reporting success and no exception raised. That is how a plate stayed behind
while two bones aligned. The documentation only says B-Rep bodies and faces may
not be mixed; it does not mention meshes at all. So there is **one move feature
per kind**, not one for everything.

**A B-Rep body inside a BaseFeature is a second case.** Moved plainly it raises
*"Some input argument is invalid"* and does not move; it works only with
`targetBaseFeature` set and the base feature's edit reopened. This matters
because bodies produced by Mesh to Part live in base features.

**Construction geometry cannot be moved at all** in a parametric design — the move
feature refuses it with *"Environment is not supported"*. That is not a bug to
work around: construction geometry is defined by what it references and follows
that. It is reported per entity rather than silently skipped. To carry it along,
put it in a component and move the occurrence.

Occurrences are composed onto their own placement, because for an occurrence the
placement *is* the transform.

## Keeping the originals

With the checkbox on, the moving entity and every move-along entity is duplicated
before anything moves. **The copy is what moves** and takes `" (registered)"`;
what you selected stays exactly where it was and takes `" (original)"`.

That way round on purpose: anything already referencing the body you picked keeps
referencing something that has not moved, and the aligned version arrives as new
geometry rather than as a silent edit to old geometry.

An occurrence is duplicated as a second instance of the same component, left at
the old placement. Anything that refuses to copy is passed through and moved
itself, with a note saying so — better than refusing the whole operation over one
selection.

## Nothing is reported as moved until it has been measured

Every entity is checked after the move: where it actually ended up against where
the matrix says it should be, printed per entity with the error in millimetres.

That check is not decoration — it is the only reason the mixed-collection bug was
found rather than shipped, and getting it right took two attempts of its own. The
probe point must be **exact**, so a bounding-box centre is out: the box is
axis-aligned, so a rotation moves its centre somewhere else entirely and the check
would call a healthy move a failure. And it must be **live**: the first version
read the display mesh, which is cached, and after a move feature it handed back
the old centroid and reported a plate that had moved perfectly as *"DID NOT MOVE
by 1311 mm"*. A solid's centre of mass and a mesh body's own node list are both
real geometry, and both are recomputed.

## The transform is checked before anything moves

`Matrix3D.setWithArray` takes sixteen numbers and the documentation does not say
whether it reads them by row or by column; getting that wrong silently applies the
transpose, which is a plausible-looking rotation in the wrong direction. So the
matrix is built the unambiguous way — a transform `p → Rp + t` *is* the coordinate
system with origin `t` and axes the columns of `R` — and then verified: three
points are pushed through Fusion's own `Point3D.transformBy` and compared against
the arithmetic done here. A disagreement raises instead of moving the body.

## Install

1. Clone or copy this folder anywhere.
2. Run `powershell -File install.ps1`. It copies the add-in into
   `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\GlobalRegistration`.
3. In Fusion: **Utilities → ADD-INS → Scripts and Add-Ins → Add-Ins** →
   `Global Registration` → tick **Run on Startup** → **Run**.
4. The button appears in **Solid → Modify** and **Mesh → Modify**.

### The one external requirement

The registration runs in a Python with **numpy and scipy**, because Fusion's
bundled interpreter has neither:

```
pip install numpy scipy
```

Found automatically through `GLOBALREG_PYTHON`, a `python_path.txt` beside the
add-in, the usual install locations, then the `py` launcher.

## Repository layout

```
GlobalRegistration.py   add-in shell: lifecycle, toolbar, events, dev bridge
gr_core.py              reads the geometry, applies the transform; hot-reloads
gr_engine.py            the registration; runs outside Fusion under numpy/scipy
install.ps1             push into Fusion's AddIns folder
resources/              toolbar icons
tools/                  dev-bridge client, icon generator
tests/                  offline tests, no Fusion required
```

`gr_core.py` hot-reloads — re-imported on every command, so pushing it takes
effect on the next click; only `GlobalRegistration.py` needs a Stop/Run.
`gr_engine.py` is read fresh by every run because it is a separate process.

## Tests

```
py -3.10 tests/test_engine.py
```

23 checks, no Fusion needed. Every answer is exact by construction rather than
recorded from a previous run: centroids against the analytic values for a box and
a cone, Kabsch and point-to-plane against correspondences built from a known
rotation, and three end-to-end registrations where a known rigid transform is
applied and has to be undone — including a 180° flip, the case where a
principal-axis seed can pick the wrong sign.

```
py -3.10 tools/gr.py ping
py -3.10 tools/gr.py register '{"fixed": "Bone CT", "moving": "Bone scan", "along": ["Plate"]}'
```

runs it inside a live Fusion through the dev bridge. Off by default — create a
`devbridge.on` file next to the installed add-in.

`tools/gr.py verify_keep` checks the checkbox above end to end: a mesh and a
solid duplicated and transformed, then measured — the copies must land exactly and
the originals must be at **zero** displacement, because a duplicate that quietly
moved too would defeat the whole point.

`tools/gr.py verify_apply` is the regression test for the part that offline tests
cannot reach: it drives the real `apply_transform` over a mesh body, an ordinary
solid and a base-feature solid **at once**, with a transform that rotates as well
as translates, and checks all three landed exactly. Everything it builds is
scratch and is deleted afterwards.

## Licence

MIT — see [LICENSE](LICENSE).
