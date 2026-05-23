"""
============================================================================
 main.py — 周末约会战术指挥中心 · 总控入口 (v3.0 模块化管道版)
============================================================================
 三层管道流水线:
   Step 0: 环境自检（Playwright / EasyOCR / IMA 凭证）
   Step 1: Extractor — IMA 拉取 → 网页抓取 → 纯净 Markdown
   Step 2: Parser — LLM + 正则 → RawActivity 字典列表
   Step 3: Validator — 字段级降级校验 → ValidActivity + error_log.json
   Step 4: 智能去重 + 时间分流（date_category 标签）
   Step 5: 生成 week_schedule.json + 历史副本
   Step 6: (可选) 推送到 GitHub
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
from typing import Any, Dict, List, Optional, Tuple

# main.py 位于 src/ 子目录，项目根目录为其上层
SRC_DIR = Path(__file__).parent
PROJECT_ROOT = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from ima_sync import IMASyncEngine, DATING_KD_ID
from extractor import Extractor

logger = logging.getLogger(__name__)

OUTPUT_DIR = PROJECT_ROOT / 'output'
FINAL_OUTPUT = PROJECT_ROOT / 'week_schedule.json'
ERROR_LOG = OUTPUT_DIR / 'error_log.json'
ARCHIVE_DIR = OUTPUT_DIR  # 历史副本保留在 output/ 方便 gitignore


# ===========================================================================
# 日期提取与分类（保持 v2.4 逻辑不变）
# ===========================================================================

DATE_PATTERNS = [
    re.compile(r'(\d{1,2})月(\d{1,2})日?'),
    re.compile(r'(\d{1,2})\.(\d{1,2})'),
    re.compile(r'(\d{1,2})/(\d{1,2})'),
]

WEEKDAY_CN = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6}
WEEKDAY_PATTERN = re.compile(r'周([一二三四五六日])')


def _extract_date(text: str) -> Optional[Tuple[int, int]]:
    if not text:
        return None
    for pat in DATE_PATTERNS:
        matches = pat.findall(text)
        if matches:
            for m in matches:
                month, day = int(m[0]), int(m[1])
                if 1 <= month <= 12 and 1 <= day <= 31:
                    return (month, day)
    return None


def get_week_range(reference_date: datetime = None) -> Tuple[datetime, datetime]:
    ref = reference_date or datetime.now()
    weekday = ref.weekday()
    monday = ref.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=weekday)
    next_monday = monday + timedelta(days=7)
    return monday, next_monday


def classify_date_category(activity: dict, week_monday: datetime,
                           next_monday: datetime, year: int = 2026) -> str:
    time_text = activity.get('activity_time', '') or ''
    title = activity.get('activity_title', '') or activity.get('title', '') or ''
    summary = activity.get('summary', '') or ''
    combined = f"{time_text} {title} {summary}"

    date_tuple = _extract_date(combined)
    if not date_tuple:
        wm = WEEKDAY_PATTERN.search(combined)
        if wm:
            wd_cn = wm.group(1)
            wd_num = WEEKDAY_CN.get(wd_cn)
            if wd_num is not None:
                target = week_monday + timedelta(days=wd_num)
                if target < next_monday:
                    return _dayname_to_category(wd_num)
        return 'unknown'

    month, day = date_tuple
    try:
        activity_date = datetime(year, month, day)
    except ValueError:
        return 'unknown'

    if activity_date >= next_monday:
        return 'next_week'
    elif activity_date < week_monday:
        prev_weekday = activity_date.weekday()
        if prev_weekday == 4:
            return 'friday'
        elif prev_weekday == 5:
            return 'saturday'
        elif prev_weekday == 6:
            return 'sunday'
        return 'weekday'
    else:
        wd = activity_date.weekday()
        if wd == 4:
            return 'friday'
        elif wd == 5:
            return 'saturday'
        elif wd == 6:
            return 'sunday'
        else:
            return 'weekday'


def _dayname_to_category(weekday_num: int) -> str:
    mapping = {0: 'weekday', 1: 'weekday', 2: 'weekday', 3: 'weekday',
               4: 'friday', 5: 'saturday', 6: 'sunday'}
    return mapping.get(weekday_num, 'unknown')


# ===========================================================================
# 智能去重（保持 v2.4 逻辑不变）
# ===========================================================================

def deduplicate_activities(activities: List[dict]) -> List[dict]:
    if not activities:
        return activities

    def _dedup_key(act: dict) -> str:
        loc = (act.get('activity_location', '') or '').strip()
        time_text = act.get('activity_time', '') or ''
        date = _extract_date(time_text)
        date_str = f"{date[0]:02d}-{date[1]:02d}" if date else 'no-date'
        org = (act.get('organizer', '') or '').strip()
        return f"{loc}|{date_str}|{org}"

    groups: Dict[str, list] = {}
    for act in activities:
        key = _dedup_key(act)
        parts = [p for p in [act.get('activity_location', ''),
                             act.get('organizer', '')] if p.strip()]
        key_info = _extract_date(act.get('activity_time', '') or '')
        if len(parts) >= 2 or (len(parts) >= 1 and key_info):
            groups.setdefault(key, []).append(act)
        else:
            groups.setdefault(f"unique-{act.get('global_id','')}", []).append(act)

    merged = []
    for key, group in groups.items():
        if len(group) == 1:
            merged.append(group[0])
        else:
            primary = dict(group[0])
            merged_ids = [a.get('global_id', '?') for a in group]

            all_types = set()
            for a in group:
                for t in (a.get('activity_type') or []):
                    all_types.add(t)
            primary['activity_type'] = sorted(all_types)

            all_targets = set()
            for a in group:
                for t in (a.get('target_group') or []):
                    all_targets.add(t)
            primary['target_group'] = sorted(all_targets)

            primary['_dedup_source_ids'] = merged_ids
            primary['_dedup_count'] = len(group)
            merged.append(primary)

    logger.info(f"  去重完成: {len(activities)} → {len(merged)} 条 "
                f"(合并了 {len(activities)-len(merged)} 条重复)")

    # 重新编号 global_id
    for i, act in enumerate(merged):
        act['global_id'] = i + 1  # 整数编号，供前端 A/B/C 区联动

    return merged


# ===========================================================================
# 环境自检
# ===========================================================================

def check_environment() -> dict:
    status = {
        'playwright': False,
        'easyocr': False,
        'ima_credentials': False,
        'chromium': False,
    }
    try:
        from playwright.sync_api import sync_playwright
        status['playwright'] = True
    except ImportError:
        pass
    try:
        import easyocr
        status['easyocr'] = True
    except ImportError:
        pass
    try:
        from ima_sync import IMAClient
        IMAClient()
        status['ima_credentials'] = True
    except Exception:
        pass
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
# LLM 配置加载
# ===========================================================================

def _load_llm_cfg() -> dict:
    """从环境变量或配置文件加载 LLM 凭证"""
    cfg = {
        'api_key': os.environ.get('OPENAI_API_KEY', ''),
        'base_url': os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1'),
        'model': os.environ.get('LLM_MODEL', 'gpt-4o'),
    }
    # 尝试从项目级 .env 文件加载
    env_file = PROJECT_ROOT / '.env'
    if env_file.exists():
        with open(env_file, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    k, v = line.split('=', 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k == 'OPENAI_API_KEY':
                        cfg['api_key'] = v
                    elif k == 'OPENAI_BASE_URL':
                        cfg['base_url'] = v
                    elif k == 'LLM_MODEL':
                        cfg['model'] = v
    return cfg


# ===========================================================================
# 主流程 (v3.0 三层管道)
# ===========================================================================

def main(
    kb_name: str = '陈伟霆相亲库',
    kb_id: str = DATING_KD_ID,
    max_items: int = 0,
    skip_scrape: bool = False,
    push_to_github: bool = False,
):
    """三层管道流水线: Extractor → Parser → Validator → 去重/分流 → 输出"""

    # ---- Step 0: 环境自检 ----
    logger.info("Step 0: 环境自检...")
    env_status = check_environment()
    for key, ok in env_status.items():
        logger.info(f"  {'✅' if ok else '❌'} {key}")
    if not all(env_status.values()):
        logger.error("环境未就绪，请运行 auto_deploy.bat 安装依赖")
        sys.exit(1)

    llm_cfg = _load_llm_cfg()
    llm_available = bool(llm_cfg.get('api_key'))
    if llm_available:
        logger.info(f"  ✅ llm [{llm_cfg['model']} @ {llm_cfg['base_url']}]")
    else:
        logger.info("  ⚠️  llm [未配置，使用正则引擎 fallback]")

    # ---- Step 1: Extractor — IMA 拉取 + 网页抓取 → Markdown ----
    logger.info(f"Step 1: Extractor — 从 IMA「{kb_name}」采集 Markdown...")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    extractor = Extractor(kb_id=kb_id, kb_name=kb_name, output_dir=OUTPUT_DIR)

    if skip_scrape:
        logger.info("  (跳过抓取，仅获取媒体列表)")
        media_list = extractor.fetch_media_list(max_items=max_items)
        documents = [{"media_id": m.get("media_id", ""),
                       "title_hint": m.get("title", ""),
                       "markdown": "",
                       "source_url": m.get("url", ""),
                       "extraction_error": "skipped"} for m in media_list]
    else:
        documents = extractor.extract_batch(max_items=max_items)

    logger.info(f"  采集完成: {len(documents)} 份 Markdown 文档")

    # ---- Step 2: Parser — Markdown → RawActivity ----
    logger.info("Step 2: Parser — LLM+正则 结构化解析...")

    from parser import Parser
    parser = Parser(
        api_key=llm_cfg['api_key'],
        base_url=llm_cfg['base_url'],
        model=llm_cfg['model'],
    )

    raw_activities = parser.parse_batch(documents)
    logger.info(f"  解析完成: {len(raw_activities)} 条原始活动")

    # ---- Step 3: Validator — 字段级降级校验 ----
    logger.info("Step 3: Validator — 字段级容错校验...")

    from validator import Validator
    validator = Validator()
    activities, error_log = validator.validate(raw_activities)

    # 保存 error_log
    error_log.save(str(ERROR_LOG))
    logger.info(f"  校验完成: {len(activities)} 条合法活动 "
                f"(降级 {len(error_log.entries)} 个字段)")

    # ---- Step 4: 智能去重 + 时间分流 ----
    logger.info("Step 4a: 智能去重（地点+日期+主办方）...")

    # 分配临时 global_id 供去重使用
    for i, act in enumerate(activities):
        act['global_id'] = i + 1

    activities = deduplicate_activities(activities)

    logger.info("Step 4b: 时间分流标签计算...")
    week_monday, next_monday = get_week_range()
    logger.info(f"  本周范围: {week_monday.strftime('%m/%d')} ~ {next_monday.strftime('%m/%d')}")

    date_cat_counts: Dict[str, int] = {}
    for act in activities:
        cat = classify_date_category(act, week_monday, next_monday)
        act['date_category'] = cat
        date_cat_counts[cat] = date_cat_counts.get(cat, 0) + 1

    logger.info(f"  时间分流结果: {date_cat_counts}")

    # ---- Step 5: 生成最终输出 ----
    logger.info("Step 5: 生成 week_schedule.json...")

    output = {
        'meta': {
            'generated_at': datetime.now().isoformat(),
            'source': f'IMA 知识库: {kb_name}',
            'kb_id': kb_id,
            'total_items': len(activities),
            'pipeline_version': 'v3.0',
            'field_degradations': len(error_log.entries),
            'date_categories': date_cat_counts,
            'week_range': {
                'monday': week_monday.strftime('%Y-%m-%d'),
                'next_monday': next_monday.strftime('%Y-%m-%d'),
            },
        },
        'activities': activities,
    }

    with open(FINAL_OUTPUT, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    logger.info(f"  最终数据已保存: {FINAL_OUTPUT}")

    # 历史副本
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    archive_path = ARCHIVE_DIR / f'week_schedule_{timestamp}.json'
    with open(archive_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    logger.info(f"  历史副本: {archive_path}")

    # ---- Step 6: GitHub 推送 ----
    if push_to_github:
        logger.info("Step 6: 推送到 GitHub...")
        _push_to_github(output)

    # ---- 完成 ----
    _print_final_summary(output, error_log)
    return output


# ===========================================================================
# GitHub 推送
# ===========================================================================

def _push_to_github(output: dict):
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
        commit_msg = f'[auto] v3.0 pipeline: {output["meta"]["total_items"]} items @ {timestamp}'
        result = subprocess.run(
            ['git', 'commit', '-m', commit_msg],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )

        if 'nothing to commit' in (result.stdout + result.stderr):
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


# ===========================================================================
# 摘要输出
# ===========================================================================

def _print_final_summary(output: dict, error_log=None):
    meta = output['meta']
    activities = output['activities']

    type_counts: Dict[str, int] = {}
    for a in activities:
        for t in a.get('activity_type', []):
            type_counts[t] = type_counts.get(t, 0) + 1
        if not a.get('activity_type'):
            type_counts['未分类'] = type_counts.get('未分类', 0) + 1

    dedup_total = sum(1 for a in activities if a.get('_dedup_count', 0) > 0)
    degradations = meta.get('field_degradations', 0)

    print("\n" + "=" * 60)
    print(f"  🎯 周末约会战术指挥中心 — v3.0 模块化管道")
    print("=" * 60)
    print(f"  📊 总活动数:     {meta['total_items']}")
    print(f"  🔗 去重合并:     {dedup_total} 条")
    print(f"  🛡️  字段降级:     {degradations} 次")
    print(f"  📁 输出文件:     {FINAL_OUTPUT}")
    print(f"  📋 降级日志:     {ERROR_LOG}")
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

    ap = argparse.ArgumentParser(
        description='周末约会战术指挥中心 v3.0 — 三层模块化管道'
    )
    ap.add_argument('--kb', default='陈伟霆相亲库', help='知识库名称')
    ap.add_argument('--kb-id', default=DATING_KD_ID, help='知识库 ID')
    ap.add_argument('--max', type=int, default=0, help='最大处理条数 (0=全部)')
    ap.add_argument('--skip-scrape', action='store_true', help='跳过抓取，仅获取列表')
    ap.add_argument('--push', action='store_true', help='推送到 GitHub')

    args = ap.parse_args()
    main(
        kb_name=args.kb,
        kb_id=args.kb_id,
        max_items=args.max,
        skip_scrape=args.skip_scrape,
        push_to_github=args.push,
    )
