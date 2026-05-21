"""
Phase 3: 冲突检测引擎
─────────────────────
功能：
  1. 同日时间轴内检测活动时间重叠（end_time_A > start_time_B）
  2. 为每组冲突活动分配 conflict_group_id
  3. 计算客单价、人流量、转场路耗三维对比
  4. 按性价比公式（value_score）为每场活动评分

输出：conflict_groups — ConflictGroup 对象数组
"""

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import DATA_DIR


# ─────────────────────────────────────────────────────────
# 1. 时间重叠检测
# ─────────────────────────────────────────────────────────


def parse_dt(iso_str: str) -> datetime:
    """安全解析 ISO 时间字符串"""
    return datetime.fromisoformat(iso_str)


def has_overlap(a_start: datetime, a_end: datetime,
                b_start: datetime, b_end: datetime) -> bool:
    """
    判断两个时间区间是否存在重叠。

    条件: A_end > B_start 且 B_end > A_start
    （若端点恰好相等视为不重叠，可调整）
    """
    return a_end > b_start and b_end > a_start


def get_overlap_window(a_start: datetime, a_end: datetime,
                       b_start: datetime, b_end: datetime) -> dict:
    """
    返回两个活动的时间重叠窗口。

    Returns:
        {"overlap_start": str, "overlap_end": str}（ISO格式）
    """
    overlap_start = max(a_start, b_start)
    overlap_end = min(a_end, b_end)
    return {
        "overlap_start": overlap_start.isoformat(),
        "overlap_end": overlap_end.isoformat(),
    }


# ─────────────────────────────────────────────────────────
# 2. 冲突分组（并查集合并重叠链）
# ─────────────────────────────────────────────────────────


def find_conflicts(activities: list, day_key: str) -> list:
    """
    对同一天的活动列表进行冲突检测，使用 Union-Find 合并重叠链。

    核心逻辑：
      - 活动按 start_time 排序后，逐个检查后续活动是否重叠
      - 使用并查集将传递性重叠的活动归入同一 conflict_group
        （例如：A 与 B 冲突，B 与 C 冲突 → {A, B, C} 同组）

    Args:
        activities: 同日 Activity 对象列表（已按时间排序）
        day_key: "friday" / "saturday" / "sunday"

    Returns:
        list[ConflictGroup]
    """
    n = len(activities)
    if n < 2:
        return []  # 少于2个活动，不可能冲突

    # 并查集
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    # 检测所有活动对的重叠
    for i in range(n):
        a_start = parse_dt(activities[i]["start_time"])
        a_end = parse_dt(activities[i]["end_time"])
        for j in range(i + 1, n):
            b_start = parse_dt(activities[j]["start_time"])
            b_end = parse_dt(activities[j]["end_time"])
            if has_overlap(a_start, a_end, b_start, b_end):
                union(i, j)

    # 按 root 分组
    groups = {}
    for i in range(n):
        root = find(i)
        if root not in groups:
            groups[root] = []
        groups[root].append(activities[i])

    # 过滤掉只有单个活动的组（无冲突）
    conflict_groups = []
    gid = 0
    for root, members in groups.items():
        if len(members) >= 2:
            conflict_groups.append({
                "conflict_group_id": f"cg_{day_key[:3]}_{gid + 1:03d}",
                "day": day_key,
                "members": members,
            })
            gid += 1

    return conflict_groups


# ─────────────────────────────────────────────────────────
# 3. 性价比评分 (value_score)
# ─────────────────────────────────────────────────────────

# 权重常量
W_SCALE = 0.35        # 参与人数权重
W_COST = 0.30         # 费用权重
W_CONVENIENCE = 0.35  # 便利性权重


def _percentile_rank(values: list, x: float, reverse: bool = False) -> float:
    """
    计算 x 在 values 中的百分位排名 (0–100)。

    Args:
        reverse: True 时，值越小排名越高（用于费用）
    """
    if not values:
        return 50.0
    sorted_vals = sorted(values)
    if reverse:
        # 反转：最小值 → 最高分
        sorted_vals = sorted_vals[::-1]
        count_leq = sum(1 for v in values if v >= x)
    else:
        count_leq = sum(1 for v in values if v <= x)
    return round((count_leq / len(values)) * 100, 1)


def calculate_value_scores(all_events: list) -> None:
    """
    为所有活动计算 value_score，直接修改事件字典。

    基于当周所有活动的统计分布计算百分位排名，
    使评分在同一批次内可比。
    """
    if not all_events:
        return

    # 收集整周数据用于归一化
    participant_counts = [e["participant_count"] for e in all_events]
    fees = [e["fee"] for e in all_events]

    # 便利性 = 距原点距离的倒数（越近越高）
    from config import ORIGIN_LAT, ORIGIN_LNG

    def distance_to_origin(e):
        return math.sqrt(
            (e["lat"] - ORIGIN_LAT) ** 2 + (e["lng"] - ORIGIN_LNG) ** 2
        )

    distances = [distance_to_origin(e) * 111 for e in all_events]  # 转为近似 km

    for evt in all_events:
        # 人数得分（越多越好）
        scale_score = _percentile_rank(participant_counts, evt["participant_count"])

        # 费用得分（越少越好 → reverse=True）
        cost_score = _percentile_rank(fees, evt["fee"], reverse=True)

        # 便利得分（越近越好 → reverse=True）
        dist = distance_to_origin(evt) * 111
        convenience_score = _percentile_rank(distances, dist, reverse=True)

        # 综合性价比评分
        raw = (scale_score * W_SCALE +
               cost_score * W_COST +
               convenience_score * W_CONVENIENCE)

        evt["value_score"] = round(raw, 1)
        evt["score_breakdown"] = {
            "scale_score": scale_score,
            "cost_score": cost_score,
            "convenience_score": convenience_score,
        }


# ─────────────────────────────────────────────────────────
# 4. 组装 ConflictGroup 完整结构
# ─────────────────────────────────────────────────────────


def find_transit_for_conflict(activity: dict, all_activities: list,
                              day_activities: list) -> tuple:
    """
    在冲突组中，计算某活动到前一个活动的转场路耗。

    Returns:
        (transit_from_prev_km, transit_from_prev_min)
    """
    # 在同日排序活动中找到该活动的位置
    idx = None
    for i, a in enumerate(day_activities):
        if a["id"] == activity["id"]:
            idx = i
            break

    if idx is None or idx == 0:
        return (0, 0)

    # 尝试从原活动中获取（如果之前已计算过 transit）
    prev = day_activities[idx - 1]
    # 使用 Haversine 快速估算
    from transit_engine import haversine_km
    straight = haversine_km(
        prev["lat"], prev["lng"],
        activity["lat"], activity["lng"],
    )
    road_km = round(straight * 1.4, 1)
    est_min = max(1, int(road_km / 30 * 60))
    return (road_km, est_min)


def build_conflict_groups(day_key: str,
                          raw_conflicts: list,
                          day_activities: list) -> list:
    """
    将原始冲突检测结果组装为完整 ConflictGroup Schema。

    Args:
        day_key: "friday" / "saturday" / "sunday"
        raw_conflicts: find_conflicts() 的原始输出
        day_activities: 当日的活动列表（已排序）

    Returns:
        list[ConflictGroup] 符合 Schema v1.0
    """
    conflict_groups = []

    for raw in raw_conflicts:
        members_detail = []

        # 计算该组的整体重叠窗口
        all_starts = [parse_dt(m["start_time"]) for m in raw["members"]]
        all_ends = [parse_dt(m["end_time"]) for m in raw["members"]]
        group_overlap_start = max(all_starts)
        group_overlap_end = min(all_ends)

        for m in raw["members"]:
            transit_km, transit_min = find_transit_for_conflict(
                m, raw["members"], day_activities
            )

            members_detail.append({
                "activity_id": m["id"],
                "activity_name": m["name"],
                "lat": m["lat"],
                "lng": m["lng"],
                "fee_per_person": m["fee"],
                "participant_count": m["participant_count"],
                "cost_per_capita": m["fee"],
                "crowd_density": _crowd_label(m["participant_count"]),
                "transit_from_prev_km": transit_km,
                "transit_from_prev_min": transit_min,
                "value_score": m.get("value_score", 0),
                "score_breakdown": m.get("score_breakdown", {}),
            })

        conflict_groups.append({
            "conflict_group_id": raw["conflict_group_id"],
            "day": day_key,
            "members": members_detail,
            "conflict_window": {
                "overlap_start": group_overlap_start.isoformat(),
                "overlap_end": group_overlap_end.isoformat(),
            },
        })

    return conflict_groups


def _crowd_label(count: int) -> str:
    """根据参与人数返回人流标签"""
    if count >= 40:
        return "高"
    elif count >= 20:
        return "中等"
    else:
        return "低"


# ─────────────────────────────────────────────────────────
# 5. 管线主函数
# ─────────────────────────────────────────────────────────


def run_conflict_engine(day_schedules: dict) -> dict:
    """
    执行完整冲突检测引擎管线。

    Args:
        day_schedules: 来自 transit_engine 的分日时间表

    Returns:
        {
            "friday": [...ConflictGroup],
            "saturday": [...ConflictGroup],
            "sunday": [...ConflictGroup],
        }
    """
    print("\n" + "=" * 60)
    print("Phase 3: 冲突检测引擎启动")
    print("=" * 60)

    # 收集所有活动用于全局性价比评分
    all_activities = []
    for day_key, schedule in day_schedules.items():
        for item in schedule.get("timeline", []):
            if item["type"] == "activity":
                all_activities.append(item)

    # 计算全局性价比评分
    print(f"\n[步骤 1] 全局性价比评分 (基于 {len(all_activities)} 个活动)...")
    calculate_value_scores(all_activities)

    top3 = sorted(all_activities, key=lambda e: e.get("value_score", 0), reverse=True)[:3]
    for rank, evt in enumerate(top3, 1):
        print(f"  #{rank} {evt['name']}: {evt['value_score']}分 "
              f"(规模{evt['score_breakdown']['scale_score']}/费用{evt['score_breakdown']['cost_score']}/便利{evt['score_breakdown']['convenience_score']})")

    # 逐日冲突检测
    print("\n[步骤 2] 冲突检测...")
    all_conflicts = {}
    total_conflicts = 0

    for day_key in ["friday", "saturday", "sunday"]:
        schedule = day_schedules.get(day_key, {})
        timeline = schedule.get("timeline", [])

        # 提取纯活动列表（排除 spacer）
        day_activities = [item for item in timeline if item["type"] == "activity"]

        cn = schedule.get("day_label_cn", day_key)

        if len(day_activities) >= 2:
            raw_conflicts = find_conflicts(day_activities, day_key)

            if raw_conflicts:
                conflict_groups = build_conflict_groups(
                    day_key, raw_conflicts, day_activities
                )
                all_conflicts[day_key] = conflict_groups
                total_conflicts += len(conflict_groups)

                print(f"  {cn}: 发现 {len(conflict_groups)} 个冲突组")
                for cg in conflict_groups:
                    names = [m["activity_name"] for m in cg["members"]]
                    print(f"    {cg['conflict_group_id']}: {' ↔ '.join(names)}")
            else:
                all_conflicts[day_key] = []
                print(f"  {cn}: 无冲突")
        else:
            all_conflicts[day_key] = []
            print(f"  {cn}: 仅 {len(day_activities)} 个活动，无需检测")

    print(f"\n冲突检测完成：共 {total_conflicts} 个冲突组\n")
    return all_conflicts


def save_conflicts(conflicts: dict, filepath: str = None) -> str:
    """保存冲突分析结果到 JSON 文件"""
    if filepath is None:
        filepath = str(Path(DATA_DIR) / "conflict_groups.json")

    Path(filepath).parent.mkdir(parents=True, exist_ok=True)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(conflicts, f, ensure_ascii=False, indent=2)

    print(f"已保存 conflict_groups.json → {filepath}")
    return filepath


if __name__ == "__main__":
    from transit_engine import run_transit_engine

    # 加载 parsed_events
    with open(Path(DATA_DIR) / "parsed_events.json", "r", encoding="utf-8") as f:
        events = json.load(f)

    schedules = run_transit_engine(events)
    conflicts = run_conflict_engine(schedules)
    save_conflicts(conflicts)
