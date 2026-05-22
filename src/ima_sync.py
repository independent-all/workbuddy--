"""
============================================================================
 ima_sync.py — 腾讯 IMA 知识库全自动同步引擎
============================================================================
 职责:
   1. 自动调用 IMA OpenAPI，获取知识库中所有媒体列表
   2. 获取每条媒体的真实 URL / 图片下载地址
   3. 根据媒体类型路由到 advanced_scraper 进行强力提取
   4. 将提取结果喂给 data_pipeline 做结构化解析
   5. 输出标准化 JSON 数据集
============================================================================
"""

import os
import json
import time
import logging
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Optional

import requests

# 尝试导入项目内模块
try:
    from advanced_scraper import AdvancedScraper
except ImportError:
    AdvancedScraper = None

try:
    from data_pipeline import DataPipeline
except ImportError:
    DataPipeline = None

logger = logging.getLogger(__name__)

# ===========================================================================
# 配置常量
# ===========================================================================

BASE_URL = 'https://ima.qq.com'
CONFIG_DIR = Path.home() / '.config' / 'ima'
SKILL_VERSION = '1.1.7'

# 知识库常量（陈伟霆相亲库）
DATING_KD_ID = 'QDwLweZ4q6hKWAUOLc4oQxMjM-EVf1kPKFZy--cmrYk='

# 输出目录（src/ 的上层项目根目录下的 output/）
OUTPUT_DIR = Path(__file__).parent.parent / 'output'

# ===========================================================================
# IMA API 客户端
# ===========================================================================


class IMAClient:
    """IMA OpenAPI 客户端，封装认证和请求逻辑"""

    def __init__(self):
        self.client_id: str = ''
        self.api_key: str = ''
        self.session = requests.Session()
        self._load_credentials()

    # ---- 凭证加载 ----

    def _load_credentials(self):
        """从环境变量或配置文件加载凭证"""
        # 1. 环境变量
        self.client_id = (
            os.environ.get('IMA_CLIENT_ID')
            or os.environ.get('IMA_OPENAPI_CLIENTID')
            or ''
        )
        self.api_key = (
            os.environ.get('IMA_API_KEY')
            or os.environ.get('IMA_OPENAPI_APIKEY')
            or ''
        )

        # 2. 配置文件
        if not self.client_id:
            cid_file = CONFIG_DIR / 'client_id'
            if cid_file.exists():
                self.client_id = cid_file.read_text().strip()

        if not self.api_key:
            key_file = CONFIG_DIR / 'api_key'
            if key_file.exists():
                self.api_key = key_file.read_text().strip()

        if not self.client_id or not self.api_key:
            raise RuntimeError(
                "未找到 IMA 凭证！请设置环境变量 IMA_CLIENT_ID/IMA_API_KEY，"
                f"或将凭证放置在 {CONFIG_DIR}/ 目录下 (client_id, api_key 文件)"
            )

        logger.info(f"IMA 凭证已加载 (ClientID: {self.client_id[:8]}...)")

    # ---- HTTP 请求 ----

    def _post(self, api_path: str, body: dict) -> dict:
        """发送 IMA API POST 请求"""
        url = f"{BASE_URL}/{api_path}"
        headers = {
            'ima-openapi-clientid': self.client_id,
            'ima-openapi-apikey': self.api_key,
            'ima-openapi-ctx': f'skill_version={SKILL_VERSION}',
            'Content-Type': 'application/json',
        }
        resp = self.session.post(url, headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get('code') != 0:
            raise RuntimeError(f"IMA API 错误 [{api_path}]: {data.get('msg')}")
        return data.get('data', {})

    # ---- 知识库搜索 ----

    def search_knowledge_base(self, query: str, limit: int = 10) -> list[dict]:
        """
        搜索知识库列表。
        传空字符串 query='' 获取全部知识库。
        """
        cursor = ''
        all_results = []
        while True:
            data = self._post('openapi/wiki/v1/search_knowledge_base', {
                'query': query,
                'cursor': cursor,
                'limit': limit,
            })
            info_list = data.get('info_list', [])
            all_results.extend(info_list)
            if data.get('is_end', True):
                break
            cursor = data.get('next_cursor', '')
            if not cursor:
                break
        return all_results

    def get_knowledge_base_info(self, kb_ids: list[str]) -> list[dict]:
        """获取知识库详细信息"""
        data = self._post('openapi/wiki/v1/get_knowledge_base', {
            'ids': kb_ids,
        })
        return data.get('knowledge_base_infos', [])

    # ---- 内容列表遍历 ----

    def get_all_media(self, kb_id: str) -> list[dict]:
        """
        遍历知识库中所有媒体条目（含子文件夹递归），返回扁平列表。
        每条记录包含: title, media_id, media_type, folder_path
        """
        all_items = []
        self._walk_folder(kb_id, '', [], all_items)
        return all_items

    def _walk_folder(self, kb_id: str, folder_id: str,
                     breadcrumb: list[str], collector: list[dict]):
        """递归遍历文件夹"""
        cursor = ''
        while True:
            body = {
                'knowledge_base_id': kb_id,
                'cursor': cursor,
                'limit': 50,
            }
            if folder_id:
                body['folder_id'] = folder_id

            data = self._post('openapi/wiki/v1/get_knowledge_list', body)

            # 收集文件/链接
            for item in data.get('knowledge_list', []):
                item['_folder_path'] = '/'.join(breadcrumb) if breadcrumb else '(根目录)'
                collector.append(item)

            # 递归处理子文件夹
            for folder in data.get('folders', []):
                sub_path = breadcrumb + [folder.get('name', '未命名')]
                self._walk_folder(kb_id, folder['folder_id'], sub_path, collector)

            if data.get('is_end', True):
                break
            cursor = data.get('next_cursor', '')
            if not cursor:
                break

    # ---- 媒体信息 ----

    def get_media_info(self, media_id: str) -> Optional[dict]:
        """获取媒体原始信息（含 URL / 图片下载地址）"""
        try:
            data = self._post('openapi/wiki/v1/get_media_info', {
                'media_id': media_id,
            })
            return data
        except Exception as e:
            logger.warning(f"获取媒体信息失败 [{media_id}]: {e}")
            return None

    def get_url_from_media_info(self, info: dict) -> Optional[str]:
        """从 get_media_info 返回中提取 URL"""
        url_info = info.get('url_info', {})
        return url_info.get('url', '') if url_info else None

    # ---- 媒体类型分类 ----

    @staticmethod
    def classify_media(media_id: str) -> str:
        """
        根据 media_id 前缀分类:
          - wechatarticle_*  → 微信公众号文章
          - weburl_*         → 网页链接
          - img_*            → 图片文件
          - file_*           → 其他文件
          - note_*           → 笔记
        """
        if not media_id:
            return 'unknown'
        prefix = media_id.split('_')[0] if '_' in media_id else media_id
        return prefix


# ===========================================================================
# 媒体记录模型
# ===========================================================================

class MediaRecord:
    """单条媒体记录的统一数据结构"""

    __slots__ = (
        'index', 'title', 'media_id', 'media_type', 'source_type',
        'url', 'folder_path', 'raw_text', 'parsed_data', 'error',
    )

    def __init__(self, index: int, item: dict, media_info: dict, url: str):
        self.index = index
        self.title = item.get('title', '(无标题)')
        self.media_id = item.get('media_id', '')
        self.media_type = IMAClient.classify_media(self.media_id)
        self.source_type = self.media_type  # 别名
        self.url = url or ''
        self.folder_path = item.get('_folder_path', '(根目录)')
        self.raw_text: str = ''
        self.parsed_data: dict = {}
        self.error: str = ''

    def to_dict(self) -> dict:
        return {
            'index': self.index,
            'title': self.title,
            'media_id': self.media_id,
            'source_type': self.source_type,
            'url': self.url,
            'folder_path': self.folder_path,
            'raw_text_preview': (self.raw_text or '')[:200],
            'raw_text_length': len(self.raw_text or ''),
            'parsed_data': self.parsed_data,
            'error': self.error,
        }


# ===========================================================================
# 同步引擎
# ===========================================================================


class IMASyncEngine:
    """
    核心同步引擎：
      1. 拉取知识库所有媒体
      2. 获取每条媒体的 URL
      3. 调用 AdvancedScraper 强力提取
      4. 调用 DataPipeline 结构化解析
      5. 输出标准化结果
    """

    def __init__(
        self,
        kb_id: str = DATING_KD_ID,
        kb_name: str = '陈伟霆相亲库',
        output_dir: Path = OUTPUT_DIR,
    ):
        self.kb_id = kb_id
        self.kb_name = kb_name
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.client = IMAClient()
        self.scraper = AdvancedScraper() if AdvancedScraper else None
        self.pipeline = DataPipeline() if DataPipeline else None

        self.records: list[MediaRecord] = []

    # ---- 主流程 ----

    def sync(self, max_items: int = 0) -> list[MediaRecord]:
        """
        执行全量同步。
        
        Parameters
        ----------
        max_items : int
            最大处理条数（0=全部）

        Returns
        -------
        list[MediaRecord]
            处理后的记录列表
        """
        logger.info(f"======== 开始同步知识库「{self.kb_name}」========")

        # Step 1: 获取所有媒体列表
        logger.info("Step 1/4: 获取媒体列表...")
        all_items = self.client.get_all_media(self.kb_id)
        logger.info(f"  共 {len(all_items)} 条媒体")

        if max_items and max_items < len(all_items):
            all_items = all_items[:max_items]
            logger.info(f"  限制处理前 {max_items} 条")

        # Step 2: 获取每条媒体的 URL
        logger.info("Step 2/4: 获取媒体 URL...")
        enhanced_items = []
        for i, item in enumerate(all_items):
            media_id = item.get('media_id', '')
            media_type = IMAClient.classify_media(media_id)
            url = ''

            if media_id:
                info = self.client.get_media_info(media_id)
                if info:
                    url = self.client.get_url_from_media_info(info) or ''

            enhanced_items.append({
                'item': item,
                'url': url,
                'media_type': media_type,
                'index': i + 1,
            })
            if (i + 1) % 10 == 0:
                logger.info(f"  已获取 {i+1}/{len(all_items)} 条 URL")

        logger.info(f"  URL 获取完成: {sum(1 for x in enhanced_items if x['url'])} 条有 URL, "
                     f"{sum(1 for x in enhanced_items if not x['url'])} 条无 URL")

        # Step 3: 强力提取
        logger.info("Step 3/4: 强力提取内容...")
        self.records = []
        for idx, item_data in enumerate(enhanced_items):
            record = MediaRecord(
                index=item_data['index'],
                item=item_data['item'],
                media_info={},
                url=item_data['url'],
            )
            record.media_type = item_data['media_type']

            if not record.url:
                record.error = '无可用 URL（可能是笔记类型或媒体信息为空）'
                self.records.append(record)
                logger.warning(f"  [{record.index}] {record.title[:40]} — 跳过（无 URL）")
                continue

            try:
                if self.scraper:
                    record.raw_text = self.scraper.scrape(
                        record.url,
                        media_type=record.media_type,
                    )
                    logger.info(
                        f"  [{record.index}] {record.title[:40]} — "
                        f"提取 {len(record.raw_text)} 字符"
                    )
                else:
                    record.error = 'advanced_scraper 模块未加载'
            except Exception as e:
                record.error = f'抓取异常: {e}'
                logger.error(f"  [{record.index}] {record.title[:40]} — 异常: {e}")

            self.records.append(record)
            # 每个请求间稍微延迟，避免频率限制
            time.sleep(0.5)

        # Step 4: 结构化解析
        logger.info("Step 4/4: 结构化解析...")
        parsed_count = 0
        for record in self.records:
            if record.raw_text:
                if self.pipeline:
                    try:
                        record.parsed_data = self.pipeline.parse(record.raw_text)
                        parsed_count += 1
                    except Exception as e:
                        logger.warning(f"  [{record.index}] 解析失败: {e}")
                else:
                    record.parsed_data = {'raw_text': record.raw_text}

        logger.info(f"  解析完成: {parsed_count}/{len(self.records)} 条成功")

        # Step 5: 保存结果
        self._save_results()
        logger.info(f"======== 同步完成 ========")
        self._print_summary()
        return self.records

    # ---- 结果输出 ----

    def _save_results(self):
        """将同步结果保存为 JSON 文件"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        # 完整数据
        full_data = {
            'meta': {
                'kb_name': self.kb_name,
                'kb_id': self.kb_id,
                'sync_time': datetime.now().isoformat(),
                'total_items': len(self.records),
                'with_url': sum(1 for r in self.records if r.url),
                'with_text': sum(1 for r in self.records if r.raw_text),
                'with_error': sum(1 for r in self.records if r.error),
            },
            'records': [r.to_dict() for r in self.records],
        }

        # 保存完整 JSON
        full_path = self.output_dir / f'ima_sync_{timestamp}.json'
        with open(full_path, 'w', encoding='utf-8') as f:
            json.dump(full_data, f, ensure_ascii=False, indent=2)
        logger.info(f"完整数据已保存: {full_path}")

        # 保存最新版本的快捷链接
        latest_path = self.output_dir / 'ima_sync_latest.json'
        with open(latest_path, 'w', encoding='utf-8') as f:
            json.dump(full_data, f, ensure_ascii=False, indent=2)

        # 保存纯文本汇总（方便查看）
        summary_path = self.output_dir / f'ima_sync_{timestamp}.txt'
        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write(f"知识库: {self.kb_name}\n")
            f.write(f"同步时间: {datetime.now().isoformat()}\n")
            f.write(f"{'='*60}\n\n")
            for r in self.records:
                f.write(f"[{r.index}] {r.title}\n")
                f.write(f"  来源: {r.source_type} | URL: {r.url[:100] if r.url else 'N/A'}\n")
                f.write(f"  文本长度: {len(r.raw_text)} | 错误: {r.error or '无'}\n")
                if r.parsed_data:
                    pd = r.parsed_data
                    f.write(f"  活动时间: {pd.get('activity_time', 'N/A')}\n")
                    f.write(f"  活动地点: {pd.get('activity_location', 'N/A')}\n")
                    f.write(f"  费用: {pd.get('cost', 'N/A')}\n")
                f.write('\n')

        logger.info(f"文本汇总已保存: {summary_path}")

    def _print_summary(self):
        """打印同步摘要"""
        total = len(self.records)
        with_url = sum(1 for r in self.records if r.url)
        with_text = sum(1 for r in self.records if r.raw_text)
        with_error = sum(1 for r in self.records if r.error)
        with_parsed = sum(1 for r in self.records if r.parsed_data)

        print("\n" + "=" * 60)
        print(f"  IMA 同步完成 — {self.kb_name}")
        print("=" * 60)
        print(f"  总条目:     {total}")
        print(f"  有 URL:     {with_url}")
        print(f"  已抓取文本: {with_text}")
        print(f"  已解析:     {with_parsed}")
        print(f"  异常:       {with_error}")
        print(f"  输出目录:   {self.output_dir}")
        print("=" * 60)

        # 按来源类型统计
        from collections import Counter
        type_counts = Counter(r.source_type for r in self.records)
        print("\n  来源分布:")
        for t, c in type_counts.most_common():
            print(f"    {t}: {c} 条")

    def get_records(self) -> list[MediaRecord]:
        return self.records


# ===========================================================================
# 便捷函数
# ===========================================================================


def sync_dating_kb() -> list[dict]:
    """一键同步「陈伟霆相亲库」所有内容"""
    engine = IMASyncEngine()
    records = engine.sync()
    return [r.to_dict() for r in records]


def search_and_sync(query: str) -> list[dict]:
    """
    按名称搜索知识库并同步。
    
    Parameters
    ----------
    query : str
        知识库名称关键词（如 "相亲"）
    """
    client = IMAClient()
    results = client.search_knowledge_base(query)
    if not results:
        logger.warning(f"未找到匹配 '{query}' 的知识库")
        return []

    kb = results[0]
    kb_id = kb.get('id', '')
    kb_name = kb.get('name', '未知')

    engine = IMASyncEngine(kb_id=kb_id, kb_name=kb_name)
    records = engine.sync()
    return [r.to_dict() for r in records]


# ===========================================================================
# 命令行入口
# ===========================================================================

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
    )

    import sys

    if len(sys.argv) > 1:
        query = sys.argv[1]
        print(f"搜索知识库: {query}")
        results = search_and_sync(query)
    else:
        print("默认同步: 陈伟霆相亲库")
        results = sync_dating_kb()

    print(f"\n同步完成，共 {len(results)} 条记录")
