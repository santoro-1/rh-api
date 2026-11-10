from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from docx import Document


CONCEPTS = {
    "meal.breakfast": ["早餐", "早饭"],
    "meal.lunch": ["午餐", "中午"],
    "meal.dinner": ["晚餐", "晚饭"],
    "nutrition.protein": ["蛋白质", "优质蛋白", "高蛋白"],
    "nutrition.carbohydrate": ["碳水", "主食"],
    "nutrition.dietary_fiber": ["膳食纤维", "纤维"],
    "nutrition.fat": ["脂肪", "优质脂肪", "反式脂肪"],
    "food.egg": ["鸡蛋", "蛋黄", "鸡蛋白"],
    "drink.soy_milk": ["豆浆", "黄豆浆", "黑豆浆"],
    "dairy.milk": ["牛奶"],
    "dairy.yogurt": ["酸奶"],
    "staple.corn": ["玉米"],
    "staple.sweet_potato": ["红薯", "地瓜"],
    "staple.oat": ["燕麦", "燕麦片"],
    "staple.yam": ["山药"],
    "staple.brown_rice": ["糙米"],
    "staple.wholegrain": ["粗粮", "杂粮", "全谷物"],
    "staple.wholewheat_bread": ["全麦面包", "全麦"],
    "food.vegetable": ["蔬菜", "青菜", "绿叶菜"],
    "food.fruit": ["水果"],
    "drink.fruit_juice": ["果汁", "橙汁"],
    "food.nuts": ["坚果", "核桃", "巴旦木", "花生", "瓜子"],
    "food.avocado": ["牛油果"],
    "food.beef": ["牛肉"],
    "food.chicken": ["鸡胸肉", "鸡腿肉", "鸡肉", "禽肉"],
    "food.fish_shrimp": ["鱼虾", "鱼肉", "虾"],
    "food.tofu": ["豆腐", "豆制品"],
    "food.lean_meat": ["瘦肉", "猪里脊"],
    "food.winter_melon": ["冬瓜"],
    "food.cucumber": ["黄瓜"],
    "food.cake": ["蛋糕"],
    "food.fried": ["油炸", "油条", "炸鸡", "薯条"],
    "food.snack": ["饼干", "薯片", "零食"],
    "health.metabolism": ["代谢", "基础代谢"],
    "health.belly": ["肚子", "小腹", "腰腹", "大肚子"],
    "health.visceral_fat": ["内脏脂肪"],
    "health.body_fat": ["体脂", "体脂率"],
    "habit.water": ["温水", "温白开", "喝水", "白开水"],
    "habit.sleep": ["睡眠", "熬夜", "早睡", "晚睡"],
    "habit.walk": ["快走", "走路", "多走", "户外活动"],
    "habit.exercise": ["运动", "有氧", "跳绳", "跳操"],
    "habit.sitting": ["久坐", "坐一个小时"],
    "habit.eating_order": ["先吃菜", "先吃蔬菜", "最后吃主食"],
    "goal.weight_loss": ["减脂", "减肥", "掉秤", "瘦下来"],
}


def main() -> int:
    doc_path = Path(sys.argv[1])
    document = Document(doc_path)
    paragraphs = [paragraph.text.strip() for paragraph in document.paragraphs]
    scripts: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    body: list[str] = []
    for text in paragraphs:
        if "｜序号：" in text:
            if current is not None:
                current["text"] = "\n".join(body)
                scripts.append(current)
            current = {"header": text}
            body = []
        elif current is not None and text:
            body.append(text)
    if current is not None:
        current["text"] = "\n".join(body)
        scripts.append(current)

    stats = []
    for concept_id, aliases in CONCEPTS.items():
        script_hits = []
        total = 0
        alias_counts = {}
        for alias in aliases:
            count = sum(script["text"].count(alias) for script in scripts)
            if count:
                alias_counts[alias] = count
                total += count
        if total:
            for index, script in enumerate(scripts, 1):
                if any(alias in script["text"] for alias in aliases):
                    script_hits.append(index)
            stats.append(
                {
                    "concept_id": concept_id,
                    "aliases": alias_counts,
                    "occurrences": total,
                    "script_count": len(script_hits),
                    "script_indices": script_hits,
                }
            )
    stats.sort(key=lambda item: (-item["script_count"], -item["occurrences"], item["concept_id"]))
    result = {
        "docx": str(doc_path),
        "paragraph_count": len(paragraphs),
        "script_count": len(scripts),
        "total_script_characters": sum(len(script["text"]) for script in scripts),
        "concept_stats": stats,
        "headers": [] if "--stats-only" in sys.argv else [script["header"] for script in scripts],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
