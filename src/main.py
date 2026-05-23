"""
============================================================================
 main.py — 周末约会战术指挥中心 · 总控入口 (v2.2 周末独立面板版)
============================================================================
 流程:
   Step 0: 环境自检（Playwright / EasyOCR / IMA 凭证）
   Step 1: 调用 ima_sync 从腾讯 IMA 知识库实时拉取并渲染/OCR
   Step 2: 调用 data_pipeline 结构化解析
   Step 3: [NEW] 智能去重 + 时间分流（date_category 标签）
   Step 4: 生成最终数据集 → week_schedule.json (项目根目录)
   Step 5: (可选) 推送到 GitHub
============================================================================
"""

import os
import sys
import json
import re
import logging
import argparse
from pathlib import Path
from datetime import datetime, timedelta

# main.py 位于 src/ 子目录，项目根目录为其上层
SRC_DIR = Path(__file__).parent
PROJECT_ROOT = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from ima_sync import IMASyncEngine, DATING_KD_ID
from data_pipeline import DataPipeline, check_llm_deps, _load_llm_config

logger = logging.getLogger(__name__)

OUTPUT_DIR = PROJECT_ROOT / 'output'
FINAL_OUTPUT = PROJECT_ROOT / 'week_schedule.json'
ARCHIVE_DIR = OUTPUT_DIR  # 历史副本保留在 output/ 方便 gitignore

# ===========================================================================
# 日期提取与分类
# ===========================================================================

# 中文日期模式
DATE_PATTERNS = [
    re.compile(r'(\d{1,2})月(\d{1,2})日?'),          # 5月24日
    re.compile(r'(\d{1,2})\.(\d{1,2})'),              # 5.24
    re.compile(r'(\d{1,2})/(\d{1,2})'),               # 5/24
]

WEEKDAY_CN = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6}
WEEKDAY_PATTERN = re.compile(r'周([一二三四五六日])')


def _extract_date(text: str) -> tuple[int, int] | None:
    """
    从文本中提取 (月份, 日期)。
    优先匹配显式的月/日格式。

    Returns
    -------
    (month, day) or None
    """
    if not text:
        return None
    for pat in DATE_PATTERNS:
        matches = pat.findall(text)
        if matches:
            for m in matches:
                month = int(m[0])
                day = int(m[1])
                if 1 <= month <= 12 and 1 <= day <= 31:
                    return (month, day)
    return None


def get_week_range(reference_date: datetime = None) -> tuple[datetime, datetime]:
    """
    获取参考日期所在周的周一 00:00 → 下周一 00:00。

    Parameters
    ----------
    reference_date : datetime
        参考日期（默认当前时间）

    Returns
    -------
    (week_monday, next_monday)
    """
    ref = reference_date or datetime.now()
    # 周一 = ref.weekday() days ago
    weekday = ref.weekday()  # Monday=0, Sunday=6
    monday = ref.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=weekday)
    next_monday = monday + timedelta(days=7)
    return monday, next_monday


def classify_date_category(activity: dict, week_monday: datetime,
                           next_monday: datetime, year: int = 2026) -> str:
    """
    根据活动的日期，判定其时间类别。

    Categories
    ----------
    - 'next_week'  : 日期在 next_monday 及以后（非本周）
    - 'weekday'    : 日期在本周的周一至周四
    - 'friday'     : 日期在本周五
    - 'saturday'   : 日期在本周六
    - 'sunday'     : 日期在本周日
    - 'unknown'    : 无法解析日期

    Parameters
    ----------
    activity : dict
        活动数据（含 activity_time 字段）
    week_monday : datetime
        本周一的 00:00
    next_monday : datetime
        下周一 00:00（本周范围 = [monday, next_monday)）
    year : int
        默认年份

    Returns
    -------
    str
    """
    # 收集所有可能的日期文本
    time_text = activity.get('activity_time', '') or ''
    title = activity.get('activity_title', '') or activity.get('title', '') or ''
    summary = activity.get('summary', '') or ''
    combined = f"{time_text} {title} {summary}"

    date_tuple = _extract_date(combined)
    if not date_tuple:
        # 尝试从 weekday 模式推断（如果文本中有"周六"等且时间文本中有5月）
        # 回退：检查是否有 "周五"/"周六" 等关键词
        wm = WEEKDAY_PATTERN.search(combined)
        if wm:
            wd_cn = wm.group(1)
            wd_num = WEEKDAY_CN.get(wd_cn)
            if wd_num is not None:
                # 查找该周几对应的日期
                target = week_monday + timedelta(days=wd_num)
                # 检查是否在本周内
                if target < next_monday:
                    return _dayname_to_category(wd_num)
        return 'unknown'

    month, day = date_tuple

    try:
        activity_date = datetime(year, month, day)
    except ValueError:
        return 'unknown'

        # 分类
    if activity_date >= next_monday:
        return 'next_week'
    elif activity_date < week_monday:
        # 过去的日期，宽容处理：根据具体周几分配
        prev_weekday = activity_date.weekday()
        if prev_weekday == 4:
            return 'friday'
        elif prev_weekday == 5:
            return 'saturday'
        elif prev_weekday == 6:
            return 'sunday'
        return 'weekday'  # 过去的周一到周四也归为 weekday
    else:
        # 在本周 [monday, next_monday)
        wd = activity_date.weekday()
        if wd == 4:      # Friday
            return 'friday'
        elif wd == 5:    # Saturday
            return 'saturday'
        elif wd == 6:    # Sunday
            return 'sunday'
        else:            # Mon=0~Thu=3
            return 'weekday'


def _dayname_to_category(weekday_num: int) -> str:
    """将 Python weekday (0=Mon) 映射到细粒度日期标签（周五/六/日独立）"""
    mapping = {0: 'weekday', 1: 'weekday', 2: 'weekday', 3: 'weekday',
               4: 'friday', 5: 'saturday', 6: 'sunday'}
    return mapping.get(weekday_num, 'unknown')


# ===========================================================================
# 智能去重
# ===========================================================================


def deduplicate_activities(activities: list[dict]) -> list[dict]:
    """
    去重逻辑：地点相同 + 日期相同 + 主办方相同 → 合并为一组。

    合并策略：
    - 保留第一条活动的完整数据
    - 合并 activity_type 标签（去重）
    - 合并 target_group
    - 记录被合并的 global_id 和标题
    """
    if not activities:
        return activities

    # 提取去重键
    def _dedup_key(act: dict) -> str:
        loc = (act.get('activity_location', '') or '').strip()
        time_text = act.get('activity_time', '') or ''
        date = _extract_date(time_text)
        date_str = f"{date[0]:02d}-{date[1]:02d}" if date else 'no-date'
        org = (act.get('organizer', '') or '').strip()
        return f"{loc}|{date_str}|{org}"

    # 分组
    groups: dict[str, list] = {}
    for act in activities:
        key = _dedup_key(act)
        # 只有 key 至少有两部分信息才去重
        parts = [p for p in [act.get('activity_location', ''),
                             act.get('organizer', '')] if p.strip()]
        key_info = _extract_date(act.get('activity_time', '') or '')
        if len(parts) >= 2 or (len(parts) >= 1 and key_info):
            groups.setdefault(key, []).append(act)
        else:
            # 信息不足，不去重，用唯一 key
            groups.setdefault(f"unique-{act.get('global_id','')}", []).append(act)

    # 合并
    merged = []
    for key, group in groups.items():
        if len(group) == 1:
            merged.append(group[0])
        else:
            # 保留第一条
            primary = dict(group[0])
            merged_ids = [a.get('global_id', '?') for a in group]
            merged_titles = [a.get('activity_title', '') or a.get('title', '') for a in group]

            # 合并类型标签
            all_types = set()
            for a in group:
                for t in (a.get('activity_type') or []):
                    all_types.add(t)
            primary['activity_type'] = sorted(all_types)

            # 合并目标人群
            all_groups = set()
            for a in group:
                for t in (a.get('target_group') or []):
                    all_groups.add(t)
            primary['target_group'] = sorted(all_groups)

            # 记录去重信息
            primary['_dedup_source_ids'] = merged_ids
            primary['_dedup_source_titles'] = merged_titles
            primary['_dedup_count'] = len(group)

            merged.append(primary)

    logger.info(f"  去重完成: {len(activities)} → {len(merged)} 条 "
                f"(合并了 {len(activities)-len(merged)} 条重复)")

    # 重新编号 global_id
    for i, act in enumerate(merged):
        act['global_id'] = f"ACT-{i+1:03d}"

    return merged


# ===========================================================================
# 环境自检
# ===========================================================================


def check_environment() -> dict:
    """检查运行环境是否就绪"""
    status = {
        'playwright': False,
        'easyocr': False,
        'ima_credentials': False,
        'chromium': False,
    }

    # Playwright
    try:
        from playwright.sync_api import sync_playwright
        status['playwright'] = True
    except ImportError:
        pass

    # EasyOCR
    try:
        import easyocr
        status['easyocr'] = True
    except ImportError:
        pass

    # IMA 凭证
    try:
        from ima_sync import IMAClient
        IMAClient()
        status['ima_credentials'] = True
    except Exception:
        pass

    # Chromium
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
            status['chromium'] = True
    except Exception:
        pass

    return status


# ===========================================================================
# 主流程
# ===========================================================================


def main(
    kb_name: str = '陈伟霆相亲库',
    kb_id: str = DATING_KD_ID,
    max_items: int = 0,
    skip_scrape: bool = False,
    push_to_github: bool = False,
):
    """
    主入口：从 IMA 知识库拉取 → 抓取 → 解析 → 去重 → 时间分流 → 输出
    """
    # ---- Step 0: 环境自检 ----
    logger.info("Step 0: 环境自检...")
    env_status = check_environment()
    for key, ok in env_status.items():
        icon = '✅' if ok else '❌'
        logger.info(f"  {icon} {key}")
    if not all(env_status.values()):
        logger.error("环境未就绪，请运行 auto_deploy.bat 安装依赖")
        sys.exit(1)

    # LLM 状态（可选，不阻断流程）
    llm_cfg = _load_llm_config()
    if check_llm_deps() and llm_cfg.get('api_key'):
        logger.info(f"  ✅ llm [{llm_cfg['model']} @ {llm_cfg['base_url']}]")
    else:
        logger.info("  ⚠️  llm [未配置，使用正则引擎 fallback]")

    # ---- Step 1: IMA 同步 ----
    logger.info(f"Step 1: 从 IMA 知识库「{kb_name}」拉取内容...")
    engine = IMASyncEngine(kb_id=kb_id, kb_name=kb_name, output_dir=OUTPUT_DIR)

    if skip_scrape:
        logger.info("  (跳过抓取，仅获取媒体列表)")
        all_items = engine.client.get_all_media(kb_id)
        logger.info(f"  共 {len(all_items)} 条媒体")
        records = []
    else:
        records = engine.sync(max_items=max_items)

    # ---- Step 2: 结构化解析 ----
    logger.info("Step 2: 结构化解析...")
    pipeline = DataPipeline()
    structured_data = []

    for record in records:
        entry = {
            'global_id': f"ACT-{record.index:03d}",
            'title': record.title,
            'source_type': record.source_type,
            'source_url': record.url,
            'folder_path': record.folder_path,
            'error': record.error,
        }

        if record.parsed_data:
            entry.update(record.parsed_data)
        elif record.raw_text:
            # 传入 title_hint 帮助 LLM 更好地理解活动背景
            entry.update(pipeline.parse(record.raw_text, title_hint=record.title))

        # 确保 contact 字段存在（提取联系方式）
        raw = record.raw_text or ''
        if raw and not entry.get('registration'):
            entry['registration'] = pipeline._extract_registration(raw)
        if raw and not entry.get('contact_wechat'):
            entry['contact_wechat'] = pipeline._extract_contact_wechat(raw)
        if raw and not entry.get('contact_phone'):
            entry['contact_phone'] = pipeline._extract_contact_phone(raw)

        structured_data.append(entry)

    # ---- Step 3: 智能去重 ----
    logger.info("Step 3: 智能去重（地点+日期+主办方）...")
    structured_data = deduplicate_activities(structured_data)

    # ---- Step 3.5: 时间分流（date_category） ----
    logger.info("Step 3.5: 时间分流标签计算...")
    week_monday, next_monday = get_week_range()
    logger.info(f"  本周范围: {week_monday.strftime('%m/%d')} ~ {next_monday.strftime('%m/%d')}")

    date_cat_counts = {}
    for act in structured_data:
        cat = classify_date_category(act, week_monday, next_monday)
        act['date_category'] = cat
        date_cat_counts[cat] = date_cat_counts.get(cat, 0) + 1

    logger.info(f"  时间分流结果: {date_cat_counts}")

    # ---- Step 4: 生成最终输出 ----
    logger.info("Step 4: 生成最终数据集...")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    output = {
        'meta': {
            'generated_at': datetime.now().isoformat(),
            'source': f'IMA 知识库: {kb_name}',
            'kb_id': kb_id,
            'total_items': len(structured_data),
            'items_with_content': sum(1 for d in structured_data if not d.get('error')),
            'items_with_error': sum(1 for d in structured_data if d.get('error')),
            'date_categories': date_cat_counts,
            'week_range': {
                'monday': week_monday.strftime('%Y-%m-%d'),
                'next_monday': next_monday.strftime('%Y-%m-%d'),
            },
        },
        'activities': structured_data,
    }

    # 保存为 week_schedule.json
    with open(FINAL_OUTPUT, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    logger.info(f"  最终数据已保存: {FINAL_OUTPUT}")

    # 同时保存一个带时间戳的副本（存入 output/，不推送到 GitHub）
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = ARCHIVE_DIR / f'week_schedule_{timestamp}.json'
    with open(archive_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # ---- Step 5: GitHub 推送 ----
    if push_to_github:
        logger.info("Step 5: 推送到 GitHub...")
        _push_to_github(output)

    # ---- 完成 ----
    _print_final_summary(output)
    return output


def _push_to_github(output: dict):
    """提交并推送到 GitHub"""
    import subprocess

    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--git-dir'],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        if result.returncode != 0:
            logger.warning("当前不在 git 仓库中，跳过 GitHub 推送")
            return

        subprocess.run(
            ['git', 'add', 'week_schedule.json'],
            capture_output=True, cwd=str(PROJECT_ROOT),
        )

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
        commit_msg = f'[auto] IMA sync: {output["meta"]["total_items"]} items @ {timestamp}'
        result = subprocess.run(
            ['git', 'commit', '-m', commit_msg],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )

        if 'nothing to commit' in result.stdout or 'nothing to commit' in result.stderr:
            logger.info("  没有变更，跳过推送")
            return

        result = subprocess.run(
            ['git', 'push'],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        if result.returncode == 0:
            logger.info("  ✅ 已推送到 GitHub")
        else:
            logger.warning(f"  推送失败: {result.stderr}")

    except FileNotFoundError:
        logger.warning("未找到 git 命令，跳过 GitHub 推送")
    except Exception as e:
        logger.warning(f"GitHub 推送失败: {e}")


def _print_final_summary(output: dict):
    """打印最终摘要"""
    meta = output['meta']
    activities = output['activities']

    type_counts = {}
    for a in activities:
        for t in a.get('activity_type', []):
            type_counts[t] = type_counts.get(t, 0) + 1
        if not a.get('activity_type'):
            type_counts['未分类'] = type_counts.get('未分类', 0) + 1

    free_count = sum(1 for a in activities if a.get('is_free'))
    with_location = sum(1 for a in activities if a.get('activity_location'))
    dedup_total = sum(1 for a in activities if a.get('_dedup_count', 0) > 0)

    print("\n" + "=" * 60)
    print(f"  🎯 周末约会战术指挥中心 — 数据同步完成")
    print("=" * 60)
    print(f"  📊 总活动数:     {meta['total_items']}")
    print(f"  ✅ 有效内容:     {meta['items_with_content']}")
    print(f"  ❌ 异常条目:     {meta['items_with_error']}")
    print(f"  🆓 免费活动:     {free_count}")
    print(f"  📍 有地点信息:   {with_location}")
    print(f"  🔗 去重合并:     {dedup_total} 条")
    print(f"  📁 输出文件:     {FINAL_OUTPUT}")
    print("-" * 60)
    print(f"  🏷️  时间分流:")
    for cat, cnt in sorted(meta.get('date_categories', {}).items()):
        print(f"     {cat}: {cnt} 个")
    print("-" * 60)
    print(f"  🏷️  活动类型分布:")
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"     {t}: {c} 个")
    print("=" * 60)


# ===========================================================================
# CLI
# ===========================================================================

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
    )

    parser = argparse.ArgumentParser(
        description='周末约会战术指挥中心 — 从 IMA 知识库全自动拉取并解析活动数据'
    )
    parser.add_argument('--kb', default='陈伟霆相亲库', help='知识库名称')
    parser.add_argument('--kb-id', default=DATING_KD_ID, help='知识库 ID')
    parser.add_argument('--max', type=int, default=0, help='最大处理条数 (0=全部)')
    parser.add_argument('--skip-scrape', action='store_true', help='跳过抓取，仅获取列表')
    parser.add_argument('--push', action='store_true', help='推送到 GitHub')

    args = parser.parse_args()
    main(
        kb_name=args.kb,
        kb_id=args.kb_id,
        max_items=args.max,
        skip_scrape=args.skip_scrape,
        push_to_github=args.push,
    )
