"""
Multi-Document Cross-Reference RAG with PageIndex
Programmatic Tree Search + LLM Synthesis

This approach is MORE aligned with PageIndex's actual design philosophy:
  - PageIndex builds tree indexes for reasoning-based retrieval
  - The tree search is done programmatically (finding relevant sections)
  - The LLM is used for synthesis (assembling a coherent answer from retrieved context)

For small local models, this works MUCH better than agentic tool-calling.

Architecture:
  1. Index all markdown documents using PageIndex (builds tree structure per doc)
  2. Build a cross-reference graph between documents
  3. For queries: programmatic tree search identifies relevant sections
  4. Cross-references are followed automatically
  5. Retrieved context is assembled and sent to LLM for final synthesis
"""

import os
import re
import sys
import json
import textwrap
from pathlib import Path

# Add PageIndex to path
sys.path.insert(0, str(Path(__file__).parent / "PageIndex"))

from pageindex import PageIndexClient
import litellm


# ── Cross-Reference Extraction ──────────────────────────────────────────────

def extract_cross_references(md_path: str, all_doc_files: list[str]) -> list[dict]:
    """Extract markdown links that point to other documents in the collection."""
    with open(md_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    known_files = {os.path.basename(f) for f in all_doc_files}
    refs = []
    link_pattern = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')

    for line_num, line in enumerate(lines, 1):
        for match in link_pattern.finditer(line):
            link_text = match.group(1)
            link_target = match.group(2)

            if link_target.startswith(('http://', 'https://', '#')):
                continue

            if '#' in link_target:
                target_file, target_section = link_target.split('#', 1)
            else:
                target_file = link_target
                target_section = None

            if target_file in known_files:
                refs.append({
                    'source': os.path.basename(md_path),
                    'target_file': target_file,
                    'target_section': target_section,
                    'link_text': link_text,
                    'line_num': line_num
                })

    return refs


# ── Multi-Document Index ────────────────────────────────────────────────────

class MultiDocPageIndex:
    """Manages multiple PageIndex-indexed documents with cross-reference awareness."""

    def __init__(self, docs_dir: str, workspace: str, model: str = None):
        self.docs_dir = Path(docs_dir)
        self.workspace = Path(workspace)
        self.model = model or "ollama/qwen2.5:3b"

        self.client = PageIndexClient(
            model=self.model,
            workspace=str(self.workspace)
        )

        self.file_to_doc_id: dict[str, str] = {}
        self.doc_id_to_file: dict[str, str] = {}
        self.cross_refs: dict[str, list[dict]] = {}
        # section_slug -> (filename, line_num) for cross-ref resolution
        self.section_index: dict[str, tuple[str, int]] = {}

    # Meta/documentation files to exclude from the RAG index
    EXCLUDE_FILES = {"HOW_IT_WORKS.md", "README.md", "CHANGELOG.md"}

    def index_all(self):
        """Index all markdown files and build cross-reference map."""
        md_files = sorted(
            f for f in self.docs_dir.glob("*.md")
            if f.name not in self.EXCLUDE_FILES
        )
        if not md_files:
            raise FileNotFoundError(f"No markdown files found in {self.docs_dir}")

        print(f"\n{'='*60}")
        print(f"Indexing {len(md_files)} documents...")
        print(f"{'='*60}\n")

        all_file_paths = [str(f) for f in md_files]

        for md_file in md_files:
            fname = md_file.name
            existing_id = next(
                (did for did, doc in self.client.documents.items()
                 if doc.get('doc_name') == md_file.stem),
                None
            )

            if existing_id:
                print(f"  [cached] {fname} -> {existing_id}")
                doc_id = existing_id
            else:
                print(f"  [indexing] {fname} ...")
                doc_id = self.client.index(str(md_file))
                print(f"    -> doc_id: {doc_id}")

            self.file_to_doc_id[fname] = doc_id
            self.doc_id_to_file[doc_id] = fname

        # Build cross-reference map
        print(f"\n{'='*60}")
        print("Building cross-reference map...")
        print(f"{'='*60}\n")

        for md_file in md_files:
            refs = extract_cross_references(str(md_file), all_file_paths)
            self.cross_refs[md_file.name] = refs
            if refs:
                targets = set(r['target_file'] for r in refs)
                print(f"  {md_file.name} -> {len(refs)} links to: {', '.join(targets)}")

        # Build section index for cross-ref resolution
        self._build_section_index()

    def _build_section_index(self):
        """Build an index mapping section slugs to (filename, line_num)."""
        for fname, doc_id in self.file_to_doc_id.items():
            structure_json = self.client.get_document_structure(doc_id)
            structure = json.loads(structure_json)
            self._index_sections(fname, structure)

    def _index_sections(self, filename: str, tree, parent_slug=""):
        if isinstance(tree, list):
            for node in tree:
                self._index_sections(filename, node, parent_slug)
        elif isinstance(tree, dict):
            title = tree.get('title', '')
            slug = self._slugify(title)
            self.section_index[f"{filename}#{slug}"] = (filename, tree.get('line_num', 1))
            if tree.get('nodes'):
                for child in tree['nodes']:
                    self._index_sections(filename, child, slug)

    @staticmethod
    def _slugify(title: str) -> str:
        """Convert title to URL slug (matching markdown anchor conventions)."""
        slug = title.lower()
        slug = re.sub(r'[^\w\s-]', '', slug)
        slug = re.sub(r'[\s]+', '-', slug)
        return slug.strip('-')

    def tree_search(self, query: str, max_sections: int = 10) -> list[dict]:
        """
        Programmatic tree search with document diversity.

        Ranks sections PER DOCUMENT first (avoiding positional bias in long lists),
        then interleaves top picks from each document for diversity.
        """
        # Gather sections grouped by document
        doc_sections: dict[str, list[dict]] = {}
        for fname, doc_id in self.file_to_doc_id.items():
            structure_json = self.client.get_document_structure(doc_id)
            structure = json.loads(structure_json)
            sections = []
            self._collect_sections(fname, doc_id, structure, sections)
            # Filter out root nodes (depth=0) that are just the doc title
            sections = [s for s in sections if s['depth'] > 0 or len(sections) <= 2]
            doc_sections[fname] = sections

        # Rank sections within each document using LLM
        per_doc_ranked: dict[str, list[dict]] = {}
        picks_per_doc = max(2, max_sections // len(doc_sections))  # At least 2 per doc

        for fname, sections in doc_sections.items():
            if not sections:
                continue
            section_list_str = "\n".join(
                f"{i+1}. {s['title']} — {(s['summary'] or '')[:120]}"
                for i, s in enumerate(sections)
            )

            ranking_prompt = f"""Question: "{query}"

Below are sections from the document "{fname}". Pick the {picks_per_doc} sections most likely to contain information relevant to the question. Return ONLY their numbers, comma-separated, most relevant first.
If NONE are relevant, respond with: NONE

{section_list_str}

Relevant section numbers:"""

            try:
                response = litellm.completion(
                    model=self.model,
                    messages=[{"role": "user", "content": ranking_prompt}],
                    temperature=0,
                    max_tokens=60,
                )
                ranking_text = response.choices[0].message.content.strip()

                if "NONE" in ranking_text.upper():
                    per_doc_ranked[fname] = []
                    continue

                numbers = [int(n.strip()) for n in re.findall(r'\d+', ranking_text)]
                seen = set()
                ranked = []
                for n in numbers:
                    idx = n - 1
                    if 0 <= idx < len(sections) and idx not in seen:
                        seen.add(idx)
                        ranked.append(sections[idx])
                        if len(ranked) >= picks_per_doc:
                            break
                per_doc_ranked[fname] = ranked
            except Exception as e:
                print(f"  Ranking error for {fname}: {e}")
                per_doc_ranked[fname] = sections[:picks_per_doc]

        # Interleave: round-robin from each document's ranked list for diversity
        selected = []
        selected_keys = set()
        max_rounds = picks_per_doc
        for round_idx in range(max_rounds):
            for fname in doc_sections:
                ranked = per_doc_ranked.get(fname, [])
                if round_idx < len(ranked):
                    s = ranked[round_idx]
                    key = (s['doc_id'], s['line_num'])
                    if key not in selected_keys:
                        selected_keys.add(key)
                        selected.append(s)
                        if len(selected) >= max_sections:
                            break
            if len(selected) >= max_sections:
                break

        return selected

    def _collect_sections(self, filename: str, doc_id: str, tree, sections: list, depth=0):
        """Collect all sections (leaf and intermediate) from the tree."""
        if isinstance(tree, list):
            for node in tree:
                self._collect_sections(filename, doc_id, node, sections, depth)
        elif isinstance(tree, dict):
            summary = tree.get('summary') or tree.get('prefix_summary', '')
            sections.append({
                'filename': filename,
                'doc_id': doc_id,
                'title': tree.get('title', ''),
                'node_id': tree.get('node_id', ''),
                'line_num': tree.get('line_num', 1),
                'summary': summary,
                'depth': depth,
            })
            if tree.get('nodes'):
                for child in tree['nodes']:
                    self._collect_sections(filename, doc_id, child, sections, depth + 1)

    def get_section_content(self, doc_id: str, line_num: int, range_size: int = 30) -> str:
        """Get content around a specific line number."""
        content_json = self.client.get_page_content(doc_id, str(line_num))
        content_data = json.loads(content_json)
        if isinstance(content_data, list) and content_data:
            return content_data[0].get('content', '')
        return str(content_data)

    def follow_cross_references(self, filename: str, content: str) -> list[dict]:
        """Find cross-references in content that point to other documents."""
        refs = []
        link_pattern = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')

        for match in link_pattern.finditer(content):
            link_text = match.group(1)
            link_target = match.group(2)

            if link_target.startswith(('http://', 'https://', '#')):
                continue

            if '#' in link_target:
                target_file, target_section = link_target.split('#', 1)
            else:
                target_file = link_target
                target_section = None

            if target_file in self.file_to_doc_id:
                refs.append({
                    'target_file': target_file,
                    'target_section': target_section,
                    'link_text': link_text,
                })

        return refs


def retrieve_with_cross_refs(index: MultiDocPageIndex, query: str, verbose: bool = True) -> str:
    """
    Full retrieval pipeline:
    1. Tree search to find relevant sections
    2. Retrieve their content
    3. Follow cross-references to get related content from other docs
    4. Assemble context for LLM synthesis
    """
    if verbose:
        print(f"\n  [1/4] Tree search across {len(index.file_to_doc_id)} documents...")

    # Step 1: Find relevant sections via tree search
    relevant_sections = index.tree_search(query, max_sections=10)

    if verbose:
        print(f"  Found {len(relevant_sections)} relevant sections:")
        for s in relevant_sections:
            print(f"    - [{s['filename']}] {s['title']} (L{s['line_num']})")

    # Step 2: Retrieve content for each section
    if verbose:
        print(f"\n  [2/4] Retrieving content...")

    context_parts = []
    seen_sections = set()

    for section in relevant_sections:
        key = (section['doc_id'], section['line_num'])
        if key in seen_sections:
            continue
        seen_sections.add(key)

        content = index.get_section_content(section['doc_id'], section['line_num'])
        if content:
            context_parts.append({
                'filename': section['filename'],
                'title': section['title'],
                'content': content[:2000],  # Limit per section
            })

    # Step 3: Follow cross-references from retrieved content
    if verbose:
        print(f"\n  [3/4] Following cross-references...")

    cross_ref_parts = []
    for part in context_parts:
        refs = index.follow_cross_references(part['filename'], part['content'])
        for ref in refs:
            target_doc_id = index.file_to_doc_id.get(ref['target_file'])
            if not target_doc_id:
                continue

            # Find the target section in the tree
            target_key = None
            if ref['target_section']:
                target_key = f"{ref['target_file']}#{ref['target_section']}"

            if target_key and target_key in index.section_index:
                _, target_line = index.section_index[target_key]
            else:
                # Get first line of the target document
                target_line = 1

            ref_content_key = (target_doc_id, target_line)
            if ref_content_key not in seen_sections:
                seen_sections.add(ref_content_key)
                ref_content = index.get_section_content(target_doc_id, target_line)
                if ref_content:
                    cross_ref_parts.append({
                        'filename': ref['target_file'],
                        'title': f"[Cross-ref from {part['filename']}] {ref['link_text']}",
                        'content': ref_content[:1500],
                    })
                    if verbose:
                        print(f"    Followed: {part['filename']} -> {ref['target_file']}#{ref.get('target_section', '')}")

    # Limit cross-refs to avoid context overflow
    cross_ref_parts = cross_ref_parts[:8]

    all_parts = context_parts + cross_ref_parts

    if verbose:
        print(f"\n  [4/4] Assembled {len(all_parts)} sections for synthesis")

    return all_parts


def synthesize_answer(index: MultiDocPageIndex, query: str, context_parts: list[dict]) -> str:
    """Use LLM to synthesize a final answer from retrieved context."""
    # Build context string
    context_str = ""
    for i, part in enumerate(context_parts, 1):
        context_str += f"\n--- Document: {part['filename']} | Section: {part['title']} ---\n"
        context_str += part['content'][:1500]
        context_str += "\n"

    synthesis_prompt = f"""Based ONLY on the following document excerpts, answer this question:

Question: {query}

{context_str}

Instructions:
- Answer based ONLY on the provided document excerpts
- Cite which document and section each piece of information comes from
- If information spans multiple documents, explain the connections
- Be specific and include technical details from the documents
- If the documents don't contain enough info, say so

Answer:"""

    try:
        response = litellm.completion(
            model=index.model,
            messages=[{"role": "user", "content": synthesis_prompt}],
            temperature=0,
            max_tokens=2048,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Synthesis error: {e}"


def query_pipeline(index: MultiDocPageIndex, question: str, verbose: bool = True) -> str:
    """Full query pipeline: retrieve + cross-ref follow + synthesize."""
    print(f"\n{'='*60}")
    print(f"Q: {question}")
    print(f"{'='*60}")

    # Retrieve context with cross-reference following
    context_parts = retrieve_with_cross_refs(index, question, verbose=verbose)

    # Synthesize answer
    if verbose:
        print(f"\n  Synthesizing answer with LLM...")

    answer = synthesize_answer(index, question, context_parts)

    print(f"\n{'='*60}")
    print("ANSWER:")
    print(f"{'='*60}")
    for line in answer.splitlines():
        print(textwrap.fill(line, width=100))

    # Show sources
    print(f"\n  Sources:")
    sources = set()
    for p in context_parts:
        sources.add(f"  - {p['filename']}: {p['title']}")
    for s in sorted(sources):
        print(s)

    return answer


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    DOCS_DIR = Path(__file__).parent / "docs"
    WORKSPACE = Path(__file__).parent / "workspace"

    # Ensure Ollama is running & pick best available model
    print("Checking Ollama...")
    import subprocess
    try:
        result = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            print("ERROR: Ollama not running. Start with: ollama serve")
            sys.exit(1)
        print(f"Available models:\n{result.stdout}")
    except FileNotFoundError:
        print("ERROR: Ollama not found.")
        sys.exit(1)

    # Pick best available model (prefer 7b, fallback to 3b)
    available = result.stdout
    if "qwen2.5:7b" in available:
        MODEL = "ollama/qwen2.5:7b"
    elif "qwen2.5:3b" in available:
        MODEL = "ollama/qwen2.5:3b"
    else:
        MODEL = "ollama/qwen2.5:3b"  # default
    print(f"Using model: {MODEL}")

    os.environ.setdefault("OPENAI_API_KEY", "ollama")

    # Index all documents
    index = MultiDocPageIndex(docs_dir=str(DOCS_DIR), workspace=str(WORKSPACE), model=MODEL)
    index.index_all()

    # Print overview
    print(f"\n{'='*60}")
    print("INDEXED DOCUMENTS")
    print(f"{'='*60}")
    for fname, doc_id in index.file_to_doc_id.items():
        doc = index.client.documents.get(doc_id, {})
        desc = doc.get('doc_description', 'N/A')
        if desc and len(desc) > 80:
            desc = desc[:80] + "..."
        print(f"  {fname} ({doc.get('line_count', '?')} lines) - {desc}")

    # Print cross-reference map
    print(f"\n{'='*60}")
    print("CROSS-REFERENCE MAP")
    print(f"{'='*60}")
    total_refs = 0
    for fname, refs in index.cross_refs.items():
        if refs:
            targets = {}
            for r in refs:
                tf = r['target_file']
                targets[tf] = targets.get(tf, 0) + 1
            for tf, count in targets.items():
                print(f"  {fname} --({count} refs)--> {tf}")
                total_refs += count
    print(f"  Total: {total_refs} cross-references across {len(index.file_to_doc_id)} documents")

    # Print tree structures
    print(f"\n{'='*60}")
    print("DOCUMENT TREE STRUCTURES (PageIndex)")
    print(f"{'='*60}")
    for fname, doc_id in index.file_to_doc_id.items():
        print(f"\n  --- {fname} ---")
        structure_json = index.client.get_document_structure(doc_id)
        structure = json.loads(structure_json)
        _print_tree(structure, indent=2)

    # Demo queries that require cross-document navigation
    demo_questions = [
        "How do I set up a new Azure SQL database and run Flyway migrations on it? Include info about credentials and infrastructure.",
        "What happens when a secret is rotated in Vault? Trace the flow from Vault to the application running in AKS.",
        "If I need to do an emergency database rollback in production, what are all the steps and systems involved?",
    ]

    print(f"\n\n{'#'*60}")
    print("RUNNING DEMO QUERIES")
    print(f"{'#'*60}")

    for i, q in enumerate(demo_questions, 1):
        print(f"\n{'#'*60}")
        print(f"QUERY {i}/{len(demo_questions)}")
        print(f"{'#'*60}")
        query_pipeline(index, q, verbose=True)

    # Interactive mode
    print(f"\n\n{'='*60}")
    print("INTERACTIVE MODE - Type your question (or 'quit' to exit)")
    print(f"{'='*60}")

    while True:
        print()
        try:
            question = input(">>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        if not question or question.lower() in ('quit', 'exit', 'q'):
            break
        query_pipeline(index, question, verbose=True)


def _print_tree(tree, indent=0):
    if isinstance(tree, list):
        for node in tree:
            _print_tree(node, indent)
    elif isinstance(tree, dict):
        title = tree.get('title', '?')
        node_id = tree.get('node_id', '?')
        line_num = tree.get('line_num', '?')
        summary = tree.get('summary') or tree.get('prefix_summary', '')
        if summary and len(summary) > 60:
            summary = summary[:60] + "..."
        summary_str = f"  — {summary}" if summary else ""
        print(f"{'  ' * indent}[{node_id}] (L{line_num}) {title}{summary_str}")
        if tree.get('nodes'):
            for child in tree['nodes']:
                _print_tree(child, indent + 1)


if __name__ == "__main__":
    main()
