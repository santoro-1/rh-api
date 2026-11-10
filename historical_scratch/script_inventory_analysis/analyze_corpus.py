from __future__ import annotations

import collections
import hashlib
import json
import re
from pathlib import Path

import jieba
import jieba.posseg as pseg


ROOT = Path(r"D:\工作内容\轻盈健\数字人\.codex_tmp\script_inventory_analysis")
CATALOG_PATH = Path(
    r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library\catalog.json"
)
HEADER_RE = re.compile(
    r"^(?P<account>.+?)｜序号：(?P<seq>\d+)｜发布日期：(?P<date>[^｜]+)"
)
BOUNDARY_RE = re.compile(r"[，,。！？!?；;：:\n\r]+")
RAPID_LIST_RE = re.compile(r"[^，。！？；：\n]{0,80}[^、，。！？；：\n]+(?:、[^、，。！？；：\n]+){1,}")
ORDERED_ITEM_RE = re.compile(r"第[一二三四五六七八九十\d]+(?:天|个|种|招|点|步|动作|样)")
NORMALIZE_RE = re.compile(r"[\s\W_\d一二三四五六七八九十百千万亿]+", re.UNICODE)

STOPWORDS = {
    "我们", "你们", "自己", "这个", "那个", "一种", "一个", "一下", "一些", "一样", "就是",
    "不是", "可以", "能够", "需要", "一定", "知道", "告诉", "觉得", "开始", "时候", "很多",
    "每天", "今天", "现在", "如果", "因为", "所以", "然后", "还有", "已经", "其实", "怎么",
    "什么", "这样", "特别", "比较", "非常", "真的", "不要", "没有", "不会", "进行", "起来",
    "下来", "出来", "进去", "上来", "下去", "的话", "问题", "方法", "方面", "效果", "身体",
    "姐妹", "女性", "张老师", "老师", "冠军", "关注", "评论区", "分享", "帮助", "视频", "内容",
    "学会", "记得", "坚持", "感觉", "发现", "适合", "保持", "选择", "建议", "左右", "分钟",
    "小时", "时间", "第一", "第二", "第三", "第四", "第五", "第六", "第七", "最后", "这个动作",
}


def normalize(text: str) -> str:
    return NORMALIZE_RE.sub("", text.lower())


def add_record(records, *, source, source_type, account, seq, text, metadata=None):
    text = str(text or "").strip()
    if not text:
        return
    records.append(
        {
            "record_id": hashlib.sha1(f"{source}|{seq}|{text}".encode("utf-8")).hexdigest()[:12],
            "source": source,
            "source_type": source_type,
            "account": account,
            "seq": str(seq),
            "text": text,
            "metadata": metadata or {},
        }
    )


def load_records():
    records = []
    books = json.loads((ROOT / "workbooks.json").read_text(encoding="utf-8"))
    for book in books:
        source = Path(book["inputPath"]).name
        values = book["sheets"][0]["values"]
        headers = [str(value or "") for value in values[0]]
        for row in values[1:]:
            item = dict(zip(headers, row))
            text = item.get("新台词") or item.get("台词")
            add_record(
                records,
                source=source,
                source_type="xlsx",
                account=str(item.get("分配账号") or source.removesuffix(".xlsx")),
                seq=item.get("序号") or "",
                text=text,
                metadata={
                    "文章类型": item.get("文章类型"),
                    "评论": item.get("评论"),
                    "点赞": item.get("点赞"),
                    "转发": item.get("转发"),
                    "收藏": item.get("收藏"),
                    "发布日期": item.get("发布日期"),
                },
            )

    documents = json.loads((ROOT / "documents.json").read_text(encoding="utf-8"))
    for document in documents:
        source = Path(document["input_path"]).name
        current = None
        for paragraph in document["paragraphs"]:
            match = HEADER_RE.match(paragraph.strip())
            if match:
                if current:
                    add_record(records, **current)
                current = {
                    "source": source,
                    "source_type": "docx",
                    "account": match.group("account"),
                    "seq": match.group("seq"),
                    "text": "",
                    "metadata": {"header": paragraph.strip(), "发布日期": match.group("date")},
                }
            elif current:
                current["text"] += ("\n" if current["text"] else "") + paragraph.strip()
        if current:
            add_record(records, **current)
    return records


def catalog_stats(records):
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    assets_by_concept = collections.defaultdict(lambda: {"image": 0, "video": 0, "enrichment_video": 0})
    for asset in catalog["assets"]:
        media_type = asset["media_type"]
        tags = {str(tag).lower() for tag in asset.get("tags", [])}
        is_enrichment = bool(tags & {"空镜", "相关素材", "b-roll", "broll", "enrichment"})
        for concept_id in asset.get("concept_ids", []):
            assets_by_concept[concept_id][media_type] += 1
            if media_type == "video" and is_enrichment:
                assets_by_concept[concept_id]["enrichment_video"] += 1

    concept_rows = []
    matched_aliases = set()
    for concept in catalog["concepts"]:
        aliases = sorted({str(a).strip() for a in concept.get("aliases", []) if str(a).strip()}, key=len, reverse=True)
        alias_pattern = re.compile("|".join(re.escape(alias) for alias in aliases)) if aliases else None
        doc_count = 0
        occurrences = 0
        per_script_occurrences = []
        examples = []
        for record in records:
            hits = len(alias_pattern.findall(record["text"])) if alias_pattern else 0
            if hits:
                doc_count += 1
                occurrences += hits
                per_script_occurrences.append(hits)
                matched_aliases.update(alias for alias in aliases if alias in record["text"])
                if len(examples) < 3:
                    examples.append(f'{record["source"]}#{record["seq"]}')
        counts = assets_by_concept[concept["concept_id"]]
        concept_rows.append(
            {
                "concept_id": concept["concept_id"],
                "label": concept["label"],
                "aliases": aliases,
                "script_count": doc_count,
                "occurrences": occurrences,
                "max_occurrences_in_one_script": max(per_script_occurrences, default=0),
                **counts,
                "examples": examples,
            }
        )
    concept_rows.sort(key=lambda row: (-row["script_count"], -row["occurrences"], row["concept_id"]))
    return catalog, concept_rows, matched_aliases


def token_stats(records, matched_aliases):
    occurrence = collections.Counter()
    documents = collections.Counter()
    examples = collections.defaultdict(list)
    for record in records:
        seen = set()
        for word, flag in pseg.cut(record["text"]):
            word = word.strip()
            if (
                len(word) < 2
                or word in STOPWORDS
                or word in matched_aliases
                or word.isdigit()
                or flag not in {"n", "nr", "ns", "nt", "nz", "vn", "v", "a"}
            ):
                continue
            occurrence[word] += 1
            seen.add(word)
            if len(examples[word]) < 3:
                examples[word].append(f'{record["source"]}#{record["seq"]}')
        documents.update(seen)
    return [
        {"token": token, "script_count": documents[token], "occurrences": count, "examples": examples[token]}
        for token, count in occurrence.most_common()
        if documents[token] >= 3
    ]


records = load_records()
catalog, concept_rows, matched_aliases = catalog_stats(records)
tokens = token_stats(records, matched_aliases)

lengths = sorted(len(record["text"]) for record in records)
source_counts = collections.Counter(record["source"] for record in records)
account_counts = collections.Counter(record["account"] for record in records)
category_counts = collections.Counter(
    str(record["metadata"].get("文章类型") or "未标注") for record in records if record["source_type"] == "xlsx"
)
hash_groups = collections.defaultdict(list)
for record in records:
    hash_groups[normalize(record["text"])].append(record["record_id"])
exact_duplicate_groups = [group for group in hash_groups.values() if len(group) > 1]

rapid_examples = []
ordered_examples = []
rapid_total = 0
ordered_total = 0
rapid_script_count = 0
ordered_script_count = 0
for record in records:
    rapid = RAPID_LIST_RE.findall(record["text"])
    ordered = ORDERED_ITEM_RE.findall(record["text"])
    rapid_total += len(rapid)
    ordered_total += len(ordered)
    rapid_script_count += bool(rapid)
    ordered_script_count += len(ordered) >= 2
    if rapid and len(rapid_examples) < 30:
        rapid_examples.append({"record_id": record["record_id"], "source": record["source"], "seq": record["seq"], "matches": rapid[:3]})
    if len(ordered) >= 2 and len(ordered_examples) < 30:
        ordered_examples.append({"record_id": record["record_id"], "source": record["source"], "seq": record["seq"], "matches": ordered[:12]})

summary = {
    "record_count": len(records),
    "source_counts": dict(source_counts),
    "account_counts": dict(account_counts),
    "category_counts": dict(category_counts),
    "characters": sum(lengths),
    "length_min": lengths[0] if lengths else 0,
    "length_median": lengths[len(lengths) // 2] if lengths else 0,
    "length_p75": lengths[int(len(lengths) * 0.75)] if lengths else 0,
    "length_max": lengths[-1] if lengths else 0,
    "exact_duplicate_groups": exact_duplicate_groups,
    "rapid_list_match_count": rapid_total,
    "rapid_list_script_count": rapid_script_count,
    "ordered_item_match_count": ordered_total,
    "ordered_item_script_count": ordered_script_count,
    "catalog": {
        "concept_count": len(catalog["concepts"]),
        "asset_count": len(catalog["assets"]),
        "image_count": sum(asset["media_type"] == "image" for asset in catalog["assets"]),
        "video_count": sum(asset["media_type"] == "video" for asset in catalog["assets"]),
        "enrichment_video_count": sum(
            asset["media_type"] == "video"
            and bool({str(tag).lower() for tag in asset.get("tags", [])} & {"空镜", "相关素材", "b-roll", "broll", "enrichment"})
            for asset in catalog["assets"]
        ),
    },
}

# Approximate the concept needed immediately after each ~30-second cloud-video seam.
# At ordinary Mandarin narration speed, 120 Chinese characters is a useful planning estimate.
seam_concepts = collections.Counter()
seam_points = 0
seam_unmatched = 0
concept_patterns = []
for concept in catalog["concepts"]:
    for alias in sorted({str(a).strip() for a in concept.get("aliases", []) if str(a).strip()}, key=len, reverse=True):
        concept_patterns.append((alias, concept["concept_id"], concept["label"]))
for record in records:
    text = record["text"]
    for boundary in range(120, len(text), 120):
        seam_points += 1
        window = text[boundary : boundary + 100]
        matches = []
        for alias, concept_id, label in concept_patterns:
            index = window.find(alias)
            if index >= 0:
                matches.append((index, -len(alias), concept_id, label, alias))
        if matches:
            _, _, concept_id, label, _ = min(matches)
            seam_concepts[(concept_id, label)] += 1
        else:
            seam_unmatched += 1
summary["estimated_seams_120_chars"] = seam_points
summary["estimated_seams_unmatched"] = seam_unmatched
summary["estimated_seams_matched"] = seam_points - seam_unmatched
seam_rows = [
    {"concept_id": concept_id, "label": label, "estimated_seams": count}
    for (concept_id, label), count in seam_concepts.most_common()
]

account_profiles = []
for account, account_records_iter in sorted(
    ((account, [record for record in records if record["account"] == account]) for account in account_counts),
    key=lambda item: (-len(item[1]), item[0]),
):
    account_lengths = sorted(len(record["text"]) for record in account_records_iter)
    account_profiles.append(
        {
            "account": account,
            "script_count": len(account_records_iter),
            "characters": sum(account_lengths),
            "median_length": account_lengths[len(account_lengths) // 2],
            "rapid_list_scripts": sum(bool(RAPID_LIST_RE.search(record["text"])) for record in account_records_iter),
            "ordered_list_scripts": sum(len(ORDERED_ITEM_RE.findall(record["text"])) >= 2 for record in account_records_iter),
        }
    )

(ROOT / "corpus.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
(ROOT / "analysis.json").write_text(
    json.dumps(
        {
            "summary": summary,
            "concept_rows": concept_rows,
            "uncovered_tokens": tokens[:300],
            "rapid_examples": rapid_examples,
            "ordered_examples": ordered_examples,
            "account_profiles": account_profiles,
            "seam_rows": seam_rows,
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)
print(json.dumps(summary, ensure_ascii=False, indent=2))
