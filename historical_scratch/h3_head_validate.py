from pathlib import Path
import json
import shutil
import sys
import subprocess

repo = Path(r'D:\工作内容\轻盈健\公寓\jyd_plain_json_probe')
sys.path.insert(0, str(repo / 'src'))
from jyd_probe.caption_alignment import _recognized_tokens
from jyd_probe.h3_audio_cleanup import clean_segment, _read_pcm, file_sha256

root = Path(__file__).parent / 'h3_head_samples'
for record_path in sorted(root.glob('segment-*-asr.json')):
    number = record_path.name.split('-')[1]
    record = json.loads(record_path.read_text(encoding='utf-8'))
    output = root / f'segment-{number}'
    output.mkdir(exist_ok=True)
    raw = output / 'original.mp4'
    shutil.copyfile(record['source'], raw)
    class RecordedASR:
        def recognize_tokens(self, _path):
            return _recognized_tokens(record['payload'])
    before = file_sha256(raw)
    cleaned = clean_segment(raw, record['script'], RecordedASR())
    assert before == file_sha256(raw)
    original_audio = output/'original.wav'
    subprocess.run(['ffmpeg','-v','error','-y','-i',str(raw),'-vn','-c:a','pcm_s16le',str(original_audio)],check=True)
    shutil.copyfile(cleaned.audio_path, output/'cleaned.wav')
    shutil.copyfile(cleaned.preview_path, output/'cleaned.mp4')
    print(json.dumps({'segment':number,**cleaned.report},ensure_ascii=False),flush=True)
