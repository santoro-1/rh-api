from pathlib import Path
import hashlib
import json
import shutil
import sys
from unittest.mock import patch

repo = Path(r'D:\工作内容\轻盈健\公寓\jyd_plain_json_probe')
sys.path.insert(0, str(repo / 'src'))
from jyd_probe.h3_audio_cleanup import read_cleanup, file_sha256
from jyd_probe.h3_video_segments import h3_video_sequence_ready
from jyd_probe.project_h3 import ProjectH3Coordinator
from jyd_probe.project_store import ProjectStore
from jyd_probe.project_video_source import build_project_video_source, build_project_speech_audio
from jyd_probe.render_job import run_render_job
from jyd_probe.cli import load_plain_draft_json

root = Path(__file__).parent / 'h3_segment_validation'
root.mkdir(exist_ok=True)
samples = Path(__file__).parent / 'h3_head_samples'
records = [json.loads((samples / f'segment-{i}-asr.json').read_text(encoding='utf-8')) for i in range(1, 4)]
raws = [samples / f'segment-{i}' / 'original.mp4' for i in range(1, 4)]
cleaned = {file_sha256(path): read_cleanup(path, record['script']) for path, record in zip(raws, records)}
assert all(cleaned.values())
original_draft = Path(r'D:\剪映草稿\JianyingPro Drafts\2-composition-06\draft_content.json')
before_hash = file_sha256(original_draft)

class LocalFixtureClient:
    def get_h3_batch(self, token, batch_id):
        return {'batch_id': 'local-validation', 'status': 'SUCCESS', 'items': [{
            'item_id': 'local-row', 'row_id': '1', 'status': 'SUCCESS',
            'segments': [{'segment_id': f's{i + 1}', 'index': i, 'status': 'SUCCESS',
                          'script_text': record['script'], 'normalized_video_download_url': f'/s{i + 1}/video',
                          'normalized_video_sha256': file_sha256(raws[i])} for i, record in enumerate(records)]}]}

    def download_h3_segment_video(self, token, segment_id, target, **kwargs):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(raws[int(segment_id[1:]) - 1], target)
        return target.stat().st_size

store = ProjectStore(root / 'validation.db')
project = store.create_project(owner_user_id='validation', owner_username='local-only', name='H3独立片段验收',
                               items=[{'row_key': '1', 'script_text': ''.join(r['script'] for r in records)}])
client = LocalFixtureClient()
store.set_h3_batch_snapshot('validation', project['project_id'], prepare_key='local', snapshot=client.get_h3_batch('', ''))
coordinator = ProjectH3Coordinator(store, client, storage_root=root / 'storage', head_cleanup_enabled=True)
with patch('jyd_probe.project_h3.read_cleanup', side_effect=lambda path, script: cleaned[file_sha256(path)]), \
     patch('jyd_probe.project_h3.request_cleanup', return_value={'status': 'READY'}):
    project = coordinator.sync('validation', project['project_id'], '')['project']
item = project['items'][0]
assert h3_video_sequence_ready(item), item['settings']['h3'].get('materialization_error')
source = build_project_video_source(item)
source['work_root'] = str(root / 'work')
job = {'source': source, 'original_video_volume': 0.0, 'audios': [build_project_speech_audio(item)],
       'output': {'draft_root': str(root / 'drafts'), 'draft_name': 'H3-独立片段验收-' + project['project_id'][:6], 'skip_export': True}}
result = run_render_job(job)
data = load_plain_draft_json(result.output_draft_dir)
track = next(t for t in data['tracks'] if t['type'] == 'video')
by_id = {m['id']: m for m in data['materials']['videos']}
summary = {'draft_dir': str(result.output_draft_dir), 'duration_us': data['duration'],
           'original_draft_unchanged': before_hash == file_sha256(original_draft),
           'segments': [{'start_us': s['target_timerange']['start'], 'duration_us': s['target_timerange']['duration'],
                         'speed': s['speed'], 'volume': s['volume'], 'path': by_id[s['material_id']]['path']}
                        for s in track['segments']],
           'audio_tracks': len([t for t in data['tracks'] if t['type'] == 'audio']),
           'clean_audio_path': item['outputs']['audio']['managed_path'],
           'transitions': [t['duration'] for t in data['materials']['transitions']]}
assert len(summary['segments']) == 3 and summary['audio_tracks'] == 1
assert all(s['speed'] == 1.0 and s['volume'] == 0 for s in summary['segments'])
assert summary['original_draft_unchanged']
(root / 'verification.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(summary, ensure_ascii=False, indent=2))
