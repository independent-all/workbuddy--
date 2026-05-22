"""
============================================================================
 data_pipeline.py — 活动信息结构化解析管道
============================================================================
 职责:
   1. 从 raw_text（高级抓取器提取的纯文本）中提取结构化字段
   2. 字段: 活动标题、时间、地点、类型、费用、报名方式、来源公众号
   3. 使用正则 + 中文 NLP 启发式规则
============================================================================
"""

import re
import json
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# ===========================================================================
# 正则模式库
# ===========================================================================

PATTERNS = {
    # 时间模式
    'time_date': re.compile(
        r'(\d{1,2}[月/.]){1,2}(\d{1,2}[日号])?'
    ),
    'time_full': re.compile(
        r'((?:活动)?时间[：:]\s*[^\n]{4,40})',
        re.IGNORECASE,
    ),
    'time_range': re.compile(
        r'(\d{1,2}[:：]\d{2}\s*[-~—至到]\s*\d{1,2}[:：]\d{2})',
    ),
    'weekday': re.compile(
        r'周[一二三四五六日]|星期[一二三四五六日]',
    ),

    # 地点模式
    'location': re.compile(
        r'(?:地点|地址|位置|坐标)[：:]\s*([^\n]{2,60})',
        re.IGNORECASE,
    ),
    'location_kw': re.compile(
        r'(北京|上海|广州|深圳|杭州|成都|武汉|南京|天津|重庆|苏州|西安|长沙|青岛|大连|厦门|郑州|东莞|合肥|佛山|沈阳|昆明|济南|无锡|宁波)'
        r'(?:市)?(?:[^\n,，。；]{0,30})',
    ),
    'district': re.compile(
        r'(朝阳|海淀|东城|西城|丰台|石景山|通州|大兴|昌平|顺义|房山|门头沟|怀柔|平谷|密云|延庆)区',
    ),

    # 费用模式
    'cost': re.compile(
        r'(?:费用|价格|票价|收费|人均|报名费)[：:]\s*([^\n]{3,30})',
        re.IGNORECASE,
    ),
    'cost_free': re.compile(
        r'(免费|不收费|无需费用|0\s*元|无费用)',
    ),
    'cost_number': re.compile(
        r'(\d{1,4}\s*[-~—至到]\s*\d{1,4}\s*元[/／人位]?)',
    ),
    'cost_single': re.compile(
        r'(\d{1,4}\s*元[/／人位次场]?)',
    ),

    # 活动类型
    'type_camping': re.compile(r'(露营|野餐|帐篷|天幕|户外)'),
    'type_speed_dating': re.compile(r'(八分钟|轮桌|速配|Speed\s*Dating|闪电约会)'),
    'type_boardgame': re.compile(r'(桌游|狼人杀|剧本杀|掼蛋|UNO|三国杀|阿瓦隆)'),
    'type_hiking': re.compile(r'(徒步|爬山|登山|穿越|户外运动)'),
    'type_party': re.compile(r'(派对|Party|轰趴|聚会|酒会|微醺|脱单)'),
    'type_singing': re.compile(r'(KTV|唱歌|K歌|麦霸)'),
    'type_sports': re.compile(r'(飞盘|羽毛球|篮球|足球|网球|骑行|滑雪)'),
    'type_social': re.compile(r'(联谊|交友|相亲|社交|单身|硕博|高知|名校|海归)'),
    'type_art': re.compile(r'(电影|爵士|音乐|画展|艺术|摄影)'),
    'type_workshop': re.compile(r'(工作坊|讲座|分享会|沙龙|读书)'),

    # 报名方式
    'registration': re.compile(
        r'(?:报名|扫码|添加|咨询|联系)[：:]\s*([^\n]{3,60})',
        re.IGNORECASE,
    ),
    'wechat_id': re.compile(r'(微信[：:]\s*[a-zA-Z0-9_-]{5,20})'),
    'wechat_id_loose': re.compile(
        r'(?:微信|WeChat|wx|vx)[：:\s]*([a-zA-Z][a-zA-Z0-9_-]{4,19})',
        re.IGNORECASE,
    ),
    'phone': re.compile(
        r'(?:电话|手机|Tel|Phone|联系方式)[：:\s]*(\d[\d\s-]{7,15})',
        re.IGNORECASE,
    ),
    'phone_loose': re.compile(r'(\b1[3-9]\d{9}\b)'),  # 中国手机号
    'qr_code': re.compile(r'(二维码|扫码|长按)'),
    'qr_url': re.compile(
        r'(https?://[^\s]{5,200}qrcode[^\s]*|https?://[^\s]{5,200}qr[^\s]*)',
        re.IGNORECASE,
    ),

    # 公众号来源
    'source_mp': re.compile(
        r'(?:公众号|微信公众号|微信\s*ID)[：:]\s*([^\n]{2,40})',
        re.IGNORECASE,
    ),

    # 人数规模
    'capacity': re.compile(
        r'(\d{1,4}\s*人[以之]?[内上下]?|\d{1,3}对|规模[：:]\s*\d+)',
    ),

    # 目标人群
    'target_group': re.compile(
        r'((?:85|90|95|00)\s*后|硕士|博士|海归|名校|京户|京房|高知)',
    ),

    # 主办方
    'organizer': re.compile(
        r'(?:主办|承办|协办|出品)[：:]\s*([^\n]{2,40})',
        re.IGNORECASE,
    ),
}


# ===========================================================================
# 活动类型映射
# ===========================================================================

TYPE_KEYWORDS = {
    '露营/户外': PATTERNS['type_camping'],
    '八分钟速配': PATTERNS['type_speed_dating'],
    '桌游': PATTERNS['type_boardgame'],
    '徒步/爬山': PATTERNS['type_hiking'],
    '派对/酒会': PATTERNS['type_party'],
    'KTV/唱歌': PATTERNS['type_singing'],
    '运动': PATTERNS['type_sports'],
    '联谊/相亲': PATTERNS['type_social'],
    '艺术/电影': PATTERNS['type_art'],
    '讲座/沙龙': PATTERNS['type_workshop'],
}


# ===========================================================================
# 解析引擎
# ===========================================================================


class DataPipeline:
    """活动文本结构化解析器"""

    def __init__(self):
        pass

    def parse(self, text: str) -> dict[str, Any]:
        """
        从原始文本中提取结构化活动信息。

        Parameters
        ----------
        text : str
            抓取/OCR 得到的纯文本

        Returns
        -------
        dict
            结构化的活动字段
        """
        if not text or len(text.strip()) < 10:
            return {'error': '文本过短，无法解析'}

        text = self._clean_text(text)

        result = {
            'activity_title': self._extract_title(text),
            'activity_time': self._extract_time(text),
            'activity_location': self._extract_location(text),
            'activity_type': self._extract_types(text),
            'cost': self._extract_cost(text),
            'is_free': self._is_free(text),
            'registration': self._extract_registration(text),
            'contact_wechat': self._extract_contact_wechat(text),
            'contact_phone': self._extract_contact_phone(text),
            'qr_url': self._extract_qr_url(text),
            'capacity': self._extract_capacity(text),
            'target_group': self._extract_target_group(text),
            'organizer': self._extract_organizer(text),
            'source_mp': self._extract_source_mp(text),
            'has_qr_code': bool(PATTERNS['qr_code'].search(text)),
        }

        # 填充摘要
        result['summary'] = self._generate_summary(result)
        return result

    # ---- 文本清洗 ----

    def _clean_text(self, text: str) -> str:
        """清洗文本，去除多余空格和换行"""
        # 合并多余换行
        text = re.sub(r'\n{3,}', '\n\n', text)
        # 合并多余空格
        text = re.sub(r' {2,}', ' ', text)
        return text.strip()

    # ---- 字段提取 ----

    def _extract_title(self, text: str) -> str:
        """提取活动标题（取第一行有意义的文本）"""
        lines = text.split('\n')
        for line in lines:
            line = line.strip()
            # 过滤明显的非标题行
            if len(line) >= 5 and not line.startswith(('http', '微信', '扫码', '公众号', '长按')):
                if any(kw in line for kw in ['活动', '联谊', '单身', '相亲', '派对', '露营', '周末',
                                              '脱单', '交友', '社交', 'KTV', '徒步', '飞盘']):
                    return line[:80]
        # 回退：取第一行
        for line in lines:
            if len(line.strip()) >= 5:
                return line.strip()[:80]
        return ''

    def _extract_time(self, text: str) -> str:
        """提取活动时间"""
        time_parts = []

        # 精确时间行
        m = PATTERNS['time_full'].search(text)
        if m:
            time_parts.append(m.group(1))

        # 时间范围
        for m in PATTERNS['time_range'].finditer(text):
            time_parts.append(m.group(1))

        # 日期模式
        dates = PATTERNS['time_date'].findall(text)
        if dates:
            date_strs = [''.join(d) for d in dates[:3]]
            time_parts.extend(date_strs)

        # 星期
        weekdays = PATTERNS['weekday'].findall(text)
        if weekdays:
            time_parts.extend(weekdays[:2])

        return '; '.join(time_parts[:5]) if time_parts else ''

    def _extract_location(self, text: str) -> str:
        """提取活动地点"""
        loc_parts = []

        # 显式地点标注
        m = PATTERNS['location'].search(text)
        if m:
            loc_parts.append(m.group(1).strip())

        # 城区关键词
        districts = PATTERNS['district'].findall(text)
        if districts:
            loc_parts.extend([f'{d}区' for d in districts[:2]])

        # 城市+地标
        cities = PATTERNS['location_kw'].findall(text)
        if cities and not loc_parts:
            loc_parts.extend(cities[:2])

        return '; '.join(loc_parts[:3]) if loc_parts else ''

    def _extract_types(self, text: str) -> list[str]:
        """识别活动类型标签"""
        types = []
        for type_name, pattern in TYPE_KEYWORDS.items():
            if pattern.search(text):
                types.append(type_name)
        return types

    def _extract_cost(self, text: str) -> str:
        """提取费用信息"""
        cost_parts = []

        # 显式费用标注
        m = PATTERNS['cost'].search(text)
        if m:
            cost_parts.append(m.group(1).strip())

        # 价格范围
        for m in PATTERNS['cost_number'].finditer(text):
            cost_parts.append(m.group(1))

        # 单一价格
        for m in PATTERNS['cost_single'].finditer(text):
            cost_parts.append(m.group(1))

        # 去重
        seen = set()
        unique = []
        for c in cost_parts:
            if c not in seen:
                seen.add(c)
                unique.append(c)

        return '; '.join(unique[:3]) if unique else ''

    def _is_free(self, text: str) -> bool:
        """判断是否免费"""
        return bool(PATTERNS['cost_free'].search(text))

    def _extract_registration(self, text: str) -> str:
        """提取报名方式"""
        parts = []

        m = PATTERNS['registration'].search(text)
        if m:
            parts.append(m.group(1).strip())

        m = PATTERNS['wechat_id'].search(text)
        if m:
            parts.append(m.group(1))

        if PATTERNS['qr_code'].search(text):
            parts.append('(含二维码)')

        return '; '.join(parts) if parts else ''

    def _extract_contact_wechat(self, text: str) -> str:
        """专门提取微信号"""
        # 精确匹配
        m = PATTERNS['wechat_id'].search(text)
        if m:
            return m.group(1).replace('微信:', '').replace('微信：', '').strip()
        # 宽松匹配
        m = PATTERNS['wechat_id_loose'].search(text)
        if m:
            return m.group(1).strip()
        return ''

    def _extract_contact_phone(self, text: str) -> str:
        """专门提取电话号码"""
        # 显式标注
        m = PATTERNS['phone'].search(text)
        if m:
            return m.group(1).strip().replace(' ', '')
        # 手机号模式
        m = PATTERNS['phone_loose'].search(text)
        if m:
            return m.group(1)
        return ''

    def _extract_qr_url(self, text: str) -> str:
        """提取二维码图片链接"""
        m = PATTERNS['qr_url'].search(text)
        if m:
            return m.group(1)
        return ''

    def _extract_capacity(self, text: str) -> str:
        """提取人数规模"""
        caps = PATTERNS['capacity'].findall(text)
        return '; '.join(caps[:2]) if caps else ''

    def _extract_target_group(self, text: str) -> list[str]:
        """提取目标人群"""
        groups = PATTERNS['target_group'].findall(text)
        return list(set(groups[:5]))

    def _extract_organizer(self, text: str) -> str:
        """提取主办方"""
        m = PATTERNS['organizer'].search(text)
        return m.group(1).strip() if m else ''

    def _extract_source_mp(self, text: str) -> str:
        """提取来源公众号"""
        m = PATTERNS['source_mp'].search(text)
        return m.group(1).strip() if m else ''

    # ---- 摘要生成 ----

    def _generate_summary(self, result: dict) -> str:
        """基于解析结果生成一行摘要"""
        parts = []
        if result.get('activity_time'):
            parts.append(result['activity_time'][:20])
        if result.get('activity_location'):
            parts.append(result['activity_location'][:15])
        if result.get('cost'):
            parts.append(result['cost'][:15])
        elif result.get('is_free'):
            parts.append('免费')
        if result.get('activity_type'):
            parts.append('/'.join(result['activity_type'][:3]))
        return ' | '.join(parts) if parts else '信息不足'


# ===========================================================================
# 批量处理
# ===========================================================================


def parse_batch(texts: list[str]) -> list[dict]:
    """批量解析多条文本"""
    pipeline = DataPipeline()
    results = []
    for i, text in enumerate(texts):
        result = pipeline.parse(text)
        result['_index'] = i
        results.append(result)
    return results


def parse_and_save(texts: list[str], output_path: str):
    """批量解析并保存为 JSON"""
    results = parse_batch(texts)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    logger.info(f"解析结果已保存: {output_path}")
    return results


# ===========================================================================
# 测试
# ===========================================================================

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    # 测试样例
    sample = """
活动时间：5月24日（周日）15:00-19:00
活动地点：北京市海淀区五道口附近（具体地址报名后通知）
活动类型：大型单身联谊会，八分钟轮桌约会
费用：男118元/人，女78元/人
报名方式：添加微信 love2024 报名
活动规模：350人
目标人群：85后-00后，本硕博
主办方：京城盛恋
    """

    pipeline = DataPipeline()
    result = pipeline.parse(sample)
    print(json.dumps(result, ensure_ascii=False, indent=2))
