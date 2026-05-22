"""
============================================================================
 ima_sync.py — 腾讯 IMA 知识库全自动同步引擎 (v3.0 分批节流版)
============================================================================
 职责:
   1. 自动调用 IMA OpenAPI，获取知识库中所有媒体列表
   2. 获取每条媒体的真实 URL / 图片下载地址
   3. 根据媒体类型路由到 advanced_scraper 进行强力提取
   4. 将提取结果喂给 data_pipeline 做结构化解析
   5. 输出标准化 JSON 数据集
   6. [NEW] 缓存增量同步：24h 内已抓取的 media_id 直接读缓存
   7. [NEW] 分批节流：5条/批，批内间隔0.5s，批间休息5s
   8. [NEW] 灾难恢复：触发限额时立即熔断所有 API 请求，保留已有数据
============================================================================
"""

import os
import json
import time
import logging
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
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

# 知识库常量（严格锁定：陈伟霆相亲库）
DATING_KD_ID = 'QDwLweZ4q6hKWAUOLc4oQxMjM-EVf1kPKFZy--cmrYk='
DATING_KB_NAME = '陈伟霆相亲库'

PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_DIR = PROJECT_ROOT / 'output'
CACHE_FILE = PROJECT_ROOT / 'output' / 'sync_cache.json'
CACHE_TTL_HOURS = 24  # 缓存有效期

# ===========================================================================
# 异常类
# ===========================================================================


class RateLimitError(RuntimeError):
    """IMA API 调用频率已达上限"""
    pass


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
        """发送 IMA API POST 请求，自动检测频率限制"""
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
            msg = data.get('msg', '')
            # 检测频率限制关键词
            if '已达上限' in msg or '次数已达上限' in msg or '请明天再尝试' in msg:
                raise RateLimitError(f"IMA API 频率限制 [{api_path}]: {msg}")
            raise RuntimeError(f"IMA API 错误 [{api_path}]: {msg}")

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
        except RateLimitError:
            # 频率限制：直接向上抛出，由调用方处理灾难恢复
            raise
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
# 缓存管理
# ===========================================================================


class SyncCache:
    """sync_cache.json 读写封装"""

    def __init__(self, cache_path: Path = CACHE_FILE):
        self.cache_path = cache_path
        self.data: dict[str, dict] = {}
        self._load()

    def _load(self):
        """加载缓存文件"""
        if self.cache_path.exists():
            try:
                with open(self.cache_path, 'r', encoding='utf-8') as f:
                    self.data = json.load(f)
                logger.info(f"缓存已加载: {len(self.data)} 条记录 ({self.cache_path})")
            except Exception as e:
                logger.warning(f"缓存加载失败，将使用空缓存: {e}")
                self.data = {}
        else:
            logger.info("缓存文件不存在，将创建新缓存")

    def save(self):
        """持久化缓存"""
        try:
            with open(self.cache_path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"缓存保存失败: {e}")

    def get(self, media_id: str) -> Optional[dict]:
        """
        获取缓存条目，如果不存在或已过期则返回 None。
        
        Returns
        -------
        dict or None
            包含 url, fetched_at, raw_text, parsed_data 的字典
        """
        entry = self.data.get(media_id)
        if not entry:
            return None

        fetched_at_str = entry.get('fetched_at', '')
        if not fetched_at_str:
            return None

        try:
            fetched_at = datetime.fromisoformat(fetched_at_str)
            if datetime.now() - fetched_at > timedelta(hours=CACHE_TTL_HOURS):
                logger.debug(f"  [缓存] {media_id[:20]}... 已过期 ({fetched_at_str})")
                return None
        except ValueError:
            return None

        logger.info(f"  [缓存命中] {media_id[:20]}... → URL: {(entry.get('url','') or '')[:40]}")
        return entry

    def set(self, media_id: str, url: str = '', raw_text: str = '',
            parsed_data: dict = None):
        """写入缓存"""
        self.data[media_id] = {
            'url': url,
            'fetched_at': datetime.now().isoformat(),
            'raw_text': raw_text,
            'parsed_data': parsed_data or {},
        }

    def get_url(self, media_id: str) -> Optional[str]:
        """仅获取缓存的 URL"""
        entry = self.get(media_id)
        return entry.get('url') if entry else None

    def has_valid(self, media_id: str) -> bool:
        """检查是否存在有效缓存"""
        return self.get(media_id) is not None


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
    核心同步引擎 v3.0：
      1. 拉取知识库所有媒体
      2. 智能跳过：24h 内已抓取的 media_id 直接读缓存
      3. 分批节流：5条/批，批内间隔 0.5s，批间休息 5s
      4. 灾难恢复：触发限额时立即熔断所有 API 请求，保留已有数据
      5. 调用 AdvancedScraper 强力提取
      6. 调用 DataPipeline 结构化解析
      7. 输出标准化结果
    """

    def __init__(
        self,
        kb_id: str = DATING_KD_ID,
        kb_name: str = DATING_KB_NAME,
        output_dir: Path = OUTPUT_DIR,
    ):
        self.kb_id = kb_id
        self.kb_name = kb_name
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.client = IMAClient()
        self.scraper = AdvancedScraper() if AdvancedScraper else None
        self.pipeline = DataPipeline() if DataPipeline else None

        self.cache = SyncCache()
        self.records: list[MediaRecord] = []
        self._rate_limited = False  # 灾难恢复标记

    # ---- 主流程 ----

    def sync(self, max_items: int = 0) -> list[MediaRecord]:
        """
        执行全量同步（含缓存增量 + 频率控制 + 灾难恢复）。

        Parameters
        ----------
        max_items : int
            最大处理条数（0=全部）

        Returns
        -------
        list[MediaRecord]
            处理后的记录列表（即使中途触发限额，也会返回已获取的数据）
        """
        logger.info(f"======== 开始同步知识库「{self.kb_name}」========")
        logger.info(f"知识库 ID: {self.kb_id}")
        logger.info(f"缓存文件: {CACHE_FILE} (TTL={CACHE_TTL_HOURS}h)")
        self._rate_limited = False

        # Step 1: 获取所有媒体列表
        logger.info("Step 1/5: 获取媒体列表...")
        all_items = self.client.get_all_media(self.kb_id)
        logger.info(f"  共 {len(all_items)} 条媒体")

        if max_items and max_items < len(all_items):
            all_items = all_items[:max_items]
            logger.info(f"  限制处理前 {max_items} 条")

        # Step 2: 获取 URL（缓存增量 + 分批节流 + 灾难恢复）
        logger.info("Step 2/5: 获取媒体 URL（缓存增量 + 分批节流）...")
        enhanced_items, cache_hit_count = self._fetch_urls_with_cache(all_items)
        logger.info(f"  URL 获取完成: {sum(1 for x in enhanced_items if x['url'])} 条有 URL, "
                     f"{cache_hit_count} 条来自缓存, "
                     f"{sum(1 for x in enhanced_items if not x['url'])} 条无 URL")

        if self._rate_limited:
            logger.warning("⚠️ 已触发 IMA API 频率限制，停止后续 URL 获取")
            logger.warning("⚠️ 将使用已有数据 + 缓存数据继续生成结果")

        # Step 3: 强力提取（跳过已缓存的文本）
        logger.info("Step 3/5: 强力提取内容...")
        self.records = self._scrape_all(enhanced_items)

        # Step 4: 结构化解析（跳过已缓存的 parsed_data）
        logger.info("Step 4/5: 结构化解析...")
        self._parse_all()

        # Step 5: 保存结果 + 缓存
        logger.info("Step 5/5: 保存结果 & 缓存...")
        self._save_results()
        self.cache.save()
        logger.info(f"  缓存已持久化: {CACHE_FILE} ({len(self.cache.data)} 条)")

        logger.info(f"======== 同步完成 ========")
        self._print_summary()
        return self.records

    # ---- Step 2 实现：缓存增量 URL 获取 ----

    def _fetch_urls_with_cache(self, all_items: list[dict]) -> tuple:
        """
        获取每条媒体的 URL，优先使用缓存。
        分批节流控制 + 灾难恢复：
          - 批次大小: 5 条/批
          - 批内间隔: 0.5s（单次 API 调用后等待）
          - 批间休息: 5s（令牌桶恢复）
          - 熔断: 触发"次数已达上限"立即停止全部 API 请求

        Returns
        -------
        (enhanced_items, cache_hit_count)
        """
        BATCH_SIZE = 5
        enhanced_items = []
        cache_hit_count = 0
        need_api: list[tuple] = []  # (original_index, item, media_id, media_type)

        # ================================================================
        # Pass 1: 遍历所有条目，缓存命中直接拿 URL，未命中加入 API 队列
        # ================================================================
        for i, item in enumerate(all_items):
            media_id = item.get('media_id', '')
            media_type = IMAClient.classify_media(media_id)
            url = ''

            if media_id:
                cached_entry = self.cache.get(media_id)
                if cached_entry and cached_entry.get('url'):
                    url = cached_entry['url']
                    cache_hit_count += 1
                else:
                    need_api.append((i, item, media_id, media_type))

            enhanced_items.append({
                'item': item,
                'url': url,
                'media_type': media_type,
                'index': i + 1,
            })

        logger.info(f"  缓存命中: {cache_hit_count} 条 | 需 API 调用: {len(need_api)} 条")
        logger.info(f"  分批策略: {BATCH_SIZE}条/批 | 批内间隔0.5s | 批间休息5s")

        # ================================================================
        # Pass 2: 对需要 API 的条目分批处理，严格控制频率
        # ================================================================
        api_call_count = 0
        total_batches = (len(need_api) + BATCH_SIZE - 1) // BATCH_SIZE if need_api else 0

        for batch_idx in range(0, total_batches):
            if self._rate_limited:
                logger.warning(f"  ⚠️ 已熔断，跳过剩余 {total_batches - batch_idx} 个批次")
                break

            batch_start = batch_idx * BATCH_SIZE
            batch = need_api[batch_start:batch_start + BATCH_SIZE]
            is_last_batch = (batch_idx + 1) >= total_batches

            logger.info(f"  ── 批次 {batch_idx + 1}/{total_batches} ({len(batch)} 条) ──")

            for j, (orig_idx, item, media_id, media_type) in enumerate(batch):
                if self._rate_limited:
                    break

                is_last_in_batch = (j == len(batch) - 1)

                try:
                    info = self.client.get_media_info(media_id)
                    if info:
                        url = self.client.get_url_from_media_info(info) or ''
                        enhanced_items[orig_idx]['url'] = url
                        self.cache.set(media_id, url=url)
                        api_call_count += 1
                        logger.info(f"    [{orig_idx + 1}/{len(all_items)}] ✅ {item.get('title', '?')[:40]}")
                except RateLimitError as e:
                    logger.error(f"💥 触发 IMA API 频率限制 [{orig_idx + 1}/{len(all_items)}]: {e}")
                    logger.error(f"💥 立即熔断！已成功 {api_call_count} 条 + 缓存 {cache_hit_count} 条，剩余条目将使用已有数据继续")
                    self._rate_limited = True
                    break

                # —— 批内间隔 (0.5s)，批次最后一个不加（后面有批间休息）——
                if not is_last_in_batch and not self._rate_limited:
                    time.sleep(0.5)

            # —— 批间休息 (5s)，最后一轮不加 ——
            if not is_last_batch and not self._rate_limited:
                logger.info(f"  ⏳ 批次 {batch_idx + 1} 完成，休息 5s 让令牌桶恢复...")
                time.sleep(5)

        logger.info(f"  URL 获取完成: {sum(1 for x in enhanced_items if x['url'])} 条有 URL, "
                     f"{cache_hit_count} 条来自缓存, "
                     f"{api_call_count} 条本次 API 获取, "
                     f"{sum(1 for x in enhanced_items if not x['url'])} 条无 URL"
                     f"{' ⚠️熔断' if self._rate_limited else ''}")

        return enhanced_items, cache_hit_count

    # ---- Step 3 实现：强力提取 ----

    def _scrape_all(self, enhanced_items: list[dict]) -> list[MediaRecord]:
        """逐条提取内容（缓存有 raw_text 则跳过抓取）"""
        records = []
        for idx, item_data in enumerate(enhanced_items):
            record = MediaRecord(
                index=item_data['index'],
                item=item_data['item'],
                media_info={},
                url=item_data['url'],
            )
            record.media_type = item_data['media_type']
            media_id = item_data['item'].get('media_id', '')

            # —— 缓存检查：是否有已抓取的 raw_text ——
            cached_entry = self.cache.get(media_id) if media_id else None
            if cached_entry and cached_entry.get('raw_text'):
                record.raw_text = cached_entry['raw_text']
                logger.info(f"  [{record.index}] {record.title[:40]} — 从缓存加载文本 ({len(record.raw_text)} 字符)")
                records.append(record)
                continue

            if not record.url:
                record.error = '无可用 URL（可能是笔记类型或媒体信息为空）'
                records.append(record)
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
                    # 更新缓存
                    if media_id:
                        self.cache.set(media_id, url=record.url, raw_text=record.raw_text)
                else:
                    record.error = 'advanced_scraper 模块未加载'
            except Exception as e:
                record.error = f'抓取异常: {e}'
                logger.error(f"  [{record.index}] {record.title[:40]} — 异常: {e}")

            records.append(record)
            # 抓取间隔
            time.sleep(1)

        return records

    # ---- Step 4 实现：结构化解析 ----

    def _parse_all(self):
        """逐条解析（缓存有 parsed_data 则跳过）"""
        parsed_count = 0
        cached_parse_count = 0

        for record in self.records:
            if not record.raw_text:
                continue

            media_id = record.media_id
            # —— 缓存检查：是否有已解析数据 ——
            cached_entry = self.cache.get(media_id) if media_id else None
            if cached_entry and cached_entry.get('parsed_data'):
                record.parsed_data = cached_entry['parsed_data']
                cached_parse_count += 1
                continue

            if self.pipeline:
                try:
                    record.parsed_data = self.pipeline.parse(record.raw_text)
                    parsed_count += 1
                    # 更新缓存
                    if media_id:
                        cached_entry = self.cache.get(media_id) or {}
                        self.cache.data[media_id] = {
                            **(self.cache.data.get(media_id, {})),
                            'parsed_data': record.parsed_data,
                            'fetched_at': datetime.now().isoformat(),
                        }
                except Exception as e:
                    logger.warning(f"  [{record.index}] 解析失败: {e}")
            else:
                record.parsed_data = {'raw_text': record.raw_text}

        logger.info(f"  解析完成: {parsed_count} 条新解析, "
                     f"{cached_parse_count} 条来自缓存, "
                     f"共 {parsed_count + cached_parse_count}/{len(self.records)} 条成功")

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
                'rate_limited': self._rate_limited,
                'cache_size': len(self.cache.data),
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
            f.write(f"缓存命中: 是 | 频率限制: {'是' if self._rate_limited else '否'}\n")
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
        print(f"  频率限制:   {'是 ⚠️' if self._rate_limited else '否 ✅'}")
        print(f"  缓存条目:   {len(self.cache.data)}")
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
        print(f"知识库 ID: {DATING_KD_ID}")
        results = sync_dating_kb()

    print(f"\n同步完成，共 {len(results)} 条记录")
