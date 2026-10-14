"""Topic matching for search results: stemming, spelling variants, category synonyms, query expansion.

Why this exists. A search hit used to be kept only if one *query* word appeared in its title as an exact whole
word. That is a precision rule, but it also threw away real companies for reasons that have nothing to do with
relevance: "centres" (typed) never equals "centers" (page), "Coach" never equals "coaching", "Institutes" never
equals "institute", and a centre whose title says "Software Training Institute" shares no word at all with the
query "coaching centers". Three small tools fix that without loosening the rule into "anything vaguely related":

  * stem()          - "centres/centers/center", "academies/academy", "coaching/coach/coaches" all compare equal.
  * SYNONYM_GROUPS  - for a name-collecting run, a query about coaching also accepts titles that say training,
                      tuition, academy or institute. Full runs keep exact (stemmed) words only.
  * expand_query()  - optional extra searches with the category word swapped for its usual equivalents.

Nothing here knows about any one site or any one place.
"""
from __future__ import annotations

import re

# British -> American, applied after plural stripping, so "centres" and "centers" meet at "center".
SPELLING = {
    "centre": "center", "programme": "program", "organisation": "organization", "licence": "license",
    "theatre": "theater", "jewellery": "jewelry", "parlour": "parlor", "catalogue": "catalog",
}

# Words that carry no kind-of-place information. As topic evidence they let "Best Biryani in Hyderabad" through a
# "best coaching centers" query, and as place evidence they let any page that says "contact" through. Dropped from
# both - unless that would leave nothing at all to check (a query made only of such words keeps them).
NOISE = frozenset({
    "best", "top", "good", "great", "cheap", "free", "list", "lists", "all", "find", "near", "nearby", "official",
    "contact", "contacts", "email", "emails", "phone", "phones", "mobile", "number", "numbers", "address",
    "addresses", "details", "detail", "info", "information", "directory", "website", "websites", "online",
})

_LATIN = re.compile(r"[a-z0-9]+")


def stem(word: str) -> str:
    """A deliberately small stemmer: plural endings, "-ing", and British spellings. Same function is applied to both
    sides of every comparison, so it only has to be *consistent*, not linguistically right."""
    w = (word or "").lower()
    if not _LATIN.fullmatch(w):
        return w  # non-Latin scripts have no word boundaries or plurals to strip here
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"                                   # academies -> academy
    elif len(w) > 4 and re.search(r"(sses|xes|ches|shes)$", w):
        w = w[:-2]                                         # classes -> class, coaches -> coach
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]                                         # centers -> center, institutes -> institute
    if len(w) > 5 and w.endswith("ing"):
        w = w[:-3]                                         # coaching -> coach, training -> train
    return SPELLING.get(w, w)


def tokens(text: str) -> list[str]:
    """Stemmed Latin word tokens of a title or domain ('coaching-hyderabad.com' -> coach, hyderabad, com)."""
    return [stem(t) for t in _LATIN.findall((text or "").lower())]


def drop_noise(words: list[str], keep_if_empty: bool = True) -> list[str]:
    kept = [w for w in words if w.lower() not in NOISE]
    return kept if kept or not keep_if_empty else list(words)


# Stemmed words that, in a name-collecting run, count as evidence of the *same kind of place* as the query. A group
# only switches on when the query itself contains one of its members. "class" is left out on purpose (dance
# classes, cooking classes) and so is "center" (shopping center).
SYNONYM_GROUPS: tuple[frozenset, ...] = (
    frozenset({"coach", "tutor", "tuition", "tutorial", "train", "academy", "institute"}),
    frozenset({"college", "university", "institute", "polytechnic", "academy"}),
    frozenset({"school", "vidyalaya", "academy", "convent"}),
    frozenset({"hospital", "clinic", "nursing", "medical"}),
    frozenset({"restaurant", "eatery", "dhaba", "cafe", "bistro"}),
    frozenset({"gym", "fitness", "yoga", "crossfit"}),
    frozenset({"salon", "parlor", "beauty", "spa"}),
    frozenset({"temple", "mandir", "devasthanam", "kovil"}),
    frozenset({"builder", "developer", "realty", "estate", "property"}),
)


def synonyms_for(stems: set[str]) -> set[str]:
    out: set[str] = set()
    for group in SYNONYM_GROUPS:
        if group & stems:
            out |= group
    return out


def topic_hit(topic_words: list[str], text: str, wide: bool = False) -> bool:
    """True when `text` shows at least one topic word (stemmed, whole-word). `wide` also accepts the category's
    synonyms - used only when collecting names, where a wrong extra row is cheap to remove and a missed company
    is not coming back."""
    latin = [w for w in topic_words if _LATIN.fullmatch(w.lower())]
    other = [w for w in topic_words if w not in latin]
    present = set(tokens(text))
    wanted = {stem(w) for w in latin}
    if wide:
        wanted |= synonyms_for(wanted)
    if wanted & present:
        return True
    low = (text or "").lower()
    return any(w in low for w in other)  # e.g. Chinese: no word boundaries, plain substring as before


# ---------------------------------------------------------------- query expansion
# Each group has "slots" (the kind word and the venue word, say) with their usual spellings. A query is expanded
# only when it contains the group's anchor slot, so "digital marketing institutes in X" is never rewritten into
# the meaningless "digital marketing centers in X".
EXPANSION_GROUPS: tuple[dict, ...] = (
    {"anchor": "kind",
     "slots": {"kind": ["coaching", "training", "tuition"], "venue": ["centers", "institutes", "academies", "classes"]}},
    # "extras" are only ever used as replacements, never as the word that triggers an expansion: a bare
    # "institutes in X" is ambiguous (coaching? engineering?) and must not be rewritten into colleges and universities.
    {"anchor": "noun", "slots": {"noun": ["colleges", "universities"]}, "extras": {"noun": ["institutes", "academies"]}},
    {"anchor": "noun", "slots": {"noun": ["schools"]}, "extras": {"noun": ["vidyalayas", "academies"]}},
    {"anchor": "noun", "slots": {"noun": ["hospitals", "clinics", "nursing homes", "medical centers"]}},
    {"anchor": "noun", "slots": {"noun": ["restaurants", "eateries", "dhabas"]}},
    {"anchor": "noun", "slots": {"noun": ["gyms", "fitness centers", "yoga studios"]}},
    {"anchor": "noun", "slots": {"noun": ["salons", "beauty parlors", "spas"]}},
    {"anchor": "noun", "slots": {"noun": ["temples", "mandirs"]}},
)

_PREP = re.compile(r"\b(in|near|at|around|across)\b", re.I)


def _split_query(q: str) -> tuple[str, str]:
    """(head, tail): everything before the last in/near/at, and the tail kept verbatim from that word on."""
    last = None
    for m in _PREP.finditer(q):
        last = m
    return (q[:last.start()], q[last.start():]) if last else (q, "")


def expand_query(q: str, limit: int = 4) -> list[str]:
    """Up to `limit` extra searches with the category word swapped for common equivalents, original not included.
    Deterministic, no network. 'coaching centers in Ameerpet' -> 'training centers in Ameerpet',
    'coaching institutes in Ameerpet', 'training institutes in Ameerpet', 'tuition centers in Ameerpet'."""
    if not q or "site:" in q.lower():
        return []
    head, tail = _split_query(q)
    words = head.split()
    stems_ = [stem(w.strip(".,;:()\"'")) for w in words]
    variants: list[str] = []
    seen = {" ".join(stems_) + "|" + tail.lower()}

    def add(new_words: list[str]):
        key = " ".join(stem(w.strip(".,;:()\"'")) for w in new_words) + "|" + tail.lower()
        if key not in seen:
            seen.add(key)
            variants.append((" ".join(new_words) + (" " + tail.lstrip() if tail else "")).strip())

    for group in EXPANSION_GROUPS:
        slots = group["slots"]
        found: dict[str, tuple[int, int]] = {}   # slot -> (word index, how many words the match covers)
        for name, alts in slots.items():
            for alt in alts:
                alt_stems = [stem(a) for a in alt.split()]
                n = len(alt_stems)
                for i in range(len(stems_) - n + 1):
                    if stems_[i:i + n] == alt_stems:
                        found.setdefault(name, (i, n))
        if group["anchor"] not in found:
            continue

        def swap(name: str, alt: str) -> list[str]:
            # always built from the original word list, at the position found in the original query
            i, n = found[name]
            return words[:i] + alt.split() + words[i + n:]

        extras = group.get("extras", {})
        singles: list[tuple[str, str]] = []
        for name, alts in slots.items():
            if name not in found:
                continue
            cur = " ".join(words[found[name][0]:found[name][0] + found[name][1]])
            for alt in list(alts) + list(extras.get(name, [])):
                if [stem(a) for a in alt.split()] != [stem(a) for a in cur.split()]:
                    singles.append((name, alt))
        # priority: first alternative of the anchor slot, first of the other slot, both swapped, then the rest
        order: list[list[str]] = []
        by_slot: dict[str, list[str]] = {}
        for name, alt in singles:
            by_slot.setdefault(name, []).append(alt)
        names = list(by_slot)
        if names:
            order.append(swap(names[0], by_slot[names[0]][0]))
        if len(names) > 1:
            order.append(swap(names[1], by_slot[names[1]][0]))
            # both swapped: apply the later position first so the earlier index stays valid
            pos = sorted(((found[n][0], n) for n in names[:2]), reverse=True)
            both = list(words)
            for _, n in pos:
                i, k = found[n]
                both = both[:i] + by_slot[n][0].split() + both[i + k:]
            order.append(both)
        for name in names:
            for alt in by_slot[name][1:]:
                order.append(swap(name, alt))
        for cand in order:
            add(cand)
            if len(variants) >= limit:
                return variants
        break  # one matching group is enough; its variants already cover the query
    return variants[:limit]
