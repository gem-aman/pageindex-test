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
12. [Lessons Learned](#12-lessons-learned)

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

### Step 7: Test End-to-End

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

### 7.4 Cross-Document Tree Search

**Problem:** PageIndex searches within a single document tree. We need to search across ALL document trees simultaneously.

**Solution:** `tree_search()` collects all sections from all documents, then uses the LLM to rank them by relevance:

```python
def tree_search(self, query, max_sections=8):
    # 1. Collect ALL sections from ALL documents
    all_sections = []
    for fname, doc_id in self.file_to_doc_id.items():
        structure = json.loads(self.client.get_document_structure(doc_id))
        self._collect_sections(fname, doc_id, structure, all_sections)

    # 2. Build a list of section titles + summaries for LLM ranking
    section_list_str = "\n".join(
        f"{i+1}. [{s['filename']}] {s['title']} — {s['summary'][:100]}"
        for i, s in enumerate(all_sections)
    )

    # 3. Ask LLM: "Which sections are most relevant to this query?"
    ranking_prompt = f"""Given this question: "{query}"
    Return ONLY the numbers of the {max_sections} most relevant sections...
    {section_list_str}"""

    # 4. Parse the LLM's response (comma-separated numbers)
    response = litellm.completion(model=self.model, messages=[...])
    numbers = [int(n) for n in re.findall(r'\d+', response)]
```

This is **the key architectural decision**: the LLM only picks section numbers from a list — it doesn't need to generate tool calls or follow a complex agent loop. This works reliably even with a 3B parameter model.

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
- Limit tree search to top 6 sections
- Limit cross-reference following to 5 additional sections
- Truncate each section to 2000 chars (direct) or 1500 chars (cross-ref)
- Total context stays within ~15K tokens — well within qwen2.5:3b's 32K context window

### Challenge 6: Model Download Speed

**Problem:** Attempted to download `qwen2.5:7b` for better quality, but download speed was ~25KB/s on the available connection.

**Solution:** Optimized the entire pipeline for `qwen2.5:3b`. Added auto-model-selection at startup that prefers `7b` but falls back to `3b`.

---

## 10. Results and Example Outputs

### Query 1: "How do I set up Azure SQL and run Flyway migrations?"

**Documents consulted:** azure-infrastructure.md, terraform-modules.md, vault-secrets.md, flyway-migrations.md, jenkins-pipeline.md

**Sources used:** 6 direct sections + 5 cross-referenced sections = 11 total

**Answer included:**
- Exact Terraform HCL code for the SQL module (`modules/sql_database/main.tf`)
- Azure SQL SKU and configuration details
- Vault secret paths for credentials (`secret/azure-sql/dev`, `secret/azure-sql/prod`)
- Flyway migration commands and JDBC connection string format
- Jenkins pipeline Groovy code for automated migrations

### Query 2: "What happens when a secret is rotated in Vault?"

**End-to-end flow traced across:**
- `vault-secrets.md` → secret rotation policies
- `jenkins-pipeline.md` → the scheduled Vault sync Jenkins job
- `azure-infrastructure.md` → Azure Key Vault integration
- `terraform-modules.md` → Key Vault Terraform module

### Query 3: "Emergency database rollback in production"

**All steps and systems identified:**
- `jenkins-pipeline.md` → emergency rollback pipeline stage
- `vault-secrets.md` → credential retrieval for rollback
- `flyway-migrations.md` → `flyway undo` commands and rollback migrations
- Step-by-step procedure assembled from multiple documents

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
                             ▼
                  ┌────────────────────┐
                  │  STEP 1: Tree      │
                  │  Search (LLM)      │──── LLM Call #1: "Which sections
                  └────────┬───────────┘     are relevant to this query?"
                           │
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
    ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
    │azure tree    │ │vault tree    │ │jenkins tree  │   ... all doc trees
    │{sections}    │ │{sections}    │ │{sections}    │
    └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
           └────────────────┼────────────────┘
                            ▼
                  ┌────────────────────┐
                  │  STEP 2: Retrieve  │
                  │  Content           │──── get_page_content(doc_id, line)
                  │  (Programmatic)    │     for each selected section
                  └────────┬───────────┘
                           │
                           ▼
                  ┌────────────────────┐
                  │  STEP 3: Follow    │     For each retrieved section:
                  │  Cross-References  │──── parse markdown links →
                  │  (Programmatic)    │     resolve slug → retrieve target
                  └────────┬───────────┘
                           │
                           ▼
                  ┌────────────────────┐
                  │  STEP 4: LLM      │
                  │  Synthesis         │──── LLM Call #2: "Answer based on
                  └────────┬───────────┘     these document excerpts..."
                           │
                           ▼
                      Final Answer
                 (with source citations)
```

**Total LLM calls per query: 2**
1. Section ranking (tree search)
2. Answer synthesis

Everything else is pure Python logic — no agent loops, no tool calling, no multi-turn LLM conversation.

---

## 12. Lessons Learned

### On PageIndex

1. **PageIndex's tree index is genuinely useful.** The hierarchical structure with summaries at each level is exactly what you need for LLM-based navigation. It preserves the structure that vector chunking destroys.

2. **PageIndex is a single-document tool by design.** To use it for multi-document scenarios, you need a wrapper layer that manages the document corpus, cross-references, and unified search.

3. **The "vectorless" approach scales differently.** Instead of scaling with embedding dimension and index size (like vector RAG), it scales with the total number of tree sections across documents. For our 5 docs, this was ~45 sections — trivially small for an LLM to rank.

### On Local LLMs

4. **Small models (3B) can do RAG well — with the right architecture.** The key is minimizing what the LLM must "decide." Don't ask it to be an agent; ask it to rank a list or write a summary. These are tasks 3B models handle reliably.

5. **Tool calling is unreliable below 7B parameters.** Both native function calling and ReAct-style text agents failed with qwen2.5:3b. Programmatic orchestration with surgical LLM calls is the way.

6. **LiteLLM's Ollama integration works well.** Just prefix model names with `ollama/` and set `OPENAI_API_KEY=ollama`.

### On Cross-Document RAG

7. **Cross-references are gold for RAG.** Technical documentation is full of "see [Other Doc](other.md#section)" links. Following these programmatically adds high-relevance context that no vector search would find.

8. **Section slug resolution is a solved problem but requires building.** The standard is slugify-the-title (lowercase, replace spaces with hyphens, strip special chars), but you need to build the index yourself.

9. **Context window management matters.** With cross-reference following, context can grow exponentially. Limiting direct sections (6) + cross-ref sections (5) and truncating content keeps things predictable.

### On Architecture

10. **"Programmatic navigation + LLM judgment" is a powerful pattern.** Let code handle structure, parsing, linking, and retrieval. Let the LLM handle relevance ranking and language synthesis. This division of labor works with even the smallest models.

---

## Files in This Project

```
pageindex-test/
├── cross_doc_rag.py          # Main implementation (559 lines)
├── docs/
│   ├── azure-infrastructure.md   # 119 lines, 14 cross-refs
│   ├── terraform-modules.md      # 230 lines, 19 cross-refs
│   ├── vault-secrets.md          # 234 lines, 22 cross-refs
│   ├── flyway-migrations.md      # 226 lines, 20 cross-refs
│   └── jenkins-pipeline.md       # 316 lines, 31 cross-refs
├── workspace/                    # PageIndex cached indexes
│   ├── _meta.json                # Document metadata
│   ├── *.json                    # Tree indexes (one per doc)
├── PageIndex/                    # Cloned VectifyAI/PageIndex repo
├── venv/                         # Python virtual environment
└── HOW_IT_WORKS.md               # This document
```

---

## Running It

```bash
# Prerequisites: Ollama running with a qwen2.5 model
ollama serve &
ollama pull qwen2.5:3b

# Run
cd /home/aman/pageindex-test
source venv/bin/activate
OPENAI_API_KEY=ollama python3 cross_doc_rag.py
```

The system will:
1. Index all 5 documents (or use cache)
2. Build the cross-reference map (106 links)
3. Run 3 demo queries showing multi-document retrieval
4. Drop into interactive mode for custom queries
