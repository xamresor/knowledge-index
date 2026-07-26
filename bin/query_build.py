#!/usr/bin/env python3
"""Build a qmd *query document* instead of handing it a raw phrase.

The problem this solves
-----------------------
qmd's lexical layer joins the terms of one query with **AND**: every word of a natural-language
question becomes a hard filter, so the page that holds the answer is *excluded* if it happens to lack
one of them — usually a function word. Measured on a 27-question golden set, asking as a phrase gave
recall 0.14; the authoritative page for "на каком порту работает клетка doc-search" was thrown out
because it contains no "работает".

Rather than patch someone else's tool, use its documented multi-line form: a query document with
several typed lines, which qmd fuses (RRF) instead of intersecting.

    intent: <the original question, for snippets and disambiguation>
    lex: <term>          one line per content word  -> fused, not intersected
    vec: <the original question>

Same golden set, same unpatched qmd: recall **0.67**, MRR **0.59** (vs 0.14 / 0.17 as a phrase).
That is ~90% of what patching qmd to OR achieved, with *better* ranking and nothing forked.

Two side effects worth naming:
  * **stop words matter now.** Each surviving term is a sub-query, so "на", "каком", "the", "does"
    would each pull their own noise. They are dropped here.
  * **qmd's own LLM query expansion no longer runs** for these searches. That is a feature: on an
    unknown proper noun it invented a domain ("Peter Cernek in philosophy") and dragged the vector
    search away from the corpus.
"""
from __future__ import annotations

import re

# Function words carry no retrieval signal but, as separate sub-queries, do carry noise.
# Kept deliberately short: only words whose removal cannot change what a question is *about*.
RU_STOP = {
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то", "все", "она", "так",
    "его", "но", "да", "ты", "к", "у", "же", "вы", "за", "бы", "по", "только", "ее", "мне", "было",
    "вот", "от", "меня", "еще", "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "ли",
    "если", "или", "ни", "быть", "был", "него", "до", "вас", "нибудь", "опять", "уж", "вам", "ведь",
    "там", "потом", "себя", "ничего", "ей", "может", "они", "тут", "где", "есть", "надо", "ней",
    "для", "мы", "тебя", "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже",
    "себе", "под", "будет", "ж", "тогда", "кто", "этот", "того", "потому", "этого", "какой",
    "совсем", "ним", "здесь", "этом", "один", "почти", "мой", "тем", "чтобы", "нее", "сейчас",
    "были", "куда", "зачем", "всех", "никогда", "можно", "при", "наконец", "два", "об", "другой",
    "хоть", "после", "над", "больше", "тот", "через", "эти", "нас", "про", "всего", "них", "какая",
    "много", "разве", "три", "эту", "моя", "впрочем", "хорошо", "свою", "этой", "перед", "иногда",
    "лучше", "чуть", "том", "нельзя", "такой", "им", "более", "всегда", "конечно", "всю", "между",
    "каком", "какие", "наш", "наши", "ваш", "свой", "каким", "каких",
}
EN_STOP = {
    "a", "an", "the", "and", "or", "of", "to", "in", "is", "are", "was", "were", "be", "been",
    "for", "on", "at", "by", "with", "from", "as", "it", "its", "this", "that", "these", "those",
    "what", "which", "who", "whom", "whose", "how", "why", "when", "where", "does", "do", "did",
    "can", "could", "should", "would", "will", "shall", "may", "might", "must", "not", "no",
    "yes", "we", "you", "they", "i", "he", "she", "our", "your", "their", "there", "here", "about",
    "into", "over", "than", "then", "so", "if", "but",
}
STOP = RU_STOP | EN_STOP

# Tokens keep dots, slashes and hyphens: `doc-search`, `renget-api`, `/srv/oldname`, `graph.json`
# are single terms, and splitting them would destroy the most discriminative words in the corpus.
TOKEN = re.compile(r"[\w\-./]{2,}", re.UNICODE)

MAX_TERMS = 12          # a pathological question must not turn into fifty sub-queries


def terms(query: str, max_terms: int = MAX_TERMS) -> list[str]:
    """Content words of a question, in order, deduplicated, stop words dropped."""
    out: list[str] = []
    for token in TOKEN.findall(query.lower()):
        word = token.strip("./-")
        if not word or word in STOP or word in out:
            continue
        out.append(word)
        if len(out) >= max_terms:
            break
    return out


def is_structured(query: str) -> bool:
    """True if the caller already wrote a query document — then we must not rewrite it."""
    return bool(re.match(r"^\s*(intent|lex|vec|hyde)\s*:", query, re.MULTILINE))


def build(query: str, want_vector: bool = True) -> str:
    """A qmd query document for `query`. Falls back to the raw text when there is nothing to split.

    `want_vector=False` keeps it purely lexical (used when the caller asked for `mode=lex`).
    """
    if is_structured(query):
        return query
    words = terms(query)
    if not words:
        return query
    lines = [f"intent: {query}"] + [f"lex: {w}" for w in words]
    if want_vector:
        lines.append(f"vec: {query}")
    return "\n".join(lines)
