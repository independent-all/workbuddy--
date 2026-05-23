"""
============================================================================
 data_pipeline.py — 活动信息结构化解析管道 (v2.4 CoT精简 + 多层防御版)
============================================================================
 职责:
   1. 优先调用 LLM (OpenAI 兼容 API) 将 raw_text 解析为结构化 JSON
   2. 若 LLM 不可用或解析失败，回退到正则引擎保底
   3. 输出标准字段：activity_title / activity_time / activity_location /
      cost / capacity / target_group / contact_info / contact_wechat /
      contact_phone / qr_url / activity_type / organizer / ...

 LLM 配置（优先级）:
   环境变量 LLM_API_KEY      → API 密钥
   环境变量 LLM_BASE_URL     → 接口地址（默认 https://api.openai.com/v1）
   环境变量 LLM_MODEL        → 模型名称（默认 gpt-4o-mini）
   配置文件 ~/.config/llm/config.json → { "api_key":..., "base_url":..., "model":... }
============================================================================
"""

import os
import re
import json
import time
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ===========================================================================
# LLM 配置加载
# ===========================================================================

LLM_CONFIG_FILE = Path.home() / '.config' / 'llm' / 'config.json'

def _load_llm_config() -> dict:
    """从环境变量或配置文件加载 LLM 配置"""
    cfg = {
        'api_key': '',
        'base_url': 'https://api.openai.com/v1',
        'model': 'gpt-4o-mini',
    }

    # 读配置文件（最低优先级）
    if LLM_CONFIG_FILE.exists():
        try:
            with open(LLM_CONFIG_FILE, encoding='utf-8') as f:
                file_cfg = json.load(f)
            cfg.update({k: v for k, v in file_cfg.items() if v})
        except Exception:
            pass

    # 环境变量覆盖（最高优先级）
    if os.environ.get('LLM_API_KEY'):
        cfg['api_key'] = os.environ['LLM_API_KEY']
    if os.environ.get('LLM_BASE_URL'):
        cfg['base_url'] = os.environ['LLM_BASE_URL'].rstrip('/')
    if os.environ.get('LLM_MODEL'):
        cfg['model'] = os.environ['LLM_MODEL']

    return cfg


# ===========================================================================
# System Prompt
# ===========================================================================

SYSTEM_PROMPT = """你是一位「活动信息结构化专家」，专门从微信公众号/海报/网页文本中提取单身联谊活动信息。

你的任务：先思考，再输出。你必须先用 _thinking_process 字段写出你的推理步骤，然后才输出具体字段值。

===== ⚠️ 思维链强制（CoT — Chain of Thought）=====

JSON 的第一个字段必须是 "_thinking_process"，格式如下：
"_thinking_process": "一步步说明你的推理：1.识别日期并转换为标准格式。2.提取地点并剥离废话。3.判断费用是否混入手机号/微信号并清理。4.提取联系方式。5.生成其余字段。"

你必须先描述清楚每一步的清洗逻辑，再填写具体字段值！

===== 输出格式（严格遵守，_thinking_process 必须在第一个！）=====

{
  "_thinking_process": "1.时间: ... 2.地点: ... 3.费用: ... 4.联系方式: ...",
  "activity_title": "简短有吸引力的活动标题，20字以内",
  "activity_time": "MM月DD日 HH:MM-HH:MM",
  "activity_location": "纯地理地址",
  "cost": "纯费用",
  "capacity": "人数",
  "target_group": ["标签"],
  "contact_info": "报名方式原文",
  "contact_wechat": "微信号",
  "contact_phone": "手机号",
  "organizer": "主办方",
  "activity_type": ["类型"],
  "is_free": false
}

===== 严苛字段约束 =====

【规则1 — activity_time】
- 强制格式：'MM月DD日 HH:MM-HH:MM'，多项用 '|' 分隔
- 禁止出现："待定""通知""详见""报名""拉群""周五""周六""周日"
- 无时间信息 → 填 '12:00-12:01'（占位符，不可为空）

【规则2 — activity_location】
- 只含省/市/区/街道/地标名
- 禁止："报名后通知""进群通知""私信""拉群""具体地址""某个""附近"
- 无地点 → 填 '北京'

【规则3 — cost】
- 只输出纯数字金额或 "免费"
- 🔴 绝对禁止填入 11 位手机号！禁止填入微信号、日期、时间文本！
- 无费用 → 填 '免费'

【规则4 — 联系方式】
- contact_wechat: 仅微信号本身（字母开头）；contact_phone: 仅 11 位手机号

【规则5 — 通用约束】
- 不编造，无信息填 '' 或 []
- 只输出纯 JSON，无 Markdown 标记

===== Few-Shot CoT 示例（必须模仿！）=====

输入：5月24日下午两点，朝阳三里屯，费用198，微信xyz123，电话13800138000。

输出：
{
  "_thinking_process": "提取198为费用，剔除电话13800138000，地点精简为北京市朝阳区三里屯",
  "activity_title": "朝阳三里屯相亲活动",
  "activity_time": "05月24日 14:00-17:00",
  "activity_location": "北京市朝阳区三里屯",
  "cost": "198",
  "capacity": "",
  "target_group": [],
  "contact_info": "微信xyz123 电话13800138000",
  "contact_wechat": "xyz123",
  "contact_phone": "13800138000",
  "organizer": "",
  "activity_type": ["联谊/相亲"],
  "is_free": false
}

请严格按以上模式输出，_thinking_process 必须最先出现！"""


# ===========================================================================
# 正则模式库（LLM 失败时的兜底）
# ===========================================================================

PATTERNS = {
    'time_date': re.compile(r'(\d{1,2}[月/.]){1,2}(\d{1,2}[日号])?'),
    'time_full': re.compile(r'((?:活动)?时间[：:]\s*[^\n]{4,40})', re.IGNORECASE),
    'time_range': re.compile(r'(\d{1,2}[:：]\d{2}\s*[-~—至到]\s*\d{1,2}[:：]\d{2})'),
    'weekday': re.compile(r'周[一二三四五六日]|星期[一二三四五六日]'),

    'location': re.compile(r'(?:地点|地址|位置)[：:]\s*([^\n]{2,60})', re.IGNORECASE),
    'district': re.compile(r'(朝阳|海淀|东城|西城|丰台|石景山|通州|大兴|昌平|顺义|房山)区'),

    'cost': re.compile(r'(?:费用|价格|票价|收费|人均|报名费)[：:]\s*([^\n]{3,30})', re.IGNORECASE),
    'cost_free': re.compile(r'(免费|不收费|0\s*元)'),
    'cost_number': re.compile(r'(\d{2,4}\s*元[/／人位]?)'),

    'type_camping': re.compile(r'(露营|野餐|帐篷|天幕)'),
    'type_speed_dating': re.compile(r'(八分钟|轮桌|速配)'),
    'type_boardgame': re.compile(r'(桌游|狼人杀|掼蛋)'),
    'type_hiking': re.compile(r'(徒步|爬山|登山)'),
    'type_party': re.compile(r'(派对|Party|微醺|脱单)'),
    'type_singing': re.compile(r'(KTV|唱歌|K歌)'),
    'type_sports': re.compile(r'(飞盘|羽毛球|篮球|骑行)'),
    'type_social': re.compile(r'(联谊|交友|相亲|社交|单身)'),
    'type_art': re.compile(r'(电影|爵士|音乐|摄影)'),
    'type_workshop': re.compile(r'(工作坊|讲座|沙龙)'),

    'registration': re.compile(r'(?:报名|扫码|添加|咨询|联系)[：:]\s*([^\n]{3,60})', re.IGNORECASE),
    'wechat_id': re.compile(r'微信[：:]\s*([a-zA-Z][a-zA-Z0-9_-]{4,19})'),
    'wechat_id_loose': re.compile(r'(?:微信|WeChat|wx|vx)[：:\s]*([a-zA-Z][a-zA-Z0-9_-]{4,19})', re.IGNORECASE),
    'phone': re.compile(r'(?:电话|手机|Tel)[：:\s]*(\d[\d\s-]{7,13})', re.IGNORECASE),
    'phone_loose': re.compile(r'\b(1[3-9]\d{9})\b'),
    'qr_code': re.compile(r'(二维码|扫码|长按)'),
    'qr_url': re.compile(r'(https?://[^\s]{5,200}(?:qrcode|qr)[^\s]*)', re.IGNORECASE),

    'source_mp': re.compile(r'(?:公众号|微信公众号)[：:]\s*([^\n]{2,40})', re.IGNORECASE),
    'capacity': re.compile(r'(\d{1,4}\s*人[以之]?[内上下]?|\d{1,3}对)'),
    'target_group': re.compile(r'((?:85|90|95|00)\s*后|硕士|博士|海归|名校|京户|京房|高知)'),
    'organizer': re.compile(r'(?:主办|承办|出品)[：:]\s*([^\n]{2,40})', re.IGNORECASE),
}

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
# 默认安全值（容错兜底）
# ===========================================================================

DEFAULT_SAFE = {
    'activity_title': '活动信息（解析中）',
    'activity_time': '12:00-12:01',           # 占位符，防止甘特图报错
    'activity_location': '北京',               # 纯地理坐标兜底，不超过4字
    'cost': '免费',
    'capacity': '',
    'target_group': [],
    'contact_info': '',
    'contact_wechat': '',
    'contact_phone': '',
    'organizer': '',
    'activity_type': [],
    'is_free': False,
    'qr_url': '',
    'registration': '',
    'source_mp': '',
    'has_qr_code': False,
}


# ===========================================================================
# LLM 调用模块
# ===========================================================================

class LLMParser:
    """
    调用 OpenAI 兼容 API 进行活动文本结构化解析。
    支持任意 base_url，适配 OpenAI / Azure / 本地 Ollama / 第三方代理。
    """

    def __init__(self):
        self._cfg = _load_llm_config()
        self._client = None
        self._available = False
        self._init_client()

    def _init_client(self):
        """初始化 openai 客户端"""
        if not self._cfg.get('api_key'):
            logger.info("LLM: 未配置 API Key，将使用正则引擎")
            return
        try:
            import openai
            self._client = openai.OpenAI(
                api_key=self._cfg['api_key'],
                base_url=self._cfg['base_url'],
                timeout=30.0,
            )
            self._available = True
            logger.info(
                f"LLM: 已初始化 [{self._cfg['model']}] @ {self._cfg['base_url']}"
            )
        except ImportError:
            logger.warning("LLM: openai 库未安装，请执行: pip install openai")
        except Exception as e:
            logger.warning(f"LLM: 初始化失败: {e}")

    @property
    def available(self) -> bool:
        return self._available and self._client is not None

    def parse(self, raw_text: str, title_hint: str = '') -> Optional[dict]:
        """
        发送 raw_text 给 LLM，返回解析后的字段字典。
        失败返回 None（由调用方决定是否用正则兜底）。

        Parameters
        ----------
        raw_text : str
            抓取/OCR 到的原始文本
        title_hint : str
            可选：IMA 中的原始标题，帮助 LLM 理解活动背景
        """
        if not self.available:
            return None

        # 截断过长文本，节省 token（保留前 2000 字）
        text_to_send = raw_text[:2000].strip()
        if not text_to_send:
            return None

        user_content = text_to_send
        if title_hint:
            user_content = f"[活动标题提示: {title_hint}]\n\n{text_to_send}"

        try:
            response = self._client.chat.completions.create(
                model=self._cfg['model'],
                messages=[
                    {'role': 'system', 'content': SYSTEM_PROMPT},
                    {'role': 'user', 'content': user_content},
                ],
                temperature=0.0,
                max_tokens=800,
                response_format={'type': 'json_object'},  # 强制 JSON 输出（gpt-4o 系列支持）
            )
            raw_json = response.choices[0].message.content or ''
            result = self._parse_json(raw_json)
            if result:
                logger.info(f"  LLM ✅ title='{result.get('activity_title','?')[:30]}'")
            return result
        except Exception as e:
            err_str = str(e)
            # 如果是参数不支持（老版本 API 不支持 response_format），降级重试
            if 'response_format' in err_str or 'json_object' in err_str:
                return self._parse_without_json_mode(user_content)
            logger.warning(f"  LLM ⚠️ 调用失败: {e}")
            return None

    def _parse_without_json_mode(self, user_content: str) -> Optional[dict]:
        """降级版：不使用 response_format 参数（兼容旧版模型）"""
        try:
            response = self._client.chat.completions.create(
                model=self._cfg['model'],
                messages=[
                    {'role': 'system', 'content': SYSTEM_PROMPT},
                    {'role': 'user', 'content': user_content},
                ],
                temperature=0.0,
                max_tokens=800,
            )
            raw_json = response.choices[0].message.content or ''
            return self._parse_json(raw_json)
        except Exception as e:
            logger.warning(f"  LLM ⚠️ 降级调用失败: {e}")
            return None

    @staticmethod
    def _parse_json(raw: str) -> Optional[dict]:
        """
        从 LLM 输出中强力提取 JSON。
        兼容所有常见 LLM 输出格式：
          1. 纯 JSON 字符串
          2. ```json ... ``` 代码块（含多行/嵌套）
          3. ` ```  ``` ` 无语言标记代码块
          4. JSON 对象混在文字说明中（前后有废话）
          5. JSON 被截断（尽力修复末尾缺失的 }）
        """
        if not raw:
            return None
        raw = raw.strip()

        # ── 第一步：剥离 Markdown 代码块包裹 ──
        # 支持 ```json、```JSON、``` 等多种写法，包含可能的空行
        code_fence = re.search(
            r'```[a-zA-Z]*\s*\n?([\s\S]*?)\n?\s*```',
            raw, re.IGNORECASE
        )
        if code_fence:
            raw = code_fence.group(1).strip()

        # ── 第二步：移除常见前缀废话 ──
        # 例如："以下是结构化结果：\n{...}" 或 "好的，这是JSON：\n{..."
        raw = re.sub(
            r'^[^{]*?(?=\{)',
            '',
            raw,
            flags=re.DOTALL
        )

        # ── 第三步：直接解析 ──
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                obj.pop('_thinking_process', None)  # 防御纵深：在 JSON 解析层即剥离 CoT
                return obj
        except json.JSONDecodeError:
            pass

        # ── 第四步：提取最外层 { ... } 块（贪婪）──
        brace_match = re.search(r'\{[\s\S]*\}', raw)
        if brace_match:
            candidate = brace_match.group(0)
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict):
                    obj.pop('_thinking_process', None)  # 防御纵深
                    return obj
            except json.JSONDecodeError:
                pass

            # ── 第五步：尝试修复截断的 JSON（补齐末尾缺失的 }）──
            # 统计未闭合的括号数量
            depth = candidate.count('{') - candidate.count('}')
            if depth > 0:
                fixed = candidate + '}' * depth
                try:
                    obj = json.loads(fixed)
                    if isinstance(obj, dict):
                        logger.info("  LLM JSON 已自动修复截断（补齐 %d 个 }）", depth)
                        obj.pop('_thinking_process', None)  # 防御纵深
                        return obj
                except json.JSONDecodeError:
                    pass

        logger.warning("  LLM JSON 解析失败，原始输出[:120]: %s", raw[:120])
        return None


# ===========================================================================
# 正则兜底解析器
# ===========================================================================

class RegexParser:
    """纯正则引擎，作为 LLM 解析失败时的 fallback"""

    def parse(self, text: str) -> dict:
        """从文本提取结构化字段，全部通过正则"""
        text = self._clean(text)
        result = {
            'activity_title': self._title(text),
            'activity_time': self._time(text),
            'activity_location': self._location(text),
            'activity_type': self._types(text),
            'cost': self._cost(text),
            'is_free': bool(PATTERNS['cost_free'].search(text)),
            'registration': self._registration(text),
            'contact_info': self._contact_info(text),
            'contact_wechat': self._wechat(text),
            'contact_phone': self._phone(text),
            'qr_url': self._qr_url(text),
            'capacity': self._capacity(text),
            'target_group': self._target_group(text),
            'organizer': self._organizer(text),
            'source_mp': self._source_mp(text),
            'has_qr_code': bool(PATTERNS['qr_code'].search(text)),
        }
        result['summary'] = self._summary(result)
        return result

    @staticmethod
    def _clean(text: str) -> str:
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r' {2,}', ' ', text)
        return text.strip()

    @staticmethod
    def _title(text: str) -> str:
        lines = text.split('\n')
        kws = ['活动', '联谊', '单身', '相亲', '派对', '露营', '周末',
               '脱单', '交友', '社交', 'KTV', '徒步', '飞盘', '桌游']
        for line in lines:
            line = line.strip()
            if 5 <= len(line) <= 80 and not line.startswith(
                    ('http', '微信', '扫码', '公众号', '长按', '活动时间', '时间：', '地点：')):
                if any(kw in line for kw in kws):
                    return line[:50]
        for line in lines:
            stripped = line.strip()
            if 5 <= len(stripped) <= 80:
                return stripped[:50]
        return text[:30].strip()

    @staticmethod
    def _time(text: str) -> str:
        parts = []
        m = PATTERNS['time_full'].search(text)
        if m:
            parts.append(m.group(1).strip())
        for m in PATTERNS['time_range'].finditer(text):
            v = m.group(1)
            if v not in parts:
                parts.append(v)
        dates = PATTERNS['time_date'].findall(text)
        if dates:
            date_strs = [''.join(d).rstrip('.') for d in dates[:2]]
            for ds in date_strs:
                if ds not in parts and len(ds) >= 3:
                    parts.append(ds)
        return '; '.join(parts[:4]) if parts else ''

    @staticmethod
    def _location(text: str) -> str:
        m = PATTERNS['location'].search(text)
        if m:
            return m.group(1).strip()
        districts = PATTERNS['district'].findall(text)
        if districts:
            return f'北京{districts[0]}区'
        return ''

    @staticmethod
    def _types(text: str) -> list:
        return [t for t, p in TYPE_KEYWORDS.items() if p.search(text)]

    @staticmethod
    def _cost(text: str) -> str:
        m = PATTERNS['cost'].search(text)
        if m:
            val = m.group(1).strip()
            # 安全检查：如果是手机号形状，跳过
            if re.match(r'^1[3-9]\d{9}$', val.replace(' ', '')):
                return ''
            return val
        for m in PATTERNS['cost_number'].finditer(text):
            return m.group(1)
        return ''

    @staticmethod
    def _registration(text: str) -> str:
        parts = []
        m = PATTERNS['registration'].search(text)
        if m:
            parts.append(m.group(1).strip())
        if PATTERNS['qr_code'].search(text):
            parts.append('(含二维码)')
        return '; '.join(parts) if parts else ''

    @staticmethod
    def _contact_info(text: str) -> str:
        """提取完整联系方式段落"""
        patterns = [
            re.compile(r'(?:报名|联系|咨询)[方式]?[：:]\s*([^\n]{3,80})'),
            re.compile(r'(?:微信|WeChat)[：:\s]+([^\n]{3,40})'),
            re.compile(r'(?:扫码|长按二维码)[^\n]{0,20}'),
        ]
        for p in patterns:
            m = p.search(text)
            if m:
                return m.group(0)[:80] if not m.lastindex else m.group(1)[:80]
        return ''

    @staticmethod
    def _wechat(text: str) -> str:
        m = PATTERNS['wechat_id'].search(text)
        if m:
            return m.group(1)
        m = PATTERNS['wechat_id_loose'].search(text)
        if m:
            return m.group(1)
        return ''

    @staticmethod
    def _phone(text: str) -> str:
        m = PATTERNS['phone'].search(text)
        if m:
            return re.sub(r'\D', '', m.group(1))
        m = PATTERNS['phone_loose'].search(text)
        if m:
            return m.group(1)
        return ''

    @staticmethod
    def _qr_url(text: str) -> str:
        m = PATTERNS['qr_url'].search(text)
        return m.group(1) if m else ''

    @staticmethod
    def _capacity(text: str) -> str:
        caps = PATTERNS['capacity'].findall(text)
        return caps[0] if caps else ''

    @staticmethod
    def _target_group(text: str) -> list:
        return list(set(PATTERNS['target_group'].findall(text)))

    @staticmethod
    def _organizer(text: str) -> str:
        m = PATTERNS['organizer'].search(text)
        return m.group(1).strip() if m else ''

    @staticmethod
    def _source_mp(text: str) -> str:
        m = PATTERNS['source_mp'].search(text)
        return m.group(1).strip() if m else ''

    @staticmethod
    def _summary(result: dict) -> str:
        parts = []
        if result.get('activity_time'):
            parts.append(result['activity_time'][:20])
        if result.get('activity_location'):
            parts.append(result['activity_location'][:15])
        if result.get('cost'):
            parts.append(result['cost'][:10])
        elif result.get('is_free'):
            parts.append('免费')
        if result.get('activity_type'):
            parts.append('/'.join(result['activity_type'][:2]))
        return ' | '.join(parts) if parts else '信息不足'


# ===========================================================================
# 主解析管道
# ===========================================================================

class DataPipeline:
    """
    活动文本结构化解析管道 v2.0 (LLM 优先 + 正则兜底)

    用法:
        pipeline = DataPipeline()
        result = pipeline.parse(raw_text, title_hint='活动标题')
    """

    def __init__(self):
        self._llm = LLMParser()
        self._regex = RegexParser()
        mode = 'LLM + 正则兜底' if self._llm.available else '纯正则（未配置 LLM）'
        logger.info(f"DataPipeline 初始化完成 [{mode}]")

    @property
    def llm_available(self) -> bool:
        return self._llm.available

    def parse(self, text: str, title_hint: str = '') -> dict[str, Any]:
        """
        解析 raw_text，优先用 LLM，失败则用正则兜底。

        Parameters
        ----------
        text : str
            原始文本（OCR / 网页抓取）
        title_hint : str
            IMA 中的原始标题，帮助 LLM 理解上下文

        Returns
        -------
        dict
            结构化活动字段
        """
        if not text or len(text.strip()) < 10:
            result = dict(DEFAULT_SAFE)
            result['error'] = '文本过短，无法解析'
            if title_hint:
                result['activity_title'] = title_hint[:30]
            return result

        # —— 1. 尝试 LLM 解析 ——
        llm_result = None
        if self._llm.available:
            llm_result = self._llm.parse(text, title_hint=title_hint)

        # —— 2. LLM 成功 → 补全缺失字段 ——
        if llm_result:
            result = dict(DEFAULT_SAFE)
            result.update(llm_result)
            result['_parsed_by'] = 'llm'
            # 从原文补充 qr_url（LLM 通常不返回这个）
            if not result.get('qr_url'):
                result['qr_url'] = self._regex._qr_url(text)
            if not result.get('source_mp'):
                result['source_mp'] = self._regex._source_mp(text)
            result['has_qr_code'] = bool(PATTERNS['qr_code'].search(text))
            result['summary'] = self._build_summary(result)
            self._validate_and_fix(result)
            return result

        # —— 3. 正则兜底 ——
        logger.info(f"  正则兜底: title_hint='{title_hint[:30]}'")
        result = self._regex.parse(text)
        result['_parsed_by'] = 'regex'
        # 用 title_hint 作为标题补充（当正则提取的标题不好时）
        if title_hint and (not result.get('activity_title') or
                           len(result.get('activity_title', '')) < 5):
            result['activity_title'] = self._clean_title_hint(title_hint)
        # 填充默认安全值
        for k, v in DEFAULT_SAFE.items():
            if k not in result:
                result[k] = v
        self._validate_and_fix(result)
        return result

    # ---- 后处理 ----

    def _build_summary(self, result: dict) -> str:
        parts = []
        if result.get('activity_time') and result['activity_time'] != DEFAULT_SAFE['activity_time']:
            parts.append(str(result['activity_time'])[:25])
        if result.get('activity_location') and result['activity_location'] != DEFAULT_SAFE['activity_location']:
            parts.append(str(result['activity_location'])[:20])
        if result.get('cost') and result['cost'] not in ('未知', '', '免费'):
            parts.append(str(result['cost'])[:10])
        elif result.get('is_free'):
            parts.append('免费')
        if result.get('activity_type'):
            parts.append('/'.join(result['activity_type'][:2]))
        return ' | '.join(parts) if parts else '信息不足'

    @staticmethod
    def _validate_and_fix(result: dict):
        """
        后处理校验：防止字段内容错填（如 cost 里出现手机号、location 出现废话）。
        保留 source_url 等关键链接字段不被污染。
        """
        # —— activity_time 校验：必须为机器可读格式 ——
        time_val = result.get('activity_time', '')
        if time_val:
            time_val = str(time_val).strip()
            # 检查是否包含禁止词
            forbidden_keywords = ['待定', '报名', '通知', '私信', '进群', '活动时间', '详见', '海报']
            if any(kw in time_val for kw in forbidden_keywords):
                result['activity_time'] = DEFAULT_SAFE['activity_time']
                logger.warning(f"  ⚠️ activity_time 含禁止词，已重置")
            # 检查是否符合标准格式 MM月DD日 HH:MM-HH:MM
            elif not re.search(r'\d{1,2}月\d{1,2}日?\s*\d{1,2}:\d{2}', time_val):
                # 如果没有月日时间格式，尝试修复
                if re.search(r'\d{1,2}:\d{2}', time_val):
                    # 有 HH:MM 但没有月日，补充占位日期
                    result['activity_time'] = '05月23日 ' + time_val
                    logger.warning(f"  ⚠️ activity_time 缺少日期，已补充占位日期")
                else:
                    result['activity_time'] = DEFAULT_SAFE['activity_time']
                    logger.warning(f"  ⚠️ activity_time 格式不符标准，已重置")
        else:
            result['activity_time'] = DEFAULT_SAFE['activity_time']

        # —— activity_time 强校验：必须包含有效时间区间（- 或 ~ 两侧有数字）——
        time_final = str(result.get('activity_time', ''))
        # 正则：HH:MM - HH:MM 或 HH:MM ~ HH:MM，宽松允许有无日期前缀
        if not re.search(r'\d{1,2}:\d{2}\s*[-~]\s*\d{1,2}:\d{2}', time_final):
            result['activity_time'] = DEFAULT_SAFE['activity_time']
            logger.warning(f"  ⚠️ activity_time 缺少有效时间区间(-/~两侧有数字)，已强制覆写为占位符")

        # —— activity_location 校验：剔除非地理信息 ——
        loc_val = result.get('activity_location', '')
        if loc_val:
            loc_val = str(loc_val).strip()
            # 检查是否全是指令性废话
            garbage_keywords = ['报名后', '进群通知', '私信', '联系', '待定', '具体地址', '活动前', '某个']
            if any(kw in loc_val for kw in garbage_keywords):
                # 尝试从原文中提取区域
                district_match = re.search(r'(朝阳|海淀|东城|西城|丰台|石景山|通州|大兴|昌平|顺义|房山)区', loc_val)
                if district_match:
                    result['activity_location'] = f'北京{district_match.group(1)}区'
                else:
                    result['activity_location'] = '北京'
                logger.warning(f"  ⚠️ activity_location 含指令性废话，已重置")
            # 过长（>80字）大概率是废话
            elif len(loc_val) > 80:
                district_match = re.search(r'(朝阳|海淀|东城|西城|丰台|石景山|通州|大兴|昌平|顺义|房山)区', loc_val)
                if district_match:
                    result['activity_location'] = f'北京{district_match.group(1)}区'
                else:
                    result['activity_location'] = '北京'
                logger.warning(f"  ⚠️ activity_location 过长({len(loc_val)}字)，已压缩")
        else:
            result['activity_location'] = '北京'

        # —— cost 安全防线：超长/非中文/手机号/日期/微信号 一律拦截 ——
        cost = str(result.get('cost', '')).strip()

        if not cost:
            result['cost'] = '免费'
        else:
            # 第1关：手机号检测
            if re.match(r'^1[3-9]\d{9}$', cost):
                result['cost'] = '详见详情'
                logger.warning(f"  ⚠️ cost 含手机号 '{cost}'，已覆写为'详见详情'")
            # 第2关：日期检测（含月/日模式）
            elif re.search(r'\d{1,2}月\d{1,2}日?', cost):
                result['cost'] = '详见详情'
                logger.warning(f"  ⚠️ cost 含日期 '{cost}'，已覆写为'详见详情'")
            # 第3关：微信号检测（字母开头5-20位）
            elif re.match(r'^[a-zA-Z][a-zA-Z0-9_-]{4,19}$', cost):
                if not result.get('contact_wechat'):
                    result['contact_wechat'] = cost
                result['cost'] = '详见详情'
                logger.warning(f"  ⚠️ cost 含微信号 '{cost}'，已迁移到 contact_wechat + 覆写")
            # 第4关：字符数超限（>5字符）== 几乎不可能是正常费用
            elif len(cost) > 5:
                result['cost'] = '详见详情'
                logger.warning(f"  ⚠️ cost 超长({len(cost)}字) '{cost}'，已覆写为'详见详情'")
            # 第5关：冗长非中文描述（连续ASCII>5 + 非标准价格格式）
            elif re.search(r'[a-zA-Z0-9\s/.:@#$%^&*]{6,}', cost) and not re.search(r'[\u4e00-\u9fff]', cost):
                result['cost'] = '详见详情'
                logger.warning(f"  ⚠️ cost 含冗长非中文 '{cost}'，已覆写为'详见详情'")

        # —— activity_title 安全检查 ——
        title = result.get('activity_title', '')
        if title and (title.startswith('http') or title.endswith(('.jpg', '.png', '.pdf'))):
            result['activity_title'] = DEFAULT_SAFE['activity_title']
            logger.warning(f"  ⚠️ activity_title 似乎是文件名/URL，已重置")

        # activity_title 不能等于 activity_time
        if title and result.get('activity_time') and title[:10] in str(result.get('activity_time', '')):
            result['activity_title'] = DEFAULT_SAFE['activity_title']

        # 确保列表字段是列表类型
        for list_field in ('target_group', 'activity_type'):
            if not isinstance(result.get(list_field), list):
                val = result.get(list_field)
                result[list_field] = [val] if val else []

        # 确保布尔字段
        if not isinstance(result.get('is_free'), bool):
            val = result.get('is_free', False)
            result['is_free'] = bool(val) if val not in ('false', 'False', '0') else False

        # —— 最后防线：删除 CoT 思维链，禁止泄露到前端 ——
        result.pop('_thinking_process', None)

    @staticmethod
    def _clean_title_hint(hint: str) -> str:
        """清洗 IMA 的原始标题（通常是文件名或转发消息头）"""
        # 移除 "好友@你一起参与【..." 格式
        m = re.search(r'【(.+?)】', hint)
        if m:
            return m.group(1)[:30]
        # 移除文件名扩展
        hint = re.sub(r'\.[a-zA-Z]{2,5}$', '', hint)
        # 移除时间戳前缀（如 "Screenshot_2026-..."）
        if hint.startswith('Screenshot') or hint.startswith('IMG_'):
            return DEFAULT_SAFE['activity_title']
        return hint[:30]

    # ---- 兼容旧接口 ----
    # 以下方法保留供 main.py 直接调用（向后兼容）

    def _extract_registration(self, text: str) -> str:
        return self._regex._registration(text)

    def _extract_contact_wechat(self, text: str) -> str:
        return self._regex._wechat(text)

    def _extract_contact_phone(self, text: str) -> str:
        return self._regex._phone(text)

    def _extract_qr_url(self, text: str) -> str:
        return self._regex._qr_url(text)


# ===========================================================================
# 批量处理
# ===========================================================================

def parse_batch(texts: list[str], title_hints: list[str] = None) -> list[dict]:
    """批量解析多条文本"""
    pipeline = DataPipeline()
    results = []
    for i, text in enumerate(texts):
        hint = (title_hints or [])[i] if title_hints and i < len(title_hints) else ''
        result = pipeline.parse(text, title_hint=hint)
        result['_index'] = i
        results.append(result)
    return results


# ===========================================================================
# 依赖安装提示
# ===========================================================================

def check_llm_deps() -> bool:
    """检查 LLM 相关依赖是否已安装"""
    try:
        import openai
        return True
    except ImportError:
        return False


# ===========================================================================
# 测试入口
# ===========================================================================

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
    )

    # 检查依赖
    if not check_llm_deps():
        print("⚠️ openai 库未安装，请执行: pip install openai")
        print("   将使用正则引擎作为 fallback")

    # 检查 LLM 配置
    cfg = _load_llm_config()
    if cfg.get('api_key'):
        print(f"✅ LLM 已配置: model={cfg['model']} @ {cfg['base_url']}")
    else:
        print("ℹ️  未配置 LLM API Key，将使用正则引擎")
        print("   配置方式（三选一）:")
        print("   1. 环境变量: set LLM_API_KEY=sk-xxxx && set LLM_BASE_URL=... && set LLM_MODEL=...")
        print("   2. 配置文件: ~/.config/llm/config.json")
        print('      {"api_key":"sk-xxx","base_url":"https://api.openai.com/v1","model":"gpt-4o-mini"}')

    # 测试解析
    sample = """
活动时间：5月24日（周日）15:00-19:00
活动地点：北京市海淀区五道口附近（具体地址报名后通知）
活动类型：大型单身联谊会，八分钟轮桌约会
费用：男118元/人，女78元/人
报名方式：添加微信 love2024 报名，或扫描下方二维码
活动规模：350人
目标人群：85后-00后，本硕博
主办方：京城盛恋
联系手机：13912345678
    """

    print("\n=== 测试解析 ===")
    pipeline = DataPipeline()
    result = pipeline.parse(sample, title_hint='5月24日北京单身联谊')
    print(json.dumps(result, ensure_ascii=False, indent=2))
