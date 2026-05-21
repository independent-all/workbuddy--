"""
Phase 1: 数据管线
─────────────────
功能：
  1. 从原始文本/模拟输入中提取结构化活动属性
  2. 通过高德 Geocoding API（或 Mock）获取经纬度
  3. 判定地址精准度 (is_precise)
  4. 基于关键词自动编码色调标签 (cold/warm/neutral)
  5. 提取活动精华文本 (essence_text)

输出：parsed_events.json — 包含完整属性的活动列表
"""

import json
import re
from datetime import datetime
from pathlib import Path

import requests

from config import (
    ORIGIN_LAT, ORIGIN_LNG,
    AMAP_KEY,
    GEOCODE_URL,
    REQUEST_TIMEOUT,
    COLD_KEYWORDS,
    WARM_KEYWORDS,
    DATA_DIR,
)

# ─────────────────────────────────────────────────────────
# 1. 原始文本解析器：从 raw_text + 结构化字段中组装 Activity
# ─────────────────────────────────────────────────────────


def parse_activity(raw: dict, event_id: str) -> dict:
    """
    将一条原始活动记录解析为标准 Activity 结构。

    raw: 来自 sample_data.py 的 SAMPLE_EVENTS 元素
    event_id: 活动唯一标识
    """
    # 解析时间
    date_part = raw["date_str"]  # "2026-05-22"
    time_parts = raw["time_range"].split("-")  # ["14:00", "17:00"]

    start_time = f"{date_part}T{time_parts[0]}:00+08:00"
    end_time = f"{date_part}T{time_parts[1]}:00+08:00"

    # 调试：验证时间格式
    try:
        datetime.fromisoformat(start_time)
        datetime.fromisoformat(end_time)
    except ValueError as e:
        print(f"  [WARN] 时间解析异常 {event_id}: {e}")

    activity = {
        "type": "activity",
        "id": event_id,
        "name": raw["title"],
        "organizer": raw["organizer"],
        "start_time": start_time,
        "end_time": end_time,
        "fee": raw["fee"],
        "fee_unit": "元/人",
        "participant_count": raw["participants"],
        "address_raw": raw["address"],
        # 以下字段由后续模块填充
        "lat": 0.0,
        "lng": 0.0,
        "is_precise": False,
        "ima_source_url": raw["ima_url"],
        "tone_tag": "neutral",
        "tone_keywords_hit": [],
        "essence_text": "",
        "tags": [],
        "notes": "",
        # v1.1 新增字段：卡片高密度信息
        "gender_ratio": raw.get("gender_ratio", ""),
        "threshold": raw.get("threshold", ""),
        "core_activities": raw.get("core_activities", ""),
        # v1.2 新增字段：封面图 URL（海报卡片背景）
        "cover_image": raw.get("cover_image", ""),
        # v1.3 新增字段：无损全文 + 图片数组（供A区Detail面板渲染）
        "full_raw_text": raw.get("full_raw_text", raw.get("raw_text", "")),
        "images": raw.get("images", []),
    }

    return activity


# ─────────────────────────────────────────────────────────
# 2. 地理编码模块：Mock + 真实 API 双模式
# ─────────────────────────────────────────────────────────

# Mock 地址 → 坐标映射表（当 API Key 不可用时使用）
MOCK_GEOCODE_MAP = {
    "朝阳区大望路SOHO现代城A座301":   (39.9042, 116.4703, True),
    "朝阳公园南门附近":                (39.9420, 116.4765, False),
    "朝阳区国贸三期B座56层云酷餐厅":   (39.9087, 116.4605, True),
    "海淀区五道口华清商务会馆B1":      (39.9930, 116.3380, True),
    "朝阳区三里屯太古里Wework":        (39.9327, 116.4551, True),
    "昌平区十三陵水库附近":            (40.2570, 116.2700, False),
    "西城区西单大悦城附近":            (39.9133, 116.3736, False),
    "朝阳区蓝色港湾KTV":               (39.9490, 116.4732, True),
    "朝阳区亮马桥官舍3层":             (39.9499, 116.4620, True),
}


def _is_likely_precise(address: str) -> bool:
    """
    基于地址字符串的启发式精准度判定。
    精确特征：有门牌号、楼层、具体建筑物名
    模糊特征：含"附近"、"周边"、"一带"、"区域" 等词
    """
    fuzzy_markers = ["附近", "周边", "一带", "区域", "旁边", "左右", "附近商圈"]
    for marker in fuzzy_markers:
        if marker in address:
            return False

    # 有门牌号或具体楼层号 → 精确
    if re.search(r"[座号栋]\d+|楼$|\d+层|F$|\d+号", address):
        return True

    # 默认：不精确
    return False


def geocode_amap(address: str, city: str = "北京") -> dict:
    """
    调用高德地理编码 API，返回 {"lat": float, "lng": float, "is_precise": bool}。

    当 API Key 不可用时，自动回退到 Mock 映射表。
    """
    if AMAP_KEY == "YOUR_AMAP_KEY_HERE":
        return _geocode_mock(address)

    try:
        resp = requests.get(
            GEOCODE_URL,
            params={
                "key": AMAP_KEY,
                "address": address,
                "city": city,
                "output": "JSON",
            },
            timeout=REQUEST_TIMEOUT,
        )
        data = resp.json()

        if data.get("status") == "1" and data.get("geocodes"):
            geo = data["geocodes"][0]
            location = geo["location"]  # "116.4703,39.9042"
            lng_str, lat_str = location.split(",")
            lat, lng = float(lat_str), float(lng_str)

            # 高德返回的 level 字段辅助判断精度
            # "门牌号" / "兴趣点" / "道路" / "区县" 等
            level = geo.get("level", "")
            is_precise = level in ("门牌号", "兴趣点", "POI")

            return {"lat": lat, "lng": lng, "is_precise": is_precise}
        else:
            print(f"  [WARN] 高德API返回异常: {data.get('info', 'unknown')}，回退 Mock")
            return _geocode_mock(address)
    except Exception as e:
        print(f"  [WARN] 高德API请求失败: {e}，回退 Mock")
        return _geocode_mock(address)


def _geocode_mock(address: str) -> dict:
    """Mock 地理编码（开发环境下使用）"""
    if address in MOCK_GEOCODE_MAP:
        lat, lng, is_precise = MOCK_GEOCODE_MAP[address]
        return {"lat": lat, "lng": lng, "is_precise": is_precise}

    # 未命中 mock 映射 → 用启发式判定
    print(f"  [WARN] Mock 未覆盖地址: {address}，使用启发式判定")
    return {
        "lat": ORIGIN_LAT,
        "lng": ORIGIN_LNG,
        "is_precise": _is_likely_precise(address),
    }


def enrich_geocode(activity: dict) -> dict:
    """
    为单个活动填充地理坐标与精准度标记。
    修改原 dict 并返回。
    """
    geo = geocode_amap(activity["address_raw"])
    activity["lat"] = round(geo["lat"], 6)
    activity["lng"] = round(geo["lng"], 6)
    activity["is_precise"] = geo["is_precise"]
    return activity


# ─────────────────────────────────────────────────────────
# 3. 色调分类器
# ─────────────────────────────────────────────────────────


def classify_tone(raw_text: str, tags: list = None) -> tuple:
    """
    基于 raw_text 中的关键词命中情况，返回 (tone_tag, keywords_hit)。

    tone_tag ∈ {"cold", "warm", "neutral"}

    规则：
      - 同时命中 cold 和 warm → "neutral"
      - 仅命中 cold       → "cold"
      - 仅命中 warm       → "warm"
      - 均未命中          → "neutral"
    """
    cold_hits = [kw for kw in COLD_KEYWORDS if kw in raw_text]
    warm_hits = [kw for kw in WARM_KEYWORDS if kw in raw_text]

    if cold_hits and warm_hits:
        tone = "neutral"
        all_hits = cold_hits + warm_hits
    elif cold_hits:
        tone = "cold"
        all_hits = cold_hits
    elif warm_hits:
        tone = "warm"
        all_hits = warm_hits
    else:
        tone = "neutral"
        all_hits = []

    return tone, all_hits


def enrich_tone(activity: dict) -> dict:
    """为单个活动填充色调标签与命中关键词"""
    tone, hits = classify_tone(activity.get("essence_text", ""))
    activity["tone_tag"] = tone
    activity["tone_keywords_hit"] = hits
    return activity


# ─────────────────────────────────────────────────────────
# 4. 精华文本提取
# ─────────────────────────────────────────────────────────


def extract_essence(raw: dict) -> str:
    """
    从 raw_text 中提取活动精华描述。
    当前策略：取 raw_text 前 100 字作为摘要，兼顾完整性与简洁性。
    后续可升级为 NLP 摘要模型。
    """
    text = raw.get("raw_text", "")
    if len(text) <= 100:
        return text
    # 截取前 100 字，并在完整句号处截断
    truncated = text[:120]
    last_period = max(truncated.rfind("。"), truncated.rfind("；"))
    if last_period > 50:
        return truncated[: last_period + 1]
    return truncated[:100] + "…"


def enrich_essence(activity: dict, raw: dict) -> dict:
    """填充精华文本"""
    activity["essence_text"] = extract_essence(raw)
    return activity


# ─────────────────────────────────────────────────────────
# 5. 管线主函数
# ─────────────────────────────────────────────────────────


def run_pipeline(raw_events: list) -> list:
    """
    执行完整数据管线：解析 → 地理编码 → 色调分类 → 精华提取

    Args:
        raw_events: SAMPLE_EVENTS 格式的原始活动列表

    Returns:
        list[dict]: 符合 Activity Schema 的活动列表
    """
    print("=" * 60)
    print("Phase 1: 数据管线启动")
    print("=" * 60)

    parsed = []

    for i, raw in enumerate(raw_events):
        event_id = f"evt_202605_{i + 1:03d}"
        print(f"\n[{i + 1}/{len(raw_events)}] 处理: {raw['title']}")

        # Step 1: 基础解析
        activity = parse_activity(raw, event_id)
        print(f"  → 时间: {activity['start_time']} ~ {activity['end_time']}")
        print(f"  → 费用: ¥{activity['fee']} | 人数: {activity['participant_count']}")

        # Step 2: 精华文本提取
        activity = enrich_essence(activity, raw)
        print(f"  → 精华: {activity['essence_text'][:50]}...")

        # Step 3: 色调分类
        activity = enrich_tone(activity)
        print(f"  → 色调: {activity['tone_tag']} | 关键词: {activity['tone_keywords_hit']}")

        # Step 4: 地理编码
        activity = enrich_geocode(activity)
        pin_type = "精确图钉" if activity["is_precise"] else "模糊热力圈"
        print(f"  → 坐标: ({activity['lat']}, {activity['lng']}) | {pin_type}")

        parsed.append(activity)

    print(f"\n管线完成：共解析 {len(parsed)} 个活动\n")
    return parsed


def save_parsed(events: list, filepath: str = None) -> str:
    """保存解析结果到 JSON 文件"""
    if filepath is None:
        filepath = str(Path(DATA_DIR) / "parsed_events.json")

    Path(filepath).parent.mkdir(parents=True, exist_ok=True)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)

    print(f"已保存 parsed_events.json → {filepath}")
    return filepath


# ─────────────────────────────────────────────────────────
# 直接运行测试
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    from sample_data import SAMPLE_EVENTS

    events = run_pipeline(SAMPLE_EVENTS)
    save_parsed(events)
    print(f"\n示例第一条活动：")
    print(json.dumps(events[0], ensure_ascii=False, indent=2))
