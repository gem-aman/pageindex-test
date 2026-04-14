# PageIndex Vectorless RAG for Cross-Referencing Technical Docs

## A Complete Guide — How It Works, What We Built, and Why

---

## Table of Contents

1. [What Is PageIndex?](#1-what-is-pageindex)
2. [Why Vectorless? The Problem with Vector RAG](#2-why-vectorless-the-problem-with-vector-rag)
3. [How PageIndex Works Internally](#3-how-pageindex-works-internally)
4. [Our Challenge: Cross-Referencing Docs](#4-our-challenge-cross-referencing-docs)
5. [What We Built](#5-what-we-built)
6. [Step-by-Step Implementation Process](#6-step-by-step-implementation-process)
7. [The Extra Work Beyond Out-of-the-Box PageIndex](#7-the-extra-work-beyond-out-of-the-box-pageindex)
8. [How a Query Flows Through the System](#8-how-a-query-flows-through-the-system)
9. [Challenges Faced and Solutions](#9-challenges-faced-and-solutions)
10. [Results and Example Outputs](#10-results-and-example-outputs)
11. [Architecture Diagram](#11-architecture-diagram)
12. [Critical Evaluation: 3b vs 7b Model Comparison](#12-critical-evaluation-3b-vs-7b-model-comparison)
13. [Bug Found and Fixed: Tree Search Diversity](#13-bug-found-and-fixed-tree-search-diversity)
14. [Known Limitations](#14-known-limitations)
15. [Lessons Learned](#15-lessons-learned)

---

## 1. What Is PageIndex?

[PageIndex](https://github.com/VectifyAI/PageIndex) is a **vectorless, reasoning-based RAG** (Retrieval-Augmented Generation) framework by VectifyAI. Instead of converting documents into vector embeddings and using similarity search (the traditional RAG approach), PageIndex builds a **hierarchical tree index** of each document and uses **LLM reasoning** to navigate that tree to find relevant information.

Think of it this way:
- **Vector RAG** = scanning every page of a textbook character-by-character looking for similar words
- **PageIndex** = reading the Table of Contents, picking the right chapter, drilling into the right subsection — just like a human would

### Core Philosophy

PageIndex treats RAG the way a human navigates a technical manual:

1. **Look at the structure** — what chapters/sections exist?
2. **Reason about relevance** — which section is likely to have what I need?
3. **Read that section** — retrieve the actual content
4. **Follow references** — if that section points elsewhere, follow the link

---

## 2. Why Vectorless? The Problem with Vector RAG

Traditional vector RAG works like this:

```
Document → Split into chunks → Embed each chunk → Store in vector DB
Query → Embed query → Find nearest vectors → Return matching chunks
```

**Problems:**

| Issue | Description |
|---|---|
| **Lost structure** | Chunking destroys document hierarchy. A heading-subheading relationship is lost. |
| **Context blindness** | A chunk about "Connection Strings" doesn't know it belongs under "Azure SQL Database". |
| **Cross-references ignored** | If a section says "see Vault Secrets for credentials," vector search can't follow that link. |
| **Semantic drift** | Embedding similarity doesn't equal logical relevance. "AKS deployment" and "Kubernetes rollback" may be far apart in vector space but tightly connected in meaning. |

**PageIndex solves all of these** by preserving the document's natural hierarchy and using LLM reasoning instead of vector distance.

---

## 3. How PageIndex Works Internally

### 3.1 Indexing Phase

When you call `client.index("document.md")`, PageIndex performs these steps:

#### Step A: Parse the Markdown Structure

PageIndex reads the markdown file and uses `#` headers to build a tree:

```
# Azure Infrastructure Guide           → Root node (depth 0)
  ## Resource Groups                    → Child node (depth 1)
  ## Azure SQL Database                 → Child node (depth 1)
    ### SQL Server Configuration        → Grandchild node (depth 2)
    ### Connection Strings              → Grandchild node (depth 2)
  ## AKS Cluster                        → Child node (depth 1)
```

This is handled by `page_index_md.py` in the PageIndex source code. It walks the file line by line, tracks the current heading level, and builds a tree of nodes.

#### Step B: Compute Token Counts and Assign Node IDs

Each node gets:
- A unique `node_id` (e.g., `0000`, `0001`, `0002`)
- A `line_num` (the line in the source file where the section starts)
- The raw `text` content of that section (between this heading and the next)
- A `token_count` for context window management

#### Step C: Generate Summaries via LLM

For each section, PageIndex calls the LLM to generate a short summary. This summary is stored in the tree and used during retrieval to help the LLM decide which sections are relevant without reading the full text.

The LLM prompt used internally is essentially:
> "Summarize this section in a few sentences, focusing on what information it contains."

#### Step D: Store the Tree as JSON

The final tree index is saved to the workspace directory as a JSON file. Here is a real example from our implementation:

```json
{
  "id": "19e4a29c-200e-4a59-ace7-cb9447406860",
  "type": "md",
  "doc_name": "azure-infrastructure",
  "doc_description": "A comprehensive guide to setting up and managing Azure infrastructure...",
  "line_count": 119,
  "structure": [
    {
      "title": "Azure Infrastructure Guide",
      "node_id": "0000",
      "line_num": 1,
      "text": "# Azure Infrastructure Guide\n\nThis document covers...",
      "nodes": [
        {
          "title": "Resource Groups",
          "node_id": "0001",
          "line_num": 5,
          "text": "## Resource Groups\n\nAll resources are organized into...",
          "summary": "Resources organized into per-environment resource groups..."
        },
        {
          "title": "Azure SQL Database",
          "node_id": "0002",
          "line_num": 15,
          "nodes": [
            {
              "title": "SQL Server Configuration",
              "node_id": "0003",
              "line_num": 19,
              "summary": "GP_Gen5_2 SKU, 32GB, TDE enabled, credentials in Key Vault..."
            }
          ]
        }
      ]
    }
  ]
}
```

#### Step E: Metadata File

PageIndex also maintains a `_meta.json` file in the workspace that maps document IDs to their paths, types, names, and descriptions. This allows re-using cached indexes on subsequent runs.

### 3.2 Retrieval Phase

PageIndex provides three retrieval APIs:

| API | Purpose |
|---|---|
| `get_document(doc_id)` | Get full document info |
| `get_document_structure(doc_id)` | Get the tree index (JSON) |
| `get_page_content(doc_id, line_num)` | Get content at a specific line |

During retrieval, the workflow is:

1. **Get the tree structure** — load the hierarchical index
2. **Reason about which sections are relevant** — the LLM reads section titles + summaries and picks the most promising ones
3. **Retrieve content** — fetch the actual text of those sections using `get_page_content()`

This is fundamentally different from vector search: the LLM **reasons** about where to look, rather than blindly matching embedding vectors.

---

## 4. Our Challenge: Cross-Referencing Docs

### The Problem

We have 5 interconnected technical documents describing a real-world DevOps platform:

| Document | Lines | Topic |
|---|---|---|
| `azure-infrastructure.md` | 119 | Azure resources (SQL, AKS, ACR, Key Vault) |
| `terraform-modules.md` | 230 | Terraform IaC modules for all Azure resources |
| `vault-secrets.md` | 234 | HashiCorp Vault secret management |
| `flyway-migrations.md` | 226 | Flyway database schema migrations |
| `jenkins-pipeline.md` | 316 | Jenkins CI/CD pipeline orchestrating everything |

These documents are **heavily interconnected** with **106 cross-references** between them. For example:

- `azure-infrastructure.md` says: *"Admin credentials are stored in Azure Key Vault — see [Vault Secrets Management](vault-secrets.md#azure-sql-credentials)"*
- `jenkins-pipeline.md` says: *"Database credentials are fetched from Vault — see [Vault Secrets Management](vault-secrets.md#database-credentials)"*
- `flyway-migrations.md` says: *"Connection details are managed in Vault — see [Vault Secrets Management](vault-secrets.md#database-credentials)"*

A real user question like **"How do I set up Azure SQL and run Flyway migrations?"** requires information from at least 4 of 5 documents:
1. `azure-infrastructure.md` — what SQL config looks like
2. `terraform-modules.md` — how to provision it
3. `vault-secrets.md` — where credentials come from
4. `flyway-migrations.md` — how to run migrations

### Why Out-of-the-Box PageIndex Isn't Enough

PageIndex is designed for **single-document** retrieval. It indexes one document at a time and retrieves from one document at a time. It has no concept of:

- **Multi-document search** — querying across multiple document trees simultaneously
- **Cross-reference following** — when retrieved content contains a link to another document, automatically fetching that target
- **Section-slug resolution** — translating a markdown anchor like `#azure-sql-credentials` into a specific line number in the target document

---

## 5. What We Built

We built `cross_doc_rag.py` — a **Multi-Document Cross-Reference RAG system** that wraps PageIndex with additional capabilities:

```
┌─────────────────────────────────────────────────────────────┐
│                   cross_doc_rag.py                          │
│                                                             │
│  ┌──────────────────────┐   ┌───────────────────────┐      │
│  │  MultiDocPageIndex   │   │  Cross-Ref Engine     │      │
│  │                      │   │                       │      │
│  │  - file_to_doc_id    │   │  - extract refs       │      │
│  │  - doc_id_to_file    │   │  - follow links       │      │
│  │  - section_index     │   │  - resolve slugs      │      │
│  │  - cross_refs map    │   │  - assemble context   │      │
│  └────────┬─────────────┘   └───────────┬───────────┘      │
│           │                             │                   │
│           ▼                             ▼                   │
│  ┌──────────────────────────────────────────────────┐      │
│  │         PageIndex (VectifyAI)                     │      │
│  │  - index()               - get_page_content()     │      │
│  │  - get_document()        - get_document_structure()│     │
│  └──────────────────────────────────────────────────┘      │
│                          │                                  │
│                          ▼                                  │
│  ┌──────────────────────────────────────────────────┐      │
│  │         Ollama (Local LLM)                        │      │
│  │  - qwen2.5:3b or 7b  (via LiteLLM)               │      │
│  └──────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────┘
```

### Components We Added on Top of PageIndex

| Component | What It Does | Why It's Needed |
|---|---|---|
| `MultiDocPageIndex` | Manages multiple PageIndex documents as a single searchable corpus | PageIndex only handles one doc at a time |
| `extract_cross_references()` | Parses markdown `[text](file.md#section)` links between docs | PageIndex doesn't know about inter-doc links |
| `section_index` | Maps `filename#slug` → `(filename, line_num)` for every section | Needed to resolve cross-reference anchors |
| `tree_search()` | LLM-ranked section search across ALL document trees | PageIndex only searches within one tree |
| `follow_cross_references()` | Automatically fetches content from linked documents | The key innovation for navigating interconnected docs |
| `retrieve_with_cross_refs()` | The 4-step retrieval pipeline | Orchestrates the full multi-doc retrieval |
| `synthesize_answer()` | Final LLM synthesis from assembled multi-doc context | Produces a coherent answer citing sources |

---

## 6. Step-by-Step Implementation Process

### Step 1: Research PageIndex (Understanding the Framework)

We studied:
- The [PageIndex README](https://github.com/VectifyAI/PageIndex) — understanding the API
- The [blog post](https://vectify.ai/blog/PageIndex) — understanding the philosophy
- Source files: `client.py`, `page_index_md.py`, `utils.py`, `retrieve.py` — understanding internals
- The `config.yaml` — understanding LLM prompts used for summarization

**Key insight from the blog:** *"Reasoning-based RAG can follow references like a human reader"* — this is what we built on.

### Step 2: Set Up Infrastructure

- **Cloned PageIndex** repo to `/home/aman/pageindex-test/PageIndex/`
- **Created Python venv** with all dependencies (`pageindex`, `litellm`, `ollama`, etc.)
- **Set up Ollama** as the local LLM server with the `qwen2.5:3b` model
- **GPU**: NVIDIA RTX 1000 Ada Generation (6GB VRAM) — enough for 3B parameter models

### Step 3: Create Interconnected Test Documents

We wrote 5 realistic technical documents (1,125 total lines) simulating a real DevOps platform:

```
azure-infrastructure.md ←→ terraform-modules.md
         ↕                          ↕
vault-secrets.md        ←→  flyway-migrations.md
         ↕                          ↕
              jenkins-pipeline.md
              (orchestrates everything)
```

Each document contains deliberate cross-references in markdown link syntax:
```markdown
Database credentials are fetched from Vault — see
[Vault Secrets Management](vault-secrets.md#database-credentials)
```

**106 cross-references** total across all 5 documents. The reference counts:

| Document | Cross-References |
|---|---|
| `jenkins-pipeline.md` | 31 (most — it touches everything) |
| `vault-secrets.md` | 22 |
| `flyway-migrations.md` | 20 |
| `terraform-modules.md` | 19 |
| `azure-infrastructure.md` | 14 |

### Step 4: Push to GitHub

Created repo `gem-aman/pageindex-test` and pushed docs to `main` branch.

### Step 5: Build the Multi-Document RAG Pipeline

This was the core engineering work. See Section 7 for details on what we had to build beyond PageIndex.

### Step 6: Iterate on the Architecture

We went through **three architectural iterations** before landing on the final design:

| Iteration | Approach | Why It Failed |
|---|---|---|
| 1 | **LLM Tool Calling** — use native function calling (like OpenAI tools) | qwen2.5:3b doesn't produce valid `tool_calls` JSON. Output was garbled. |
| 2 | **ReAct Agent** — text-based `THOUGHT: ... ACTION: ...` loop | 3B model hallucinated and issued multiple actions per turn, breaking the parsing loop. |
| 3 | **Programmatic Tree Search + LLM Synthesis** ✅ | LLM only scores section relevance and synthesizes final answer. All navigation is programmatic. |

The final architecture is the **right** approach for small local models: minimize what the LLM has to "decide" and maximize what's done programmatically.

### Step 7: Upgrade to qwen2.5:7b and Critical Evaluation

After initial 3b testing, we downloaded `qwen2.5:7b` and ran a formal 10-question evaluation suite (`eval_test.py`) that:
- Tests simple, vague, procedural, cross-cutting, troubleshooting, and inventory questions
- Scores each answer on 5 dimensions: Must-Contain terms, Cross-Doc coverage, Procedural quality, Specificity, and Length adequacy
- Acts as "LLM-as-Judge" to determine if downstream agents could act on the answers

This evaluation revealed a **critical tree search bug** (see Section 13) and drove significant improvements to the architecture.

### Step 8: Test End-to-End

Ran 3 demo queries + 1 interactive query, all producing correct multi-document answers with cross-reference following.

---

## 7. The Extra Work Beyond Out-of-the-Box PageIndex

Here is everything we had to build on top of PageIndex to make cross-document RAG work:

### 7.1 Multi-Document Indexing Wrapper

**Problem:** PageIndex's `client.index()` returns a single doc_id. There's no built-in way to manage multiple documents as a corpus.

**Solution:** `MultiDocPageIndex` class maintains:
- `file_to_doc_id` — maps filename → PageIndex doc_id
- `doc_id_to_file` — reverse mapping
- `index_all()` — indexes every `.md` file in a directory, reusing cached indexes

```python
class MultiDocPageIndex:
    def index_all(self):
        md_files = sorted(self.docs_dir.glob("*.md"))
        for md_file in md_files:
            # Check if already cached in PageIndex workspace
            existing_id = next(
                (did for did, doc in self.client.documents.items()
                 if doc.get('doc_name') == md_file.stem), None)
            if existing_id:
                doc_id = existing_id   # Re-use cache
            else:
                doc_id = self.client.index(str(md_file))  # Index new
            self.file_to_doc_id[md_file.name] = doc_id
```

### 7.2 Cross-Reference Extraction Engine

**Problem:** PageIndex has no concept of links between documents.

**Solution:** `extract_cross_references()` parses every markdown file for `[text](target.md#section)` patterns, filtering out HTTP links and intra-document anchors:

```python
def extract_cross_references(md_path, all_doc_files):
    link_pattern = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')
    for match in link_pattern.finditer(line):
        link_target = match.group(2)
        if '#' in link_target:
            target_file, target_section = link_target.split('#', 1)
        # Only track links to other docs in our collection
        if target_file in known_files:
            refs.append({...})
```

This produces a full cross-reference map:
```
azure-infrastructure.md --(3 refs)--> terraform-modules.md
azure-infrastructure.md --(3 refs)--> vault-secrets.md
jenkins-pipeline.md     --(6 refs)--> terraform-modules.md
jenkins-pipeline.md     --(7 refs)--> vault-secrets.md
...
Total: 106 cross-references across 5 documents
```

### 7.3 Section Slug Index

**Problem:** Cross-references use markdown anchors like `vault-secrets.md#database-credentials`. PageIndex uses line numbers. We need to translate slugs to line numbers.

**Solution:** `_build_section_index()` walks every document's tree structure and builds a mapping from `filename#slug` → `(filename, line_number)`:

```python
def _build_section_index(self):
    for fname, doc_id in self.file_to_doc_id.items():
        structure = json.loads(self.client.get_document_structure(doc_id))
        self._index_sections(fname, structure)

def _index_sections(self, filename, tree, parent_slug=""):
    title = tree.get('title', '')
    slug = self._slugify(title)  # "Azure SQL Credentials" → "azure-sql-credentials"
    self.section_index[f"{filename}#{slug}"] = (filename, tree.get('line_num', 1))
```

Now when we encounter a cross-reference to `vault-secrets.md#database-credentials`, we can look up the exact line number in the Vault document and call `get_page_content()` on it.

### 7.4 Cross-Document Tree Search (with Diversity)

**Problem:** PageIndex searches within a single document tree. We need to search across ALL document trees simultaneously. A naive approach (dumping all sections into one list for LLM ranking) causes **positional bias** — the LLM picks the same sections regardless of the query (see Section 13).

**Solution:** `tree_search()` uses **per-document ranking with round-robin interleaving**:

```python
def tree_search(self, query, max_sections=10):
    # 1. Gather sections GROUPED BY DOCUMENT
    doc_sections = {}
    for fname, doc_id in self.file_to_doc_id.items():
        structure = json.loads(self.client.get_document_structure(doc_id))
        doc_sections[fname] = self._collect_sections(...)

    # 2. Rank sections WITHIN each document separately
    per_doc_ranked = {}
    for fname, sections in doc_sections.items():
        ranking_prompt = f"""Question: "{query}"
        Sections from "{fname}". Pick the most relevant ones..."""
        # LLM picks top 2-3 from this document only (small list, no bias)
        per_doc_ranked[fname] = llm_rank(sections, ranking_prompt)

    # 3. Interleave: round-robin from each document's ranked list
    selected = []
    for round_idx in range(max_rounds):
        for fname in doc_sections:
            ranked = per_doc_ranked[fname]
            if round_idx < len(ranked):
                selected.append(ranked[round_idx])
```

This ensures **diversity** — every document contributes its most relevant sections, preventing a single document from dominating the results. The LLM makes small, focused decisions (ranking ~8 sections within one doc) instead of one overwhelming decision (ranking ~45 sections across all docs).

### 7.5 Cross-Reference Following

**Problem:** When we retrieve content from Azure docs that says *"credentials stored in Vault — see [Vault Secrets](vault-secrets.md#azure-sql-credentials)"*, we need to automatically fetch that referenced Vault section too.

**Solution:** `follow_cross_references()` scans retrieved content for markdown links and fetches the target sections:

```python
def follow_cross_references(self, filename, content):
    """Find cross-references in content that point to other documents."""
    for match in link_pattern.finditer(content):
        target_file, target_section = link_target.split('#', 1)
        if target_file in self.file_to_doc_id:
            refs.append({'target_file': target_file, 'target_section': target_section})

# In retrieve_with_cross_refs():
for ref in refs:
    target_key = f"{ref['target_file']}#{ref['target_section']}"
    _, target_line = index.section_index[target_key]  # Slug → line number
    ref_content = index.get_section_content(target_doc_id, target_line)
```

This mimics how a human reader follows "see also" links in documentation.

---

## 8. How a Query Flows Through the System

Let's trace a real query: **"How do I set up a new Azure SQL database and run Flyway migrations on it?"**

### Phase 1: Tree Search (Find Relevant Sections)

```
Input: "How do I set up a new Azure SQL database and run Flyway migrations?"

All document tree sections collected:
  1. [azure-infrastructure.md] Azure Infrastructure Guide — comprehensive guide...
  2. [azure-infrastructure.md] Resource Groups — organized per environment...
  3. [azure-infrastructure.md] Azure SQL Database — relational data storage...
  4. [azure-infrastructure.md] SQL Server Configuration — GP_Gen5_2 SKU...
  ...
  15. [flyway-migrations.md] Running Migrations — flyway migrate commands...
  16. [flyway-migrations.md] Database Connection Config — JDBC URLs...
  ...
  35. [jenkins-pipeline.md] Flyway Migration Stage — automated DB updates...

LLM ranks: 3, 4, 15, 16, 35, 12  (Azure SQL, SQL Config, Running Migrations, DB Connection, Jenkins Flyway Stage, Terraform SQL Module)
```

### Phase 2: Content Retrieval

For each of the 6 selected sections, call `get_page_content(doc_id, line_num)` to fetch the actual text from the original markdown files.

### Phase 3: Cross-Reference Following

The retrieved content from `azure-infrastructure.md` contains:
```markdown
Admin credentials are stored in Azure Key Vault — see [Vault Secrets](vault-secrets.md#azure-sql-credentials)
```

The system:
1. Detects this link via regex
2. Looks up `vault-secrets.md#azure-sql-credentials` in the section index
3. Finds it maps to line 87 in `vault-secrets.md`
4. Calls `get_page_content("vault-doc-id", "87")`
5. Adds the Vault credentials section to the context

Similarly for other cross-references found in retrieved content. This adds ~5 more sections from other documents.

### Phase 4: LLM Synthesis

All retrieved + cross-referenced sections (≈11 total) are assembled into a single prompt:

```
Based ONLY on the following document excerpts, answer this question:

Question: How do I set up a new Azure SQL database and run Flyway migrations?

--- Document: azure-infrastructure.md | Section: Azure SQL Database ---
[actual content from the doc]

--- Document: terraform-modules.md | Section: SQL Database Module ---
[actual Terraform HCL code from the doc]

--- Document: vault-secrets.md | Section: Azure SQL Credentials ---
[actual Vault paths from the doc]

--- Document: flyway-migrations.md | Section: Running Migrations ---
[actual Flyway commands from the doc]

--- Document: jenkins-pipeline.md | Section: Flyway Migration Stage ---
[actual Jenkins Groovy code from the doc]

Instructions:
- Answer based ONLY on the provided document excerpts
- Cite which document and section each piece of information comes from
- If information spans multiple documents, explain the connections
```

The LLM produces a coherent, multi-source answer with specific technical details pulled from each document.

---

## 9. Challenges Faced and Solutions

### Challenge 1: Small Model Can't Do Tool Calling

**Problem:** The qwen2.5:3b model doesn't reliably produce structured tool calls. Native function calling (`tool_choice: auto`) produced garbled JSON instead of proper `tool_calls` responses.

**Solution:** Removed tool-calling entirely. The LLM only does two simple tasks:
1. Pick relevant section numbers from a list (tree search ranking)
2. Write a coherent answer from provided context (synthesis)

Both are standard text-completion tasks that even 3B models handle well.

### Challenge 2: ReAct Agent Loop Hallucinations

**Problem:** We tried a ReAct-style approach (`THOUGHT: ... ACTION: tool(args)`), but the 3B model would hallucinate multiple actions per turn, or call non-existent tools, breaking the parsing loop.

**Solution:** Abandoned the agent loop entirely. All navigation logic is **programmatic** — Python code handles the tree walking, cross-reference extraction, slug resolution, and content assembly. The LLM is only called for judgment (ranking) and language (synthesis).

### Challenge 3: PageIndex Is Single-Document Only

**Problem:** `PageIndexClient` has no multi-document search capability. Each document is indexed independently.

**Solution:** Built `MultiDocPageIndex` wrapper that:
- Indexes all docs in a directory
- Collects section summaries from ALL trees
- Presents a unified section list to the LLM for ranking

### Challenge 4: Cross-Reference Resolution

**Problem:** A markdown link like `vault-secrets.md#azure-sql-credentials` must be translated to a PageIndex line-number-based content retrieval.

**Solution:** Built a section slug index by walking all document trees, slugifying each section title (matching GitHub/markdown anchor conventions), and mapping `filename#slug` → `line_num`.

### Challenge 5: Context Window Management

**Problem:** With 5 documents and aggressive cross-reference following, the assembled context could exceed the model's context window.

**Solution:**
- Limit tree search to top 10 sections (2 per document via diversity interleaving)
- Limit cross-reference following to 8 additional sections
- Truncate each section to 2000 chars (direct) or 1500 chars (cross-ref)
- Total context stays within ~20K tokens — well within qwen2.5:7b's 32K context window

### Challenge 6: Model Download Speed

**Problem:** Attempted to download `qwen2.5:7b` for better quality, but download speed was ~25KB/s on the available connection.

**Solution:** Optimized the entire pipeline for `qwen2.5:3b` first. Added auto-model-selection at startup that prefers `7b` but falls back to `3b`. Eventually completed the 7b download for the final evaluation.

### Challenge 7: Tree Search Positional Bias (Found During 7b Evaluation)

**Problem:** When all ~45 sections from all documents were presented to the LLM in a single ranked list, the 7b model exhibited **positional bias** — returning the same 6 sections for every query regardless of topic. Sections early in the list (from HOW_IT_WORKS.md and the first few docs) were always selected. This caused `azure-infrastructure.md`, `flyway-migrations.md`, and `vault-secrets.md` to never appear directly in results.

**Solution:** Three-part fix:
1. **Exclude meta-documents** like HOW_IT_WORKS.md from the RAG index
2. **Per-document ranking** — rank sections within each document separately (small, focused lists)
3. **Round-robin interleaving** — ensure every document contributes to the result set

This improved pass rate from **40% → 80%** and average score from **4.1 → 4.8** out of 5.

---

## 10. Results and Example Outputs

### Model: qwen2.5:7b (Final Evaluation)

We ran a comprehensive 10-question evaluation suite testing simple, vague, procedural, cross-cutting, troubleshooting, and inventory questions. Each answer was auto-scored on Must-Contain terms, Cross-Doc coverage, Procedural quality, Specificity, and Length.

#### Final Scorecard

| ID | Category | Question | Score | Pass |
|----|----------|----------|-------|------|
| Q1 | simple-vague | "how do I deploy to production?" | 5.0/5 | ✓ |
| Q2 | simple-lookup | "where are the database passwords stored?" | 4.8/5 | ✓ |
| Q3 | procedural | "how to add a new database table" | 5.0/5 | ✓ |
| Q4 | cross-cutting | "set up a brand new environment from scratch" | 5.0/5 | ✓ |
| Q5 | simple-lookup | "what's the networking setup?" | 5.0/5 | ✓ |
| Q6 | troubleshooting | "flyway migration failed in CI, what do I check?" | 4.8/5 | ✓ |
| Q7 | cross-cutting | "how does the app get secrets at runtime in kubernetes?" | 5.0/5 | ✓ |
| Q8 | simple-vague | "tell me about monitoring and alerts" | 3.9/5 | ✗ |
| Q9 | procedural-complex | "rotate sql admin password without downtime" | 4.7/5 | ✓ |
| Q10 | inventory | "what terraform modules do we have?" | 4.7/5 | ✗ |

**Average: 4.8/5 | Passed: 8/10 (80%)**

#### By Category

| Category | Average Score |
|----------|-------------|
| cross-cutting | 5.0/5 |
| procedural | 5.0/5 |
| simple-lookup | 4.9/5 |
| troubleshooting | 4.8/5 |
| procedural-complex | 4.7/5 |
| inventory | 4.7/5 |
| simple-vague | 4.5/5 |

#### Improvement Over Initial Run (Before Bug Fixes)

| Metric | Before (v1) | After (v3) | Delta |
|--------|-------------|------------|-------|
| Average Score | 4.1/5 | 4.8/5 | +0.7 |
| Pass Rate | 40% (4/10) | 80% (8/10) | +40% |
| Cross-Doc Score | 3.4/5 avg | 5.0/5 avg | +1.6 |
| Docs always missing | azure-infrastructure.md | None systematic | Fixed |

### Example: Q4 — "Set up a brand new environment from scratch"

This is the hardest question — it requires ALL 5 documents. The system:

1. **Tree search** found 10 sections across all 5 docs:
   - azure: AKS, Networking
   - flyway: Running Migrations, Local Development
   - jenkins: Pipeline Architecture, Terraform Plan and Apply
   - terraform: Prerequisites, Resource Group Module
   - vault: Server Setup, Azure SQL Credentials

2. **Cross-references** followed 6 links to additional sections:
   - terraform → vault (service principal creds)
   - vault → azure (Key Vault)
   - vault → terraform (SQL module)
   - vault → flyway (database connection)
   - azure → terraform (AKS cluster module, networking module)

3. **Assembled 16 sections** into context and synthesized a 473-word answer that included:
   - Complete Terraform HCL code for resource groups, networking, AKS, SQL
   - Vault credential configuration with JSON paths
   - Local Flyway migration commands with Docker option
   - Jenkins pipeline integration steps
   - All with citations to specific documents and sections

### Example: Q7 — "How does the app get secrets at runtime in kubernetes?"

Answer traced the full secrets flow from 3 documents:
1. **Vault** → Database Secrets Engine generates dynamic credentials (`vault read -format=json database/creds/flyway-migration`)
2. **Jenkins** → `withVaultSecrets` injects `DB_CONNECTION_STRING` and `EXTERNAL_API_KEY` as env vars
3. **Kubernetes** → Jobs use `secretKeyRef` to mount `db-credentials` and `db-connection` secrets
4. **Azure** → CSI Secrets Store Driver syncs Vault → Azure Key Vault → AKS pod volume mounts

Each step included actual code snippets (bash, groovy, YAML, HCL) copied from the source documents.

---

## 11. Architecture Diagram

```
                         User Query
                             │
                             ▼
                    ┌──────────────────┐
                    │  query_pipeline() │
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────────┐
              │              │                  │
              ▼              ▼                  ▼         STEP 1: Per-Doc
    ┌──────────────┐ ┌──────────────┐ ┌──────────────┐   Tree Search
    │ azure tree   │ │ vault tree   │ │ jenkins tree │   (1 LLM call per doc)
    │ LLM: top 2   │ │ LLM: top 2   │ │ LLM: top 2   │
    └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
           │                │                │
           └────────────────┼────────────────┘
                            │  Round-Robin Interleave
                            ▼  (ensures diversity)
                  ┌────────────────────┐
                  │  10 sections from  │
                  │  all 5 documents   │
                  └────────┬───────────┘
                           │
                           ▼
                  ┌────────────────────┐
                  │  STEP 2: Retrieve  │──── get_page_content(doc_id, line)
                  │  Content           │     for each selected section
                  └────────┬───────────┘
                           │
                           ▼
                  ┌────────────────────┐     For each retrieved section:
                  │  STEP 3: Follow    │──── parse markdown links →
                  │  Cross-References  │     resolve slug → retrieve target
                  │  (up to 8 extra)   │     (NO LLM call — pure Python)
                  └────────┬───────────┘
                           │
                           ▼
                  ┌────────────────────┐
                  │  STEP 4: LLM      │──── LLM Call: "Answer based on
                  │  Synthesis         │     these document excerpts..."
                  └────────┬───────────┘
                           │
                           ▼
                      Final Answer
                 (with source citations)
```

**Total LLM calls per query: N+1** (where N = number of documents)
- N calls for per-document section ranking (one per doc, ~5 calls)
- 1 call for final answer synthesis

Everything else is pure Python logic — no agent loops, no tool calling, no multi-turn LLM conversation.

---

## 12. Critical Evaluation: 3b vs 7b Model Comparison

### Evaluation Methodology

We built `eval_test.py` — a test suite with 10 questions across 7 categories that simulates real user behavior (lazy questions, procedural requests, cross-cutting queries). Each answer is scored automatically on:

| Criterion | Measures |
|-----------|---------|
| **Must-Contain** | Does the answer include specific expected terms/paths/commands? |
| **Cross-Doc** | Did the system pull content from all required source documents? |
| **Procedural** | Does the answer have step-by-step structure an agent could follow? |
| **Specificity** | Are there concrete code snippets, paths, CLI commands? |
| **Length** | Is the answer comprehensive enough (not too terse)? |

### Results Comparison

| Metric | qwen2.5:3b (v1) | qwen2.5:7b (v1, same code) | qwen2.5:7b (v3, fixed) |
|--------|-----------------|---------------------------|----------------------|
| Average Score | ~3.5/5 | 4.1/5 | **4.8/5** |
| Pass Rate | ~30% | 40% | **80%** |
| Avg response time | ~25s | ~43s | ~43s |
| Cross-doc coverage | Poor | Poor (same bug) | **Near-perfect** |

### Key Observations

1. **Model size alone didn't fix retrieval quality.** Going from 3b → 7b only improved from ~3.5 to 4.1. The tree search bug (Section 13) was the dominant problem.

2. **7b produces better synthesis.** Given the same retrieved context, 7b writes more coherent, better-cited answers with more accurate technical details. This is expected — synthesis is a language skill.

3. **7b is more reliable at section ranking.** With the per-doc ranking approach, 7b consistently picks the right sections. 3b occasionally picks irrelevant ones.

4. **Speed tradeoff is acceptable.** ~43s per query (7b) vs ~25s (3b) for significantly better quality. Most time is in LLM inference, not retrieval.

5. **The architecture matters more than the model.** The bug fix (Section 13) produced a bigger quality jump (+0.7 points) than the model upgrade (+0.6 points).

---

## 13. Bug Found and Fixed: Tree Search Diversity

### The Bug

When running the 10-question eval with qwen2.5:7b, we discovered that the tree search returned the **exact same 6 sections for all 10 questions**:

```
EVERY query returned:
  - [HOW_IT_WORKS.md] Query 3: "Emergency database rollback..." (L610)
  - [HOW_IT_WORKS.md] 11. Architecture Diagram (L620)
  - [jenkins-pipeline.md] Main Pipeline (Jenkinsfile) (L39)
  - [jenkins-pipeline.md] Terraform Plan and Apply (L70)
  - [terraform-modules.md] Prerequisites (L5)
  - [terraform-modules.md] Backend Configuration (L14)
```

This meant:
- `azure-infrastructure.md` was **NEVER** directly retrieved
- `flyway-migrations.md` was **NEVER** directly retrieved
- `vault-secrets.md` only appeared via cross-references (not tree search)

### Root Causes

**1. Meta-document pollution.** `HOW_IT_WORKS.md` (this documentation file) was in the `docs/` directory and got indexed. It ate 2 of 6 retrieval slots with irrelevant meta-content.

**2. Positional bias in long lists.** All ~60 sections from all documents were dumped into a single numbered list. The LLM consistently picked items near the top of the list — a known issue with LLMs processing long numbered lists.

**3. No document diversity guarantee.** With 6 slots and 6 documents, a single document could dominate all slots.

### The Fix

Three changes:

```python
# 1. Exclude meta-documents
EXCLUDE_FILES = {"HOW_IT_WORKS.md", "README.md", "CHANGELOG.md"}
md_files = [f for f in docs_dir.glob("*.md") if f.name not in EXCLUDE_FILES]

# 2. Per-document ranking (instead of one giant list)
for fname, sections in doc_sections.items():
    ranking_prompt = f"""Question: "{query}"
    Sections from "{fname}". Pick the most relevant..."""
    per_doc_ranked[fname] = llm_rank(sections)  # Small list, no bias

# 3. Round-robin interleaving
for round_idx in range(max_rounds):
    for fname in doc_sections:
        selected.append(per_doc_ranked[fname][round_idx])
```

### Impact

| Metric | Before Fix | After Fix |
|--------|-----------|-----------|
| Pass rate | 40% | 80% |
| Average score | 4.1/5 | 4.8/5 |
| Cross-doc score | 3.4/5 | 5.0/5 |
| Unique docs in results | 2-3 of 5 | 5 of 5 |
| azure-infrastructure.md hit rate | 0% | 100% |

---

## 14. Known Limitations

### 1. Inventory Questions (Listing ALL Items)

Q10 ("what terraform modules do we have?") scored 4.7/5 but failed because it only listed 4 of 8 Terraform modules. The per-doc ranking retrieves the **most relevant 2** sections per doc, which for an "inventory" question means it misses the less-prominent modules (ACR, SQL Database).

**Root cause:** The system is optimized for **depth** (following cross-references to get detailed, interconnected answers) not **breadth** (listing all items exhaustively).

**Possible fix:** Detect inventory-style questions and increase `picks_per_doc` for the target document.

### 2. Informational Questions Penalized for Non-Procedural Answers

Q8 ("tell me about monitoring and alerts") scored 3.9/5 partly because the procedural score was 1.0/5 — but monitoring is an informational topic, not a procedural one. The eval criteria penalizes non-procedural answers uniformly.

**Root cause:** Evaluation metric assumes all questions benefit from step-by-step structure.

### 3. LLM Synthesis Has a 2048 Token Limit

The synthesis prompt limits `max_tokens=2048`. For complex questions assembling 16+ sections, this can truncate the answer before all sources are cited.

### 4. Cross-Reference Slug Resolution Is Approximate

The slug resolution (`title → lowercase-hyphenated`) follows standard markdown convention but doesn't handle all edge cases (e.g., titles with parentheses, non-ASCII characters, or duplicate headings).

### 5. No Incremental Index Updates

If a document changes, the entire index must be regenerated. PageIndex doesn't support patching a tree index — you must re-index the full file.

---

## 15. Lessons Learned

### On PageIndex

1. **PageIndex's tree index is genuinely useful.** The hierarchical structure with summaries at each level is exactly what you need for LLM-based navigation. It preserves the structure that vector chunking destroys.

2. **PageIndex is a single-document tool by design.** To use it for multi-document scenarios, you need a wrapper layer that manages the document corpus, cross-references, and unified search.

3. **The "vectorless" approach scales differently.** Instead of scaling with embedding dimension and index size (like vector RAG), it scales with the total number of tree sections across documents. For our 5 docs, this was ~45 sections — trivially small for an LLM to rank.

### On Local LLMs

4. **Small models (3B) can do RAG — with the right architecture.** The key is minimizing what the LLM must "decide." Don't ask it to be an agent; ask it to rank a list or write a summary.

5. **7B is the sweet spot for local RAG.** qwen2.5:7b produces noticeably better answers than 3b — more accurate citations, better reasoning about connections, fewer hallucinations. It's the minimum size we'd recommend for production use.

6. **Tool calling is unreliable below 7B parameters.** Both native function calling and ReAct-style text agents failed with qwen2.5:3b. Programmatic orchestration with surgical LLM calls is the way.

7. **Architecture matters more than model size.** The tree search diversity fix produced a bigger quality gain (+0.7) than upgrading from 3b to 7b (+0.6). Get the retrieval right first, upgrade the model second.

### On Cross-Document RAG

8. **Cross-references are gold for RAG.** Technical documentation is full of "see [Other Doc](other.md#section)" links. Following these programmatically adds high-relevance context that no vector search would find.

9. **Document diversity is non-negotiable.** Dumping all sections into one big list for ranking causes positional bias. Per-document ranking with interleaving is essential for multi-doc systems.

10. **Section slug resolution is a solved problem but requires building.** The standard is slugify-the-title (lowercase, replace spaces with hyphens, strip special chars), but you need to build the index yourself.

11. **Context window management matters.** With cross-reference following, context can grow quickly. Limiting direct sections (10) + cross-ref sections (8) and truncating content keeps things predictable.

### On Evaluation

12. **Always build an eval suite.** Without the 10-question test suite, the tree search bias bug would have gone undetected. The system would have appeared to work (it produced plausible answers) while systematically missing entire documents.

13. **"LLM as judge" catches real issues.** Checking for must-contain terms and cross-doc coverage catches retrieval failures that human eye-balling misses. When the same 6 sections appear for every query, a structured eval catches it immediately.

14. **Test with simple, real-user questions.** Don't just test with carefully crafted queries. "where are the database passwords" is how real humans ask things. If the system can't handle vague, one-line questions, it doesn't work.

---

## Files in This Project

```
pageindex-test/
├── cross_doc_rag.py          # Main RAG implementation (~600 lines)
├── eval_test.py              # 10-question evaluation suite with auto-scoring
├── eval_results_7b.json      # Full eval results (JSON)
├── HOW_IT_WORKS.md           # This document
├── docs/
│   ├── azure-infrastructure.md   # 119 lines, 14 cross-refs
│   ├── terraform-modules.md      # 230 lines, 19 cross-refs
│   ├── vault-secrets.md          # 234 lines, 22 cross-refs
│   ├── flyway-migrations.md      # 226 lines, 20 cross-refs
│   └── jenkins-pipeline.md       # 316 lines, 31 cross-refs
├── workspace/                    # PageIndex cached indexes
│   ├── _meta.json                # Document metadata
│   └── *.json                    # Tree indexes (one per doc)
├── PageIndex/                    # Cloned VectifyAI/PageIndex repo
└── venv/                         # Python virtual environment
```

---

## Running It

```bash
# Prerequisites: Ollama running with a qwen2.5 model
ollama serve &
ollama pull qwen2.5:7b    # Recommended (4.7 GB)
# or: ollama pull qwen2.5:3b  # Lighter alternative (1.9 GB)

# Run main RAG system
cd /home/aman/pageindex-test
source venv/bin/activate
OPENAI_API_KEY=ollama python3 cross_doc_rag.py

# Run evaluation suite
OPENAI_API_KEY=ollama python3 eval_test.py
```

The system will:
1. Index all 5 documents (or use cache) — ~2 min first time with 7b
2. Build the cross-reference map (106 links)
3. Run 3 demo queries (main) or 10 eval queries (eval) showing multi-document retrieval
4. Drop into interactive mode for custom queries (main only)
