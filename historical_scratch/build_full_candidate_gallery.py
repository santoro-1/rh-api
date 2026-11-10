from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime
from html import escape
import json
from pathlib import Path
import re


SOURCE_ROOT = Path(r"D:\工作内容\素材\_筛选结果")
CATALOG_PATH = Path(
    r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe"
    r"\data\libraries\semantic_visual_library\catalog.json"
)
OUTPUT_PATH = Path(
    r"D:\工作内容\轻盈健\数字人\runninghub_mvp\outputs"
    r"\019ff5d6-f57f-7e73-b974-2c694ed0d95a"
    r"\catalog_v3_人工审核材料\去重后2320条完整候选池.html"
)
BUCKET_FILES = {
    "food": "01_食物候选_去重后.csv",
    "exercise": "02_运动候选_去重后.csv",
    "review": "03_待视觉判断_去重后.csv",
    "deferred": "04_本轮暂不处理_去重后.csv",
}
BUCKET_LABELS = {
    "food": "食物候选",
    "exercise": "运动候选",
    "review": "待视觉判断",
    "deferred": "本轮暂缓",
}
STATUS_LABELS = {
    "approved": "首批已批准",
    "needs_clip": "首批需切片",
    "rejected": "首批已拒绝",
    "deferred": "首批暂缓",
    "unreviewed": "未进入首批",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def h(value: object) -> str:
    return escape(str(value or ""), quote=True)


def media_markup(row: dict[str, str]) -> str:
    path = Path(row["full_path"])
    uri = path.resolve().as_uri()
    if row["media_type"] == "video":
        return (
            f'<video controls preload="none" src="{uri}" '
            f'aria-label="{h(row["filename"])}"></video>'
        )
    return f'<img loading="lazy" src="{uri}" alt="{h(row["filename"])}">'


def main() -> None:
    rows: list[dict[str, str]] = []
    for bucket, filename in BUCKET_FILES.items():
        for row in read_csv(SOURCE_ROOT / filename):
            row["candidate_bucket"] = bucket
            rows.append(row)
    if len(rows) != 2320 or len({row["sha256"] for row in rows}) != 2320:
        raise RuntimeError("去重候选池不是预期的 2320 条唯一素材")

    reviewed = {
        row["sha256"]: row
        for row in read_csv(
            SOURCE_ROOT / "首批200_人工打标结果" / "首批200条_人工打标结果.csv"
        )
    }
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    catalog_hash_prefixes: set[str] = set()
    catalog_review_indices: set[int] = set()
    for asset in catalog.get("assets") or []:
        asset_id = str(asset.get("asset_id") or "")
        match = re.search(r"\.([0-9a-f]{10})$", asset_id)
        if match:
            catalog_hash_prefixes.add(match.group(1))
        manual = re.match(r"manual\.(\d{3})\.", asset_id)
        if manual:
            catalog_review_indices.add(int(manual.group(1)))

    counts = Counter()
    cards: list[str] = []
    for index, row in enumerate(rows, start=1):
        review = reviewed.get(row["sha256"], {})
        review_status = review.get("review_status") or "unreviewed"
        review_index = int(review["review_index"]) if review.get("review_index") else None
        in_catalog = (
            review_index in catalog_review_indices
            if review_index is not None
            else row["sha256"][:10] in catalog_hash_prefixes
        )
        bucket = row["candidate_bucket"]
        counts[("bucket", bucket)] += 1
        counts[("media", row["media_type"])] += 1
        counts[("review", review_status)] += 1
        counts[("catalog", str(in_catalog).lower())] += 1
        labels = [
            BUCKET_LABELS[bucket],
            STATUS_LABELS.get(review_status, review_status),
            "当前已入 catalog" if in_catalog else "当前未入 catalog",
        ]
        search = " ".join(
            [
                row.get("filename", ""),
                row.get("relative_path", ""),
                row.get("screen_reason", ""),
                review.get("asset_name", ""),
                review.get("primary_concept_id", ""),
                review.get("secondary_concept_ids", ""),
                review.get("tags", ""),
                review.get("review_notes", ""),
                *labels,
            ]
        ).lower()
        details = ""
        if review:
            details = f"""
              <dt>首批打标</dt><dd>{h(review_status)} · #{h(review.get('review_index'))}</dd>
              <dt>素材名</dt><dd>{h(review.get('asset_name'))}</dd>
              <dt>主概念</dt><dd><code>{h(review.get('primary_concept_id'))}</code></dd>
              <dt>次概念</dt><dd><code>{h(review.get('secondary_concept_ids'))}</code></dd>
              <dt>标签</dt><dd>{h(review.get('tags'))}</dd>
              <dt>审核备注</dt><dd>{h(review.get('review_notes'))}</dd>
            """
        cards.append(
            f"""
            <article class="card" data-bucket="{h(bucket)}" data-media="{h(row['media_type'])}"
              data-review="{h(review_status)}" data-catalog="{str(in_catalog).lower()}"
              data-search="{h(search)}">
              <div class="media">{media_markup(row)}</div>
              <div class="body">
                <div class="badges"><span>{index:04d}</span><span>{h(BUCKET_LABELS[bucket])}</span>
                  <span class="{'ok' if in_catalog else 'muted'}">{'已入库' if in_catalog else '未入库'}</span></div>
                <h2>{h(review.get('asset_name') or row['filename'])}</h2>
                <p class="file">{h(row['relative_path'])}</p>
                <dl>
                  <dt>首批状态</dt><dd>{h(STATUS_LABELS.get(review_status, review_status))}</dd>
                  <dt>类型/尺寸</dt><dd>{h(row['media_type'])} · {h(row['width'])}×{h(row['height'])}</dd>
                  <dt>时长</dt><dd>{h(row.get('duration_seconds')) or '—'} 秒</dd>
                  <dt>筛选原因</dt><dd>{h(row.get('screen_reason'))}</dd>
                  {details}
                  <dt>源文件</dt><dd><code>{h(row['full_path'])}</code></dd>
                  <dt>SHA-256</dt><dd><code>{h(row['sha256'])}</code></dd>
                </dl>
              </div>
            </article>
            """
        )

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>去重后 2320 条完整候选池</title>
<style>
:root{{--bg:#07101d;--panel:#111b2c;--line:#293a55;--text:#edf4ff;--muted:#95a8c4;--cyan:#52ddff;--green:#68e3a4;--amber:#ffd16a}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Microsoft YaHei",sans-serif}}
header{{position:sticky;top:0;z-index:5;padding:16px 22px;background:rgba(7,16,29,.97);border-bottom:1px solid var(--line)}}h1{{margin:0;font-size:23px}}
.meta,.muted{{color:var(--muted)}}.stats,.filters{{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}}.stat{{padding:6px 10px;background:var(--panel);border:1px solid var(--line);border-radius:9px}}
input,select{{background:#0b1525;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px 10px}}input{{min-width:320px;flex:1}}
main{{padding:20px}}.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(350px,1fr));gap:15px}}.card{{background:var(--panel);border:1px solid var(--line);border-radius:13px;overflow:hidden}}
.media{{height:250px;background:#03070d;display:flex;align-items:center;justify-content:center}}.media img,.media video{{width:100%;height:100%;object-fit:contain}}.body{{padding:14px}}
.badges{{display:flex;justify-content:space-between;gap:8px;font-size:12px;color:var(--muted)}}.ok{{color:var(--green)}}h2{{margin:6px 0 2px;font-size:18px}}.file{{color:var(--cyan);overflow-wrap:anywhere}}
dl{{display:grid;grid-template-columns:75px 1fr;gap:7px;margin:10px 0 0}}dt{{color:var(--muted)}}dd{{margin:0;min-width:0;overflow-wrap:anywhere}}code{{font:11px Consolas,monospace;color:#c5d5eb}}
.hidden{{display:none!important}}#empty{{display:none;text-align:center;padding:60px;color:var(--muted)}}
</style></head><body><header><h1>去重后 2,320 条完整候选池</h1>
<div class="meta">生成：{generated_at}　这不是正式 catalog；用于查看首批 200 之外尚未筛选和打标的素材。</div>
<div class="stats"><span class="stat">全部 2320</span><span class="stat">图片 {counts[('media','image')]}</span><span class="stat">视频 {counts[('media','video')]}</span>
<span class="stat">当前已入 catalog {counts[('catalog','true')]}</span><span class="stat">未进入首批 {counts[('review','unreviewed')]}</span></div>
<div class="filters"><input id="q" placeholder="搜索文件名、路径、概念、标签、审核备注">
<select id="bucket"><option value="">全部候选桶</option><option value="food">食物候选 1728</option><option value="exercise">运动候选 60</option><option value="review">待视觉判断 80</option><option value="deferred">本轮暂缓 452</option></select>
<select id="media"><option value="">图片+视频</option><option value="image">图片</option><option value="video">视频</option></select>
<select id="review"><option value="">全部审核状态</option><option value="unreviewed">未进入首批</option><option value="approved">首批已批准</option><option value="needs_clip">首批需切片</option><option value="rejected">首批已拒绝</option><option value="deferred">首批暂缓</option></select>
<select id="catalog"><option value="">入库+未入库</option><option value="true">当前已入 catalog</option><option value="false">当前未入 catalog</option></select></div></header>
<main><section class="grid">{''.join(cards)}</section><div id="empty">没有匹配项</div></main>
<script>
const ids=['q','bucket','media','review','catalog'];const el=Object.fromEntries(ids.map(id=>[id,document.getElementById(id)]));
function filter(){{let shown=0;const term=el.q.value.trim().toLowerCase();document.querySelectorAll('.card').forEach(c=>{{const ok=(!term||c.dataset.search.includes(term))&&(!el.bucket.value||c.dataset.bucket===el.bucket.value)&&(!el.media.value||c.dataset.media===el.media.value)&&(!el.review.value||c.dataset.review===el.review.value)&&(!el.catalog.value||c.dataset.catalog===el.catalog.value);c.classList.toggle('hidden',!ok);if(ok)shown++;}});document.getElementById('empty').style.display=shown?'none':'block';}}
ids.forEach(id=>el[id].addEventListener(id==='q'?'input':'change',filter));
</script></body></html>"""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(OUTPUT_PATH)
    print(json.dumps({"rows": len(rows), "counts": {str(k): v for k, v in counts.items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
