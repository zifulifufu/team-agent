"""轻量全文检索:中英文分词 + BM25。资料库和记忆库共用,不依赖外部服务。

中文没有空格,按「相邻两字」切成二元组(北京大学 -> 北京/京大/大学),
英文和数字按单词切。这是最朴素但对中文检索足够好用的做法。
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

_WORD = re.compile(r"[A-Za-z0-9_]+|[一-鿿]+")  # i18n-keep: Chinese-aware tokenizer: the ranges and stop words are the algorithm, not copy
_CJK = re.compile(r"[一-鿿]+")  # i18n-keep: Chinese-aware tokenizer: the ranges and stop words are the algorithm, not copy
STOP = set("的了是在和与及或就都而也很把被对为这那有你我他她它们吗呢吧啊呀请帮要能会") | {"the", "a", "an", "of", "to", "in", "on", "at", "is", "are", "and", "or", "for", "with"}  # i18n-keep: Chinese-aware tokenizer: the ranges and stop words are the algorithm, not copy


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for w in _WORD.findall(text.lower()):
        if _CJK.fullmatch(w):
            if len(w) == 1:
                if w not in STOP:
                    out.append(w)
            else:
                out += [w[i:i + 2] for i in range(len(w) - 1)]
        elif len(w) > 1 and w not in STOP:
            out.append(w)
    return out


def chunk_text(text: str, size: int = 600, overlap: int = 80) -> list[str]:
    """按段落合并成约 size 字的块;超长段落按句号等标点再切,相邻块保留少量重叠。"""
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    pieces: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            pieces.append(para)
            continue
        buf = ""
        for sent in re.split(r"(?<=[。!?!?;;\n.])", para):  # i18n-keep: Chinese-aware tokenizer: the ranges and stop words are the algorithm, not copy
            if len(buf) + len(sent) > size and buf:
                pieces.append(buf)
                buf = ""
            while len(sent) > size:  # 没有标点的超长句子硬切
                pieces.append(sent[:size])
                sent = sent[size:]
            buf += sent
        if buf.strip():
            pieces.append(buf)
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        if cur and len(cur) + len(p) + 2 > size:
            chunks.append(cur)
            tail = cur[-overlap:] if overlap else ""
            cur = (tail + "\n" + p) if tail and len(p) + len(tail) < size else p
        else:
            cur = (cur + "\n\n" + p) if cur else p
    if cur.strip():
        chunks.append(cur)
    return [c.strip() for c in chunks if c.strip()]


def join_chunks(chunks: list[str], overlap: int = 80) -> str:
    """把 chunk_text 切出来的块还原成连续文本:去掉相邻块之间为检索而保留的重叠部分。"""
    out = ""
    prev = ""
    for c in chunks:
        if not out:
            out = c
        else:
            cut = 0
            for o in range(min(len(prev), overlap), 7, -1):
                needle = prev[-o:].strip()
                if len(needle) >= 8 and c.startswith(needle + "\n"):
                    cut = len(needle) + 1
                    break
            out += "\n\n" + c[cut:] if cut else "\n\n" + c
        prev = c
    return out


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.lens = [len(d) for d in docs]
        self.avg = (sum(self.lens) / self.n) if self.n else 0.0
        self.index: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, d in enumerate(docs):
            for tok, tf in Counter(d).items():
                self.index[tok].append((i, tf))

    def scores(self, query: list[str]) -> dict[int, float]:
        out: dict[int, float] = defaultdict(float)
        for tok in set(query):
            postings = self.index.get(tok)
            if not postings:
                continue
            idf = math.log(1 + (self.n - len(postings) + 0.5) / (len(postings) + 0.5))
            for i, tf in postings:
                denom = tf + self.k1 * (1 - self.b + self.b * self.lens[i] / (self.avg or 1))
                out[i] += idf * tf * (self.k1 + 1) / denom
        return out
