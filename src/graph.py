"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (TODO KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (TODO KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (TODO KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (TODO KG-4)

Everything else in this file is a HINT: one possible ontology (below). Use it as is, change it,
or design your own — your own ontology + report/ONTOLOGY.md earns the bonus (see SUBMISSION.md).

Suggested ontology (Crime is the bridge between the law KB and the news KB):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())
    return name.removeprefix("tội ").strip()

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    if not name or not name.strip() or not known:
        return None

    norm_name = normalize(name)
    if not norm_name:
        return None

    norm_to_orig: dict[str, str] = {}
    for item in known:
        norm_key = normalize(item)
        if norm_key not in norm_to_orig:
            norm_to_orig[norm_key] = item

    if norm_name in norm_to_orig:
        return norm_to_orig[norm_name]

    matches = difflib.get_close_matches(norm_name, list(norm_to_orig.keys()), n=1, cutoff=0.8)
    if matches:
        return norm_to_orig[matches[0]]

    return None

SUBSTANCE_SYNONYMS: dict[str, list[str]] = {
    "MDMA": ["thuốc lắc", "kẹo", "ecstasy", "viên nén"],
    "Methamphetamine": ["ma túy đá", "hàng đá", "đá", "pha lê", "meth"],
    "Heroine": ["hàng trắng", "bạch phiến", "heroin"],
    "Cocaine": ["côca", "cocain", "coca"],
    "Ketamine": ["ke", "nước vui", "kẹo ke"],
    "cần sa": ["cỏ", "bồ đà", "marijuana"],
    "thuốc phiện": ["a phiến", "nha phiến"],
}

def find_substances(text: str) -> list[str]:
    lowered = text.lower()
    found = set()
    for name in SUBSTANCES:
        if name.lower() in lowered:
            found.add(name)
    for canonical, syns in SUBSTANCE_SYNONYMS.items():
        for syn in syns:
            if re.search(r"\b" + re.escape(syn) + r"\b", lowered):
                found.add(canonical)
    return sorted(found)

# ----------------------------------------------------------------------------------------------
# HINT — suggested ontology: extraction helpers
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' — law text is regular enough to skip the LLM."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        first_line = text.splitlines()[0]
        penalty = re.search(r"\bbị ((?:phạt|tù|cảnh cáo).+?)(?::|$)", first_line)
        penalty_str = penalty.group(1).rstrip(".") if penalty else ""
        is_life = "chung thân" in penalty_str.lower()
        is_death = "tử hình" in penalty_str.lower()
        clauses.append({
            "id": f"{article_id} khoản {start.group(1)}",
            "number": int(start.group(1)),
            "penalty": penalty_str,
            "max_penalty": "tử hình" if is_death else ("tù chung thân" if is_life else penalty_str),
            "has_life_sentence": is_life,
            "has_death_penalty": is_death,
            "text": text,
            "substances": find_substances(text),
        })
    return {
        "id": article_id,
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges are re-linked to law-KB crimes in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges", [])) if c})
        for person in case.get("people", []):
            person["charge"] = link_entity(person.get("charge") or "", known_crimes) or ""
        for s in case.get("substances", []):
            s_name = s.get("name", "")
            mapped = find_substances(s_name)
            if mapped:
                s["name"] = mapped[0]
    return cases

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- HINT — suggested ontology: writes

    def suggested_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "name"),
                           ("Substance", "name"), ("Person", "name"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number,
                  cl.penalty = clause.penalty,
                  cl.max_penalty = clause.max_penalty,
                  cl.has_life_sentence = clause.has_life_sentence,
                  cl.has_death_penalty = clause.has_death_penalty,
                  cl.text = clause.text,
                  cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            """,
            **article,
        )

    def add_news_case(self, case: dict, doc: Document) -> None:
        self.run(
            """
            MERGE (k:Case {name: $name})
              SET k.summary = $summary, k.date = $date, k.doc_id = $doc_id, k.source_title = $title,
                  k.defendant_names = $defendant_names, k.charges_list = $charges
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount)
            FOREACH (p IN $people | MERGE (person:Person {name: p.name})
                SET person.aliases = coalesce(p.aliases, [])
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=case.get("charges", []), people=[p for p in case.get("people", []) if p.get("name")],
            defendant_names=[p.get("name") for p in case.get("people", []) if p.get("name")],
            substances=[s for s in case.get("substances", []) if s.get("name")],
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Graph facts for a question: seeds + 1 hop, then the legal basis of every case reached."""
        seed_ids, facts = self.seed_facts(question, doc_ids)

        # a. Cases that are a seed or adjacent to a seed
        cases = self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids }
            RETURN DISTINCT elementId(k) AS id, k.name AS name, coalesce(k.summary, '') AS summary,
                   coalesce(k.source_title, '') AS source_title, coalesce(k.defendant_names, []) AS defendants,
                   coalesce(k.charges_list, []) AS charges
            """,
            ids=seed_ids,
        )
        case_ids = [c["id"] for c in cases]
        for c in cases:
            parts = [f"Vụ việc '{c['name']}'"]
            if c["source_title"]:
                parts.append(f"Bài báo: '{c['source_title']}'")
            if c["defendants"]:
                parts.append(f"Đối tượng/Bị cáo: {', '.join(c['defendants'])}")
            if c["charges"]:
                parts.append(f"Tội danh: {', '.join(c['charges'])}")
            if c["summary"]:
                parts.append(f"Tóm tắt: {c['summary']}")
            facts.append(". ".join(parts))

        # b. Follow (Case)-[:CHARGED_WITH]->(Crime)<-[:DEFINES]-(Article)-[:HAS_CLAUSE]->(Clause)
        is_max_penalty_q = any(w in question.lower() for w in ["tối đa", "cao nhất", "khung cao", "bao nhiêu năm", "mức án", "chung thân", "tử hình"])
        if case_ids:
            clauses = self.run(
                """
                MATCH (k:Case)
                WHERE elementId(k) IN $case_ids
                MATCH (k)-[:CHARGED_WITH]->(c:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
                WHERE cl.number = 1
                   OR EXISTS { MATCH (k)-[:INVOLVES]->(sub:Substance)<-[:MENTIONS]-(cl) }
                   OR ($is_max AND cl.number >= 3)
                RETURN DISTINCT a.id AS article_id, a.title AS title, c.name AS crime, cl.number AS number, cl.text AS text, cl.max_penalty AS max_penalty
                ORDER BY a.id, cl.number
                """,
                case_ids=case_ids,
                is_max=is_max_penalty_q,
            )
            for cl in clauses:
                facts.append(f"[{cl['article_id']} - {cl['title']}] tội '{cl['crime']}' khoản {cl['number']}: {cl['text']}")

        # c. Articles named directly in the question ("Điều 251" -> re.findall(r"[Đđ]iều (\d+)", question))
        article_numbers = re.findall(r"[Đđ]iều\s*(\d+)", question)
        q_substances = find_substances(question)
        for num in article_numbers:
            article_clauses = self.run(
                """
                MATCH (a:Article)
                WHERE a.id CONTAINS $num
                MATCH (a)-[:HAS_CLAUSE]->(cl:Clause)
                WHERE cl.number = 1
                   OR (size($substances) > 0 AND EXISTS { MATCH (cl)-[:MENTIONS]->(sub:Substance) WHERE sub.name IN $substances })
                   OR ($is_max AND cl.number >= 3)
                RETURN DISTINCT a.id AS article_id, a.title AS title, cl.number AS number, cl.text AS text
                ORDER BY a.id, cl.number
                """,
                num=num,
                substances=q_substances,
                is_max=is_max_penalty_q,
            )
            for cl in article_clauses:
                facts.append(f"[{cl['article_id']} - {cl['title']}] khoản {cl['number']}: {cl['text']}")

        seen = set()
        deduped_facts = []
        for f in facts:
            if f not in seen:
                seen.add(f)
                deduped_facts.append(f)
        return deduped_facts[:max_facts]

# ---------------------------------------------------------------------------------------------- KG-2

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat)."""
    graph.suggested_constraints()
    articles = [parse_law_article(d) for d in law_docs]
    for a in articles:
        graph.add_law_article(a)
    crimes = sorted({a["crime"] for a in articles if a.get("crime")})
    for d in news_docs:
        cases = extract_news_cases(d, lambda p: llm_fn(p, json_mode=True), crimes)
        for case in cases:
            graph.add_news_case(case, d)

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Trả lời câu hỏi chỉ dựa trên ngữ cảnh (đoạn văn bản và dữ kiện từ knowledge graph).
Nêu rõ số Điều luật khi có. Nếu ngữ cảnh không đủ, nói không đủ thông tin.

Dữ kiện knowledge graph:
{facts}

Đoạn văn bản:
{chunks}

Câu hỏi: {question}
Trả lời:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        chunks = self.store.search(question, top_k=top_k)
        vector_doc_ids = [chunk["metadata"]["doc_id"] for chunk in chunks if "doc_id" in chunk.get("metadata", {})]

        entity_doc_ids = []
        if hasattr(self.graph, "run"):
            person_cases = self.graph.run(
                """
                MATCH (p:Person)-[:INVOLVED_IN]->(k:Case)
                WHERE (toLower($q) CONTAINS toLower(p.name)
                   OR any(a IN coalesce(p.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a)))
                  AND k.doc_id IS NOT NULL
                RETURN DISTINCT k.doc_id AS doc_id
                """,
                q=question,
            )
            entity_doc_ids.extend([r["doc_id"] for r in person_cases if r["doc_id"]])

            q_substances = find_substances(question)
            if q_substances:
                sub_cases = self.graph.run(
                    """
                    MATCH (k:Case)-[:INVOLVES]->(s:Substance)
                    WHERE s.name IN $substances AND k.doc_id IS NOT NULL
                    RETURN DISTINCT k.doc_id AS doc_id
                    """,
                    substances=q_substances,
                )
                entity_doc_ids.extend([r["doc_id"] for r in sub_cases if r["doc_id"]])

        all_doc_ids = list(dict.fromkeys(vector_doc_ids + entity_doc_ids))
        facts = self.graph.context(question, all_doc_ids)
        facts_text = "\n".join(f"- {f}" for f in facts) if facts else "Không có dữ kiện graph bổ sung."
        chunks_text = "\n\n".join(f"[{i}] {chunk['content']}" for i, chunk in enumerate(chunks, start=1))
        prompt = GRAPH_PROMPT.format(facts=facts_text, chunks=chunks_text, question=question)
        return self.llm_fn(prompt)
