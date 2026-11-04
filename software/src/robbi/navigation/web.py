"""Short HTTP admission only. All map computation and disk work is deferred."""
from flask import Blueprint, jsonify, request

from ..errors import UnavailableError, ValidationError
from .model import Halt, finite
from .service import NavigationService


def point(value, *, heading=False):
    if not isinstance(value,dict) or set(value)-({'x_cm','y_cm','theta_deg'} if heading else {'x_cm','y_cm'}):
        raise ValidationError('Supply map coordinates in centimeters')
    result = {k:finite(value.get(k),k,-3000,3000) for k in ('x_cm','y_cm')}
    if heading and value.get('theta_deg') is not None:
        result['theta_deg'] = finite(value['theta_deg'],'heading',-180,180)
    return result


def blueprint(runtime):
    bp = Blueprint('navigation',__name__,url_prefix='/api/navigation')
    def service():
        value = runtime.controller().slam
        if not isinstance(value,NavigationService):
            raise UnavailableError('Navigation could not initialize. Check Overview diagnostics.')
        return value

    @bp.errorhandler(Halt)
    def halted(exc): return jsonify(ok=False,error=str(exc)),409

    @bp.get('')
    def status():
        result = service().status()
        result['filming_mode'] = bool(getattr(runtime.controller(),'_filming_mode',False))
        return jsonify(result)

    @bp.get('/map')
    def snapshot():
        since = request.args.get('since',type=int)
        return jsonify(service().map_data(since))

    @bp.get('/maps')
    def maps(): return jsonify(service().sessions())

    @bp.post('/<operation>')
    def command(operation):
        data = request.get_json()
        if not isinstance(data,dict): raise ValidationError('Request must be an object')
        allowed = {'stop':set(),'new':set(),'scan':{'hint'},'relocalize':{'hint'},'explore':{'max_steps','max_distance_cm','timeout_s'},
                   'plan':{'goal'},'navigate':{'preview_id','goal'},'save':{'name'},'load':{'id'},'delete':{'id'},'settings':{'settings'}}
        if operation not in allowed or set(data)-allowed[operation]:
            raise ValidationError('Unknown navigation operation or parameter')
        s = service()
        if operation=='stop': result=s.stop()
        elif operation=='new': result=s.reset()
        elif operation=='scan': result=s.start_scan(point(data['hint'],heading=True) if data.get('hint') is not None else None)
        elif operation=='relocalize': result=s.start_scan(point(data['hint'],heading=True) if data.get('hint') is not None else None,relocalize=True)
        elif operation=='explore': result=s.start_explore(**data)
        elif operation=='plan':
            goal=point(data.get('goal')); result=s.plan((goal['x_cm'],goal['y_cm']))
        elif operation=='navigate':
            if 'goal' in data:
                if 'preview_id' in data:raise ValidationError('Supply a destination or a preview ID, not both')
                goal=point(data['goal']);result=s.navigate(goal=(goal['x_cm'],goal['y_cm']))
            else:
                preview_id = data.get('preview_id')
                if not isinstance(preview_id,str) or len(preview_id)!=32:
                    raise ValidationError('Select a destination before driving.')
                result=s.navigate(preview_id)
        elif operation=='save': result=s.save(data.get('name'))
        elif operation=='load': result=s.load(data.get('id'))
        elif operation=='delete': result=s.delete(data.get('id'))
        else: result=s.configure(data.get('settings'))
        return jsonify(result),200 if operation=='stop' else 202
    return bp
