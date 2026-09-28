"""Conservative, local evidence checks for unsolicited memory injection."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class FragmentEvidence:
    fragment: str
    reason: str
    matched_terms: tuple[str, ...] = ()


def _key(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text.lower())


def _sentences(text: str) -> list[str]:
    return [
        part.strip() for part in re.split(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+|\n+", text)
        if part.strip() and not part.lstrip().startswith("#")
    ]


def select_automatic_fragment(
    text: str,
    title: str,
    terms: list[str],
    *,
    strong_score: bool = False,
    scored_text: str = "",
    max_chars: int = 360,
) -> FragmentEvidence:
    # Count independent anchors, not nested tokenizations of the same word.
    text_key = _key(text)
    keys = sorted({_key(term) for term in terms if len(_key(term)) >= 2 and _key(term) in text_key}, key=lambda k: (-len(k), k))
    keys = [key for key in keys if not any(key != other and key in other for other in keys)]
    title_key = _key(title)
    candidates = []
    for index, sentence in enumerate(_sentences(text)):
        if len(sentence) > max_chars:
            continue  # Never cut a sentence into an unsupported assertion.
        sentence_key = _key(sentence)
        matched = tuple(key for key in keys if key in sentence_key)
        if not matched:
            continue
        title_match = any(key in title_key for key in matched)
        # Preferences/constraints can be useful even in a broadly titled note.
        preference = bool(re.search(
            r"喜欢|偏好|爱吃|爱喝|不吃|不喝|忌口|过敏|习惯|最爱|讨厌|不耐受|"
            r"\b(?:prefer\w*|favourite|favorite|allerg\w*|dislike\w*|intoleran\w*)\b",
            sentence, re.I,
        ))
        scored_match = strong_score and sentence in scored_text
        if not (title_match or len(matched) >= 2 or preference or scored_match):
            continue
        reason = "topic_title" if title_match else "multiple_anchors" if len(matched) >= 2 else "preference_or_constraint" if preference else "strong_scored_fragment"
        candidates.append((index, sentence, matched, reason))
    if not candidates and strong_score:
        # A genuinely scored moment may be a paraphrase with no lexical overlap.
        for sentence in _sentences(scored_text):
            if len(sentence) <= max_chars:
                return FragmentEvidence(sentence, "strong_scored_fragment")
    if not candidates:
        return FragmentEvidence("", "incidental_keyword_or_no_fragment")
    ranked = sorted(candidates, key=lambda row: (-len(row[2]), row[0]))[:2]
    selected = []
    size = 0
    for row in sorted(ranked):
        if row[1] in selected or size + len(row[1]) + bool(selected) > max_chars:
            continue
        selected.append(row[1])
        size += len(row[1]) + 1
    return FragmentEvidence("\n".join(selected), ranked[0][3], ranked[0][2])
