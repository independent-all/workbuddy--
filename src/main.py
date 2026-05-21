"""
周末交友战术指挥舱 — 主编排脚本
──────────────────────────────
串联 Phase 1 → Phase 2 → Phase 3 三大管线，
输出符合 Schema v1.0 规范的 week_schedule.json。

用法:
  python main.py

输出:
  output/week_schedule.json — 前端直接加载的静态数据文件
"""

import json
import sys
from datetime import datetime, date
from pathlib import Path

# 添加 src 目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from sample_data import SAMPLE_EVENTS
from data_pipeline import run_pipeline, save_parsed
from transit_engine import run_transit_engine, save_day_schedules
from conflict_engine import run_conflict_engine, save_conflicts
from config import (
    ORIGIN_ADDRESS,
    ORIGIN_LAT,
    ORIGIN_LNG,
    OUTPUT_DIR,
    DATA_DIR,
)


def get_week_label(dates: list) -> str:
    """从活动日期列表中推断 ISO 周标签，如 '2026-W21'"""
    if not dates:
        return ""
    first_date = min(dates)
    d = date.fromisoformat(first_date)
    iso_year, iso_week, _ = d.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def annotate_global_ids(week_schedule: dict) -> dict:
    """
    为整周所有活动按 start_time 升序赋予全局唯一编号 global_id。

    遍历 friday → saturday → sunday 的 timeline，
    提取所有 type=="activity" 的条目，按时间排序，
    依次赋予 global_id = 1, 2, 3, ... N。

    修改原 dict 并返回。
    """
    # 收集所有活动（保持 timeline 原始顺序即已按时间排序）
    all_activities = []
    for day_key in ["friday", "saturday", "sunday"]:
        day = week_schedule["days"].get(day_key, {})
        for item in day.get("timeline", []):
            if item.get("type") == "activity":
                all_activities.append(item)

    # 按 start_time 升序二次确认排序
    all_activities.sort(key=lambda a: a["start_time"])

    # 分配全局编号
    for i, act in enumerate(all_activities, 1):
        act["global_id"] = i

    print(f"\n[global_id] 已为 {len(all_activities)} 个活动分配全局编号 1~{len(all_activities)}")
    for act in all_activities:
        print(f"  #{act['global_id']:02d} {act['start_time'][:16]}  {act['name']}")

    return week_schedule


def assemble_week_schedule(parsed_events: list,
                           day_schedules: dict,
                           conflict_groups: dict) -> dict:
    """
    组装最终 WeekSchedule JSON（Schema v1.0）。

    将 conflict_groups 挂载到对应 day_schedule 上，
    封装顶层元数据，并注入全局编号 global_id。
    """
    # 收集所有活动日期
    dates = [e["start_time"][:10] for e in parsed_events]
    week_label = get_week_label(dates)

    # 将冲突组注入各日
    days = {}
    for day_key in ["friday", "saturday", "sunday"]:
        schedule = day_schedules.get(day_key, {})
        conflicts = conflict_groups.get(day_key, [])

        days[day_key] = {
            "date": schedule.get("date", ""),
            "day_label": day_key,
            "day_label_cn": schedule.get("day_label_cn", ""),
            "timeline": schedule.get("timeline", []),
            "conflict_groups": conflicts,
        }

    week_schedule = {
        "$schema": "urn:tactical-dating:schema:1.0",
        "version": "1.0.0",
        "generated_at": datetime.now().astimezone().isoformat(),
        "origin": {
            "address": ORIGIN_ADDRESS,
            "lat": ORIGIN_LAT,
            "lng": ORIGIN_LNG,
        },
        "week": week_label,
        "days": days,
    }

    # ── 注入全局唯一编号 ──
    week_schedule = annotate_global_ids(week_schedule)

    return week_schedule


def print_summary(week_schedule: dict):
    """打印最终汇总报告"""
    print("\n" + "=" * 60)
    print("📊 周末交友战术指挥舱 — 汇总报告")
    print("=" * 60)
    print(f"原点:    {ORIGIN_ADDRESS} ({ORIGIN_LAT}, {ORIGIN_LNG})")
    print(f"周次:    {week_schedule['week']}")
    print(f"生成时间: {week_schedule['generated_at']}")
    print("-" * 60)

    total_activities = 0
    total_conflicts = 0
    total_distance = 0.0
    total_transit_min = 0

    for day_key in ["friday", "saturday", "sunday"]:
        day = week_schedule["days"].get(day_key, {})
        timeline = day.get("timeline", [])
        conflicts = day.get("conflict_groups", [])

        activities = [t for t in timeline if t["type"] == "activity"]
        spacers = [t for t in timeline if t["type"] == "transit_spacer"]

        act_count = len(activities)
        spacer_count = len(spacers)
        conf_count = len(conflicts)

        total_activities += act_count
        total_conflicts += conf_count

        day_km = sum(s["distance_km"] for s in spacers)
        day_min = sum(s["duration_min"] for s in spacers)
        total_distance += day_km
        total_transit_min += day_min

        print(f"\n{day.get('day_label_cn', day_key)} ({day.get('date', '无活动')}):")
        print(f"  活动: {act_count} 场 | 转场: {spacer_count} 次 | 冲突: {conf_count} 组")

        for i, act in enumerate(activities, 1):
            tone_icon = {"cold": "🔵", "warm": "🟠", "neutral": "⚪"}
            icon = tone_icon.get(act.get("tone_tag", "neutral"), "⚪")
            score = act.get("value_score", "—")
            print(f"    {i}. {icon} {act['name']}")
            print(f"       {act['start_time'][11:16]}~{act['end_time'][11:16]} "
                  f"| ¥{act['fee']} | {act['participant_count']}人 | 性价比{score}分")

        for sp in spacers:
            print(f"       └ {sp['display_label']}")

    print(f"\n{'=' * 60}")
    print(f"总计: {total_activities} 场活动 | {total_conflicts} 个冲突组 | "
          f"转场 {total_distance:.1f}km / {total_transit_min}min")
    print(f"输出文件: output/week_schedule.json")
    print(f"{'=' * 60}\n")


def main():
    """主编排入口"""
    print("╔══════════════════════════════════════════════════════╗")
    print("║     周末交友战术指挥舱 · 数据处理管道 v1.0          ║")
    print("╚══════════════════════════════════════════════════════╝")

    # ── Phase 1: 数据管线 ──
    parsed = run_pipeline(SAMPLE_EVENTS)
    save_parsed(parsed)

    # ── Phase 2: 转场矩阵 ──
    day_schedules = run_transit_engine(parsed)
    save_day_schedules(day_schedules)

    # ── Phase 3: 冲突检测 ──
    conflict_groups = run_conflict_engine(day_schedules)
    save_conflicts(conflict_groups)

    # ── 组装最终输出 ──
    print("\n" + "=" * 60)
    print("组装最终 week_schedule.json (Schema v1.0)")
    print("=" * 60)

    week_schedule = assemble_week_schedule(parsed, day_schedules, conflict_groups)

    # 写入输出
    output_path = Path(OUTPUT_DIR) / "week_schedule.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(week_schedule, f, ensure_ascii=False, indent=2)

    print(f"✅ 已生成 week_schedule.json → {output_path}")

    # ── 汇总报告 ──
    print_summary(week_schedule)

    return week_schedule


if __name__ == "__main__":
    main()
