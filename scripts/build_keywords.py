"""词库扩充：主体 / 主体画像 / 成员归属 / 厂牌 / 别名。

为什么用脚本而不是手改 JSON：
- 150 个主体 + 400 个成员 + 别名，手改必错
- 脚本可重复运行，改动有据可查

权重四档（决定检索强度与圈层判定）：
  S 1.0  当打团体、顶流 solo，饭圈二创密集
  A 0.6  有稳定二创，量级次一档
  B 0.3  综艺跨界、新团、偶发
  C 0.1  大众情怀型，已脱离追星日常 → 默认不检索，且不算「圈内」

用法：python scripts/build_keywords.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / "CoreAssets" / "keywords.json"

# ---------------- 主体 ----------------
# (名字, tier, region, type, org)

S = [
    # 内娱团体
    ("时代少年团", "内娱", "男团", "时代峰峻"),
    ("登陆少年", "内娱", "男团", "时代峰峻"),
    ("TF家族三代", "内娱", "男团", "时代峰峻"),
    ("TF家族四代", "内娱", "男团", "时代峰峻"),
    ("十个勤天", "内娱", "男团", ""),
    ("R.E.D组合", "内娱", "女团", "染色体"),
    ("NAME女团", "内娱", "女团", ""),
    ("SNH48", "内娱", "女团", ""),
    ("A2O LITE", "内娱", "女团", "A2O"),
    ("A2O MAY", "内娱", "女团", "A2O"),
    ("A2O SOUL", "内娱", "女团", "A2O"),
    ("南国少年团", "内娱", "男团", ""),
    # 内娱 solo
    ("肖战", "内娱", "solo", ""),
    ("王一博", "内娱", "solo", ""),
    ("易烊千玺", "内娱", "solo", ""),
    ("王俊凯", "内娱", "solo", ""),
    ("王源", "内娱", "solo", ""),
    ("蔡徐坤", "内娱", "solo", ""),
    ("张艺兴", "内娱", "solo", "染色体"),
    ("华晨宇", "内娱", "solo", ""),
    ("周深", "内娱", "solo", ""),
    ("单依纯", "内娱", "solo", ""),
    ("刘雨昕", "内娱", "solo", ""),
    ("虞书欣", "内娱", "solo", ""),
    ("鞠婧祎", "内娱", "solo", ""),
    # 韩娱团体
    ("aespa", "韩娱", "女团", "SM"),
    ("IVE", "韩娱", "女团", "STARSHIP"),
    ("NewJeans", "韩娱", "女团", "ADOR"),
    ("LE SSERAFIM", "韩娱", "女团", "HYBE"),
    ("I-DLE", "韩娱", "女团", ""),
    ("BABYMONSTER", "韩娱", "女团", "YG"),
    ("NMIXX", "韩娱", "女团", "JYP"),
    ("ITZY", "韩娱", "女团", "JYP"),
    ("ILLIT", "韩娱", "女团", "HYBE"),
    ("TWICE", "韩娱", "女团", "JYP"),
    ("BLACKPINK", "韩娱", "女团", "YG"),
    ("BTS", "韩娱", "男团", "HYBE"),
    ("Stray Kids", "韩娱", "男团", "JYP"),
    ("SEVENTEEN", "韩娱", "男团", "PLEDIS"),
    ("ENHYPEN", "韩娱", "男团", "HYBE"),
    ("TXT", "韩娱", "男团", "HYBE"),
    ("RIIZE", "韩娱", "男团", "SM"),
    ("BOYNEXTDOOR", "韩娱", "男团", "HYBE"),
    ("NCT DREAM", "韩娱", "男团", "SM"),
    ("NCT WISH", "韩娱", "男团", "SM"),
    ("ATEEZ", "韩娱", "男团", "KQ"),
    ("TWS", "韩娱", "男团", "PLEDIS"),
    ("ZEROBASEONE", "韩娱", "男团", "WAKEONE"),
    ("CORTIS", "韩娱", "男团", "HYBE"),
    # 厂牌 / 公司
    ("时代峰峻", "内娱", "厂牌", ""),
    ("乐华娱乐", "内娱", "厂牌", ""),
    ("哇唧唧哇", "内娱", "厂牌", ""),
    ("染色体", "内娱", "厂牌", ""),
    ("天浩盛世", "内娱", "厂牌", ""),
    ("A2O", "内娱", "厂牌", ""),
    ("SM", "韩娱", "厂牌", ""),
    ("YG", "韩娱", "厂牌", ""),
    ("JYP", "韩娱", "厂牌", ""),
    ("HYBE", "韩娱", "厂牌", ""),
    ("ADOR", "韩娱", "厂牌", ""),
    ("PLEDIS", "韩娱", "厂牌", ""),
]

A = [
    ("TFBOYS", "内娱", "男团", "时代峰峻"),
    ("BOYHOOD", "内娱", "男团", ""),
    ("ONER", "内娱", "男团", ""),
    ("SING女团", "内娱", "女团", ""),
    ("GNZ48", "内娱", "女团", ""),
    ("BEJ48", "内娱", "女团", ""),
    ("MODYSSEY", "内娱", "男团", ""),
    ("AND2BLE", "内娱", "男团", "乐华娱乐"),
    ("RaceBace", "内娱", "男团", ""),
    ("AM8IC", "内娱", "男团", ""),
    ("RESCENE", "韩娱", "女团", ""),
    ("MEOVV", "韩娱", "女团", ""),
    ("KATSEYE", "韩娱", "女团", "HYBE"),
    ("Hearts2Hearts", "韩娱", "女团", "SM"),
    ("FIFTY FIFTY", "韩娱", "女团", ""),
    ("薛之谦", "华语", "solo", ""),
    ("汪苏泷", "华语", "solo", ""),
    ("毛不易", "内娱", "solo", ""),
    ("希林娜依高", "内娱", "solo", ""),
    ("陈卓璇", "内娱", "solo", ""),
    ("张靓颖", "华语", "solo", ""),
    ("邓紫棋", "华语", "solo", ""),
    ("孙燕姿", "华语", "solo", ""),
    ("刘宇宁", "内娱", "solo", ""),
    ("黄子弘凡", "内娱", "solo", ""),
    ("李宇春", "内娱", "solo", ""),
    ("张碧晨", "内娱", "solo", ""),
    ("五月天", "华语", "乐队", ""),
    ("GAI", "内娱", "rapper", ""),
    ("艾热", "内娱", "rapper", ""),
    ("法老", "内娱", "rapper", ""),
    ("乃万", "内娱", "rapper", ""),
    ("杨和苏", "内娱", "rapper", ""),
    ("马思唯", "内娱", "rapper", ""),
    ("刘聪", "内娱", "rapper", ""),
    ("万妮达", "内娱", "rapper", ""),
    ("赵露思", "内娱", "演员", ""),
    ("王鹤棣", "内娱", "演员", ""),
    ("白鹿", "内娱", "演员", ""),
    ("杨紫", "内娱", "演员", ""),
    ("迪丽热巴", "内娱", "演员", ""),
    ("成毅", "内娱", "演员", ""),
    ("张凌赫", "内娱", "演员", ""),
    ("田曦薇", "内娱", "演员", ""),
    ("陈哲远", "内娱", "演员", ""),
    ("吴磊", "内娱", "演员", ""),
    ("赵今麦", "内娱", "演员", ""),
    ("刘昊然", "内娱", "演员", ""),
    ("谭松韵", "内娱", "演员", ""),
    ("李现", "内娱", "演员", ""),
]

B = [
    # 有独立活动的团体成员
    ("宋亚轩", "内娱", "solo", "时代少年团"),
    ("刘耀文", "内娱", "solo", "时代少年团"),
    ("丁程鑫", "内娱", "solo", "时代少年团"),
    ("马嘉祺", "内娱", "solo", "时代少年团"),
    ("张真源", "内娱", "solo", "时代少年团"),
    ("严浩翔", "内娱", "solo", "时代少年团"),
    ("贺峻霖", "内娱", "solo", "时代少年团"),
    # 新生 / 跨界
    ("D.A.O", "内娱", "男团", ""),
    ("ROS共和少年", "内娱", "男团", ""),
    ("杨幂", "内娱", "演员", ""),
    ("赵丽颖", "内娱", "演员", ""),
    ("刘诗诗", "内娱", "演员", ""),
    ("唐嫣", "内娱", "演员", ""),
    ("关晓彤", "内娱", "演员", ""),
    ("欧阳娜娜", "内娱", "演员", ""),
]

C = [
    ("周杰伦", "华语", "solo", ""),
    ("刘德华", "华语", "solo", ""),
    ("张学友", "华语", "solo", ""),
    ("郭富城", "华语", "solo", ""),
    ("黎明", "华语", "solo", ""),
    ("周星驰", "华语", "演员", ""),
    ("汪峰", "华语", "solo", ""),
    ("王菲", "华语", "solo", ""),
    ("陈奕迅", "华语", "solo", ""),
    ("林俊杰", "华语", "solo", ""),
    ("罗大佑", "华语", "solo", ""),
    ("李宗盛", "华语", "solo", ""),
]

WEIGHT = {"S": 1.0, "A": 0.6, "B": 0.3, "C": 0.1}

# ---------------- 成员归属（只在命中时做归因，不单独检索） ----------------
MEMBERS = {
    "时代少年团": ["马嘉祺", "丁程鑫", "宋亚轩", "刘耀文", "张真源", "严浩翔", "贺峻霖"],
    "登陆少年": ["朱志鑫", "张泽禹", "张极", "左航", "苏新皓"],
    "TF家族三代": ["张峻豪", "余宇涵", "穆祉丞", "邓佳鑫", "童禹坤"],
    "TF家族四代": ["张桂源", "杨博文", "左奇函", "张函瑞", "陈浚铭", "王橹杰"],
    "十个勤天": ["蒋敦豪", "鹭卓", "李耕耘", "李昊", "赵一博", "卓沅", "赵小童", "何浩铭", "陈少熙", "王一珩"],
    "R.E.D组合": ["周洛汐", "李芊语", "刘婷秀", "彭湘蕊", "李雨林", "胡静怡", "尹子璐"],
    "aespa": ["Karina", "Giselle", "Winter", "Ningning"],
    "IVE": ["安宥真", "Gaeul", "Rei", "张元英", "Liz", "李瑞"],
    "NewJeans": ["Minji", "Hanni", "Danielle", "Haerin", "Hyein"],
    "LE SSERAFIM": ["金采源", "宫脇咲良", "许允真", "Kazuha", "洪恩采"],
    "I-DLE": ["美延", "Minnie", "小娟", "雨琦", "舒华"],
    "BABYMONSTER": ["Ruka", "Pharita", "Asa", "Ahyeon", "Rami", "Rora", "Chiquita"],
    "ILLIT": ["Yunah", "Minju", "Moka", "Wonhee", "Iroha"],
    "RIIZE": ["Shotaro", "Eunseok", "Sungchan", "Wonbin", "Seunghan", "Sohee"],
    "BOYNEXTDOOR": ["Sungho", "Riwoo", "Jaehyun", "Taesan", "Leehan", "Woonhak"],
    "TWS": ["Shinyu", "Dohoon", "Youngjae", "Hanjin", "Jihoon", "Kyungmin"],
    "ENHYPEN": ["Jungwon", "Heeseung", "Jay", "Jake", "Sunghoon", "Sunoo", "Ni-ki"],
    "TXT": ["Yeonjun", "Soobin", "Beomgyu", "Taehyun", "Huening Kai"],
    "BTS": ["RM", "Jin", "SUGA", "j-hope", "Jimin", "Jungkook"],
    "Stray Kids": ["Bang Chan", "Lee Know", "Changbin", "Hyunjin", "Han", "Felix", "Seungmin", "I.N"],
    "SEVENTEEN": [
        "S.Coups", "Jeonghan", "Joshua", "Jun", "Hoshi", "Wonwoo", "Woozi",
        "DK", "Mingyu", "The8", "Seungkwan", "Vernon", "Dino",
    ],
    "NCT DREAM": ["Mark", "Renjun", "Jeno", "Haechan", "Jaemin", "Chenle", "Jisung"],
    "ITZY": ["Yeji", "Lia", "Ryujin", "Chaeryeong", "Yuna"],
    "NMIXX": ["Lily", "Haewon", "Sullyoon", "Bae", "Jiwoo", "Kyujin"],
    "TWICE": ["Nayeon", "Jeongyeon", "Momo", "Sana", "Jihyo", "Mina", "Dahyun", "Chaeyoung", "Tzuyu"],
    "BLACKPINK": ["Jisoo", "Jennie", "Rosé", "Lisa"],
    "ZEROBASEONE": ["章昊", "成韩彬", "石马修", "Ricky", "朴乾煜", "金泰来", "金奎彬", "金志雄", "韩维辰"],
}

# ---------------- 厂牌 → 旗下 ----------------
LABELS = {
    "时代峰峻": ["TFBOYS", "时代少年团", "登陆少年", "TF家族三代", "TF家族四代"],
    "乐华娱乐": ["AND2BLE"],
    "哇唧唧哇": [],
    "染色体": ["R.E.D组合", "张艺兴"],
    "A2O": ["A2O LITE", "A2O MAY", "A2O SOUL"],
    "SM": ["aespa", "RIIZE", "NCT DREAM", "NCT WISH", "Hearts2Hearts"],
    "YG": ["BABYMONSTER", "BLACKPINK"],
    "JYP": ["NMIXX", "ITZY", "TWICE", "Stray Kids"],
    "HYBE": ["BTS", "SEVENTEEN", "ENHYPEN", "TXT", "BOYNEXTDOOR", "LE SSERAFIM", "ILLIT", "KATSEYE", "CORTIS"],
    "ADOR": ["NewJeans"],
    "PLEDIS": ["SEVENTEEN", "TWS"],
}

# ---------------- 别名 ----------------
ALIASES = {
    "时代少年团": ["TNT", "时团", "十八楼", "时代少年"],
    "登陆少年": ["TOP登陆少年", "登陆少年组合", "T.O.P"],
    "TF家族三代": ["TF_ING", "三代"],
    "TF家族四代": ["四代"],
    "R.E.D组合": ["RED组合", "R.E.D", "小红包"],
    "NAME女团": ["NAME"],
    "SNH48": ["SNH", "塞纳河"],
    "A2O LITE": ["A2OLITE", "A2O"],
    "A2O MAY": ["A2OMAY"],
    "A2O SOUL": ["A2OSOUL"],
    "染色体": ["染色体娱乐"],
    "时代峰峻": ["sdfj", "十八楼"],
    "乐华娱乐": ["乐华", "YH"],
    "哇唧唧哇": ["哇唧"],
    "aespa": ["艾斯帕"],
    "NewJeans": ["NJZ", "鲸"],
    "LE SSERAFIM": ["LESSERAFIM", "炽"],
    "I-DLE": ["(G)I-DLE", "GIDLE", "女娃", "孩子们"],
    "BABYMONSTER": ["宝怪", "BM"],
    "BLACKPINK": ["BP", "粉墨"],
    "BTS": ["防弹少年团", "防弹"],
    "Stray Kids": ["SKZ", "迷"],
    "SEVENTEEN": ["SVT", "小十七", "十七"],
    "ENHYPEN": ["符"],
    "TXT": ["TOMORROW X TOGETHER", "档"],
    "BOYNEXTDOOR": ["门童", "BND"],
    "NCT DREAM": ["梦队", "NCTdream"],
    "ZEROBASEONE": ["ZB1", "零基一"],
    "IVE": ["芙"],
    "ILLIT": ["礼"],
    "肖战": ["小站", "战战"],
    "王一博": ["一博", "耶啵"],
    "易烊千玺": ["千玺", "四字"],
    "王俊凯": ["小凯"],
    "王源": ["源源"],
    "蔡徐坤": ["坤坤", "IKUN"],
    "张艺兴": ["LAY", "艺兴"],
    "周深": ["深深"],
    "单依纯": ["依依"],
    "刘宇宁": ["宁哥"],
    "虞书欣": ["欣欣"],
    "鞠婧祎": ["鞠婧祎", "小鞠"],
    "薛之谦": ["谦谦"],
    "邓紫棋": ["GEM", "邓紫棋"],
    "孙燕姿": ["燕姿"],
    "GAI": ["周延"],
    "黄子弘凡": ["黄子"],
    "十个勤天": ["种地吧", "十个晴天"],
}


def main() -> int:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    anchor = data.setdefault("锚点层", {})

    subjects: list[str] = []
    profile: dict[str, dict] = {}
    for tier, rows in (("S", S), ("A", A), ("B", B), ("C", C)):
        for name, region, etype, org in rows:
            if name in profile:
                continue
            subjects.append(name)
            profile[name] = {
                "tier": tier,
                "weight": WEIGHT[tier],
                "region": region,
                "type": etype,
                "org": org,
            }

    anchor["主体"] = subjects
    anchor["主体画像"] = profile
    anchor["成员归属"] = MEMBERS
    anchor["厂牌"] = {
        name: {"tier": profile.get(name, {}).get("tier", "S"), "artists": artists}
        for name, artists in LABELS.items()
    }

    aliases = data.setdefault("别名映射", {})
    for name, extra in ALIASES.items():
        current = aliases.get(name, [])
        for item in extra:
            if item and item != name and item not in current:
                current.append(item)
        aliases[name] = current

    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    counts: dict[str, int] = {}
    for info in profile.values():
        counts[info["tier"]] = counts.get(info["tier"], 0) + 1
    print(f"主体 {len(subjects)} 个：{counts}")
    print(f"成员归属 {sum(len(v) for v in MEMBERS.values())} 人 / {len(MEMBERS)} 团")
    print(f"厂牌 {len(LABELS)} 个，别名 {len(aliases)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
