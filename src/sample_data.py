"""
周末交友战术指挥舱 — 示例原始活动数据
模拟从 IMA 知识库拉取的原始文本记录。

每条记录包含：
  - title: 活动名称
  - organizer: 主办方
  - date_str: 日期 (YYYY-MM-DD)
  - time_range: 时间段字符串
  - fee: 费用 (元)
  - participants: 参与人数
  - address: 原始地址文本
  - raw_text: IMA 笔记中描述该活动的大段文字（用于关键词提取）
  - ima_url: IMA 知识库原始链接
"""

SAMPLE_EVENTS = [
    {
        "title": "央企海归高端相亲酒会",
        "organizer": "遇见·朝阳",
        "date_str": "2026-05-22",
        "time_range": "14:00-17:00",
        "fee": 198,
        "participants": 40,
        "address": "朝阳区大望路SOHO现代城A座301",
        "raw_text": "面向央企、金融、海归等高学历人群的周末下午茶相亲活动。参与门槛：硕士以上学历或年薪50万+，活动含专业主持人破冰环节、一对一交流与自由酒会。国企、央企员工优先。\n\n周五 朝阳区大望路",
        "full_raw_text": "面向央企、金融、海归等高学历人群的周末下午茶相亲活动。参与门槛：硕士以上学历或年薪50万+，活动含专业主持人破冰环节、一对一交流与自由酒会。国企、央企员工优先。\n\n周五 朝阳区大望路",
        "images": [],
        "ima_url": "https://ima.qq.com/kb/note/evt_001",
        "gender_ratio": "男女1:1",
        "threshold": "硕士以上学历 / 年薪50万+",
        "core_activities": "主持人破冰 → 一对一交流 → 自由酒会",
        "cover_image": "https://images.unsplash.com/photo-1519671482749-fd09be7ccebf?w=800&q=80"
    },
    {
        "title": "周五晚间飞盘社交局",
        "organizer": "盘友圈",
        "date_str": "2026-05-22",
        "time_range": "19:00-21:00",
        "fee": 68,
        "participants": 24,
        "address": "朝阳公园南门附近",
        "raw_text": "轻松户外飞盘活动，新手友好，无需经验。活动后有集体夜宵（AA制）。\n以运动交友为主，氛围轻松活泼，适合想通过户外运动拓展社交圈的小伙伴。\n\n男女不限，无门槛·新手友好\n核心环节：飞盘教学 → 分组对抗 → 集体夜宵",
        "full_raw_text": "轻松户外飞盘活动，新手友好，无需经验。活动后有集体夜宵（AA制）。\n以运动交友为主，氛围轻松活泼，适合想通过户外运动拓展社交圈的小伙伴。\n\n男女不限，无门槛·新手友好\n核心环节：飞盘教学 → 分组对抗 → 集体夜宵",
        "images": [],
        "ima_url": "https://ima.qq.com/kb/note/evt_002",
        "gender_ratio": "男女不限",
        "threshold": "无门槛·新手友好",
        "core_activities": "飞盘教学 → 分组对抗 → 集体夜宵",
        "cover_image": "https://images.unsplash.com/photo-1529900748604-07564a03e7a6?w=800&q=80"
    },
    {
        "title": "金融圈精英早午餐会",
        "organizer": "CBD鹊桥会",
        "date_str": "2026-05-23",
        "time_range": "10:30-13:00",
        "fee": 288,
        "participants": 30,
        "address": "朝阳区国贸三期B座56层云酷餐厅",
        "raw_text": "专为金融行业从业者、投行、PE/VC精英打造的高端早午餐相亲会。\n参与者多为硕士以上学历，含米其林主厨定制菜单，环境私密雅致。需着正装出席。",
        "full_raw_text": "专为金融行业从业者、投行、PE/VC精英打造的高端早午餐相亲会。\n参与者多为硕士以上学历，含米其林主厨定制菜单，环境私密雅致。需着正装出席。",
        "images": [],
        "ima_url": "https://ima.qq.com/kb/note/evt_003",
        "gender_ratio": "男女1:1（15男15女）",
        "threshold": "金融行业从业者 / 硕士以上",
        "core_activities": "米其林早午餐 → 嘉宾分享 → 圆桌轮转交流",
        "cover_image": ""
    },
    {
        "title": "剧本杀《年轮》脱单专场",
        "organizer": "谜局工作室",
        "date_str": "2026-05-23",
        "time_range": "14:00-18:00",
        "fee": 158,
        "participants": 12,
        "address": "海淀区五道口华清商务会馆B1",
        "raw_text": "经典推理剧本杀《年轮》，DM专业带队，6男6女配对。剧本杀是脱单交友的最佳方式之一，在角色扮演中自然破冰，无需尴尬自我介绍。活动含零食饮料。",
        "full_raw_text": "经典推理剧本杀《年轮》，DM专业带队，6男6女配对。剧本杀是脱单交友的最佳方式之一，在角色扮演中自然破冰，无需尴尬自我介绍。活动含零食饮料。",
        "images": [],
        "ima_url": "https://ima.qq.com/kb/note/evt_004",
        "gender_ratio": "6男6女（严格配对）",
        "threshold": "无学历门槛·趣味推理",
        "core_activities": "角色分配 → 剧本推理 → 真相复盘 → 自由交流",
        "cover_image": ""
    },
    {
        "title": "海归博士创业相亲派对",
        "organizer": "海归之家",
        "date_str": "2026-05-23",
        "time_range": "15:00-18:00",
        "fee": 268,
        "participants": 36,
        "address": "朝阳区三里屯太古里Wework",
        "raw_text": "面向海归博士、名校硕士群体的高端交流派对。主题为'创业与爱情'，邀请3位创业嘉宾分享，设置speed dating环节。参与者需提交学历证明。",
        "full_raw_text": "面向海归博士、名校硕士群体的高端交流派对。主题为'创业与爱情'，邀请3位创业嘉宾分享，设置speed dating环节。参与者需提交学历证明。",
        "images": [],
        "ima_url": "https://ima.qq.com/kb/note/evt_005",
        "gender_ratio": "男女1:1（名校优先）",
        "threshold": "海归/博士/名校硕士 · 需提交学历证明",
        "core_activities": "创业嘉宾分享 → Speed Dating → 自由社交",
        "cover_image": "https://images.unsplash.com/photo-1540575467063-178a50c2df87?w=800&q=80"
    },
    {
        "title": "露营烧烤户外脱单营",
        "organizer": "野趣户外",
        "date_str": "2026-05-24",
        "time_range": "09:00-16:00",
        "fee": 198,
        "participants": 30,
        "address": "昌平区十三陵水库附近",
        "raw_text": "一整天户外露营+烧烤+团队游戏。上午集合出发，中午BBQ，下午徒步或桌游自由活动。纯户外体验，零压力社交环境，适合喜欢大自然和运动的单身青年。",
        "full_raw_text": "一整天户外露营+烧烤+团队游戏。上午集合出发，中午BBQ，下午徒步或桌游自由活动。纯户外体验，零压力社交环境，适合喜欢大自然和运动的单身青年。",
        "images": [],
        "ima_url": "https://ima.qq.com/kb/note/evt_006",
        "gender_ratio": "男女1:1 单身优先",
        "threshold": "无门槛·户外爱好者",
        "core_activities": "集合出发 → 户外BBQ → 徒步/桌游 → 篝火夜话",
        "cover_image": "https://images.unsplash.com/photo-1504280390367-361c6d9f38f4?w=800&q=80"
    },
    {
        "title": "体制内公务员相亲下午茶",
        "organizer": "京缘汇",
        "date_str": "2026-05-24",
        "time_range": "14:00-17:00",
        "fee": 128,
        "participants": 50,
        "address": "西城区西单大悦城附近",
        "raw_text": "面向公务员、事业单位、国企等体制内人员的专场相亲活动。轻松下午茶形式，门槛为本科以上学历、有稳定体制内工作。主办方已成功配对超过200对。",
        "full_raw_text": "面向公务员、事业单位、国企等体制内人员的专场相亲活动。轻松下午茶形式，门槛为本科以上学历、有稳定体制内工作。主办方已成功配对超过200对。",
        "images": [],
        "ima_url": "https://ima.qq.com/kb/note/evt_007",
        "gender_ratio": "男女1:1 体制内限定",
        "threshold": "本科以上 / 体制内在编",
        "core_activities": "下午茶破冰 → 标签速配 → 深度交流 → 互换联系方式",
        "cover_image": ""
    },
    {
        "title": "K歌交友之夜",
        "organizer": "麦霸社",
        "date_str": "2026-05-24",
        "time_range": "19:00-22:00",
        "fee": 88,
        "participants": 16,
        "address": "朝阳区蓝色港湾KTV",
        "raw_text": "K歌交友局，包间已定，麦霸和新手都欢迎。以歌会友，气氛轻松热闹。歌曲接龙、情歌对唱等互动环节帮助破冰。",
        "full_raw_text": "K歌交友局，包间已定，麦霸和新手都欢迎。以歌会友，气氛轻松热闹。歌曲接龙、情歌对唱等互动环节帮助破冰。",
        "images": [],
        "ima_url": "https://ima.qq.com/kb/note/evt_008",
        "gender_ratio": "男女不限·自由报名",
        "threshold": "无门槛·爱唱歌即可",
        "core_activities": "自由点歌 → 歌曲接龙 → 情歌对唱 → 合唱收尾",
        "cover_image": "https://images.unsplash.com/photo-1493225457124-a3eb161ffa5f?w=800&q=80"
    },
    {
        "title": "高端红酒品鉴单身夜",
        "organizer": "Wine&Love",
        "date_str": "2026-05-23",
        "time_range": "19:30-22:00",
        "fee": 388,
        "participants": 20,
        "address": "朝阳区亮马桥官舍3层",
        "raw_text": "精品红酒品鉴会，特邀WSET三级品酒师主讲。5款新旧世界佳酿品鉴，搭配法式奶酪拼盘。参与人群以金融、外企、海归为主，着装要求business casual。",
        "full_raw_text": "精品红酒品鉴会，特邀WSET三级品酒师主讲。5款新旧世界佳酿品鉴，搭配法式奶酪拼盘。参与人群以金融、外企、海归为主，着装要求business casual。",
        "images": [],
        "ima_url": "https://ima.qq.com/kb/note/evt_009",
        "gender_ratio": "男女1:1（10男10女）",
        "threshold": "金融/外企/海归 · 商务休闲着装",
        "core_activities": "品酒教学 → 5款佳酿盲品 → 奶酪搭配 → 自由社交",
        "cover_image": ""
    },
]
