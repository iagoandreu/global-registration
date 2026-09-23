"""Global Registration - all the working code. RE-IMPORTED ON EVERY COMMAND EVENT.

Because of that reload, nothing may keep state in a module global between events.

WHAT THIS DOES
    Aligns a moving entity onto a fixed one by shape alone - no landmarks, no
    common coordinate system - and carries any number of "move along" entities
    with it on the same transform, so a plate stays where it was put relative to
    the bone it was designed on.

    Mesh bodies and solids are interchangeable throughout. Both are read as
    triangles: a mesh body has them already, a solid is tessellated. Fixed and
    moving may be one of each.

WHERE THE WORK HAPPENS
    Reading the geometry and applying the transform happen here. The registration
    itself is numpy and scipy, which Fusion's Python does not have, so it runs in
    py -3.10 through gr_engine.py - the same split Mesh to Part uses.

THE TRANSFORM IS APPLIED ONCE, TO EVERYTHING, AS ONE FEATURE
    Moving the bodies in separate move features would put separate entries in the
    timeline, and rolling back one of them would take the assembly apart. One
    ObjectCollection, one MoveFeature, one thing to undo.
"""

import json
import math
import os
import subprocess
import traceback

import adsk.core
import adsk.fusion

ADDIN_DIR = os.path.dirname(os.path.realpath(__file__))
RES_DIR = os.path.join(ADDIN_DIR, 'resources', 'GlobalRegistration')
LOG_PATH = os.path.join(ADDIN_DIR, 'GlobalRegistration.log')
SETTINGS = os.path.join(ADDIN_DIR, 'last_settings.json')
ENGINE = os.path.join(ADDIN_DIR, 'gr_engine.py')
WORK = os.path.join(ADDIN_DIR, 'devjobs')

CMD_ID = 'GlobalRegistrationRun'
CMD_NAME = 'Global Registration'

CM = 10.0          # internal cm -> mm
MM = 0.1           # mm -> internal cm
NO_WINDOW = 0x08000000

VERSION = '1.0.0'

GC_MODES = [
    ('auto', 'Volume if closed, else surface'),
    ('volume', 'Volume centroid'),
    ('surface', 'Surface (area-weighted) centroid'),
]

METRICS = [
    ('plane', 'Point to plane (faster, default)'),
    ('point', 'Point to point'),
]

DEFAULTS = {
    'samplePoints': 40000,
    'fixedPoints': 120000,
    'iterations': 60,
    'overlap': 100.0,          # %
    'maxDistance': 0.0,        # mm, 0 = no limit
    'tolerance': 0.0001,       # mm
    'gcMode': 0,
    'metric': 0,
    'principalAxes': True,
    'preview': False,
}


def log(msg):
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as fh:
            fh.write('%s\n' % msg)
    except Exception:
        pass


def short_error(tb):
    for line in reversed(tb.strip().splitlines()):
        line = line.strip()
        if line and not line.startswith(('File "', 'Traceback', '^', '~')):
            return line
    return tb.strip().splitlines()[-1]


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS, encoding='utf-8') as fh:
            s.update(json.load(fh))
    except Exception:
        pass
    return s


def save_settings(s):
    try:
        with open(SETTINGS, 'w', encoding='utf-8') as fh:
            json.dump(s, fh, indent=2)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# finding a Python that has numpy and scipy
# --------------------------------------------------------------------------- #

_PYTHON = None


def find_python():
    override = os.environ.get('GLOBALREG_PYTHON') or os.environ.get('MESHTOPART_PYTHON')
    cands = []
    if override:
        cands.append([override])
    txt = os.path.join(ADDIN_DIR, 'python_path.txt')
    if os.path.exists(txt):
        try:
            with open(txt, encoding='utf-8') as fh:
                line = fh.read().strip()
            if line:
                cands.append([line])
        except Exception:
            pass
    local = os.environ.get('LOCALAPPDATA', '')
    for v in ('313', '312', '311', '310', '39'):
        cands.append([os.path.join(local, 'Programs', 'Python', 'Python%s' % v,
                                   'python.exe')])
    cands.append(['py', '-3.10'])
    cands.append(['python'])
    for c in cands:
        try:
            r = subprocess.run(c + ['-c', 'import numpy, scipy'],
                               capture_output=True, timeout=60,
                               creationflags=NO_WINDOW)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return None


def python_cmd():
    global _PYTHON
    if _PYTHON is None:
        _PYTHON = find_python() or []
    return list(_PYTHON)


# --------------------------------------------------------------------------- #
# reading an entity as triangles - mesh or solid, it does not matter
# --------------------------------------------------------------------------- #

def body_mesh(body):
    """A TriangleMesh for a solid, or the PolygonMesh of a mesh body.

    A solid's display mesh only exists once it has been DISPLAYED; on a hidden
    body, or one built a moment ago, bestMesh raises
    "InternalValidationError : count > 0". Hence the calculator fallback, with
    tolerances tied to the body's own size - a tight tolerance with no side or
    aspect limit has taken this Fusion down before.
    """
    mesh_body = adsk.fusion.MeshBody.cast(body)
    if mesh_body:
        pm = mesh_body.mesh
        if pm and pm.triangleCount > 0 and pm.quadCount == 0 and pm.polygonCount == 0:
            return list(pm.nodeCoordinatesAsDouble), list(pm.triangleNodeIndices)
        dm = mesh_body.displayMesh
        return list(dm.nodeCoordinatesAsDouble), list(dm.nodeIndices)

    mm = body.meshManager
    tm = None
    try:
        meshes = mm.displayMeshes
        if meshes.count > 0:
            tm = meshes.bestMesh
    except Exception:
        tm = None
    if tm is None:
        bb = body.boundingBox
        diag = bb.minPoint.distanceTo(bb.maxPoint) or 1.0
        calc = mm.createMeshCalculator()
        calc.surfaceTolerance = max(diag / 2000.0, 1.0e-4)
        calc.maxSideLength = diag / 20.0
        calc.maxAspectRatio = 10.0
        tm = calc.calculate()
    return list(tm.nodeCoordinatesAsDouble), list(tm.nodeIndices)


def entity_label(e):
    try:
        return e.name
    except Exception:
        return e.objectType


def selected(sel_input):
    return [sel_input.selection(i).entity for i in range(sel_input.selectionCount)]


# --------------------------------------------------------------------------- #
# running the engine
# --------------------------------------------------------------------------- #

def run_engine(fixed, moving, params, timeout=1800):
    os.makedirs(WORK, exist_ok=True)
    jp = os.path.join(WORK, 'reg_job.json')
    op = os.path.join(WORK, 'reg_out.json')
    job = dict(params)
    job['fixed'] = {'coords': fixed[0], 'idx': fixed[1]}
    job['moving'] = {'coords': moving[0], 'idx': moving[1]}
    with open(jp, 'w', encoding='utf-8') as fh:
        json.dump(job, fh)
    if os.path.exists(op):
        os.remove(op)
    exe = python_cmd()
    if not exe:
        raise RuntimeError(
            'No Python with numpy and scipy was found. Set the GLOBALREG_PYTHON '
            'environment variable, or put the full path to the interpreter in '
            'python_path.txt next to the add-in.')
    r = subprocess.run(exe + [ENGINE, jp, op], capture_output=True, text=True,
                       timeout=timeout, creationflags=NO_WINDOW)
    if not os.path.exists(op):
        raise RuntimeError('the registration engine produced nothing (rc=%s)\n%s\n%s'
                           % (r.returncode, r.stdout[-800:], r.stderr[-800:]))
    with open(op, encoding='utf-8') as fh:
        res = json.load(fh)
    if not res.get('ok'):
        raise RuntimeError('the registration engine failed:\n%s'
                           % res.get('error', '')[-1200:])
    return res


def matrix_from(rows):
    """A 4x4 nested list -> Matrix3D, and CHECKED against Fusion's own arithmetic.

    setWithArray takes sixteen numbers and the documentation does not say whether
    it reads them by row or by column. Getting that wrong silently applies the
    transpose - a plausible-looking rotation in the wrong direction - so the
    matrix is built the unambiguous way instead: a transform p -> R p + t IS the
    coordinate system whose origin is t and whose axes are the columns of R.

    Then it is verified. Three points are pushed through Fusion's own
    Point3D.transformBy and compared against R p + t computed here; a disagreement
    raises rather than moves the body. A registration that lands the bone
    somewhere wrong is worse than one that refuses.
    """
    R = [[float(rows[i][j]) for j in range(3)] for i in range(3)]
    t = [float(rows[i][3]) for i in range(3)]
    m = adsk.core.Matrix3D.create()
    ok = m.setWithCoordinateSystem(
        adsk.core.Point3D.create(t[0], t[1], t[2]),
        adsk.core.Vector3D.create(R[0][0], R[1][0], R[2][0]),
        adsk.core.Vector3D.create(R[0][1], R[1][1], R[2][1]),
        adsk.core.Vector3D.create(R[0][2], R[1][2], R[2][2]))
    if not ok:
        raise RuntimeError('Fusion refused the transform matrix; the rotation may '
                           'not be orthogonal')

    for probe in ((1.0, 0.0, 0.0), (0.0, 2.5, 0.0), (-3.0, 1.0, 7.0)):
        p = adsk.core.Point3D.create(*probe)
        p.transformBy(m)
        want = [sum(R[i][k] * probe[k] for k in range(3)) + t[i] for i in range(3)]
        got = (p.x, p.y, p.z)
        if max(abs(a - b) for a, b in zip(want, got)) > 1.0e-9:
            raise RuntimeError(
                'the transform Fusion built does not match the one computed: '
                '%s became %s, expected %s. Refusing to move anything.'
                % (probe, [round(v, 6) for v in got],
                   [round(v, 6) for v in want]))
    return m


# --------------------------------------------------------------------------- #
# applying it
# --------------------------------------------------------------------------- #

def apply_transform(design, entities, matrix):
    """Move every entity by one matrix, as ONE feature where the API allows it.

    Occurrences are not moved by a move feature - their placement IS their
    transform - so they are composed directly and kept out of the collection.
    """
    comp = design.rootComponent
    occs = []
    movable = adsk.core.ObjectCollection.create()
    for e in entities:
        occ = adsk.fusion.Occurrence.cast(e)
        if occ:
            occs.append(occ)
            continue
        movable.add(e)

    made = []
    if movable.count:
        mi = comp.features.moveFeatures.createInput2(movable)
        if not mi.defineAsFreeMove(matrix):
            raise RuntimeError('Fusion refused defineAsFreeMove for those bodies')
        feat = comp.features.moveFeatures.add(mi)
        made.append('move feature over %d entit%s'
                    % (movable.count, 'y' if movable.count == 1 else 'ies'))
        if feat is None:
            made[-1] += ' (direct design: no timeline entry)'

    for occ in occs:
        t = occ.transform2
        t.transformBy(matrix)
        occ.transform2 = t
        made.append('occurrence "%s" transform composed' % occ.name)
    return made


# --------------------------------------------------------------------------- #
# the registration, end to end
# --------------------------------------------------------------------------- #

def params_from(o):
    return {
        'sample_points': int(o['sample_points']),
        'fixed_points': int(o['fixed_points']),
        'iterations': int(o['iterations']),
        'overlap': max(0.05, min(1.0, float(o['overlap']) / 100.0)),
        'max_distance_mm': float(o['max_distance_mm']),
        'tolerance_mm': float(o['tolerance_mm']),
        'gc': o['gc'],
        'metric': o['metric'],
        'principal_axes': bool(o['principal_axes']),
    }


def register(app, fixed, moving, along, o, step=None):
    des = adsk.fusion.Design.cast(app.activeProduct)

    def say(m):
        log(m)
        if step:
            step(m)

    say('Reading %s' % entity_label(fixed))
    fx = body_mesh(fixed)
    say('Reading %s' % entity_label(moving))
    mv = body_mesh(moving)
    say('Registering (%d -> %d triangles)'
        % (len(mv[1]) // 3, len(fx[1]) // 3))
    res = run_engine(fx, mv, params_from(o))

    if o.get('apply', True):
        say('Applying the transform')
        matrix = matrix_from(res['matrix'])
        moved = apply_transform(des, [moving] + list(along), matrix)
        res['applied'] = moved
    else:
        res['applied'] = []
    res['fixed'] = entity_label(fixed)
    res['moving'] = entity_label(moving)
    res['along'] = [entity_label(a) for a in along]
    return res


def format_report(res):
    L = []
    L.append('%s  ->  %s' % (res['moving'], res['fixed']))
    L.append('')
    L.append('CENTRE OF GRAVITY')
    L.append('  fixed   %s   %.3f, %.3f, %.3f mm'
             % (res['gc_kind']['fixed'], *res['gc_fixed_mm']))
    L.append('  moving  %s   %.3f, %.3f, %.3f mm'
             % (res['gc_kind']['moving'], *res['gc_moving_mm']))
    L.append('')
    L.append('REGISTRATION')
    L.append('  seed            %s' % res['seed'])
    L.append('  iterations      %d, %d correspondences'
             % (res['iterations'], res['pairs']))
    L.append('  points          %d moving against %d fixed'
             % (res['moving_points'], res['fixed_points']))
    L.append('  rotation        %.4f deg' % res['rotation_deg'])
    L.append('  translation     %.3f, %.3f, %.3f mm' % tuple(res['translation_mm']))
    L.append('')
    b, a = res['before'], res['after']
    L.append('DEVIATION, moving surface to fixed surface')
    L.append('  before   rms %9.4f   p95 %9.4f   max %9.4f mm'
             % (b['rms_mm'], b['p95_mm'], b['max_mm']))
    L.append('  after    rms %9.4f   p95 %9.4f   max %9.4f mm'
             % (a['rms_mm'], a['p95_mm'], a['max_mm']))
    if 'plane_rms_mm' in a:
        L.append('  after, measured to the tangent plane rather than to the '
                 'nearest sample point:')
        L.append('           rms %9.4f   p95 %9.4f   max %9.4f mm'
                 % (a['plane_rms_mm'], a['plane_p95_mm'], a['plane_max_mm']))
    L.append('  point-to-point cannot read below %.4f mm here: that is half the '
             'spacing' % res['sampling_floor_mm'])
    L.append('  of the fixed point cloud, and a perfect alignment still reports it.')
    L.append('')
    if res.get('applied'):
        L.append('APPLIED')
        for m in res['applied']:
            L.append('  %s' % m)
        if res.get('along'):
            L.append('  moved along: %s' % ', '.join(res['along']))
    else:
        L.append('NOT APPLIED - the transform was computed only.')
    L.append('')
    L.append('engine %.1f s   GlobalRegistration %s' % (res['secs'], VERSION))
    return '\n'.join(L)


# --------------------------------------------------------------------------- #
# dialog
# --------------------------------------------------------------------------- #

def _esc(t):
    return str(t).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _rows(pairs):
    out = ['<table cellpadding="0" cellspacing="0">']
    for k, v in pairs:
        out.append('<tr><td width="150"><b>%s</b></td><td>%s</td></tr>'
                   % (_esc(k), v if k == '' else _esc(v)))
    out.append('</table>')
    return ''.join(out)


def on_created(app, cmd):
    s = load_settings()
    cmd.okButtonText = 'Register'
    cmd.isRepeatable = False
    inputs = cmd.commandInputs

    for cid, name, prompt in (
            ('selFixed', 'Fixed entity', 'The body to stay where it is'),
            ('selMoving', 'Moving entity', 'The body to be aligned onto it')):
        si = inputs.addSelectionInput(cid, name, prompt)
        si.addSelectionFilter('SolidBodies')
        si.addSelectionFilter('MeshBodies')
        si.addSelectionFilter('SurfaceBodies')
        si.setSelectionLimits(0, 1)

    sa = inputs.addSelectionInput('selAlong', 'Move along',
                                  'Anything that must travel with the moving '
                                  'entity - plates, guides, components')
    for f in ('SolidBodies', 'MeshBodies', 'SurfaceBodies', 'Occurrences'):
        sa.addSelectionFilter(f)
    sa.setSelectionLimits(0, 0)

    g = inputs.addGroupCommandInput('grpInit', 'Initial alignment').children
    gc = g.addDropDownCommandInput('gcMode', 'Centre of gravity',
                                   adsk.core.DropDownStyles.TextListDropDownStyle)
    for i, (_k, lab) in enumerate(GC_MODES):
        gc.listItems.add(lab, i == int(s['gcMode']))
    g.addBoolValueInput('principalAxes', 'Also try the principal axes', True, '',
                        bool(s['principalAxes']))

    g2 = inputs.addGroupCommandInput('grpIcp', 'Registration').children
    mt = g2.addDropDownCommandInput('metric', 'Metric',
                                    adsk.core.DropDownStyles.TextListDropDownStyle)
    for i, (_k, lab) in enumerate(METRICS):
        mt.listItems.add(lab, i == int(s['metric']))
    g2.addIntegerSpinnerCommandInput('iterations', 'Max iterations', 1, 500, 5,
                                     int(s['iterations']))
    g2.addIntegerSpinnerCommandInput('samplePoints', 'Sample points (moving)',
                                     500, 500000, 5000, int(s['samplePoints']))
    g2.addIntegerSpinnerCommandInput('fixedPoints', 'Sample points (fixed)',
                                     1000, 2000000, 10000, int(s['fixedPoints']))
    g2.addFloatSpinnerCommandInput('overlap', 'Overlap used', '', 5.0, 100.0, 5.0,
                                   float(s['overlap']))
    g2.addValueInput('maxDistance', 'Reject pairs beyond (0 = no limit)', 'mm',
                     adsk.core.ValueInput.createByReal(
                         float(s['maxDistance']) * MM))
    g2.addValueInput('tolerance', 'Converged when rms changes less than', 'mm',
                     adsk.core.ValueInput.createByReal(
                         float(s['tolerance']) * MM))

    inputs.addBoolValueInput('applyIt', 'Apply the transform', True, '', True)

    info = inputs.addTextBoxCommandInput('info', '', '', 10, True)
    info.isFullWidth = True
    refresh(inputs)


def refresh(inputs):
    fx = selected(inputs.itemById('selFixed'))
    mv = selected(inputs.itemById('selMoving'))
    al = selected(inputs.itemById('selAlong'))
    pairs = []
    if not fx or not mv:
        pairs.append(('', 'Select the fixed entity and the moving entity. '
                          'Meshes and solids may be mixed.'))
    else:
        if fx[0] == mv[0]:
            pairs.append(('', 'The fixed and moving entities are the same body.'))
        pairs.append(('fixed', entity_label(fx[0])))
        pairs.append(('moving', entity_label(mv[0])))
        if al:
            pairs.append(('move along', ', '.join(entity_label(a) for a in al)))
        pairs.append(('', '&nbsp;'))
        pairs.append(('', 'The centre of gravity is matched first, then the '
                          'principal axes are tried in all four orientations, '
                          'and the best of them is refined by ICP. The report '
                          'gives the deviation before and after.'))
    inputs.itemById('info').formattedText = _rows(pairs)


def on_input_changed(app, inputs, changed_id):
    refresh(inputs)


def on_validate(inputs):
    fx = selected(inputs.itemById('selFixed'))
    mv = selected(inputs.itemById('selMoving'))
    return bool(fx and mv and fx[0] != mv[0])


def options_from(inputs):
    return {
        'sample_points': inputs.itemById('samplePoints').value,
        'fixed_points': inputs.itemById('fixedPoints').value,
        'iterations': inputs.itemById('iterations').value,
        'overlap': inputs.itemById('overlap').value,
        'max_distance_mm': inputs.itemById('maxDistance').value * CM,
        'tolerance_mm': inputs.itemById('tolerance').value * CM,
        'gc': GC_MODES[inputs.itemById('gcMode').selectedItem.index][0],
        'metric': METRICS[inputs.itemById('metric').selectedItem.index][0],
        'principal_axes': inputs.itemById('principalAxes').value,
        'apply': inputs.itemById('applyIt').value,
    }


def settings_from(inputs):
    return {
        'samplePoints': inputs.itemById('samplePoints').value,
        'fixedPoints': inputs.itemById('fixedPoints').value,
        'iterations': inputs.itemById('iterations').value,
        'overlap': inputs.itemById('overlap').value,
        'maxDistance': inputs.itemById('maxDistance').value * CM,
        'tolerance': inputs.itemById('tolerance').value * CM,
        'gcMode': inputs.itemById('gcMode').selectedItem.index,
        'metric': inputs.itemById('metric').selectedItem.index,
        'principalAxes': inputs.itemById('principalAxes').value,
    }


def on_execute(app, ui, inputs):
    o = options_from(inputs)
    save_settings(settings_from(inputs))
    fixed = selected(inputs.itemById('selFixed'))[0]
    moving = selected(inputs.itemById('selMoving'))[0]
    along = selected(inputs.itemById('selAlong'))
    prog = ui.createProgressDialog()
    prog.isCancelButtonShown = False
    prog.show('Global Registration', 'Starting', 0, 4)
    n = [0]

    def step(msg):
        n[0] = min(n[0] + 1, 4)
        prog.progressValue = n[0]
        prog.message = msg
        adsk.doEvents()

    try:
        res = register(app, fixed, moving, along, o, step)
    finally:
        prog.hide()
    text = format_report(res)
    log(text)
    ui.messageBox(text, 'Global Registration')
    return res


def on_destroy(app):
    pass


# --------------------------------------------------------------------------- #
# toolbar
# --------------------------------------------------------------------------- #

TOOLTIP = ('Align one body onto another by shape alone.\n\n'
           'Matches the centre of gravity, tries the principal axes in all four '
           'orientations, then refines with ICP. Any number of "move along" '
           'entities travel with the moving body on the same transform. Meshes '
           'and solids are interchangeable. Reports the deviation before and '
           'after.')

PANELS = ['SolidModifyPanel', 'ParaMeshModifyPanel']


def install(app, ui, created_handler_factory):
    old = ui.commandDefinitions.itemById(CMD_ID)
    if old:
        old.deleteMe()
    folder = RES_DIR if os.path.isdir(RES_DIR) else ''
    cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, TOOLTIP,
                                                        folder)
    cmd_def.commandCreated.add(created_handler_factory())
    placed = []
    for pid in PANELS:
        panel = ui.allToolbarPanels.itemById(pid)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)
            placed.append(pid)
    return placed


def uninstall(ui):
    for pid in PANELS:
        panel = ui.allToolbarPanels.itemById(pid)
        if panel:
            c = panel.controls.itemById(CMD_ID)
            if c:
                c.deleteMe()
    d = ui.commandDefinitions.itemById(CMD_ID)
    if d:
        d.deleteMe()


# --------------------------------------------------------------------------- #
# dev bridge
# --------------------------------------------------------------------------- #

def all_bodies(des):
    out = []
    for occ in des.rootComponent.allOccurrences:
        for b in occ.component.bRepBodies:
            out.append(b.createForAssemblyContext(occ) if b.assemblyContext is None
                       else b)
        for b in occ.component.meshBodies:
            out.append(b)
    for b in des.rootComponent.bRepBodies:
        out.append(b)
    for b in des.rootComponent.meshBodies:
        out.append(b)
    return out


def find_body(des, name):
    for b in all_bodies(des):
        if b.name == name:
            return b
    raise RuntimeError('no body named %r' % name)


def dev_ping(app, job):
    des = adsk.fusion.Design.cast(app.activeProduct)
    return {'version': VERSION, 'document': app.activeDocument.name,
            'python': python_cmd(),
            'bodies': [{'name': b.name,
                        'kind': 'mesh' if adsk.fusion.MeshBody.cast(b) else 'solid'}
                       for b in all_bodies(des)]}


def dev_register(app, job):
    des = adsk.fusion.Design.cast(app.activeProduct)
    fixed = find_body(des, job['fixed'])
    moving = find_body(des, job['moving'])
    along = [find_body(des, n) for n in job.get('along', [])]
    o = {'sample_points': job.get('sample_points', 40000),
         'fixed_points': job.get('fixed_points', 120000),
         'iterations': job.get('iterations', 60),
         'overlap': job.get('overlap', 100.0),
         'max_distance_mm': job.get('max_distance_mm', 0.0),
         'tolerance_mm': job.get('tolerance_mm', 0.0001),
         'gc': job.get('gc', 'auto'),
         'metric': job.get('metric', 'plane'),
         'principal_axes': job.get('principal_axes', True),
         'apply': job.get('apply', True)}
    res = register(app, fixed, moving, along, o)
    res['text'] = format_report(res)
    return res


DEV_COMMANDS = {
    'ping': dev_ping,
    'register': dev_register,
}
