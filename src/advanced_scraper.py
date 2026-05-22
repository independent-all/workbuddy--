"""
============================================================================
 advanced_scraper.py — 重型自动化抓取引擎
============================================================================
 职责:
   1. Playwright 无头浏览器 → 攻克 SPA (themarryapp.com) 与微信 (mp.weixin.qq.com) 的 JS 渲染壁垒
   2. EasyOCR 中文识别引擎 → 攻克图片截图的 OCR 文字提取壁垒
   3. 统一输出纯文本，供 data_pipeline 结构化解析
============================================================================
"""

import os
import tempfile
import hashlib
import logging
import re
from pathlib import Path
from datetime import datetime

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OCR 引擎（懒加载，首次使用时初始化模型）
# ---------------------------------------------------------------------------
_ocr_reader = None


def _get_ocr():
    """懒加载 EasyOCR 读取器（中文 + 英文，CPU 模式）"""
    global _ocr_reader
    if _ocr_reader is None:
        import easyocr
        logger.info("EasyOCR 模型初始化中（首次使用，约需 10-30 秒）...")
        _ocr_reader = easyocr.Reader(['ch_sim', 'en'], gpu=False, verbose=False)
        logger.info("EasyOCR 模型加载完成")
    return _ocr_reader


# ---------------------------------------------------------------------------
# Playwright 无头浏览器 — SPA + 微信专用
# ---------------------------------------------------------------------------

class PlaywrightScraper:
    """
    基于 Playwright 的无头浏览器抓取器。
    自动识别 URL 类型，采用对应的等待策略，适配 SPA 和微信文章。
    """

    WECHAT_SELECTORS = ['#js_content', '.rich_media_content', '#page-content']
    SPA_WAIT_MS = 3000  # SPA 额外等待时间
    WECHAT_WAIT_MS = 2000  # 微信文章额外等待时间

    def __init__(self, headless: bool = True, timeout: int = 30000):
        self.headless = headless
        self.timeout = timeout

    # SPA API 拦截模式（针对 themarryapp.com 等需要登录态的 SPA）
    SPA_API_PATTERNS = [
        'wapi/orange/activity/get',   # 看准/them marry app 活动详情 API
        'wapi/zpApm',                  # 排除 APM 监控请求
    ]

    def scrape(self, url: str) -> str:
        """
        抓取一个 URL 的完整渲染后纯文本。

        Parameters
        ----------
        url : str
            目标 URL（支持 themarryapp.com / mp.weixin.qq.com / 普通页面）

        Returns
        -------
        str
            页面 body.innerText 全文（或 SPA API 拦截数据 / 直接 API 调用）
        """
        logger.info(f"Playwright 抓取: {url[:100]}")

        # themarryapp.com 特殊处理：直接从 URL 提取 activityId 调 API
        if 'themarryapp.com' in url and '/activity/detail' in url:
            api_text = self._scrape_marryapp_api(url)
            if api_text and len(api_text) > 50:
                logger.info(f"themarryapp API 直接调用成功，文本长度: {len(api_text)} 字符")
                return api_text

        # SPA API 数据收集器（必须在 goto 前注册）
        api_data_parts = []

        def _on_spa_response(response):
            resp_url = response.url
            if 'wapi/orange/activity/get' in resp_url:
                try:
                    body = response.text()
                    if body and len(body) > 50:
                        api_data_parts.append(body)
                        logger.info(f"拦截到 SPA API 数据: {len(body)} 字符")
                except Exception:
                    pass

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context(
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/148.0.0.0 Safari/537.36'
                ),
                viewport={'width': 1280, 'height': 720},
                locale='zh-CN',
            )
            page = context.new_page()

            # 在导航前注册响应拦截器
            if 'themarryapp.com' in url:
                page.on('response', _on_spa_response)

            try:
                # 第一步：导航到页面
                page.goto(url, wait_until='domcontentloaded', timeout=self.timeout)

                # 第二步：根据 URL 类型采用不同等待策略
                if 'mp.weixin.qq.com' in url:
                    self._wait_wechat(page)
                elif 'themarryapp.com' in url:
                    self._wait_spa(page)
                else:
                    self._wait_generic(page)

                # 第三步：提取全文（优先从 DOM，SPA 回退到 API 拦截数据）
                text = page.evaluate('() => document.body.innerText')

                # 如果文本过短，尝试提取 Nuxt payload
                if len(text) < 100:
                    nuxt_text = self._extract_nuxt_payload(page)
                    if nuxt_text and len(nuxt_text) > len(text):
                        text = nuxt_text

                # 如果仍然过短，尝试读取拦截的 SPA API 数据
                if len(text) < 100 and api_data_parts:
                    spa_text = self._parse_spa_api_data(api_data_parts)
                    if spa_text and len(spa_text) > len(text):
                        text = spa_text

                logger.info(f"Playwright 抓取成功，文本长度: {len(text)} 字符")
                return text

            except PlaywrightTimeout:
                logger.warning(f"页面超时，尝试获取已有内容: {url[:100]}")
                try:
                    return page.evaluate('() => document.body.innerText')
                except Exception:
                    return ''
            except Exception as e:
                logger.error(f"Playwright 抓取失败: {url[:100]} — {e}")
                return ''
            finally:
                context.close()
                browser.close()

    def _wait_wechat(self, page):
        """等待微信公众号文章内容加载"""
        for selector in self.WECHAT_SELECTORS:
            try:
                page.wait_for_selector(selector, timeout=10000)
                logger.debug(f"微信文章内容已加载 (selector={selector})")
                break
            except PlaywrightTimeout:
                continue
        page.wait_for_timeout(self.WECHAT_WAIT_MS)

    def _wait_spa(self, page):
        """等待 SPA 页面完成 JS 渲染"""
        try:
            page.wait_for_load_state('networkidle', timeout=20000)
        except PlaywrightTimeout:
            logger.debug("SPA networkidle 超时，继续尝试")
        page.wait_for_timeout(self.SPA_WAIT_MS)

    def _scrape_marryapp_api(self, url: str) -> str:
        """
        直接调用 themarryapp.com (看准) 的活动详情 API。
        从 URL 中提取 activityId，通过 /wapi/orange/activity/get 获取 JSON 数据。
        """
        import json as _json

        # 从 URL 提取 activityId
        match = re.search(r'activityId=([^&\s]+)', url)
        if not match:
            return ''
        activity_id = match.group(1)

        api_url = 'https://www.themarryapp.com/wapi/orange/activity/get'
        headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/148.0.0.0 Safari/537.36'
            ),
            'Referer': 'https://www.themarryapp.com/',
            'Accept': 'application/json',
        }

        try:
            resp = requests.get(
                api_url,
                params={'activityId': activity_id},
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get('code') != 0:
                logger.warning(f"themarryapp API 返回错误: {data.get('msg')}")
                return ''

            # 将 JSON 扁平化为可读文本
            texts = []
            self._flatten_api_json(data.get('data', {}), texts)
            return '\n'.join(texts)

        except Exception as e:
            logger.warning(f"themarryapp API 调用失败: {e}")
            return ''

    def _extract_nuxt_payload(self, page) -> str:
        """
        从 Nuxt SSR 页面提取 #__NUXT_DATA__ 中的结构化数据并转成可读文本。
        适配 themarryapp.com (zhipin.com / 看准) 等 Nuxt SPA。
        """
        import json as _json

        try:
            raw = page.evaluate(
                '() => {'
                '  const el = document.querySelector("#__NUXT_DATA__");'
                '  return el ? el.textContent : null;'
                '}'
            )
            if not raw:
                return ''

            data = _json.loads(raw)
            if not isinstance(data, list) or len(data) < 3:
                return ''

            # Nuxt payload 是扁平结构: [types_array, state_object, ...]
            # 遍历所有字符串值，提取中文内容
            texts = []
            self._walk_nuxt_payload(data, texts)

            return '\n'.join(texts)

        except Exception as e:
            logger.debug(f"Nuxt payload 提取失败: {e}")
            return ''

    @staticmethod
    def _walk_nuxt_payload(obj, collector: list, depth: int = 0):
        """递归遍历 Nuxt payload，收集有意义的中文字符串"""
        if depth > 20:
            return
        if isinstance(obj, str):
            # 过滤短字符串、URL、CSS、JS代码
            if (len(obj) > 3 and not obj.startswith(('http', 'data:', 'blob:'))
                    and not obj.startswith('_') and not '{' in obj
                    and any('\u4e00' <= c <= '\u9fff' for c in obj)):
                collector.append(obj)
        elif isinstance(obj, list):
            for item in obj:
                PlaywrightScraper._walk_nuxt_payload(item, collector, depth + 1)
        elif isinstance(obj, dict):
            for v in obj.values():
                PlaywrightScraper._walk_nuxt_payload(v, collector, depth + 1)

    def _parse_spa_api_data(self, api_data_parts: list) -> str:
        """
        从拦截的 SPA API 响应 JSON 中提取可读文本。
        适配 themarryapp.com 等通过 API 加载数据的 SPA。
        """
        import json as _json

        all_texts = []
        for part in api_data_parts:
            try:
                data = _json.loads(part)
                self._flatten_api_json(data, all_texts)
            except _json.JSONDecodeError:
                pass

        return '\n'.join(all_texts)

    @staticmethod
    def _flatten_api_json(obj, collector: list, depth: int = 0):
        """
        将 API JSON 响应扁平成 key: value 文本行。
        对中文活动数据特别优化。
        """
        if depth > 15:
            return

        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, str) and len(value) > 2:
                    # 有意义的值：直接输出为 key: value
                    if any('\u4e00' <= c <= '\u9fff' for c in value):
                        # 翻译常见英文字段名
                        key_cn = {
                            'activityName': '活动名称',
                            'title': '活动名称',
                            'activityAddress': '活动地点',
                            'address': '活动地点',
                            'activityTime': '活动时间',
                            'timeStr': '活动时间',
                            'activityEndTime': '结束时间',
                            'endTime': '结束时间',
                            'activityStartTime': '开始时间',
                            'startTime': '开始时间',
                            'signUpEndTime': '报名截止',
                            'enrollEndTime': '报名截止',
                            'enrollStartTime': '报名开始',
                            'activityPrice': '活动费用',
                            'activityDesc': '活动描述',
                            'content': '活动描述',
                            'activityType': '活动类型',
                            'activityStatus': '活动状态',
                            'organizerName': '主办方',
                            'cityName': '城市',
                            'districtName': '区域',
                            'addressDetail': '详细地址',
                            'maxPersonCount': '人数上限',
                            'minPersonCount': '人数下限',
                            'userTotalNum': '已报名人数',
                            'activityNotice': '活动须知',
                            'activityRule': '活动规则',
                            'priceDesc': '费用说明',
                            'tagList': '标签',
                            'activityCover': '封面图',
                            'signUpCount': '已报名人数',
                            'likeCount': '点赞数',
                            'shareCount': '分享数',
                            'viewCount': '浏览数',
                            'shareTitle': '分享标题',
                            'shareContent': '分享内容',
                            'friendCircleContent': '朋友圈文案',
                            'wechatCustomerName': '客服微信名',
                            'canEnrolled': '可报名',
                            'needCoupon': '需优惠券',
                            'viewType': '浏览类型',
                            'timeStatus': '时间状态',
                            'innerActivityLabel': '活动标签',
                            'activityModel': '活动模式',
                        }.get(key, key)
                        collector.append(f'{key_cn}: {value}')
                elif isinstance(value, (dict, list)):
                    PlaywrightScraper._flatten_api_json(value, collector, depth + 1)
                elif isinstance(value, (int, float)) and value > 0 and key not in ('id', 'status', 'code'):
                    collector.append(f'{key}: {value}')

        elif isinstance(obj, list):
            for item in obj:
                PlaywrightScraper._flatten_api_json(item, collector, depth + 1)


# ---------------------------------------------------------------------------
# OCR 图片文字提取
# ---------------------------------------------------------------------------

class OCRProcessor:
    """基于 EasyOCR 的图片文字提取器，专注中文识别"""

    def __init__(self, confidence_threshold: float = 0.35):
        self.threshold = confidence_threshold

    def extract_text(self, image_path: str) -> str:
        """
        对图片执行 OCR 识别，返回提取的纯文本。

        Parameters
        ----------
        image_path : str
            本地图片文件路径

        Returns
        -------
        str
            识别到的文本（按行拼接）
        """
        logger.info(f"OCR 识别图片: {image_path}")
        reader = _get_ocr()
        results = reader.readtext(image_path)

        lines = []
        for bbox, text, confidence in results:
            if confidence >= self.threshold:
                lines.append(text)

        text = '\n'.join(lines)
        logger.info(f"OCR 完成，识别 {len(lines)} 行，共 {len(text)} 字符")
        return text

    def download_and_extract(self, url: str, save_dir: str = None) -> tuple[str, str]:
        """
        从 URL 下载图片 → OCR 提取文字。

        Parameters
        ----------
        url : str
            图片下载地址（来自 IMA get_media_info 返回的 url_info.url）
        save_dir : str, optional
            临时保存目录，默认使用系统临时目录

        Returns
        -------
        tuple[str, str]
            (OCR文本, 本地图片路径)
        """
        if save_dir is None:
            save_dir = tempfile.mkdtemp(prefix='ima_ocr_')
        os.makedirs(save_dir, exist_ok=True)

        # 下载图片
        logger.info(f"下载图片: {url[:100]}")
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()

        # 推断扩展名
        ct = resp.headers.get('Content-Type', '')
        ext = '.jpg'
        if 'png' in ct:
            ext = '.png'
        elif 'webp' in ct:
            ext = '.webp'
        elif 'gif' in ct:
            ext = '.gif'

        # 用 URL hash 做文件名，避免冲突
        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
        img_path = os.path.join(save_dir, f'{url_hash}{ext}')

        with open(img_path, 'wb') as f:
            f.write(resp.content)
        logger.info(f"图片已保存: {img_path} ({os.path.getsize(img_path)} bytes)")

        # OCR
        text = self.extract_text(img_path)
        return text, img_path


# ---------------------------------------------------------------------------
# 统一接口
# ---------------------------------------------------------------------------

class AdvancedScraper:
    """
    统一抓取入口，自动路由到 Playwright 或 OCR：
      - wechatarticle / weburl → Playwright
      - img / 图片扩展名 → OCR
    """

    def __init__(
        self,
        headless: bool = True,
        timeout: int = 30000,
        ocr_confidence: float = 0.35,
    ):
        self.playwright = PlaywrightScraper(headless=headless, timeout=timeout)
        self.ocr = OCRProcessor(confidence_threshold=ocr_confidence)

    def scrape(self, url: str, media_type: str = 'weburl') -> str:
        """
        根据媒体类型自动选择抓取引擎。

        Parameters
        ----------
        url : str
            目标 URL
        media_type : str
            媒体类型标识：'wechatarticle', 'weburl', 'img'

        Returns
        -------
        str
            提取的纯文本内容
        """
        if media_type == 'img':
            text, _ = self.ocr.download_and_extract(url)
            return text
        else:
            return self.playwright.scrape(url)


# ---------------------------------------------------------------------------
# 便捷函数（模块级调用）
# ---------------------------------------------------------------------------

_scraper_instance = None


def get_scraper() -> AdvancedScraper:
    """获取单例 AdvancedScraper"""
    global _scraper_instance
    if _scraper_instance is None:
        _scraper_instance = AdvancedScraper()
    return _scraper_instance


def scrape_url(url: str, media_type: str = 'weburl') -> str:
    """快速抓取单个 URL"""
    return get_scraper().scrape(url, media_type)


def ocr_image(image_path: str) -> str:
    """快速 OCR 单张图片（本地路径）"""
    return OCRProcessor().extract_text(image_path)


# ---------------------------------------------------------------------------
# 测试入口
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    import sys
    scraper = AdvancedScraper()

    if len(sys.argv) > 1:
        url = sys.argv[1]
        media_type = sys.argv[2] if len(sys.argv) > 2 else 'weburl'
        text = scraper.scrape(url, media_type)
        print(text)
    else:
        print("用法: python advanced_scraper.py <URL> [media_type]")
