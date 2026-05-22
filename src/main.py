"""
============================================================================
 main.py — 周末约会战术指挥中心 · 总控入口
============================================================================
 流程:
   Step 0: 环境自检（Playwright / EasyOCR / IMA 凭证）
   Step 1: 调用 ima_sync 从腾讯 IMA 知识库实时拉取并渲染/OCR
   Step 2: 调用 data_pipeline 结构化解析
   Step 3: 生成最终数据集 → output/week_schedule.json
   Step 4: (可选) 推送到 GitHub
============================================================================
"""

import os
import sys
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime

# main.py 位于 src/ 子目录，项目根目录为其上层
SRC_DIR = Path(__file__).parent
PROJECT_ROOT = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))

from ima_sync import IMASyncEngine, DATING_KD_ID
from data_pipeline import DataPipeline

logger = logging.getLogger(__name__)

OUTPUT_DIR = PROJECT_ROOT / 'output'
FINAL_OUTPUT = OUTPUT_DIR / 'week_schedule.json'


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
    主入口：从 IMA 知识库拉取 → 抓取 → 解析 → 输出
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

    # ---- Step 1: IMA 同步 ----
    logger.info(f"Step 1: 从 IMA 知识库「{kb_name}」拉取内容...")
    engine = IMASyncEngine(kb_id=kb_id, kb_name=kb_name, output_dir=OUTPUT_DIR)

    if skip_scrape:
        # 只获取列表，不抓取
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
            # 重新解析一次
            entry.update(pipeline.parse(record.raw_text))

        structured_data.append(entry)

    # ---- Step 3: 生成最终输出 ----
    logger.info("Step 3: 生成最终数据集...")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    output = {
        'meta': {
            'generated_at': datetime.now().isoformat(),
            'source': f'IMA 知识库: {kb_name}',
            'kb_id': kb_id,
            'total_items': len(structured_data),
            'items_with_content': sum(1 for d in structured_data if not d.get('error')),
            'items_with_error': sum(1 for d in structured_data if d.get('error')),
        },
        'activities': structured_data,
    }

    # 保存为 week_schedule.json
    with open(FINAL_OUTPUT, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    logger.info(f"  最终数据已保存: {FINAL_OUTPUT}")

    # 同时保存一个带时间戳的副本
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    archive_path = OUTPUT_DIR / f'week_schedule_{timestamp}.json'
    with open(archive_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # ---- Step 4: GitHub 推送 ----
    if push_to_github:
        logger.info("Step 4: 推送到 GitHub...")
        _push_to_github(output)

    # ---- 完成 ----
    _print_final_summary(output)
    return output


def _push_to_github(output: dict):
    """提交并推送到 GitHub"""
    import subprocess

    try:
        # 检查是否在 git 仓库中
        result = subprocess.run(
            ['git', 'rev-parse', '--git-dir'],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        if result.returncode != 0:
            logger.warning("当前不在 git 仓库中，跳过 GitHub 推送")
            return

        # 添加 output 目录下的文件
        subprocess.run(
            ['git', 'add', 'output/'],
            capture_output=True, cwd=str(PROJECT_ROOT),
        )

        # 提交
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
        commit_msg = f'[auto] IMA sync: {output["meta"]["total_items"]} items @ {timestamp}'
        result = subprocess.run(
            ['git', 'commit', '-m', commit_msg],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )

        if 'nothing to commit' in result.stdout or 'nothing to commit' in result.stderr:
            logger.info("  没有变更，跳过推送")
            return

        # 推送
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

    # 统计
    type_counts = {}
    for a in activities:
        for t in a.get('activity_type', []):
            type_counts[t] = type_counts.get(t, 0) + 1
        if not a.get('activity_type'):
            type_counts['未分类'] = type_counts.get('未分类', 0) + 1

    free_count = sum(1 for a in activities if a.get('is_free'))
    with_location = sum(1 for a in activities if a.get('activity_location'))

    print("\n" + "=" * 60)
    print(f"  🎯 周末约会战术指挥中心 — 数据同步完成")
    print("=" * 60)
    print(f"  📊 总活动数:     {meta['total_items']}")
    print(f"  ✅ 有效内容:     {meta['items_with_content']}")
    print(f"  ❌ 异常条目:     {meta['items_with_error']}")
    print(f"  🆓 免费活动:     {free_count}")
    print(f"  📍 有地点信息:   {with_location}")
    print(f"  📁 输出文件:     {FINAL_OUTPUT}")
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
