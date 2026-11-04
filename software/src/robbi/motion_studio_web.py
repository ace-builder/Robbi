"""Motion editor routes inherit the dashboard's authentication and origin checks."""
import re
from flask import Blueprint, abort, jsonify, request, send_file, send_from_directory

from .motion_studio import MotionStudio, compile_project
from .motion_studio import validate_project
from .motion_media import MAX_FILE, MAX_BUNDLE
from .errors import ValidationError


def blueprint(root, data_dir, runtime=None):
    bp = Blueprint('motion_studio', __name__)
    studio = MotionStudio(root, data_dir, runtime)

    @bp.get('/motion-studio')
    @bp.get('/motion-studio/')
    def editor():
        return send_from_directory(studio.root/'web/motion-studio', 'index.html')

    @bp.route('/api/motion-studio/library', methods=['GET', 'POST'])
    def library():
        if request.method == 'POST':
            data = request.get_json() or {}
            return jsonify(studio.save(data.get('project'), data.get('id'))), 201
        return jsonify(clips=studio.library())

    @bp.route('/api/motion-studio/library/<clip_id>', methods=['GET', 'DELETE'])
    def clip(clip_id):
        if request.method == 'DELETE':
            studio.delete(clip_id)
            return jsonify(ok=True)
        try:
            return jsonify(project=studio.get(clip_id))
        except FileNotFoundError:
            abort(404, 'Animation not found')

    @bp.post('/api/motion-studio/validate')
    def validate():
        plan = compile_project((request.get_json() or {}).get('project'))
        return jsonify(**{key: plan[key] for key in ('duration', 'distance', 'warnings')}, segments=len(plan['segments']))

    @bp.get('/api/motion-studio/status')
    def status():
        return jsonify(studio.status())

    @bp.route('/api/motion-studio/media', methods=['GET', 'POST'])
    def media():
        if request.method == 'GET':
            return jsonify(media=studio.media.catalog(refresh=request.args.get('refresh') == '1'), max_file_bytes=MAX_FILE)
        upload = request.files.get('file')
        if upload is None or not upload.filename:
            raise ValidationError('Choose a media file to upload')
        entry = studio.media.install(upload.filename, upload.read(MAX_FILE+1))
        return jsonify(media=entry), 201

    @bp.get('/api/motion-studio/media/<media_id>/file')
    def media_file(media_id):
        entry = studio.media.resolve(media_id)
        return send_file(entry['_path'], mimetype=entry['mime'], conditional=True, max_age=60)

    @bp.post('/api/motion-studio/bundle/export')
    def export_bundle():
        project = validate_project((request.get_json() or {}).get('project'))
        name = re.sub('[^a-zA-Z0-9_-]+', '-', project['name']).strip('-') or 'robbi-animation'
        return send_file(studio.media.export_bundle(project), mimetype='application/zip',
                         as_attachment=True, download_name=name+'.robbi.zip')

    @bp.post('/api/motion-studio/bundle/import')
    def import_bundle():
        upload = request.files.get('file')
        if upload is None:
            raise ValidationError('Choose an animation bundle to import')
        project = studio.media.import_bundle(upload.read(MAX_BUNDLE+1024*1024+1), validate_project)
        return jsonify(project=project, media=studio.media.catalog(refresh=True))

    @bp.post('/api/motion-studio/play')
    def play():
        try:
            return jsonify(studio.play((request.get_json() or {}).get('id'))), 202
        except FileNotFoundError:
            abort(404, 'Animation not found')

    @bp.post('/api/motion-studio/stop')
    def stop():
        studio.stop()
        studio.capture.stop()
        if runtime:
            runtime.emergency_stop()
        return jsonify(ok=True)

    @bp.route('/api/motion-studio/capture', methods=['GET', 'POST'])
    def capture():
        if request.method == 'POST':
            return jsonify(studio.capture.start(request.get_json() or {})), 201
        return jsonify(capabilities=studio.capture.capabilities(), current=studio.capture.status(), takes=studio.capture.library())

    @bp.post('/api/motion-studio/capture/poll')
    def capture_poll():
        return jsonify(studio.capture.poll((request.get_json() or {}).get('id')))

    @bp.post('/api/motion-studio/capture/stop')
    def capture_stop():
        data = request.get_json(silent=True) or {}
        if not data.get('id'):
            raise ValidationError('Recording identifier required')
        return jsonify(studio.capture.stop(data['id']))

    @bp.post('/api/motion-studio/capture/calibrate')
    def capture_calibrate():
        if studio.capture.active():
            raise ValidationError('Stop recording before calibrating')
        return jsonify(studio._controller().imu.calibrate())

    @bp.route('/api/motion-studio/capture/takes/<take_id>', methods=['GET', 'DELETE'])
    def capture_take(take_id):
        if request.method == 'DELETE':
            studio.capture.delete(take_id)
            return jsonify(ok=True)
        response = jsonify(studio.capture.get(take_id))
        response.headers['Content-Disposition'] = f'attachment; filename="{take_id}.robbi-take.json"'
        return response

    @bp.post('/api/motion-studio/capture/takes/<take_id>/convert')
    def capture_convert(take_id):
        return jsonify(studio.capture.convert(take_id, (request.get_json() or {}).get('tolerance', 1.5))), 201

    return bp
