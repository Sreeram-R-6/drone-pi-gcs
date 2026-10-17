"""Allowlisted ArduCopter 4.7 controls; units/ranges from firmware dbe79216 metadata."""
import math


def field(label, group, low=None, high=None, unit='', choices=None, help=''):
    return dict(label=label,group=group,min=low,max=high,unit=unit,choices=choices,help=help)

SCHEMA={
 'WP_SPD':field('Horizontal navigation speed','Speed',.1,20,'m/s',help='Navigation target, not a universal speed cap. Flight scripts can override it.'),
 'WP_SPD_UP':field('Navigation climb speed','Speed',.1,10,'m/s'),
 'WP_SPD_DN':field('Navigation descent speed','Speed',.1,10,'m/s'),
 'WP_ACC':field('Horizontal acceleration','Speed',.5,5,'m/s²'),
 'WP_ACC_Z':field('Vertical acceleration','Speed',.5,5,'m/s²'),
 'WP_JERK':field('Horizontal jerk','Speed',1,20,'m/s³'),
 'FENCE_TYPE':field('Configured fence types','Fence',0,15,choices={'bits':{'1':'Maximum altitude','2':'Circle around home','4':'Drawn boundaries','8':'Minimum altitude'}},help='Select the fence types to enforce when enabled.'),
 'FENCE_ALT_MAX':field('Maximum altitude','Vertical fence',.1,1000,'m',help='Firmware accepts positive heights below the recommended 10 m minimum. Ceiling minus margin is the navigation height limit; small fences need careful margins.'),
 'FENCE_ALT_MAX_TP':field('Ceiling altitude reference','Vertical fence',choices={'0':'Above sea level','1':'Above home','2':'Above EKF origin','3':'Above terrain'}),
 'FENCE_ALT_MIN':field('Minimum altitude','Vertical fence',-100,100,'m',help='Floor activates after reaching its height; landing modes can disable it. This is not rangefinder takeoff height.'),
 'FENCE_ALT_MIN_TP':field('Floor altitude reference','Vertical fence',choices={'0':'Above sea level','1':'Above home','2':'Above EKF origin','3':'Above terrain'}),
 'FENCE_RADIUS':field('Home circle radius','Fence',30,10000,'m',help='Used when Circle around home is selected.'),
 'FENCE_MARGIN':field('Fence margin','Fence',1,10,'m'),
 'FENCE_MARGIN_XY':field('Horizontal margin override','Fence',0,50,'m',help='0 uses Fence margin.'),
 'FENCE_ACTION':field('Action on breach','Fence',choices={'0':'Report only (no recovery)','1':'RTL, otherwise Land','2':'Always Land','3':'SmartRTL, otherwise RTL or Land','4':'Brake, otherwise Land','5':'SmartRTL, otherwise Land'}),
 'FENCE_AUTOENABLE':field('Automatic fence activation','Fence',choices={'0':'Off (use explicit enable)','1':'After automatic takeoff; disable on landing','2':'After automatic takeoff; disable floor on landing','3':'When armed'},help='Can activate fences later even when enforcement is currently disabled.'),
 'FENCE_OPTIONS':field('Fence options','Fence',0,7,choices={'bits':{'1':'Prevent mode change during breach recovery','2':'Union of inclusion areas','4':'Notify near margin'}},help='Union permits any inclusion area; otherwise all inclusion areas must contain the aircraft.'),
 'FENCE_NTF_FREQ':field('Margin notification frequency','Fence',0,10,'Hz',help='0 reports only new margin breaches; requires Notify near margin.'),
}


def validate_changes(changes,expected):
    if not isinstance(changes,dict) or not changes or len(changes)>len(SCHEMA):raise ValueError('Choose at least one supported setting')
    if not isinstance(expected,dict) or set(expected)!=set(changes):raise ValueError('Original controller values are required for each change')
    for name,value in changes.items():
        if name not in SCHEMA:raise ValueError('Unsupported setting: '+name)
        spec=SCHEMA[name]
        for v in (value,expected[name]):
            if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v):raise ValueError(name+' must be a finite number')
        choices=spec['choices']
        if choices and (int(value)!=value or ('bits' not in choices and str(int(value)) not in choices)):raise ValueError('Invalid choice for '+name)
        if spec['min'] is not None and not spec['min']<=value<=spec['max']:raise ValueError(f'{name} must be {spec["min"]}–{spec["max"]} {spec["unit"]}')
    return dict(changes)


def validate_combined(values):
    types=int(values['FENCE_TYPE']);margin=values['FENCE_MARGIN']
    if not types:raise ValueError('Select at least one fence type')
    if types&9==9:
        if values['FENCE_ALT_MAX_TP']!=values['FENCE_ALT_MIN_TP']:raise ValueError('Use the same altitude reference for floor and ceiling')
        if values['FENCE_ALT_MAX']-values['FENCE_ALT_MIN']<=2*margin:raise ValueError('Floor and ceiling must leave space for both fence margins')
    if types&2 and values['FENCE_RADIUS']<=max(margin,values.get('FENCE_MARGIN_XY',0)):raise ValueError('Home radius must exceed the horizontal margin')
