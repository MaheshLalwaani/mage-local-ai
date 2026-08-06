from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from xml.etree import ElementTree

PROJECT = Path(__file__).resolve().parents[2]
DATABASE = PROJECT / "data" / "agent.sqlite"
OLLAMA_CHAT = "http://127.0.0.1:11434/api/chat"
# Ollama silently truncates the prompt to num_ctx, so retrieval has to fit the
# window we ask for: CHUNK_LINES * the per-command limit, plus system prompt.
CHUNK_LINES = 40
MODEL_CONTEXT = 8192
MODEL_PREDICT = 512
# A complete PHPUnit class needs more output than an advisory answer. Keep the
# source cap below the context window so Ollama still has room to write it.
TEST_CODE_PREDICT = 2400
TEST_SOURCE_CHARS = 14_000
REVIEW_SOURCE_CHARS = 18_000
# Applies per socket read, so under streaming it means "no token for this long"
# rather than a budget for the whole answer.
MODEL_TIMEOUT = 300
ALLOWED_ROOTS = ("app/code", "app/design", "app/etc/config.php", "dev/tests", "composer.json")
DENIED_PARTS = {".git", ".env", "var", "pub", "vendor", "generated", "node_modules", "keys", "secrets", "backup", "backups", "dump", "dumps"}
DENIED_NAMES = {"env.php", "auth.json", "composer-auth.json", "id_rsa", "id_ed25519", ".my.cnf"}
SUFFIXES = {".php", ".phtml", ".xml", ".js", ".ts", ".json", ".less", ".css", ".md"}
# Only function words. bm25 already demotes terms that are common in the corpus;
# these are stripped because they add noise without ever being the evidence.
STOPWORDS = {"a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "during", "from", "at", "by", "as", "is", "are", "be", "it", "its", "that", "this", "these", "should", "can", "could", "would", "when", "where", "what", "which", "how", "do", "does", "i", "we", "my", "our", "you", "your"}
# XML that declares behaviour: the evidence an extend-or-build decision needs.
STRUCTURAL_XML = {"di.xml", "events.xml", "webapi.xml", "module.xml", "fieldset.xml", "extension_attributes.xml", "acl.xml", "crontab.xml", "routes.xml", "system.xml", "db_schema.xml", "indexer.xml", "mview.xml"}
# Core capability index. Only these declarative files are ever read from
# vendor/magento, and only their declarations are stored -- never a method body.
CORE_VENDOR = "vendor/magento"
CORE_XML = {"events.xml", "di.xml", "extension_attributes.xml", "webapi.xml", "db_schema.xml", "module.xml"}
CORE_MAX_BYTES = 4_000_000
CORE_DETAIL_CHARS = 300
# Running Magento from a container is the common local setup, so the allowlist
# has to reach it. The prefix is argv, never a shell string.
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")
DOCKER_WORKDIR = "/var/www/html"
INTERFACE_RE = re.compile(r"^\s*interface\s+(\w+)", re.M)
NAMESPACE_RE = re.compile(r"^\s*namespace\s+([\w\\]+)\s*;", re.M)
METHOD_RE = re.compile(r"^\s*(?:public\s+)?function\s+(\w+)\s*\(", re.M)


def require_repo(repo: Path) -> Path:
    repo = repo.resolve()
    if not (repo / "bin/magento").is_file():
        raise ValueError(f"{repo} is not a Magento repository: bin/magento is missing")
    return repo


def allowed(repo: Path, path: Path) -> bool:
    try:
        relative = path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return False
    if path.name in DENIED_NAMES or any(part in DENIED_PARTS for part in Path(relative).parts):
        return False
    if not any(relative == root or relative.startswith(root + "/") for root in ALLOWED_ROOTS):
        return False
    return path.is_file() and path.suffix in SUFFIXES and path.stat().st_size <= 1_000_000


def path_weight(path: str, profile: str = "balanced") -> float:
    """Multiplier applied to a chunk's bm25 score. bm25 is negative and lower
    sorts first, so a factor above 1 promotes a path and below 1 demotes it.

    `structural` suits extend-or-build decisions, where declarations in etc/ are
    the evidence. `balanced` suits open questions, where an implementation or a
    design note usually answers better than a wiring file."""
    structural = profile == "structural"
    name = path.rsplit("/", 1)[-1]
    if path.endswith((".css", ".less")):
        return 0.15
    if path.endswith((".js", ".ts")) or "/web/" in path:
        return 0.3
    if path.endswith(".phtml"):
        return 0.6 if structural else 0.9
    if "/etc/" in path:
        if name in STRUCTURAL_XML:
            return 3.0 if structural else 1.8
        return 2.0 if structural else 1.3
    if path.endswith(".md"):
        return 0.8 if structural else 1.4
    if path.endswith(".php"):
        return 1.2 if "/Test/" in path or path.startswith("dev/tests/") else 1.6
    return 1.0


def wordify(name: str) -> str:
    """Split an identifier into searchable words, so a plain-language query can
    reach `sales_order_place_after` or `CustomerRepositoryInterface` at all.
    This is what lets the lexical index answer questions phrased as prose."""
    words: list[str] = []
    for token in re.split(r"[^0-9A-Za-z]+", name):
        words.extend(re.findall(r"[A-Z]{2,}(?![a-z])|[A-Z][a-z]+|[a-z]+|[0-9]+", token) or [token])
    return " ".join(dict.fromkeys(word.lower() for word in words if word))


def fts_query(query_text: str) -> str:
    """Build an FTS5 OR query. The previous AND-of-phrases meant a single absent
    word returned nothing at all; OR plus bm25 lets the best evidence rank up."""
    terms, seen = [], set()
    for raw in re.split(r"[^0-9A-Za-z_]+", query_text):
        term = raw.lower()
        if not term or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    meaningful = [term for term in terms if term not in STOPWORDS]
    return " OR ".join(f'"{term}"' for term in (meaningful or terms))


def module_states(repo: Path) -> dict[str, str]:
    """Read module on/off state from app/etc/config.php. A capability from a
    module this install has disabled is not a capability it actually has."""
    config = repo / "app/etc/config.php"
    if not config.is_file():
        return {}
    try:
        text = config.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    start = text.find("'modules'")
    if start < 0:
        return {}
    section = text[start:]
    end = section.find("]")
    return dict(re.findall(r"'([A-Za-z0-9_]+)'\s*=>\s*([01])", section[:end] if end > 0 else section))


def core_allowed(repo: Path, path: Path) -> bool:
    """Gate for the core index. Independent of `allowed`, which still refuses
    vendor/ outright, so the repository index cannot reach core by accident."""
    try:
        relative = path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return False
    if not relative.startswith(CORE_VENDOR + "/") or path.is_symlink() or not path.is_file():
        return False
    if path.name in DENIED_NAMES or any(part in DENIED_PARTS - {"vendor"} for part in Path(relative).parts):
        return False
    parts = Path(relative).parts
    if path.suffix == ".xml":
        return path.name in CORE_XML and "etc" in parts and path.stat().st_size <= CORE_MAX_BYTES
    return path.suffix == ".php" and "Api" in parts and path.stat().st_size <= CORE_MAX_BYTES


def store() -> sqlite3.Connection:
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.create_function("path_weight", 2, path_weight, deterministic=True)
    # An FTS5 table cannot be altered, so an older core index is dropped and
    # rebuilt. index-core takes about a second, so this costs nothing.
    columns = {row[1] for row in db.execute("PRAGMA table_info(core)")}
    if columns and "enabled" not in columns:
        db.execute("DROP TABLE core")
    db.executescript("""
      PRAGMA journal_mode=WAL;
      CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(content, path UNINDEXED, repo UNINDEXED, line UNINDEXED);
      CREATE VIRTUAL TABLE IF NOT EXISTS core USING fts5(content, kind UNINDEXED, name UNINDEXED, detail UNINDEXED, module UNINDEXED, enabled UNINDEXED, path UNINDEXED, repo UNINDEXED);
      CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, event TEXT NOT NULL, details TEXT NOT NULL);
    """)
    return db


def audit(db: sqlite3.Connection, event: str, details: str) -> None:
    db.execute("INSERT INTO audit(event, details) VALUES (?, ?)", (event, details))
    db.commit()


def index(repo: Path, db: sqlite3.Connection) -> tuple[int, int]:
    files = chunks = 0
    # Walk only allowlisted roots. Scanning the whole Magento root first would
    # needlessly traverse vendor/, var/, media and generated output.
    candidates = [
        *(path for root in (repo / "app/code", repo / "app/design", repo / "dev/tests") if root.is_dir() for path in root.rglob("*")),
        repo / "app/etc/config.php", repo / "composer.json",
    ]
    for path in candidates:
        if not allowed(repo, path):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(repo).as_posix()
        db.execute("DELETE FROM chunks WHERE repo = ? AND path = ?", (str(repo), rel))
        for start in range(0, len(lines), CHUNK_LINES):
            content = "\n".join(lines[start:start + CHUNK_LINES])
            if content.strip():
                db.execute("INSERT INTO chunks(content, path, repo, line) VALUES (?, ?, ?, ?)", (content, rel, str(repo), start + 1))
                chunks += 1
        files += 1
        if files % 100 == 0:
            db.commit()
    db.commit()
    audit(db, "index", f"repo={repo}; files={files}; chunks={chunks}")
    return files, chunks


def xml_capabilities(filename: str, root: ElementTree.Element) -> list[tuple[str, str, str]]:
    """Declarations only: what core exposes, never how core implements it."""
    found: list[tuple[str, str, str]] = []
    if filename == "events.xml":
        for event in root.iter("event"):
            observers = ", ".join(o.get("instance", "") for o in event.iter("observer") if o.get("instance"))
            found.append(("event", event.get("name", ""), f"observed by: {observers}" if observers else "no core observer"))
    elif filename == "di.xml":
        for preference in root.iter("preference"):
            found.append(("preference", preference.get("for", ""), f"default implementation: {preference.get('type', '')}"))
        for target in root.iter("type"):
            for plugin in target.iter("plugin"):
                found.append(("plugin", target.get("name", ""), f"core plugin {plugin.get('name', '')} -> {plugin.get('type', '')}"))
    elif filename == "extension_attributes.xml":
        for group in root.iter("extension_attributes"):
            codes = ", ".join(a.get("code", "") for a in group.iter("attribute") if a.get("code"))
            found.append(("extension_point", group.get("for", ""), f"extension attributes: {codes}" if codes else "extendable interface"))
    elif filename == "webapi.xml":
        for route in root.iter("route"):
            service = next((s for s in route.iter("service")), None)
            detail = f"{service.get('class', '')}::{service.get('method', '')}" if service is not None else ""
            found.append(("webapi", f"{route.get('method', '')} {route.get('url', '')}", detail))
    elif filename == "db_schema.xml":
        for table in root.iter("table"):
            found.append(("table", table.get("name", ""), table.get("comment", "") or "core table"))
    return [(kind, name, detail) for kind, name, detail in found if name]


def api_capabilities(text: str) -> list[tuple[str, str, str]]:
    interface = INTERFACE_RE.search(text)
    if not interface:
        return []
    namespace_match = NAMESPACE_RE.search(text)
    namespace = namespace_match.group(1) if namespace_match else ""
    methods = ", ".join(dict.fromkeys(METHOD_RE.findall(text)))
    fqn = f"{namespace}\\{interface.group(1)}" if namespace else interface.group(1)
    return [("service", fqn, f"methods: {methods}" if methods else "service contract")]


def index_core(repo: Path, db: sqlite3.Connection) -> tuple[int, int]:
    """Index Magento core's declarative surface so USE_CORE can cite evidence.
    Opt-in and separate from `index`: it is the only path that reads vendor/."""
    vendor = repo / CORE_VENDOR
    if not vendor.is_dir():
        raise ValueError(f"{vendor} not found. Run composer install, or index a checkout that vendors Magento.")
    db.execute("DELETE FROM core WHERE repo = ?", (str(repo),))
    states = module_states(repo)
    modules = records = disabled = 0
    for module_dir in sorted(p for p in vendor.iterdir() if p.is_dir() and not p.is_symlink()):
        manifest = module_dir / "etc/module.xml"
        module = module_dir.name
        if manifest.is_file():
            try:
                node = next((m for m in ElementTree.parse(manifest).getroot().iter("module")), None)
                module = (node.get("name") if node is not None else None) or module
            except ElementTree.ParseError:
                pass
        found: list[tuple[str, str, str, str]] = []
        for path in (*(module_dir / "etc").rglob("*.xml"), *(module_dir / "Api").rglob("*.php")):
            if not core_allowed(repo, path):
                continue
            rel = path.relative_to(repo).as_posix()
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            try:
                items = api_capabilities(text) if path.suffix == ".php" else xml_capabilities(path.name, ElementTree.fromstring(text))
            except ElementTree.ParseError:
                continue
            found.extend((kind, name, detail, rel) for kind, name, detail in items)
        enabled = states.get(module, "?")
        for kind, name, detail, rel in found:
            content = f"{kind} {name} {wordify(name)} {wordify(module)} {wordify(detail)}"
            db.execute("INSERT INTO core(content, kind, name, detail, module, enabled, path, repo) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (content, kind, name, detail, module, enabled, rel, str(repo)))
        records += len(found)
        if found:
            modules += 1
            disabled += enabled == "0"
            db.commit()
    db.commit()
    audit(db, "index_core", f"repo={repo}; modules={modules}; disabled={disabled}; capabilities={records}")
    return modules, records, disabled


def core_retrieve(repo: Path, query_text: str, db: sqlite3.Connection, limit: int = 8) -> list[sqlite3.Row]:
    query = fts_query(query_text)
    if not query:
        return []
    return db.execute("SELECT kind, name, detail, module, path FROM core WHERE repo = ? AND core MATCH ? ORDER BY bm25(core) LIMIT ?", (str(repo), query, limit)).fetchall()


def core_context(rows: list[sqlite3.Row], indexed: bool) -> str:
    if not indexed:
        return "Core capability index not built (run `magento-agent index-core`). You therefore have NO evidence about Magento core and must not answer USE_CORE."
    if not rows:
        return "No matching Magento core capability was found for this requirement."
    # A wide interface such as OrderInterface lists hundreds of methods; left
    # whole, a handful of rows would overrun num_ctx and be silently truncated.
    lines = []
    for row in rows:
        detail = row["detail"] if len(row["detail"]) <= CORE_DETAIL_CHARS else row["detail"][:CORE_DETAIL_CHARS].rsplit(", ", 1)[0] + ", ..."
        lines.append(f"- [{row['kind']}] {row['name']} ({row['module']}) -- {detail} [{row['path']}]")
    return "\n".join(lines)


def core_indexed(repo: Path, db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM core WHERE repo = ? LIMIT 1", (str(repo),)).fetchone() is not None


def retrieve(repo: Path, query_text: str, db: sqlite3.Connection, limit: int = 5, per_file: int = 2, profile: str = "balanced") -> list[sqlite3.Row]:
    query = fts_query(query_text)
    if not query:
        return []
    # Over-fetch, then cap per file so one large match cannot take every slot.
    rows = db.execute(
        "SELECT path, line, content, bm25(chunks) * path_weight(path, ?) AS score "
        "FROM chunks WHERE repo = ? AND chunks MATCH ? ORDER BY score LIMIT ?",
        (profile, str(repo), query, limit * 8),
    ).fetchall()
    picked: list[sqlite3.Row] = []
    counts: dict[str, int] = {}
    for row in rows:
        if counts.get(row["path"], 0) >= per_file:
            continue
        counts[row["path"]] = counts.get(row["path"], 0) + 1
        picked.append(row)
        if len(picked) == limit:
            break
    return picked


def model_reply(model: str, system: str, prompt: str, stream: bool = True, predict: int = MODEL_PREDICT, show_stream: bool = True) -> str:
    """Call Ollama. When streaming, tokens are printed as they arrive and also
    returned, so a slow local model looks slow instead of looking hung."""
    payload = json.dumps({"model": model, "stream": stream, "messages": [
        {"role": "system", "content": system}, {"role": "user", "content": prompt},
    ], "options": {"num_ctx": MODEL_CONTEXT, "num_predict": predict}}).encode()
    request = urllib.request.Request(OLLAMA_CHAT, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=MODEL_TIMEOUT) as response:
            if not stream:
                return json.loads(response.read())["message"]["content"]
            parts: list[str] = []
            hidden_pieces = 0
            for line in response:
                if not line.strip():
                    continue
                message = json.loads(line)
                if message.get("error"):
                    raise RuntimeError(f"Ollama reported an error: {message['error']}")
                piece = message.get("message", {}).get("content", "")
                if piece:
                    if show_stream:
                        print(piece, end="", flush=True)
                    else:
                        hidden_pieces += 1
                        if hidden_pieces % 25 == 0:
                            print(".", end="", flush=True)
                    parts.append(piece)
                if message.get("done"):
                    break
            if show_stream or hidden_pieces:
                print()
            return "".join(parts)
    except TimeoutError:
        raise RuntimeError(f"No response from Ollama within {MODEL_TIMEOUT}s. The model may be loading, or {model} may be too large for this machine; try a smaller model with --model.") from None
    except urllib.error.HTTPError as error:
        # Ollama answers a missing model with 404 and a JSON body worth showing.
        detail = json.loads(error.read() or b"{}").get("error", error.reason)
        raise RuntimeError(f"Ollama rejected the request ({error.code}): {detail}. Check `ollama list`.") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"Cannot reach Ollama at {OLLAMA_CHAT} ({error.reason}). Is `ollama serve` running?") from None


def context(rows: list[sqlite3.Row]) -> str:
    # Defensive truncation: an index built by an older chunk size would
    # otherwise overflow num_ctx and be silently cut by Ollama instead.
    blocks = [f"--- {r['path']}:{r['line']} ---\n" + "\n".join(r["content"].splitlines()[:CHUNK_LINES]) for r in rows]
    return "\n\n".join(blocks) or "No matching indexed code found."


def ask(repo: Path, question: str, model: str, db: sqlite3.Connection, audit_prompts: bool, stream: bool = True) -> str:
    rows = retrieve(repo, question, db, 4)
    audit(db, "ask", json.dumps({"repo": str(repo), "prompt": question if audit_prompts else "[not stored]", "paths": [r["path"] for r in rows]}))
    return model_reply(model, "You are a local Magento assistant. Use only retrieved code. Never invent files, secrets, or command results.", f"Question: {question}\n\nRetrieved code:\n{context(rows)}", stream)


def assess(repo: Path, requirement: str, model: str, db: sqlite3.Connection, audit_prompts: bool, stream: bool = True, include_code: bool = False) -> str:
    rows = retrieve(repo, requirement, db, profile="structural")
    core_rows = core_retrieve(repo, requirement, db)
    audit(db, "assess", json.dumps({"repo": str(repo), "requirement": requirement if audit_prompts else "[not stored]", "paths": [r["path"] for r in rows], "core": [r["name"] for r in core_rows]}))
    code_instruction = """The user explicitly requested implementation guidance.
After the assessment, provide a PROPOSED PATCH as a unified diff. Only modify files
supported by the supplied evidence. Never claim the patch was applied; do not execute
code or commands; do not include secrets, environment configuration, or unrelated files.""" if include_code else "Do not write or execute code."
    system = f"""You are a Magento 2 customization advisor. Use only supplied evidence.
Choose exactly one: USE_CORE, EXTEND_EXISTING, NEW_CUSTOMIZATION, INSUFFICIENT_EVIDENCE.
USE_CORE requires citing a specific capability from the Magento core section.
EXTEND_EXISTING requires citing a file from the local project section.
If neither section supports the requirement, answer INSUFFICIENT_EVIDENCE.
Return concise sections: Decision, Evidence (file paths), Recommendation, Risks, Tests.
Never claim a feature exists without an evidence path. {code_instruction}"""
    prompt = f"Requirement: {requirement}\n\nMagento core capabilities:\n{core_context(core_rows, core_indexed(repo, db))}\n\nLocal project evidence:\n{context(rows)}"
    return model_reply(model, system, prompt, stream)


def review(repo: Path, target: str, model: str, db: sqlite3.Connection, audit_prompts: bool, stream: bool = True, include_code: bool = False) -> str:
    """Review one allowlisted PHP source file without writing to the checkout."""
    relative = Path(target)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".php":
        raise ValueError("review needs a safe relative .php path")
    path = repo / relative
    if not allowed(repo, path):
        raise ValueError("review target must be an allowlisted PHP source file")
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise ValueError(f"Cannot read review target: {target}") from None
    if len(source) > REVIEW_SOURCE_CHARS:
        raise ValueError(f"Review target exceeds {REVIEW_SOURCE_CHARS} characters; review a smaller class or split it first")
    numbered = "\n".join(f"{number:4}: {line}" for number, line in enumerate(source.splitlines(), 1))
    audit(db, "review", json.dumps({"repo": str(repo), "target": target, "prompt": target if audit_prompts else "[not stored]"}))
    code_instruction = """The user requested a fix proposal. End with PROPOSED PATCH as a unified diff for this file only. Do not claim it was applied.""" if include_code else "Do not write a patch or execute code."
    system = f"""You are a conservative Magento 2 PHP code reviewer. Use only the supplied file.
Review correctness, Magento conventions, dependency injection, error handling, performance,
security, and testability. Do not invent surrounding code, configuration, or runtime results.
Return concise sections: Summary, Findings, Test gaps, Recommendation. Each finding must
include severity (critical/high/medium/low), exact line number(s), evidence, risk, and a
specific recommendation. If there are no supported findings, say so. {code_instruction}"""
    return model_reply(model, system, f"File: {relative.as_posix()}\n\nPHP source with line numbers:\n```php\n{numbered}\n```", stream, TEST_CODE_PREDICT if include_code else MODEL_PREDICT)


def module_overview(repo: Path, module: str, db: sqlite3.Connection) -> str:
    vendor_module = module.split("_")
    if len(vendor_module) != 2 or not all(vendor_module):
        raise ValueError("Module name must use Vendor_Module format")
    directory = repo / "app/code" / vendor_module[0] / vendor_module[1]
    if not directory.is_dir():
        raise ValueError(f"Custom module not found: {module}")
    files = [path.relative_to(repo).as_posix() for path in directory.rglob("*") if path.is_file()]
    tests = [path for path in files if "/Test/" in path]
    configs = [path for path in files if "/etc/" in path]
    audit(db, "explain", f"repo={repo}; module={module}")
    return "\n".join([f"Module: {module}", f"Files: {len(files)}", f"Configuration files: {len(configs)}", f"Existing tests: {len(tests)}", "", "Configuration:", *(configs[:30] or ["(none found)"]), "", "Tests:", *(tests[:30] or ["(none found)"])])


def test_target(repo: Path, module_directory: Path, target: str) -> tuple[Path, str]:
    """Resolve one safe, custom-module PHP class for test generation."""
    relative = Path(target)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".php":
        raise ValueError("--target must be a safe .php path relative to the custom module")
    path = module_directory / relative
    try:
        path.resolve().relative_to(module_directory.resolve())
    except ValueError:
        raise ValueError("--target must stay inside the custom module") from None
    if "/Test/" in path.as_posix() or not allowed(repo, path):
        raise ValueError("--target must be an allowlisted, non-test PHP source file")
    return path, path.relative_to(repo).as_posix()


def proposed_test_path(module_directory: Path, target_path: Path) -> Path:
    """Map Model/Foo.php to Test/Unit/Model/FooTest.php inside one module."""
    module_relative = target_path.relative_to(module_directory)
    return (module_directory / "Test/Unit" / module_relative).with_name(module_relative.stem + "Test.php")


def extract_test_file(answer: str) -> str:
    """Accept one complete fenced PHP file and reject ambiguous model output."""
    blocks = re.findall(r"```(?:php)?\s*\n(.*?)```", answer, flags=re.IGNORECASE | re.DOTALL)
    if len(blocks) != 1:
        raise ValueError("Generated response must contain exactly one fenced PHP block; no test file was written")
    code = blocks[0].strip() + "\n"
    if not code.startswith("<?php"):
        raise ValueError("Generated PHP block is missing the <?php opening tag; no test file was written")
    if not re.search(r"\bclass\s+\w+Test\b", code) or not re.search(r"\bextends\s+(?:\\?PHPUnit\\Framework\\)?TestCase\b", code):
        raise ValueError("Generated PHP block does not look like a complete PHPUnit test class; no test file was written")
    return code


def write_test_file(repo: Path, module_directory: Path, target: str, answer: str, db: sqlite3.Connection) -> Path:
    """Write only the calculated Unit-test path after the caller's explicit approval."""
    target_path, _ = test_target(repo, module_directory, target)
    destination = proposed_test_path(module_directory, target_path)
    code = extract_test_file(answer)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(code, encoding="utf-8")
    except OSError as error:
        raise ValueError(f"Could not write generated test file: {error}") from None
    audit(db, "test_written", f"repo={repo}; target={target_path.relative_to(repo)}; test={destination.relative_to(repo)}")
    return destination


def test_plan(repo: Path, module: str, model: str, db: sqlite3.Connection, stream: bool = True, target: str | None = None, include_code: bool = False, show_stream: bool = True) -> str:
    overview = module_overview(repo, module, db)
    vendor_module = module.split("_")
    directory = repo / "app/code" / vendor_module[0] / vendor_module[1]
    candidates = [path.relative_to(repo).as_posix() for path in directory.rglob("*.php") if "/Test/" not in path.as_posix()][:20]
    audit(db, "tests", f"repo={repo}; module={module}")
    if include_code:
        if not target:
            raise ValueError("--include-code needs --target, for example --target Model/Foo.php")
        target_path, target_rel = test_target(repo, directory, target)
        try:
            source = target_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise ValueError(f"Cannot read target source file: {target}") from None
        if len(source) > TEST_SOURCE_CHARS:
            raise ValueError(f"Target source exceeds {TEST_SOURCE_CHARS} characters; select a smaller class or split it before generating a focused unit test")
        test_rel = proposed_test_path(directory, target_path).relative_to(repo).as_posix()
        system = """You are a Magento 2 PHPUnit 9.5 test author. Write one complete,
isolated unit-test class for the supplied PHP class. Use only class names, constructor
dependencies, methods, and behaviour visible in the supplied source. Mock constructor
dependencies; do not use ObjectManager, a database, Magento bootstrap, or integration
fixtures. Do not invent production classes, methods, or configuration.
Name every test method with a `test...` prefix. Add an accurate PHPDoc block to the
test class and every test method. Include an `@covers` tag for the supplied target
class and concise `@return void` tags on test methods; describe the behaviour under
test rather than repeating the method name.
Return exactly: Target class, Test file path, Assumptions, then one fenced `php` block
containing the complete proposed test file. This is a proposal only: never claim it was
written, executed, passing, or sufficient for a coverage percentage."""
        prompt = f"Module overview:\n{overview}\n\nTarget source path: {target_rel}\nProposed test path: {test_rel}\n\nTarget PHP source:\n```php\n{source}\n```"
        return model_reply(model, system, prompt, stream, TEST_CODE_PREDICT, show_stream)
    system = """You are a Magento 2 PHPUnit test planner. Do not write files. Create a PHPUnit 9.5 unit-test plan.
Prefer isolated tests with mocks for constructor dependencies; identify integration-only behavior separately.
Return: Target class, Test cases, Mocks, Test file path, Why. Do not invent classes outside the candidate list."""
    return model_reply(model, system, f"{overview}\n\nCandidate PHP classes:\n" + "\n".join(f"- {path}" for path in candidates), stream)


def command(repo: Path, name: str, argument: str | None) -> tuple[str, ...]:
    """Return the command as an argv array, relative to the Magento root so the
    same tuple works on the host or inside a container."""
    choices = {"module-status": ("bin/magento", "module:status"), "cache-status": ("bin/magento", "cache:status")}
    if name in choices:
        return choices[name]
    if not argument or argument.startswith("/") or ".." in Path(argument).parts or not argument.endswith(".php"):
        raise ValueError("php-lint needs an existing, safe relative .php path")
    target = Path(argument)
    # The indexer's deny lists apply here too: without this, php-lint accepts
    # app/etc/env.php, the one file holding database credentials and crypt keys.
    if target.name in DENIED_NAMES or any(part in DENIED_PARTS for part in target.parts):
        raise ValueError(f"php-lint refuses sensitive or non-source path: {argument}")
    if not (repo / argument).is_file():
        raise ValueError("php-lint target does not exist")
    return ("php", "-l", argument)


def compose_root(repo: Path) -> Path:
    for candidate in (repo, *repo.parents):
        if any((candidate / name).is_file() for name in COMPOSE_FILES):
            return candidate
    raise ValueError(f"No compose file found at or above {repo}; use --runner direct")


def wrap_command(proposed: tuple[str, ...], repo: Path, runner: str, service: str) -> tuple[tuple[str, ...], Path]:
    """Return the argv actually executed plus its working directory. Still an
    argv array with shell=False, so routing through Docker adds no injection."""
    if runner == "direct":
        return proposed, repo
    root = compose_root(repo)
    return ("docker", "compose", "exec", "-T", "-w", DOCKER_WORKDIR, service, *proposed), root


def run() -> None:
    parser = argparse.ArgumentParser(description="Offline-first Magento customization advisor")
    parser.add_argument("--model", default="qwen2.5-coder:7b")
    parser.add_argument("--audit-prompts", action="store_true")
    parser.add_argument("--no-stream", action="store_true", help="Buffer the whole answer instead of printing tokens as they arrive")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("index", "index-core", "ask", "assess", "review", "explain", "tests", "command"):
        p = sub.add_parser(action); p.add_argument("repo", type=Path)
    sub.choices["ask"].add_argument("question")
    sub.choices["assess"].add_argument("requirement")
    sub.choices["assess"].add_argument("--include-code", action="store_true", help="Include an evidence-based proposed patch; never applies changes")
    sub.choices["review"].add_argument("target", help="Allowlisted PHP path relative to the Magento checkout")
    sub.choices["review"].add_argument("--include-code", action="store_true", help="Include a proposed unified diff for the reviewed file; never applies changes")
    sub.choices["explain"].add_argument("module")
    sub.choices["tests"].add_argument("module")
    sub.choices["tests"].add_argument("--target", help="PHP path relative to the custom module, required with --include-code")
    sub.choices["tests"].add_argument("--include-code", action="store_true", help="Generate a proposed PHPUnit test file for --target; never writes it")
    sub.choices["tests"].add_argument("--write", action="store_true", help="Write the generated test to its calculated Test/Unit path; requires --approve RUN")
    sub.choices["tests"].add_argument("--approve", metavar="TOKEN", help="Required token for --write; use RUN after reviewing the target")
    p = sub.choices["command"]
    p.add_argument("name", choices=["module-status", "cache-status", "php-lint"])
    p.add_argument("argument", nargs="?")
    p.add_argument("--approve", metavar="TOKEN")
    p.add_argument("--runner", choices=["direct", "docker"], default="direct", help="Run on the host, or inside the compose service holding Magento")
    p.add_argument("--service", default="phpfpm", help="Compose service to exec into when --runner docker")
    args = parser.parse_args(); repo = require_repo(args.repo); db = store(); stream = not args.no_stream
    if args.action == "index":
        files, chunks = index(repo, db); print(f"Indexed {files} files into {chunks} chunks; sensitive/non-allowlisted paths were skipped."); return
    if args.action == "index-core":
        modules, records, disabled = index_core(repo, db); print(f"Indexed {records} core capabilities from {modules} modules under {CORE_VENDOR}/ (declarations only); {disabled} of those modules are disabled in this install."); return
    if args.action in ("ask", "assess", "review", "tests"):
        # model_reply already printed the answer when streaming.
        if args.action == "ask":
            answer = ask(repo, args.question, args.model, db, args.audit_prompts, stream)
        elif args.action == "assess":
            answer = assess(repo, args.requirement, args.model, db, args.audit_prompts, stream, args.include_code)
        elif args.action == "review":
            answer = review(repo, args.target, args.model, db, args.audit_prompts, stream, args.include_code)
        else:
            if args.write and not args.include_code:
                raise ValueError("--write requires --include-code")
            if args.write and args.approve != "RUN":
                raise ValueError("--write requires --approve RUN")
            if args.write and not stream:
                raise ValueError("--write uses streaming to avoid timeouts; remove --no-stream")
            if args.write:
                print(f"Generating a PHPUnit test for {args.target}; waiting for the local model", end="", flush=True)
            answer = test_plan(repo, args.module, args.model, db, stream, args.target, args.include_code, not args.write)
            if args.write:
                print("Validating generated PHP and writing the test file...")
                directory = repo / "app/code" / args.module.split("_")[0] / args.module.split("_")[1]
                destination = write_test_file(repo, directory, args.target, answer, db)
                print(f"Wrote generated test file: {destination}")
        if not stream:
            print(answer)
        return
    if args.action == "explain":
        print(module_overview(repo, args.module, db)); return
    proposed, workdir = wrap_command(command(repo, args.name, args.argument), repo, args.runner, args.service)
    shown = " ".join(repr(x) if " " in x else x for x in proposed)
    print(f"Proposed read-only command ({args.runner}, cwd {workdir}):\n  {shown}")
    if args.approve != "RUN":
        audit(db, "command_proposed", f"repo={repo}; command={shown}"); print("Not executed. Rerun with --approve RUN after review."); return
    result = subprocess.run(proposed, cwd=workdir, shell=False, capture_output=True, text=True, timeout=120, check=False)
    output = re.sub(r"(?i)(password|secret|token|api[_-]?key)\s*[:=]\s*\S+", r"\1=[REDACTED]", (result.stdout + result.stderr).strip())[:12000]
    audit(db, "command_executed", f"repo={repo}; command={shown}; exit={result.returncode}")
    print(f"exit={result.returncode}\n{output}")


def main() -> None:
    """Console-script entry point: turn expected failures into clean messages."""
    try:
        run()
    except (RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr); raise SystemExit(1) from None
    except KeyboardInterrupt:
        print(file=sys.stderr); raise SystemExit(130) from None


if __name__ == "__main__":
    main()
