"""修订号比较：支持数字、点分数字与预发布后缀（如 7、1.2、R-0042、2026.11.07）。
无法解析为数字的片段按字符串自然序比较，仍保证“同号或更旧的迟到提交不算新”。
"""
import functools
import re
import unicodedata

_NUM = re.compile(r'^[0-9]+(\.[0-9]+)*$')
_TOKEN = re.compile(r'([0-9]+(?:\.[0-9]+)*)')


def _parts(raw):
    text = unicodedata.normalize('NFKC', str(raw)).strip()
    out = []
    for part in _TOKEN.split(text):
        if not part:
            continue
        if _NUM.match(part):
            numbers = [int(p) for p in part.split('.')]
            while len(numbers) > 1 and numbers[-1] == 0:  # 3 == 3.0
                numbers.pop()
            out.append((1, tuple(numbers)))
        else:
            out.append((0, part.casefold()))
    return tuple(out)


@functools.total_ordering
class RevisionKey:
    """可排序的修订号；相等即视为同一修订。纯数字段按数值比较，避免 "10" < "9"。"""

    __slots__ = ('raw', 'parts')

    def __init__(self, raw):
        self.raw = str(raw).strip()
        if not self.raw:
            raise ValueError('修订号不能为空')
        self.parts = _parts(self.raw)

    def __eq__(self, other):
        return isinstance(other, RevisionKey) and self.parts == other.parts

    def __lt__(self, other):
        if not isinstance(other, RevisionKey):
            return NotImplemented
        return self.parts < other.parts

    def __hash__(self):
        return hash(self.parts)

    def __repr__(self):
        return f'RevisionKey({self.raw!r})'


def is_newer(candidate, current):
    """candidate 严格新于 current 时返回 True；相同或更旧为 False。"""
    return RevisionKey(candidate) > RevisionKey(current)


def compare(a, b):
    ka, kb = RevisionKey(a), RevisionKey(b)
    return 0 if ka == kb else (1 if ka > kb else -1)
