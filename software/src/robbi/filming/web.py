from flask import Blueprint, jsonify, request, send_file, Response
import json


def blueprint(studio, root):
    bp=Blueprint('filming',__name__,url_prefix='/api/filming')
    @bp.get('/status')
    def status():return jsonify(studio.status())
    @bp.post('/mode')
    def mode():return jsonify(studio.set_mode((request.get_json() or {}).get('enabled')))
    @bp.get('/shots')
    def shots():return jsonify(shots=studio.library.listing(request.args.get('q','')))
    @bp.post('/import')
    def import_shots():
        d=request.get_json() or {}
        return jsonify(studio.import_jsonl(d.get('jsonl'),d.get('replace',False),d.get('generate',True))),201
    @bp.get('/export')
    def export():
        ids=[request.args['id']] if request.args.get('id') else [s['id'] for s in studio.library.listing()]
        content='\n'.join(json.dumps(studio.library.get(i)['definition'],ensure_ascii=False,separators=(',',':')) for i in ids)+'\n'
        return Response(content,mimetype='application/x-ndjson',headers={'Content-Disposition':'attachment; filename=robbi-shots.jsonl'})
    @bp.route('/shots/<identifier>',methods=['GET','PUT','DELETE'])
    def shot(identifier):
        if request.method=='PUT':return jsonify(studio.update(identifier,request.get_json()))
        if request.method=='DELETE':studio.delete(identifier);return jsonify(ok=True)
        return jsonify(studio.library.get(identifier))
    @bp.post('/shots/<identifier>/prepare')
    def prepare(identifier):
        d=request.get_json() or {}
        return jsonify(studio.prepare([identifier],d.get('cue_id'),d.get('regenerate',False))),202
    @bp.post('/prepare/stop')
    def stop_preparing():studio.preparation.stop_batch();return jsonify(studio.status())
    @bp.post('/shots/<identifier>/play')
    def play(identifier):return jsonify(studio.play(identifier,(request.get_json() or {}).get('delay_ms',5000))),202
    @bp.post('/stop')
    def stop():studio.stop();return jsonify(studio.status())
    @bp.route('/shots/<identifier>/audio/<variant>',methods=['POST','DELETE'])
    def choice(identifier,variant):return jsonify(studio.audio_choice(identifier,variant,request.method=='DELETE'))
    @bp.get('/audio/<variant>')
    def audio(variant):
        handle,size=studio.library.audio_file(variant)
        try:
            response=send_file(handle,mimetype='audio/wav',download_name='robbi-line.wav',conditional=False)
            response.content_length=size
            response.make_conditional(request,accept_ranges=True,complete_length=size)
            response.call_on_close(handle.close)
            return response
        except Exception:handle.close();raise
    @bp.delete('/shots/<identifier>/videos/<video>')
    def delete_video(identifier,video):return jsonify(studio.delete_video(identifier,video))
    @bp.get('/video/<video>')
    def video_file(video):
        handle,size=studio.library.videos.file(video)
        try:
            response=send_file(handle,mimetype='video/mp4',download_name='robbi-take-'+video[:8]+'.mp4',
                               as_attachment=request.args.get('download')=='1',conditional=False)
            response.content_length=size
            response.make_conditional(request,accept_ranges=True,complete_length=size)
            response.call_on_close(handle.close)
            return response
        except Exception:handle.close();raise
    @bp.get('/examples')
    def examples():return Response((root/'examples/filming-shots.jsonl').read_text(encoding='utf-8'),mimetype='application/x-ndjson')
    @bp.get('/spec')
    def spec():return Response((root/'docs/filming-jsonl.txt').read_text(encoding='utf-8'),mimetype='text/plain')
    return bp
