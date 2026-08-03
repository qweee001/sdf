from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass


MAX_REPLY_CHARACTERS = 4000
_SAFE_CONTEXT_PLACEHOLDER = "[內容已依帳號規則隱去]"
_LEET_TRANSLATION = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "8": "b",
        "9": "g",
    }
)
_SEXUAL_CONTENT_MARKERS = (
    "性行為",
    "性愛",
    "性交",
    "性關係",
    "上床",
    "做愛",
    "色情",
    "裸片",
    "裸照",
    "裸體",
    "露骨",
)
_MINOR_MARKERS = (
    "未成年",
    "兒童",
    "幼童",
    "男童",
    "女童",
    "小孩",
    "未滿十八歲",
    "未滿18歲",
    "十七歲",
    "十六歲",
    "十五歲",
    "十四歲",
    "十三歲",
    "十二歲",
)
_NONCONSENT_MARKERS = (
    "不同意",
    "沒同意",
    "未同意",
    "拒絕",
    "說不要",
    "不願意",
    "不情願",
    "撤回同意",
    "強迫",
    "逼迫",
    "逼她",
    "逼他",
    "失去意識",
    "昏迷",
    "灌醉",
    "迷昏",
)
_REAL_PERSON_MARKERS = (
    "真人",
    "真實人物",
    "演員",
    "歌手",
    "明星",
    "藝人",
    "網紅",
    "名人",
)
_DEEPFAKE_MARKERS = ("深偽", "換臉", "合成")
_COVERT_RECORDING_MARKERS = (
    "偷拍",
    "偷錄",
    "暗中錄",
    "偷偷錄",
    "隱藏攝影機",
    "針孔攝影",
    "藏一台相機",
    "藏相機",
)
_INTIMATE_RECORDING_MARKERS = _SEXUAL_CONTENT_MARKERS + (
    "親密行為",
    "親密畫面",
    "私密畫面",
    "床上畫面",
)
_UNDERAGE_NUMBER = re.compile(r"(?<!\d)(?:[0-9]|1[0-7])\s*歲")
_CJK_NAME_RE = re.compile(r"(?:把|將)([\u3400-\u9fff]{2,4})(?:換臉|合成)")
_HARMFUL_CONTINUATION_MARKERS = (
    "仍然",
    "還是",
    "繼續",
    "照樣",
    "無視",
    "不顧",
    "硬是",
)
_MINOR_SAFETY_RE = re.compile(
    r"(?:未成年(?:人)?|兒童|幼童|男童|女童|小孩|"
    r"未滿(?:18|十八)歲|(?:[0-9]|1[0-7])歲).{0,6}"
    r"(?:不得|不能|不可以|禁止)(?:發生|進行|有|涉及)?"
    r"(?:性行為|性愛|性交|性關係|上床|做愛)"
)
_NONCONSENT_SAFETY_RE = re.compile(
    r"(?:性行為|性愛|性交|性關係|上床|做愛).{0,8}"
    r"(?:必須|應該|應|要)(?:立即|立刻)?停止"
)
_DEEPFAKE_SAFETY_RE = re.compile(
    r"(?:不得|不能|不可以|禁止).{0,12}(?:深偽|換臉|合成)"
)
_COVERT_RECORDING_SAFETY_RE = re.compile(
    r"(?:不得|不能|不可以|禁止).{0,12}"
    r"(?:偷拍|偷錄|暗中錄|偷偷錄|藏一台相機|藏相機)"
)


def normalize_visible(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    visible = "".join(
        char
        for char in normalized
        if not unicodedata.category(char).startswith("C")
    )
    return re.sub(r"\s+", " ", visible).strip()


def compact_text(value: str) -> str:
    visible = normalize_visible(value)
    return "".join(
        char
        for char in visible
        if unicodedata.category(char)[0] in {"L", "N"}
    )


def _contains_any(value: str, markers: tuple[str, ...]) -> bool:
    return any(compact_text(marker) in value for marker in markers)


def _is_explicit_safety_statement(compact: str, pattern: re.Pattern[str]) -> bool:
    return not _contains_any(
        compact,
        _HARMFUL_CONTINUATION_MARKERS,
    ) and bool(pattern.search(compact))


def _fixed_adult_hard_floor_violation(value: str) -> bool:
    visible = normalize_visible(value[:MAX_REPLY_CHARACTERS])
    compact = compact_text(value[:MAX_REPLY_CHARACTERS])
    sexual = _contains_any(compact, _SEXUAL_CONTENT_MARKERS)
    minor = _contains_any(compact, _MINOR_MARKERS) or bool(
        _UNDERAGE_NUMBER.search(visible)
    )
    if minor and sexual and not _is_explicit_safety_statement(
        compact,
        _MINOR_SAFETY_RE,
    ):
        return True
    if (
        sexual
        and _contains_any(compact, _NONCONSENT_MARKERS)
        and not _is_explicit_safety_statement(compact, _NONCONSENT_SAFETY_RE)
    ):
        return True
    named_real_person = bool(_CJK_NAME_RE.search(visible))
    if (
        sexual
        and _contains_any(compact, _DEEPFAKE_MARKERS)
        and (_contains_any(compact, _REAL_PERSON_MARKERS) or named_real_person)
        and not _is_explicit_safety_statement(compact, _DEEPFAKE_SAFETY_RE)
    ):
        return True
    covert_intimate = _contains_any(
        compact,
        _COVERT_RECORDING_MARKERS,
    ) and _contains_any(compact, _INTIMATE_RECORDING_MARKERS)
    return covert_intimate and not _is_explicit_safety_statement(
        compact,
        _COVERT_RECORDING_SAFETY_RE,
    )


def _collapse_repeats(value: str) -> str:
    result: list[str] = []
    previous = ""
    for char in value:
        if char != previous:
            result.append(char)
        previous = char
    return "".join(result)


def _bounded_damerau_levenshtein(left: str, right: str, limit: int) -> int:
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous_previous: list[int] | None = None
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        row_minimum = left_index
        for right_index, right_char in enumerate(right, start=1):
            substitution = previous[right_index - 1] + (
                0 if left_char == right_char else 1
            )
            value = min(
                previous[right_index] + 1,
                current[right_index - 1] + 1,
                substitution,
            )
            if (
                previous_previous is not None
                and left_index > 1
                and right_index > 1
                and left_char == right[right_index - 2]
                and left[left_index - 2] == right_char
            ):
                value = min(
                    value,
                    previous_previous[right_index - 2] + 1,
                )
            current.append(value)
            row_minimum = min(row_minimum, value)
        if row_minimum > limit:
            return limit + 1
        previous_previous, previous = previous, current
    return previous[-1]


def _anchor_candidates(term: str, candidate: str, edits: int) -> set[int]:
    segments = 2 if edits == 1 else 3
    starts: set[int] = set()
    for segment_index in range(segments):
        offset = (len(term) * segment_index) // segments
        end = (len(term) * (segment_index + 1)) // segments
        anchor = term[offset:end]
        if len(anchor) < 2:
            continue
        search_from = 0
        matches = 0
        while matches < 500:
            position = candidate.find(anchor, search_from)
            if position < 0:
                break
            estimated = position - offset
            for adjustment in range(-edits, edits + 1):
                starts.add(max(estimated + adjustment, 0))
            search_from = position + 1
            matches += 1
    if not starts and len(candidate) <= 500:
        starts.update(range(max(len(candidate) - len(term) + edits + 1, 0)))
    return starts


def _approximately_contains(term: str, candidate: str) -> bool:
    length = len(term)
    if length < 4 or length > 64:
        return False
    edits = 1 if length < 8 else 2
    if len(candidate) < length - edits:
        return False
    checked: set[tuple[int, int]] = set()
    for start in _anchor_candidates(term, candidate, edits):
        for window_length in range(length - edits, length + edits + 1):
            key = (start, window_length)
            if key in checked:
                continue
            checked.add(key)
            window = candidate[start : start + window_length]
            if len(window) != window_length:
                continue
            if _bounded_damerau_levenshtein(term, window, edits) <= edits:
                return True
    return False


@dataclass(frozen=True, slots=True)
class ScreeningResult:
    blocked: bool
    reason: str = ""
    rule_kind: str = ""
    rule_index: int = -1


@dataclass(frozen=True, slots=True)
class SafeReply:
    text: str
    policy_digest: str


class BlockedReplyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _CompiledRule:
    kind: str
    index: int
    visible: str
    compact: str
    leet_compact: str
    contains_ascii_letters: bool


class ContentGuard:
    def __init__(
        self,
        blocked_terms: tuple[str, ...] = (),
        blocked_topics: tuple[str, ...] = (),
    ) -> None:
        self.blocked_terms = tuple(blocked_terms)
        self.blocked_topics = tuple(blocked_topics)
        self.rules = tuple(
            self._compile_rule("term", index, value)
            for index, value in enumerate(self.blocked_terms)
        ) + tuple(
            self._compile_rule("topic", index, value)
            for index, value in enumerate(self.blocked_topics)
        )
        encoded = json.dumps(
            {
                "terms": self.blocked_terms,
                "topics": self.blocked_topics,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.policy_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _compile_rule(kind: str, index: int, value: str) -> _CompiledRule:
        visible = normalize_visible(value)
        compact = compact_text(value)
        contains_ascii_letters = any(
            "a" <= char <= "z" for char in compact
        )
        return _CompiledRule(
            kind=kind,
            index=index,
            visible=visible,
            compact=compact,
            leet_compact=compact.translate(_LEET_TRANSLATION),
            contains_ascii_letters=contains_ascii_letters,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.rules)

    def screen(self, value: str) -> ScreeningResult:
        if not value:
            return ScreeningResult(False)
        if _fixed_adult_hard_floor_violation(value):
            return ScreeningResult(
                True,
                "adult_hard_floor",
                "hard_floor",
                -1,
            )
        if not self.rules:
            return ScreeningResult(False)
        visible = normalize_visible(value[:MAX_REPLY_CHARACTERS])
        compact = compact_text(value[:MAX_REPLY_CHARACTERS])
        collapsed = _collapse_repeats(compact)
        leet_compact = compact.translate(_LEET_TRANSLATION)
        for rule in self.rules:
            if rule.visible and rule.visible in visible:
                return ScreeningResult(
                    True,
                    "exact",
                    rule.kind,
                    rule.index,
                )
            if len(rule.compact) < 2:
                continue
            variants = [(compact, rule.compact)]
            if collapsed != compact:
                variants.append((collapsed, rule.compact))
            if (
                rule.contains_ascii_letters
                and leet_compact != compact
            ):
                variants.append((leet_compact, rule.leet_compact))
            for candidate, expected in variants:
                if expected and expected in candidate:
                    return ScreeningResult(
                        True,
                        "obfuscated",
                        rule.kind,
                        rule.index,
                    )
                if _approximately_contains(expected, candidate):
                    return ScreeningResult(
                        True,
                        "approximate",
                        rule.kind,
                        rule.index,
                    )
        return ScreeningResult(False)

    def safe_context(self, value: str) -> str:
        return (
            _SAFE_CONTEXT_PLACEHOLDER
            if self.screen(value).blocked
            else value
        )
