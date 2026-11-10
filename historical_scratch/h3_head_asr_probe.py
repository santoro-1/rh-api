from pathlib import Path
import json
import sqlite3
import sys
import subprocess

repo = Path(r'D:\工作内容\轻盈健\公寓\jyd_plain_json_probe')
sys.path.insert(0, str(repo / 'src'))
from jyd_probe.caption_alignment import FunASRCaptionAligner

root = repo / 'runtime/test_environment/web_storage'
item_id = '14fbbed077834870856a72a149b54259'
db = sqlite3.connect((root / 'control.db').as_uri() + '?mode=ro', uri=True)
row = db.execute('SELECT settings_json FROM project_items WHERE item_id=?', (item_id,)).fetchone()
db.close()
segments = json.loads(row[0])['h3']['segments']
out = Path(__file__).parent / 'h3_head_samples'
out.mkdir(exist_ok=True)
aligner = FunASRCaptionAligner('http://127.0.0.1:18084', timeout_seconds=180)
for directory in (root / 'projects/1/a84876805e8d4fcf9189f709db63811d' / item_id / 'h3/segment-cache').iterdir():
    meta = json.loads((directory / 'current.json').read_text(encoding='utf-8'))
    segment = next((s for s in segments if s['segment_id'] == meta['segment_id']), None)
    if segment is None:
        continue
    number = int(segment['index']) + 1
    audio = out / f'segment-{number}-asr.wav'
    subprocess.run(['ffmpeg','-v','error','-y','-i',str(directory/'current.mp4'),'-map','0:a:0','-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',str(audio)], check=True)
    payload = aligner._transcribe(audio)
    # Diagnostic artifacts only; never writes the source database or draft.
    record = {'source':str(directory/'current.mp4'), 'script':segment['script_text'], 'payload':payload}
    (out/f'segment-{number}-asr.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'number':number,'prefix':segment['script_text'][:25],'tokens':payload.get('tokens',[])[:8]},ensure_ascii=False), flush=True)
