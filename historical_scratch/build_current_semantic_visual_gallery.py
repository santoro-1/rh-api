from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from html import escape
import json
from pathlib import Path


CATALOG_ROOT = Path(
    r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library"
)
CATALOG_PATH = CATALOG_ROOT / "catalog.json"
OUTPUT_PATH = Path(
    r"D:\工作内容\轻盈健\数字人\runninghub_mvp\outputs"
    r"\019ff5d6-f57f-7e73-b974-2c694ed0d95a"
    r"\catalog_v3_人工审核材料\catalog_v3_当前完整素材库.html"
)


def text(value: object) -> str:
    return escape(str(value or ""), quote=True)


def chips(values: list[object] | tuple[object, ...], css: str = "") -> str:
    return "".join(
        f'<span class="chip {css}">{text(value)}</span>' for value in values if value
    ) or '<span class="muted">无</span>'


def file_uri(path: Path) -> str:
    return path.resolve().as_uri()


def resolved_resource(asset: dict[str, object], key: str) -> Path | None:
    resource = asset.get("resource")
    if not isinstance(resource, dict):
        return None
    value = resource.get(key)
    if not value:
        return None
    return (CATALOG_ROOT / str(value)).resolve()


def media_html(asset: dict[str, object]) -> tuple[str, Path | None]:
    media_type = str(asset.get("media_type") or "")
    preview = resolved_resource(asset, "preview")
    if media_type == "video":
        video = resolved_resource(asset, "video")
        if video is None:
            return '<div class="missing">缺少视频路径</div>', None
        poster = f' poster="{file_uri(preview)}"' if preview and preview.is_file() else ""
        return (
            f'<video controls preload="metadata"{poster} src="{file_uri(video)}"></video>',
            video,
        )
    if preview is None:
        return '<div class="missing">缺少预览路径</div>', None
    return f'<img loading="lazy" src="{file_uri(preview)}" alt="{text(asset.get("name"))}">', preview


def main() -> None:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    assets = list(payload.get("assets") or [])
    concepts = list(payload.get("concepts") or [])
    concept_by_id = {str(item["concept_id"]): item for item in concepts}
    assets_by_concept: dict[str, list[dict[str, object]]] = defaultdict(list)
    for asset in assets:
        for concept_id in asset.get("concept_ids") or []:
            assets_by_concept[str(concept_id)].append(asset)

    type_counts = Counter(str(asset.get("media_type") or "unknown") for asset in assets)
    rights_counts = Counter(str(asset.get("rights_status") or "unknown") for asset in assets)
    cards: list[str] = []
    for index, asset in enumerate(assets, start=1):
        concept_ids = [str(value) for value in asset.get("concept_ids") or []]
        concept_labels = [
            str(concept_by_id.get(concept_id, {}).get("label") or concept_id)
            for concept_id in concept_ids
        ]
        aliases = sorted(
            {
                str(alias)
                for concept_id in concept_ids
                for alias in concept_by_id.get(concept_id, {}).get("aliases") or []
            }
        )
        media, local_path = media_html(asset)
        roles = asset.get("semantic_roles") or {}
        basis = asset.get("trigger_basis") or {}
        searchable = " ".join(
            [
                str(asset.get("asset_id") or ""),
                str(asset.get("name") or ""),
                str(asset.get("description") or ""),
                *concept_ids,
                *concept_labels,
                *aliases,
                *(str(value) for value in asset.get("tags") or []),
            ]
        ).lower()
        cards.append(
            f"""
            <article class="asset-card" data-type="{text(asset.get('media_type'))}"
              data-eligible="{str(asset.get('auto_eligible') is True).lower()}"
              data-rights="{text(asset.get('rights_status') or 'unknown')}"
              data-search="{text(searchable)}">
              <div class="media">{media}</div>
              <div class="body">
                <div class="eyebrow"><span>#{index:03d}</span><span>{text(asset.get('media_type'))}</span>
                  <span class="{'ok' if asset.get('auto_eligible') is True else 'warn'}">{'自动可用' if asset.get('auto_eligible') is True else '仅人工/禁用'}</span></div>
                <h2>{text(asset.get('name'))}</h2>
                <div class="asset-id">{text(asset.get('asset_id'))}</div>
                <p>{text(asset.get('description'))}</p>
                <dl>
                  <dt>概念</dt><dd>{chips([f'{cid} · {label}' for cid, label in zip(concept_ids, concept_labels)], 'concept')}</dd>
                  <dt>触发词</dt><dd>{chips(aliases, 'alias')}</dd>
                  <dt>语义角色</dt><dd>
                    <div><b>depicts</b> {chips(list(roles.get('depicts') or []))}</div>
                    <div><b>expresses</b> {chips(list(roles.get('expresses') or []))}</div>
                    <div><b>related</b> {chips(list(roles.get('related') or []))}</div>
                  </dd>
                  <dt>触发依据</dt><dd>{chips([f'{key}: {value}' for key, value in basis.items()])}</dd>
                  <dt>使用方式</dt><dd>{chips(list(asset.get('usage_modes') or []))}</dd>
                  <dt>标签</dt><dd>{chips(list(asset.get('tags') or []))}</dd>
                  <dt>授权</dt><dd>{text(asset.get('rights_status') or 'unknown')}</dd>
                  <dt>本地文件</dt><dd><code>{text(local_path or '')}</code></dd>
                </dl>
              </div>
            </article>
            """
        )

    concept_rows: list[str] = []
    for concept in concepts:
        concept_id = str(concept.get("concept_id") or "")
        linked = assets_by_concept.get(concept_id, [])
        searchable = " ".join(
            [
                concept_id,
                str(concept.get("label") or ""),
                str(concept.get("description") or ""),
                *(str(value) for value in concept.get("aliases") or []),
                *(str(asset.get("name") or "") for asset in linked),
            ]
        ).lower()
        concept_rows.append(
            f"""
            <article class="concept-card" data-search="{text(searchable)}">
              <div class="eyebrow"><span>{len(linked)} 个素材</span></div>
              <h2>{text(concept.get('label'))}</h2>
              <div class="asset-id">{text(concept_id)}</div>
              <p>{text(concept.get('description'))}</p>
              <div class="group"><b>触发词</b>{chips(list(concept.get('aliases') or []), 'alias')}</div>
              <div class="group"><b>对应素材</b>{chips([str(asset.get('name') or '') for asset in linked], 'concept')}</div>
            </article>
            """
        )

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    catalog_modified = datetime.fromtimestamp(CATALOG_PATH.stat().st_mtime).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>catalog v3 当前完整素材库</title>
<style>
:root{{--bg:#08101d;--panel:#111b2c;--panel2:#17243a;--line:#2a3b58;--text:#edf4ff;--muted:#9fb0c9;--cyan:#46d9ff;--green:#69e6a6;--amber:#ffd36a}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 "Microsoft YaHei",sans-serif}}
header{{position:sticky;top:0;z-index:20;padding:18px 24px;background:rgba(8,16,29,.96);border-bottom:1px solid var(--line);backdrop-filter:blur(12px)}}
h1{{margin:0 0 5px;font-size:24px}}.meta,.muted{{color:var(--muted)}}.stats{{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}}
.stat{{padding:7px 11px;background:var(--panel);border:1px solid var(--line);border-radius:10px}}.controls{{display:flex;gap:10px;flex-wrap:wrap}}
input,select,button{{background:#0c1626;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:9px 11px}}input{{min-width:320px;flex:1}}
button.active{{border-color:var(--cyan);color:var(--cyan)}}main{{padding:22px}}.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px}}
.asset-card,.concept-card{{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden;box-shadow:0 8px 24px rgba(0,0,0,.18)}}
.media{{height:260px;background:#050a12;display:flex;align-items:center;justify-content:center}}.media img,.media video{{width:100%;height:100%;object-fit:contain}}
.body,.concept-card{{padding:15px}}.eyebrow{{display:flex;justify-content:space-between;gap:8px;color:var(--muted);font-size:12px}}
h2{{margin:5px 0 1px;font-size:19px}}.asset-id{{color:var(--cyan);font:12px Consolas,monospace;overflow-wrap:anywhere}}p{{color:#c9d5e7}}
dl{{display:grid;grid-template-columns:72px 1fr;gap:8px;margin:12px 0 0}}dt{{color:var(--muted)}}dd{{margin:0;min-width:0}}
.chip{{display:inline-block;margin:2px 4px 2px 0;padding:2px 7px;border:1px solid #3a4b68;border-radius:999px;background:var(--panel2);font-size:12px}}
.chip.alias{{border-color:#74602c;color:#ffe39b}}.chip.concept{{border-color:#275c72;color:#8ce9ff}}.ok{{color:var(--green)}}.warn{{color:var(--amber)}}
code{{display:block;color:#b5c6df;white-space:normal;overflow-wrap:anywhere;background:#0b1422;padding:7px;border-radius:7px}}.group{{margin-top:12px}}.group b{{display:block;color:var(--muted);margin-bottom:4px}}
.hidden{{display:none!important}}#concept-view{{grid-template-columns:repeat(auto-fill,minmax(310px,1fr))}}.empty{{display:none;text-align:center;color:var(--muted);padding:50px}}
</style></head><body>
<header><h1>catalog v3 当前完整素材库</h1>
<div class="meta">生成：{generated_at}　catalog 修改：{catalog_modified}　源文件：{text(CATALOG_PATH)}</div>
<div class="stats"><span class="stat">素材 {len(assets)}</span><span class="stat">图片 {type_counts['image']}</span><span class="stat">视频 {type_counts['video']}</span><span class="stat">概念 {len(concepts)}</span><span class="stat">需标注来源 {rights_counts['attributed']}</span></div>
<div class="controls"><button id="asset-tab" class="active">按素材看</button><button id="concept-tab">按概念/触发词看</button>
<input id="search" placeholder="搜索素材名、概念、触发词、asset_id、标签">
<select id="type"><option value="">全部类型</option><option value="image">图片</option><option value="video">视频</option></select>
<select id="eligible"><option value="">全部状态</option><option value="true">自动可用</option><option value="false">仅人工/禁用</option></select></div></header>
<main><section id="asset-view" class="grid">{''.join(cards)}</section><section id="concept-view" class="grid hidden">{''.join(concept_rows)}</section><div id="empty" class="empty">没有匹配项</div></main>
<script>
const q=document.getElementById('search'),type=document.getElementById('type'),eligible=document.getElementById('eligible');let mode='asset';
function filter(){{const term=q.value.trim().toLowerCase();let shown=0;const selector=mode==='asset'?'.asset-card':'.concept-card';document.querySelectorAll(selector).forEach(card=>{{const ok=(!term||card.dataset.search.includes(term))&&(mode!=='asset'||(!type.value||card.dataset.type===type.value)&&(!eligible.value||card.dataset.eligible===eligible.value));card.classList.toggle('hidden',!ok);if(ok)shown++;}});document.getElementById('empty').style.display=shown?'none':'block';}}
function tab(next){{mode=next;document.getElementById('asset-view').classList.toggle('hidden',mode!=='asset');document.getElementById('concept-view').classList.toggle('hidden',mode!=='concept');document.getElementById('asset-tab').classList.toggle('active',mode==='asset');document.getElementById('concept-tab').classList.toggle('active',mode==='concept');type.disabled=eligible.disabled=mode!=='asset';filter();}}
q.addEventListener('input',filter);type.addEventListener('change',filter);eligible.addEventListener('change',filter);document.getElementById('asset-tab').onclick=()=>tab('asset');document.getElementById('concept-tab').onclick=()=>tab('concept');
</script></body></html>"""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(OUTPUT_PATH)
    print(f"assets={len(assets)} concepts={len(concepts)}")


if __name__ == "__main__":
    main()
