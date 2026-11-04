"""Normal V2 dashboard routes; existing owner authentication and CSRF apply."""
from flask import Blueprint,jsonify,request
from robbi.app_runtime.owner import require_owner
from robbi.errors import ValidationError
from dataclasses import fields
from .settings import ContinuousSettings


def blueprint(runtime):
    bp=Blueprint('conversation_lab',__name__)
    def service():
        require_owner()
        return runtime.conversation_lab
    def body(allowed):
        value=request.get_json(silent=True)
        if not isinstance(value,dict) or set(value)-set(allowed):raise ValidationError('Invalid conversation request')
        return value
    @bp.get('/api/conversation/status')
    def status():
        svc=service()
        cursor=request.args.get('since','0')
        revision=request.args.get('jobs_revision')
        if not cursor.isascii() or not cursor.isdecimal() or len(cursor)>16 or (revision and len(revision)>1000):
            raise ValidationError('Invalid conversation cursor')
        return jsonify(svc.status(since=int(cursor),jobs_revision=revision))
    @bp.get('/api/conversation/settings')
    def settings():return jsonify(service().settings())
    @bp.post('/api/conversation/settings')
    def configure():return jsonify(service().configure(body(f.name for f in fields(ContinuousSettings))))
    @bp.post('/api/conversation/start')
    def start():return jsonify(service().start(**body(('microphone',))))
    @bp.post('/api/conversation/stop')
    def stop():return jsonify(service().stop())
    @bp.post('/api/conversation/mood/reset')
    def reset_mood():
        svc=service();body(())
        return jsonify(svc.mood(reset=True))
    @bp.post('/api/conversation/arm')
    def arm():return jsonify(service().arm(body(('enabled',)).get('enabled')))
    @bp.post('/api/conversation/text')
    def text():return jsonify(service().text(body(('text',)).get('text'))),202
    return bp
