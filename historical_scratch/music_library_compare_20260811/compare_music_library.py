import json
import re
from pathlib import Path


MANIFEST = Path(r"D:\JYD\JianyingRenderServer-windows-x64\data\libraries\audio_library\manifest\music_profiles.v1.json")
FILES_DIR = Path(r"D:\JYD\JianyingRenderServer-windows-x64\data\libraries\audio_library\files")

TARGETS = [
    ("鸡汤人设", "你赠我独有段戒感", "你赠我独有段戒感"),
    ("鸡汤人设", "III (Find Yourself)", "III (Find Yourself)"),
    ("鸡汤人设", "晴（宿命感BGM）", "晴（宿命感BGM）"),
    ("鸡汤人设", "Apollo's Triumph（抖音原版）", "Apollo's Triumph (抖音原版)"),
    ("鸡汤人设", "经济舱（钢琴版）", "经济舱（钢琴版）"),
    ("鸡汤人设", "Take me hand", "Take me hand"),
    ("鸡汤人设", "可不可以（音乐）（Cover张紫豪）（剪辑版2）", "可不可以（音乐）（Cover 张紫豪）（剪辑版2）"),
    ("鸡汤人设", "Letting Go（Cover蔡健雅）", "Letting Go（Cover 蔡健雅）"),
    ("鸡汤人设", "偏爱（钢琴版）（剪辑版）", "偏爱（钢琴版）（剪辑版）"),
    ("鸡汤人设", "@Knight1.创作的原声", "@Knight1.创作的原声"),
    ("鸡汤人设", "还是分开", "还是分开"),
    ("鸡汤人设", "复刻回忆（无掌声版）", "复刻回忆（无掌声版）"),
    ("鸡汤人设", "做最好的自己", "做最好的自己"),
    ("鸡汤人设", "@周佳亮·马头琴（冈仁波齐转山）创作的原声", "@周佳亮·马头琴（冈仁波齐转山）创作的原声"),
    ("鸡汤人设", "越界（剪辑版）", "越界（剪辑版）"),
    ("鸡汤人设", "世界赠与我的（Cover王菲）", "世界赠与我的（Cover 王菲）"),
    ("鸡汤人设", "焚情（纯音乐）（Cover黄凯芹）", "焚情（纯音乐）（Cover 黄凯芹）"),
    ("鸡汤人设", "独角戏（Cover许茹芸）", "独角戏（Cover 许茹芸）"),
    ("鸡汤人设", "落在杯沿的光", "落在杯沿的光"),
    ("鸡汤人设", "起风了（间奏加长版）", "起风了（间奏加长版）"),
    ("鸡汤人设", "花海（钢琴版）（改编版）", "花海（钢琴版）（改编版）"),
    ("鸡汤人设", "远去的列车", "远去的列车"),
    ("鸡汤人设", "请你好好爱自己（剪辑版）", "请你好好爱自己（剪辑版）"),
    ("干货", "妈妈的温柔", "妈妈的温柔"),
    ("干货", "国风轻快阳光俏皮", "国风 轻快 阳光 俏皮"),
    ("干货", "@艺术家的妈妈（老歌分享）创作的原声", "@艺术家的妈妈（老歌分享）创作的原声"),
    ("干货", "Happy People", "Happy People"),
    ("干货", "爱江山更爱美人（Cover小阿枫）", "爱江山更爱美人（Cover 小阿枫）"),
    ("干货", "@唐尼（上上钢琴）创作的原声", "@唐尼（上上钢琴）创作的原声"),
    ("干货", "美丽的神话钢琴演奏", "美丽的神话 钢琴演奏"),
    ("干货", "美好的一天（古筝版）", "美好的一天（古筝版）"),
    ("干货", "欢乐节奏·治愈…（清风至，03:38）", None),
    ("访谈", "萱草花哼唱版（电影《你好，李焕英》主题曲）", "萱草花 哼唱版（电影《你好，李焕英》主题曲）"),
    ("访谈", "是妈妈是女儿（Live）", "是妈妈是女儿 (Live)"),
    ("访谈", "妈妈陪我长大了", "妈妈陪我长大了"),
    ("访谈", "愿妈妈平安健康（剪辑版）", "愿妈妈平安健康（剪辑版）"),
    ("访谈", "亲爱的你啊", "亲爱的你啊"),
    ("访谈", "时光琥珀", "时光琥珀"),
    ("访谈", "致伟大的妈妈", "致伟大的妈妈"),
    ("减肥打卡", "Dehors（中法合唱版）", "Dehors（中法合唱版）"),
    ("减肥打卡", "Tikolin Purre（Explicit）", "Tikolin Purre（Explicit）"),
    ("减肥打卡", "喜剧之王（剪辑版）", "喜剧之王（剪辑版）"),
]


def normalize(value: str) -> str:
    value = value.casefold()
    value = value.replace("（", "(").replace("）", ")")
    value = re.sub(r"[\s_·.\-—－'’]", "", value)
    return value


payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
profiles = payload["profiles"]
by_name = {normalize(profile["name"]): profile for profile in profiles}
files = list(FILES_DIR.glob("*.mp3"))

results = []
matched_identities = set()
for category, sheet_name, local_name in TARGETS:
    profile = by_name.get(normalize(local_name)) if local_name else None
    identity = profile["identity"] if profile else None
    music_id = identity.split(":", 1)[1] if identity else None
    physical = next((path for path in files if music_id and path.stem.endswith("_" + music_id)), None)
    status = "有" if profile and physical else "没有"
    if identity:
        matched_identities.add(identity)
    results.append(
        {
            "category": category,
            "sheet_name": sheet_name,
            "status": status,
            "manifest_name": profile["name"] if profile else None,
            "identity": identity,
            "file": physical.name if physical else None,
            "enabled": profile.get("enabled") if profile else None,
        }
    )

extras = [
    {"name": profile["name"], "identity": profile["identity"]}
    for profile in profiles
    if profile["identity"] not in matched_identities
]

print(json.dumps({
    "manifest_count": len(profiles),
    "physical_mp3_count": len(files),
    "target_count": len(results),
    "present_count": sum(row["status"] == "有" for row in results),
    "missing_count": sum(row["status"] == "没有" for row in results),
    "results": results,
    "local_extras": extras,
}, ensure_ascii=False, indent=2))
