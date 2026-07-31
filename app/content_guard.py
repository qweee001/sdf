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
        if not self.rules or not value:
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
