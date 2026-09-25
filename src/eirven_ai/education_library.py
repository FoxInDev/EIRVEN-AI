# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


# Small deterministic RU->EN expansion layer for the bundled English/public-domain
# corpus. It is intentionally conservative; semantic embeddings are the next stage.
RU_EN = {
    "свобода": "freedom liberty", "воля": "will freedom", "вина": "guilt",
    "совесть": "conscience", "мораль": "morality ethics", "этика": "ethics virtue",
    "добродетель": "virtue", "счастье": "happiness", "дружба": "friendship",
    "любовь": "love", "брак": "marriage", "семья": "family", "общество": "society",
    "власть": "power authority", "государство": "state government", "политика": "politics",
    "справедливость": "justice", "закон": "law", "милосердие": "mercy compassion",
    "ответственность": "responsibility", "наука": "science", "разум": "reason",
    "знание": "knowledge", "сомнение": "doubt", "война": "war", "мир": "peace",
    "история": "history", "бедность": "poverty", "месть": "revenge",
    "преступление": "crime", "наказание": "punishment", "смерть": "death mortality",
    "страх": "fear", "выбор": "choice", "характер": "character", "юмор": "humor wit",
    "одиночество": "одиночество одиночества одиночестве одинокий одинокая одиноко solitude loneliness lonely",
    "ирония": "irony wit", "дедукция": "deduction", "наблюдение": "observation",
    "доказательство": "evidence proof", "раскольников": "Raskolnikov",
    "карамазов": "Karamazov", "карамазовы": "Karamazov", "мышкин": "Myshkin",
    "базаров": "Bazarov", "онегин": "Onegin", "татьяна": "Tatyana Tatiana",
    "гринев": "Grinev", "пугачев": "Pugachev", "чичиков": "Chichikov",
    "каренина": "Karenina", "вронский": "Vronsky", "левин": "Levin",
    "безухов": "Bezukhov Pierre", "болконский": "Bolkonsky", "ростов": "Rostov",
    "наполеон": "Napoleon", "кутузов": "Kutuzov", "дарси": "Darcy",
    "элизабет": "Elizabeth", "холмс": "Holmes", "ватсон": "Watson",
    "франкенштейн": "Frankenstein", "дориан": "Dorian", "джекил": "Jekyll",
    "хайд": "Hyde", "дантес": "Dantes", "вальжан": "Valjean", "жавер": "Javert",
    "гамлет": "Hamlet", "макбет": "Macbeth", "лир": "Lear",
    "макиавелли": "Machiavelli", "платон": "Plato", "аврелий": "Aurelius",
    "гоббс": "Hobbes", "милль": "Mill", "аристотель": "Aristotle", "декарт": "Descartes",
}

RU_STEMS = {
    "свобод": "freedom liberty", "совест": "conscience", "морал": "morality ethics",
    "этик": "ethics virtue", "добродетел": "virtue", "счаст": "happiness",
    "дружб": "friendship", "любов": "love", "брак": "marriage", "семь": "family",
    "обществ": "society", "власт": "power authority", "государств": "state government",
    "полит": "politics", "справедлив": "justice", "закон": "law", "милосерд": "mercy compassion",
    "ответствен": "responsibility", "наук": "science", "разум": "reason", "знан": "knowledge",
    "сомнен": "doubt", "войн": "war", "истор": "history", "бедност": "poverty",
    "мест": "revenge", "преступлен": "crime", "наказан": "punishment", "смерт": "death mortality",
    "страх": "fear", "выбор": "choice", "характер": "character", "ирони": "irony wit",
    "одиноч": "одиночество одиночества одиночестве одинокий одинокая одиноко solitude loneliness lonely",
    "раскольников": "Raskolnikov", "карамазов": "Karamazov", "мышкин": "Myshkin",
    "базаров": "Bazarov", "онегин": "Onegin", "татьян": "Tatyana Tatiana",
    "гринев": "Grinev", "пугачев": "Pugachev", "чичиков": "Chichikov",
    "каренин": "Karenina", "вронск": "Vronsky", "левин": "Levin",
    "безухов": "Bezukhov Pierre", "болконск": "Bolkonsky", "ростов": "Rostov",
    "наполеон": "Napoleon", "кутузов": "Kutuzov", "дарси": "Darcy", "элизабет": "Elizabeth",
    "холмс": "Holmes", "ватсон": "Watson", "франкенштейн": "Frankenstein",
    "дориан": "Dorian", "джекил": "Jekyll", "хайд": "Hyde", "дантес": "Dantes",
    "вальжан": "Valjean", "жавер": "Javert", "гамлет": "Hamlet", "макбет": "Macbeth",
    "макиавелл": "Machiavelli", "платон": "Plato", "аврели": "Aurelius",
    "гоббс": "Hobbes", "милл": "Mill", "аристотел": "Aristotle", "декарт": "Descartes",
}

STOP = {
    "что","как","это","такой","такая","такое","почему","зачем","когда","где","кто",
    "какой","какая","какие","про","для","или","если","мне","меня","мой","моя","его","ее","её",
    "the","a","an","is","are","of","to","and","in","on","for","why","what","how","who",
}
ACTION_PREFIXES = (
    "открой ", "запусти ", "нажми ", "кликни ", "скачай ", "установи ", "удали ",
    "перемести ", "переименуй ", "создай файл", "выполни команд", "отправь ", "напиши в telegram",
)


@dataclass(slots=True)
class EducationHit:
    chunk_id: str
    book_id: str
    title: str
    title_ru: str
    author: str
    author_ru: str
    text: str
    score: float
    source: str = "curated"


class EducationLibrary:
    """Dependency-free federated retrieval over curated + optional mega corpus.

    ``education/corpus/library.sqlite3`` ships with the release and provides the
    carefully curated core. ``education/mega/library.sqlite3`` is created locally by
    the 10k+ builder. Keeping them separate makes updates/recovery cheap and prevents a
    multi-gigabyte local corpus from ever being included in a release by accident.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        education_root = self.db_path.parent.parent
        self.mega_db_path = education_root / "mega" / "library.sqlite3"

    @property
    def available(self) -> bool:
        return any(path.is_file() for path in self.db_paths)

    @property
    def db_paths(self) -> list[Path]:
        values = [self.db_path]
        if self.mega_db_path != self.db_path:
            values.append(self.mega_db_path)
        return values

    @staticmethod
    def _terms(query: str) -> list[str]:
        base = [x.lower() for x in re.findall(r"[A-Za-zА-Яа-яЁё0-9-]{3,}", query or "")]
        out: list[str] = []
        for term in base:
            if term in STOP:
                continue
            out.append(term)
            expanded = RU_EN.get(term)
            if not expanded and re.search(r"[а-яё]", term):
                for stem, value in RU_STEMS.items():
                    if term.startswith(stem):
                        expanded = value
                        break
            if expanded:
                out.extend(expanded.lower().split())
        return list(dict.fromkeys(out))[:18]

    @staticmethod
    def _search_one(path: Path, terms: list[str], limit: int, *, source: str) -> list[EducationHit]:
        if not path.is_file() or not terms:
            return []
        match = " OR ".join('"' + t.replace('"', '') + '"' for t in terms)
        try:
            with sqlite3.connect(path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT c.chunk_id,c.book_id,c.title,c.title_ru,c.author,c.author_ru,
                           c.topics_en,c.topics_ru,c.text,
                           bm25(chunks_fts, 0.0,0.0,3.0,3.0,1.5,1.5,0.7,0.7,2.0) AS rank
                    FROM chunks_fts
                    JOIN chunks c ON c.chunk_id=chunks_fts.chunk_id
                    WHERE chunks_fts MATCH ?
                    ORDER BY rank ASC
                    LIMIT ?
                    """,
                    (match, max(12, min(int(limit) * 6, 60))),
                ).fetchall()
        except (sqlite3.Error, OSError):
            return []

        def adjusted(row: sqlite3.Row) -> float:
            rank = float(row["rank"] or 0.0)
            meta = " ".join(str(row[k] or "") for k in (
                "title", "title_ru", "author", "author_ru", "topics_en", "topics_ru"
            )).lower()
            exact = sum(1 for term in terms if len(term) >= 4 and term in meta)
            body = str(row["text"] or "").lower()
            # Prefer chunks that contain several semantic query terms, not merely the
            # named author's metadata. This matters for Russian inflections such as
            # «Лермонтов ... одиночестве», where every chunk matches the author.
            content_matches = sum(1 for term in terms if len(term) >= 4 and term in body)
            # The hand-curated core wins close ties over the broad automatic corpus.
            curated_bonus = 2.5 if source == "curated" else 0.0
            return rank - (7.0 * exact) - (2.2 * content_matches) - curated_bonus

        rows = sorted(rows, key=adjusted)[:max(1, min(int(limit), 10))]
        return [EducationHit(
            chunk_id=str(r["chunk_id"]), book_id=str(r["book_id"]), title=str(r["title"]),
            title_ru=str(r["title_ru"]), author=str(r["author"]), author_ru=str(r["author_ru"]),
            text=str(r["text"]), score=adjusted(r), source=source,
        ) for r in rows]

    def search(self, query: str, limit: int = 3) -> list[EducationHit]:
        terms = self._terms(query)
        if not terms:
            return []
        combined: list[EducationHit] = []
        combined.extend(self._search_one(self.db_path, terms, limit + 2, source="curated"))
        combined.extend(self._search_one(self.mega_db_path, terms, limit + 2, source="mega"))
        combined.sort(key=lambda hit: hit.score)
        # Avoid identical chunk ids if a local rebuild happens to contain the curated book too.
        result: list[EducationHit] = []
        seen: set[tuple[str, str]] = set()
        for hit in combined:
            key = (hit.book_id, hit.chunk_id)
            if key in seen:
                continue
            seen.add(key)
            result.append(hit)
            if len(result) >= max(1, min(int(limit), 8)):
                break
        return result

    @staticmethod
    def prompt_context_from_hits(query: str, hits: list[EducationHit], *, max_chars: int = 6200) -> str:
        q = (query or "").strip().lower()
        if not q or any(q.startswith(prefix) for prefix in ACTION_PREFIXES) or not hits:
            return ""
        blocks: list[str] = []
        used = 0
        seen_books: set[str] = set()
        focused_book = len(hits) >= 2 and hits[0].book_id == hits[1].book_id
        for hit in hits:
            if not focused_book and hit.book_id in seen_books and len(seen_books) >= 1:
                continue
            header = f"Источник: {hit.title_ru or hit.title} — {hit.author_ru or hit.author}\n"
            room = max_chars - used - len(header) - 80
            if room < 500:
                break
            body = hit.text[:min(room, 2600)].strip()
            blocks.append(header + body)
            used += len(header) + len(body)
            seen_books.add(hit.book_id)
            if len(blocks) >= 2:
                break
        if not blocks:
            return ""
        return (
            "\n\nРелевантные фрагменты из локальной библиотеки EIRVEN Education (RAG). "
            "Используй их как источник знаний, а не как обязательную тему ответа. "
            "Не выдумывай цитаты и явно отличай точный текст источника от собственного вывода. "
            "ВАЖНО: стиль, лексика и манера автора источника не являются стилем ассистента. "
            "Не имитируй стиль книги, если пользователь прямо этого не попросил; форму ответа всегда "
            "определяют текущие настройки StyleDNA/режима общения.\n\n"
            + "\n\n---\n\n".join(blocks)
        )

    def prompt_context(self, query: str, *, max_chars: int = 6200) -> str:
        return self.prompt_context_from_hits(query, self.search(query, limit=4), max_chars=max_chars)

    def fallback_answer(self, query: str, *, max_chars: int = 1800) -> str:
        """Diagnostic answer when the LLM runtime is down but retrieval is healthy."""
        hits = self.search(query, limit=2)
        if not hits:
            return ""
        parts = [
            "Локальная модель сейчас недоступна, но библиотека EIRVEN Education работает. "
            "Вот что я нашла в источниках; после запуска Ollama я смогу нормально осмыслить это и ответить своими словами."
        ]
        used = len(parts[0])
        for hit in hits:
            title = hit.title_ru or hit.title
            author = hit.author_ru or hit.author
            # Keep excerpt diagnostic and short rather than dumping book text.
            excerpt = re.sub(r"\s+", " ", hit.text).strip()[:650]
            block = f"\n\n{title} — {author}: {excerpt}"
            if used + len(block) > max_chars:
                break
            parts.append(block)
            used += len(block)
        return "".join(parts)

    def status(self) -> dict[str, int | bool]:
        total_books = 0
        total_chunks = 0
        curated_books = curated_chunks = mega_books = mega_chunks = 0
        for path, label in ((self.db_path, "curated"), (self.mega_db_path, "mega")):
            if not path.is_file():
                continue
            try:
                with sqlite3.connect(path) as conn:
                    books = int(conn.execute("SELECT COUNT(*) FROM books").fetchone()[0])
                    chunks = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
                total_books += books
                total_chunks += chunks
                if label == "curated":
                    curated_books, curated_chunks = books, chunks
                else:
                    mega_books, mega_chunks = books, chunks
            except sqlite3.Error:
                continue
        return {
            "available": total_books > 0,
            "books": total_books,
            "chunks": total_chunks,
            "curated_books": curated_books,
            "curated_chunks": curated_chunks,
            "mega_books": mega_books,
            "mega_chunks": mega_chunks,
        }
