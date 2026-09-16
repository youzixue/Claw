"""新闻去重 — 内容hash + 标题相似度

策略:
1. source + source_id → 精确去重
2. 内容hash(MD5) → 内容去重
3. 标题Jaccard相似度 → 近似去重
"""

import hashlib
from loguru import logger

from app.news.sources.base import NewsItem


def content_hash(text: str) -> str:
    """计算内容哈希"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def title_similarity(t1: str, t2: str) -> float:
    """标题Jaccard相似度(字级别)"""
    s1 = set(t1)
    s2 = set(t2)
    if not s1 or not s2:
        return 0.0
    return len(s1 & s2) / len(s1 | s2)


class NewsDeduplicator:
    """新闻去重器"""

    SIMILARITY_THRESHOLD = 0.7  # 标题相似度阈值

    def __init__(self):
        self._seen_ids: set[str] = set()       # source+source_id
        self._seen_hashes: set[str] = set()    # 内容hash
        self._recent_titles: list[tuple[str, str, frozenset[str]]] = []  # (来源, 标题, 个股代码)

    def is_duplicate(self, item: NewsItem) -> bool:
        """判断是否重复"""
        # 1. source_id 去重
        if item.source_id:
            key = f"{item.source}:{item.source_id}"
            if key in self._seen_ids:
                return True
            self._seen_ids.add(key)

        # 2. 内容hash 去重
        normalized_codes = frozenset(str(code or "").strip() for code in item.related_codes if str(code or "").strip())
        # 公告标题高度模板化；同一标题但关联公司不同，不能当成重复公告。
        text = f"{item.source} {'/'.join(sorted(normalized_codes))} {item.title} {item.content}"
        h = content_hash(text)
        if h in self._seen_hashes:
            return True
        self._seen_hashes.add(h)

        # 3. 标题相似度去重
        for existing_source, existing_title, existing_codes in self._recent_titles:
            same_scope = bool(
                item.source == existing_source
                and (
                    not normalized_codes
                    or not existing_codes
                    or normalized_codes.intersection(existing_codes)
                )
            )
            if same_scope and title_similarity(item.title, existing_title) > self.SIMILARITY_THRESHOLD:
                return True
        self._recent_titles.append((item.source, item.title, normalized_codes))

        # 限制内存
        if len(self._recent_titles) > 1000:
            self._recent_titles = self._recent_titles[-500:]

        return False

    def dedup(self, items: list[NewsItem]) -> list[NewsItem]:
        """批量去重"""
        unique = []
        dup_count = 0
        for item in items:
            if not self.is_duplicate(item):
                unique.append(item)
            else:
                dup_count += 1

        if dup_count > 0:
            logger.info(f"新闻去重: {len(items)} → {len(unique)} (去除{dup_count}条)")

        return unique

    def reset(self):
        """重置去重状态"""
        self._seen_ids.clear()
        self._seen_hashes.clear()
        self._recent_titles.clear()


# 全局去重器
news_dedup = NewsDeduplicator()
