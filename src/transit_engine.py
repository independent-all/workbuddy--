"""
Phase 2: 转场矩阵计算
─────────────────────
功能：
  1. 按日期（五/六/日）将活动分桶
  2. 桶内按 start_time 升序排列
  3. 对相邻活动对，调用高德导航 API（或 Mock）计算行车距离与耗时
  4. 以 "Activity ↔ TransitSpacer" 交替规则注入 timeline 序列
  5. 输出 DaySchedule 容器

输出：各日 bucket，含完整的 timeline（activity + transit_spacer）
"""

import json
import math
from datetime import datetime, date
from pathlib import Path

import requests

from config import (
    AMAP_KEY,
    DRIVING_URL,
    REQUEST_TIMEOUT,
    DAY_LABEL_MAP,
    DATA_DIR,
    OUTPUT_DIR,
)


# ─────────────────────────────────────────────────────────
# 1. 分日分桶
# ─────────────────────────────────────────────────────────


def get_weekday_cn(iso_date_str: str) -> str:
    """
    根据 ISO 日期字符串返回中文星期标签。

    Args:
        iso_date_str: "2026-05-22"

    Returns:
        "周五" / "周六" / "周日"
    """
    d = date.fromisoformat(iso_date_str)
    wd = d.weekday()  # 周一=0, 周日=6
    mapping = {4: "周五", 5: "周六", 6: "周日"}
    return mapping.get(wd, "未知")


def get_day_key(iso_date_str: str) -> str:
    """
    返回 day_label 键名: "friday" / "saturday" / "sunday"
    """
    d = date.fromisoformat(iso_date_str)
    wd = d.weekday()
    mapping = {4: "friday", 5: "saturday", 6: "sunday"}
    return mapping.get(wd, f"day_{wd}")


def bucket_by_day(events: list) -> dict:
    """
    将活动列表按日期分桶。

    Returns:
        {
            "friday": {
                "date": "2026-05-22",
                "day_label": "friday",
                "day_label_cn": "周五",
                "events": [...]  # 按 start_time 升序
            },
            ...
        }
    """
    buckets = {}

    for evt in events:
        start = evt["start_time"]
        date_str = start[:10]  # "2026-05-22"
        day_key = get_day_key(date_str)

        if day_key not in buckets:
            buckets[day_key] = {
                "date": date_str,
                "day_label": day_key,
                "day_label_cn": get_weekday_cn(date_str),
                "events": [],
            }

        buckets[day_key]["events"].append(evt)

    # 每桶内按 start_time 升序
    for key in buckets:
        buckets[key]["events"].sort(key=lambda e: e["start_time"])
        print(f"  [{buckets[key]['day_label_cn']}] {len(buckets[key]['events'])} 个活动已排序")

    return buckets


# ─────────────────────────────────────────────────────────
# 2. 导航距离/耗时计算 (Mock + API)
# ─────────────────────────────────────────────────────────


# 两地直线距离（Haversine公式）用作 Mock 估算基础
def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """计算两点间的球面距离（km）"""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


# Mock 导航数据表（预计算的驾车距离/耗时，比 Haversine 准）
MOCK_TRANSIT_MAP = {
    # (起点地址, 终点地址) → (距离km, 耗时min)
    ("朝阳区大望路SOHO现代城A座301", "朝阳公园南门附近"): (6.2, 22),
    ("朝阳公园南门附近", "朝阳区国贸三期B座56层云酷餐厅"): (5.8, 18),
    ("朝阳区国贸三期B座56层云酷餐厅", "海淀区五道口华清商务会馆B1"): (16.5, 38),
    ("海淀区五道口华清商务会馆B1", "朝阳区三里屯太古里Wework"): (14.2, 35),
    ("朝阳区三里屯太古里Wework", "朝阳区亮马桥官舍3层"): (3.1, 12),
    ("昌平区十三陵水库附近", "西城区西单大悦城附近"): (48.0, 65),
    ("西城区西单大悦城附近", "朝阳区蓝色港湾KTV"): (14.5, 32),
}


def fetch_driving_info(origin_lat: float, origin_lng: float,
                       dest_lat: float, dest_lng: float) -> dict:
    """
    调用高德驾车路径规划 API 获取距离与耗时。

    Returns:
        {"distance_km": float, "duration_min": int, "traffic_condition": str}
    """
    if AMAP_KEY == "YOUR_AMAP_KEY_HERE":
        return None  # 触发 Mock 回退

    try:
        origin = f"{origin_lng},{origin_lat}"
        destination = f"{dest_lng},{dest_lat}"

        resp = requests.get(
            DRIVING_URL,
            params={
                "key": AMAP_KEY,
                "origin": origin,
                "destination": destination,
                "strategy": 0,          # 速度优先
                "extensions": "base",   # 基础信息（不含步骤）
                "output": "JSON",
            },
            timeout=REQUEST_TIMEOUT,
        )
        data = resp.json()

        if data.get("status") == "1" and data.get("route", {}).get("paths"):
            path = data["route"]["paths"][0]
            distance_m = int(path["distance"])         # 米
            duration_s = int(path["duration"])         # 秒
            return {
                "distance_km": round(distance_m / 1000, 1),
                "duration_min": max(1, duration_s // 60),
                "traffic_condition": "normal",
            }
    except Exception as e:
        print(f"  [WARN] 高德导航API失败: {e}，回退 Mock")

    return None


def compute_transit(from_activity: dict, to_activity: dict) -> dict:
    """
    计算两个活动地点间的转场信息。

    优先使用 API，失败则回退 Mock → Haversine 估算。
    """
    from_addr = from_activity["address_raw"]
    to_addr = to_activity["address_raw"]

    # 尝试 Mock 表
    pair_key = (from_addr, to_addr)
    if pair_key in MOCK_TRANSIT_MAP:
        km, min_val = MOCK_TRANSIT_MAP[pair_key]
        return {
            "distance_km": km,
            "duration_min": min_val,
            "traffic_condition": "normal",
        }

    # 尝试高德 API
    result = fetch_driving_info(
        from_activity["lat"], from_activity["lng"],
        to_activity["lat"], to_activity["lng"],
    )
    if result:
        return result

    # 最终回退：Haversine × 1.4（道路系数）
    straight = haversine_km(
        from_activity["lat"], from_activity["lng"],
        to_activity["lat"], to_activity["lng"],
    )
    road_km = round(straight * 1.4, 1)
    # 预估：市区平均速度 30km/h
    est_min = max(1, int(road_km / 30 * 60))
    return {
        "distance_km": road_km,
        "duration_min": est_min,
        "traffic_condition": "estimated",
    }


# ─────────────────────────────────────────────────────────
# 3. Spacer 注入
# ─────────────────────────────────────────────────────────


def create_transit_spacer(from_act: dict, to_act: dict) -> dict:
    """
    在两个相邻活动之间创建 TransitSpacer 对象。

    符合 Schema v1.0 中的 TransitSpacer 定义。
    """
    transit = compute_transit(from_act, to_act)

    from_name = from_act.get("name", from_act.get("address_raw", "未知"))
    to_name = to_act.get("name", to_act.get("address_raw", "未知"))
    from_short = _shorten(from_act.get("address_raw", "未知地点"))
    to_short = _shorten(to_act.get("address_raw", "未知地点"))

    km = transit["distance_km"]
    min_val = transit["duration_min"]

    spacer = {
        "type": "transit_spacer",
        "from_activity_id": from_act["id"],
        "to_activity_id": to_act["id"],
        "from_name": from_short,
        "to_name": to_short,
        "distance_km": km,
        "duration_min": min_val,
        "traffic_condition": transit["traffic_condition"],
        "display_label": (
            f"🚗 驱车转场：{km} 公里 | "
            f"预计耗时：{min_val} 分钟（{from_short} → {to_short}）"
        ),
    }

    print(f"  spacer: {from_short} → {to_short} | {km}km / {min_val}min")
    return spacer


def _shorten(addr: str, max_len: int = 12) -> str:
    """截短地址用于 spacer 显示"""
    if len(addr) <= max_len:
        return addr
    # 去掉"北京市朝阳区"等前缀
    for prefix in ["北京市", "北京", "朝阳区", "海淀区", "西城区", "昌平区"]:
        if addr.startswith(prefix):
            addr = addr[len(prefix):]
            break
    if len(addr) > max_len:
        addr = addr[:max_len - 1] + "…"
    return addr


def inject_spacers(events_sorted: list) -> list:
    """
    将 TransitSpacer 对象注入活动列表，实现交替排列。

    Args:
        events_sorted: 按时间升序排列的活动列表

    Returns:
        timeline: [Activity, TransitSpacer, Activity, TransitSpacer, ...]
        始终以 Activity 起始，以 Activity 结束。
    """
    if not events_sorted:
        return []

    timeline = []
    for i, evt in enumerate(events_sorted):
        timeline.append(evt)
        # 如果不是最后一个活动，插入 spacer
        if i < len(events_sorted) - 1:
            spacer = create_transit_spacer(events_sorted[i], events_sorted[i + 1])
            timeline.append(spacer)

    return timeline


# ─────────────────────────────────────────────────────────
# 4. 管线主函数
# ─────────────────────────────────────────────────────────


def run_transit_engine(events: list) -> dict:
    """
    执行完整转场矩阵计算管线。

    Args:
        events: 来自 data_pipeline 的已解析活动列表

    Returns:
        {
            "friday": { "date": ..., "day_label": ..., "day_label_cn": ..., "timeline": [...] },
            "saturday": { ... },
            "sunday": { ... },
        }
    """
    print("\n" + "=" * 60)
    print("Phase 2: 转场矩阵计算启动")
    print("=" * 60)

    # Step 1: 分桶
    print("\n[步骤 1] 分日分桶...")
    buckets = bucket_by_day(events)

    # Step 2: 每组注入 spacer
    print("\n[步骤 2] 注入行车盲区隔离带...")
    day_schedules = {}
    for key in ["friday", "saturday", "sunday"]:
        if key in buckets:
            bucket = buckets[key]
            timeline = inject_spacers(bucket["events"])
            day_schedules[key] = {
                "date": bucket["date"],
                "day_label": key,
                "day_label_cn": bucket["day_label_cn"],
                "timeline": timeline,
            }
            act_count = sum(1 for item in timeline if item["type"] == "activity")
            spacer_count = sum(1 for item in timeline if item["type"] == "transit_spacer")
            print(f"  {bucket['day_label_cn']}: {act_count} 活动 + {spacer_count} 隔离带")
        else:
            # 该日无活动 → 空时间轴
            cn = DAY_LABEL_MAP.get(key, key)
            day_schedules[key] = {
                "date": "",
                "day_label": key,
                "day_label_cn": cn,
                "timeline": [],
            }
            print(f"  {cn}: 无活动")

    print(f"\n转场矩阵完成：3 日行程已就绪\n")
    return day_schedules


def save_day_schedules(day_schedules: dict, filepath: str = None) -> str:
    """保存分日时间表到 JSON 文件"""
    if filepath is None:
        filepath = str(Path(DATA_DIR) / "day_schedules.json")

    Path(filepath).parent.mkdir(parents=True, exist_ok=True)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(day_schedules, f, ensure_ascii=False, indent=2)

    print(f"已保存 day_schedules.json → {filepath}")
    return filepath


if __name__ == "__main__":
    # 加载 parsed_events 进行独立测试
    with open(Path(DATA_DIR) / "parsed_events.json", "r", encoding="utf-8") as f:
        events = json.load(f)

    schedules = run_transit_engine(events)
    save_day_schedules(schedules)
