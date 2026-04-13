"""外盘主题 -> A股主题映射字典(第一版)"""

GLOBAL_TO_CN_SECTOR_MAPPING = {
    "us_ai_semiconductor": {
        "label": "美股AI/半导体",
        "a_share_themes": ["算力", "CPO", "PCB", "液冷", "服务器", "先进封装", "半导体"],
    },
    "us_ev_clean_energy": {
        "label": "美股新能源/电动车",
        "a_share_themes": ["锂电", "光伏", "储能", "逆变器", "电新设备"],
    },
    "gold": {
        "label": "黄金",
        "a_share_themes": ["黄金", "有色金属"],
    },
    "oil": {
        "label": "原油",
        "a_share_themes": ["油气", "炼化", "煤化工", "航运"],
    },
    "china_adr": {
        "label": "中概/金龙",
        "a_share_themes": ["平台经济映射", "AI应用", "软件服务", "金融科技", "传媒"],
    },
    "us_nasdaq": {
        "label": "纳斯达克",
        "a_share_themes": ["成长风格", "科技股", "AI链", "创业板"],
    },
    "us_sp500": {
        "label": "标普500",
        "a_share_themes": ["全球风险偏好", "权重白马", "核心资产"],
    },
    "rates_us10y": {
        "label": "美债10Y",
        "a_share_themes": ["成长风格", "高估值科技", "创业板"],
    },
    "fx_usdcny": {
        "label": "美元人民币",
        "a_share_themes": ["外资权重", "出口链", "风险偏好"],
    },
    "a50": {
        "label": "A50",
        "a_share_themes": ["上证50", "沪深300", "金融地产", "白马权重"],
    },
}
