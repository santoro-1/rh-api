"""Read-only production task audit; execute over SSH stdin, no app imports."""
import hashlib
import json
import sqlite3
from pathlib import Path

ROOT = Path('/opt/runninghub-video')
db = sqlite3.connect('file:/opt/runninghub-video/data/app.db?mode=ro', uri=True)
db.row_factory = sqlite3.Row
db.execute('PRAGMA query_only=ON')

def rows(sql, params=()):
    return [dict(r) for r in db.execute(sql, params)]

def parsed(value):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return value

def redact(value):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if any(s in k.lower() for s in ('api_key', 'apikey', 'authorization', 'password', 'secret', 'token')) else redact(v) for k,v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value

def emit(label, value):
    print(json.dumps({label: redact(value)}, ensure_ascii=False, default=str))

task = rows('''SELECT t.id,t.user_id,u.username,t.segment_id,t.batch_item_id,
t.runninghub_task_id,t.execution_account_id,t.workflow_type,t.status,t.error_code,
t.error_message,t.runninghub_failed_reason,t.runninghub_attempt_history,t.runninghub_usage,
t.input_payload,t.image_path,t.image_original_name,t.audio_path,t.audio_duration_seconds,
t.created_at,t.runninghub_submitted_at,t.completed_at,t.updated_at,t.runninghub_auto_retry_count
FROM generation_tasks t JOIN users u ON u.id=t.user_id
WHERE t.runninghub_task_id=?''', ('2094329103514824705',))
if not task:
    emit('not_found', True)
    raise SystemExit()
t = task[0]
for key in ('runninghub_failed_reason','runninghub_attempt_history','runninghub_usage','input_payload'):
    t[key] = parsed(t[key])
payload = t.pop('input_payload', {})
emit('target_task', t)
emit('input_assets', payload.get('assets') if isinstance(payload,dict) else payload)
emit('input_other_keys', list(payload) if isinstance(payload,dict) else [])
segment = rows('SELECT id,batch_item_id,segment_index,start_seconds,end_seconds,status FROM generation_segments WHERE id=?', (t['segment_id'],))
emit('segment', segment)
item_id = segment[0]['batch_item_id'] if segment else t['batch_item_id']
emit('batch', rows('''SELECT b.id,b.name,b.created_at,b.workflow_type,b.status,i.id item_id,i.row_number,i.row_key,i.status item_status
FROM generation_batch_items i JOIN generation_batches b ON b.id=i.batch_id WHERE i.id=?''',(item_id,)))
emit('siblings', rows('''SELECT s.segment_index,s.id segment_id,t.id task_id,t.status,t.runninghub_task_id,t.execution_account_id,t.error_code,
t.created_at,t.runninghub_submitted_at,t.completed_at,t.image_path,t.image_original_name
FROM generation_segments s LEFT JOIN generation_tasks t ON t.segment_id=s.id
WHERE s.batch_item_id=? ORDER BY s.segment_index''',(item_id,)))
emit('attempts', rows('''SELECT attempt_number,execution_account_id,remote_task_id,status,error_code,error_message,submitted_at,finished_at
FROM generation_task_attempts WHERE generation_task_id=? ORDER BY attempt_number''',(t['id'],)))
emit('h3_segment_config', rows('SELECT * FROM h3_segment_configs WHERE segment_id=?',(t['segment_id'],)))
assets = payload.get('assets',{}) if isinstance(payload,dict) else {}
asset_list = list(assets.values()) if isinstance(assets,dict) else assets if isinstance(assets,list) else []
checked=[]
for a in asset_list:
    if not isinstance(a,dict):
        continue
    p = Path(a.get('path') or '')
    if not p.is_absolute():
        p = ROOT/'data'/p
    entry = {'path':str(p),'exists':p.is_file(),'kind':a.get('kind'),'original_name':a.get('original_name')}
    if p.is_file():
        entry['size']=p.stat().st_size
        if entry['size']<20*1024*1024:
            entry['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    checked.append(entry)
emit('asset_files', checked)
details=[]
for r in rows('''SELECT s.segment_index,t.input_payload,t.runninghub_failed_reason,t.status
FROM generation_segments s JOIN generation_tasks t ON t.segment_id=s.id WHERE s.batch_item_id=? ORDER BY s.segment_index''',(item_id,)):
    inp=parsed(r['input_payload'])
    details.append({'segment_index':r['segment_index'],'status':r['status'],'identity_image':inp.get('assets',{}).get('identity_image_1'),
                    'failed_reason':parsed(r['runninghub_failed_reason']) if r['status']=='FAILED' else None})
emit('sibling_assets_and_failures', details)
fingerprints=[]
for name in ('app/workers/task_worker.py','app/services/runninghub.py','app/services/runninghub_uploads.py','app/workflows/h3_ref2va.py','app/services/h3/graph.py'):
    p=ROOT/name
    fingerprints.append({'file':name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'modified_at':p.stat().st_mtime})
emit('deployed_source_fingerprints',fingerprints)
import struct
import zlib
for a in checked:
    if a['exists'] and a['kind']=='image':
        data=Path(a['path']).read_bytes()
        assert data[:8]==b'\x89PNG\r\n\x1a\n'
        pos=8
        idat=[]
        chunks=[]
        while pos+12<=len(data):
            size=struct.unpack('>I',data[pos:pos+4])[0]
            kind=data[pos+4:pos+8]
            chunk=data[pos+8:pos+8+size]
            crc=struct.unpack('>I',data[pos+8+size:pos+12+size])[0]
            assert zlib.crc32(kind+chunk)&0xffffffff==crc
            chunks.append(kind.decode('ascii'))
            if kind==b'IHDR':
                dimensions=struct.unpack('>IIBBBBB',chunk)
            if kind==b'IDAT':
                idat.append(chunk)
            pos+=size+12
            if kind==b'IEND':
                break
        uncompressed=zlib.decompress(b''.join(idat))
        emit('image_validation',{'path':a['path'],'format':'PNG','width':dimensions[0],'height':dimensions[1],
                                'chunk_checksums_valid':True,'pixel_stream_decompressed_bytes':len(uncompressed)})
for name, needle, before,after in [('app/workers/task_worker.py','uploaded_files = {}',12,60),
                                 ('app/services/h3/graph.py','slot_images =',5,24)]:
    lines=(ROOT/name).read_text(encoding='utf-8').splitlines()
    for idx,line in enumerate(lines):
        if needle in line:
            emit('deployed_code_'+name, [{'line':i+1,'text':lines[i]} for i in range(max(0,idx-before), min(len(lines),idx+after))])
needles = [t['id'], t['runninghub_task_id'], '7d35443731f616c6622170b10452ab321082d77db11acf0b9380a649952b83d6']
for path in (ROOT/'data/logs/video_worker.log', ROOT/'data/logs/web.log'):
    matched=[]
    with path.open(encoding='utf-8',errors='replace') as f:
        for number,line in enumerate(f,1):
            if any(n in line for n in needles):
                matched.append({'line':number,'text':line.rstrip()[:14000]})
    emit('log_matches_'+path.name, matched[-50:])
