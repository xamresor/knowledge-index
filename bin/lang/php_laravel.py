"""PHP / Laravel knowledge: the patterns and vocabulary an AST parser does not carry.

`graphify` gives us classes, methods and calls. What it cannot know is that `$this->belongsTo(X::class)`
is a *relation between two entities*, that `protected $table` names a database table, that
`->constrained('guests')` is a foreign key, or that a class ending in `Controller` sits in a different
architectural layer than one ending in `Service`. That is framework knowledge, and it lives here.

This module is **patterns, not pipeline**: regexes plus pure functions over text. It has no `enrich()`
and never touches a graph — `enrich.py` and `link_data.py` do that, and they now read their PHP
knowledge from one place instead of each holding a private copy. Adding a framework (a Symfony dialect,
a Doctrine mapping) means a sibling module, not another regex buried in a pipeline script.

Deliberately *not* here:

* the domain/layer bucketing in `enrich.py` — that is a policy about how to group nodes, and it spans
  PHP and frontend paths alike;
* the route→controller matching in `link_http.py` — a Laravel route table meeting a JS call site is a
  join between two languages, so it belongs to neither plugin.
"""
from __future__ import annotations

import os
import re

from . import PHP

NAME = "php_laravel"
EXTENSIONS = PHP
COMMENT_PREFIXES = ("//", "#", "/*", "*")
CONTRIBUTES = {
    "nodes": ["db_table"],
    "relations": ["eloquent", "fk", "sql", "defines_table"],
    "patterns": ["class declaration", "$table", "Eloquent relations", "schema/FK", "query builder"],
}

#: Laravel's architectural suffixes. Stripping them yields the *entity* a class is about
#: (`BookingController` → `Booking`), which is what the domain clustering groups on.
LAYER_SUFFIX = re.compile(
    r"(Controller|Resource|Request|Service|Repository|Policy|Observer|Factory|Seeder|"
    r"Cast|Enum|Type|Job|Listener|Event|Command|Middleware|Exception|Provider|Mixin|"
    r"Collection|Trait|Interface|Test|Handler|Builder|Manager|Action|Rule|Scope)$"
)
BARE_CLASS = re.compile(r"^[A-Z][A-Za-z0-9]*$")

#: `class X`, tolerating the modifiers Laravel code actually uses.
CLASS_DECL = re.compile(r"^\s*(?:final\s+|abstract\s+|readonly\s+)*class\s+(\w+)", re.M)
#: The loose variant: matches `class X` anywhere, including inside a docblock line. The two callers
#: below genuinely used different patterns before this module existed, and unifying them would change
#: which class a file is credited to — a measurable change, so it stays a separate question.
CLASS_ANYWHERE = re.compile(r"class\s+(\w+)")
TABLE_PROP = re.compile(r"protected\s+\$table\s*=\s*'([^']+)'")

#: `$this->belongsTo(Guest::class)` and the rest of the Eloquent relation family.
ELOQUENT = re.compile(
    r"\$this->(belongsTo|hasMany|hasOne|belongsToMany|hasManyThrough|hasOneThrough|"
    r"morphTo|morphMany|morphOne|morphToMany)\(\s*([A-Za-z_][A-Za-z0-9_]*)::class")

#: Query-builder and schema-builder table references. These match *PHP source*, which is why they are
#: here and not in a would-be `sql.py`: nothing in this project parses `.sql` files, and inventing a
#: plugin for a language we never read would be a fake boundary.
SQL_REF = re.compile(r"(?:DB::table|->from|->join|->leftJoin|->rightJoin)\(\s*'([a-z0-9_]+)'")
SCHEMA_TABLE = re.compile(
    r"(?:Schema::(?:create|table)|->(?:create|table))\(\s*['\"]([a-z0-9_]+)['\"]")
FK_ON = re.compile(r"->on\(\s*['\"]([a-z0-9_]+)['\"]\s*\)")        # ->references(..)->on('table')
FK_CONSTRAINED = re.compile(r"->constrained\(\s*['\"]([a-z0-9_]+)['\"]\s*\)")
#: A migration file may touch several tables; each `Schema::create/table(` starts a new block.
SCHEMA_BLOCK = re.compile(r"(?=(?:Schema::|->)(?:create|table)\()")

MIGRATIONS_SUBDIR = ("database", "migrations")
MODEL_DIR_MARKERS = ("/Models", "/Model")
#: `$this->belongsTo(self::class)` is a self-relation, not an edge between two entities.
SELF_REFERENCES = ("self", "static")


# --- class-level facts ---------------------------------------------------------------------------
def class_type(label: str, source_file: str = "") -> str | None:
    """The architectural kind of a PHP class, or None if the label is not a bare class name."""
    if not BARE_CLASS.match(label):
        return None
    for suffix, kind in (("Controller", "controller"), ("Resource", "resource"),
                         ("Request", "request"), ("Service", "service"),
                         ("Repository", "repository"), ("Interface", "interface"),
                         ("Trait", "trait")):
        if label.endswith(suffix):
            return kind
    if label.endswith("Enum") or "/Enums/" in source_file or "/Enum/" in source_file:
        return "enum"
    if ("/Models/" in source_file or source_file.startswith("app/Models")
            or "\\Models\\" in source_file):
        return "model"
    return "class"


def entity(name: str) -> str:
    """`OrderController` → `Order`; `BookingResourceCollection` → `Booking`; bare names unchanged."""
    previous = None
    while previous != name:
        previous = name
        name = LAYER_SUFFIX.sub("", name)
    return name or previous


def class_name(text: str) -> str | None:
    match = CLASS_DECL.search(text)
    return match.group(1) if match else None


# --- data layer ----------------------------------------------------------------------------------
def eloquent_relations(text: str) -> list[tuple[str, str]]:
    """[(relation kind, target class), …], self-relations dropped."""
    return [(kind, target) for kind, target in ELOQUENT.findall(text)
            if target not in SELF_REFERENCES]


def table_refs(text: str) -> set[str]:
    """Tables named by a query-builder call."""
    return set(SQL_REF.findall(text))


def schema_tables(text: str) -> list[str]:
    """Tables a migration declares via `Schema::create/table`."""
    return SCHEMA_TABLE.findall(text)


def foreign_keys(text: str) -> set[str]:
    """Tables this text references as a foreign key target."""
    return set(FK_ON.findall(text)) | set(FK_CONSTRAINED.findall(text))


def schema_blocks(text: str) -> list[str]:
    """Split a multi-table migration so each foreign key is attributed to the right owner."""
    return SCHEMA_BLOCK.split(text)


def table_from_filename(filename: str) -> str | None:
    """Laravel migration name → the table it targets. Best effort, hence an AMBIGUOUS edge."""
    base = re.sub(r"^\d{4}_\d{2}_\d{2}_\d{6}_", "", filename).replace(".php", "")
    base = re.sub(r"^SM-?\d+_", "", base)
    for pattern in (r"create_(.+?)_table$", r"create_(.+)$", r"_to_(.+?)(?:_table)?$",
                    r"(?:alter|update|change|modify|add\w*|drop\w*)_(.+?)(?:_table)?$"):
        match = re.search(pattern, base)
        if match:
            return match.group(1)
    return None


# --- project layout ------------------------------------------------------------------------------
def migrations_dir(project_root: str) -> str:
    return os.path.join(project_root, *MIGRATIONS_SUBDIR)


def is_model_dir(dirpath: str) -> bool:
    return any(marker in dirpath for marker in MODEL_DIR_MARKERS)


def walk(root: str):
    """Every PHP file under `root`."""
    for dirpath, _, files in os.walk(root):
        for filename in files:
            if filename.endswith(EXTENSIONS):
                yield os.path.join(dirpath, filename)


def read(path: str) -> str | None:
    """File text, or None if it cannot be read — a bad file must not stop a build."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as handle:
            return handle.read()
    except OSError:
        return None


def declared_tables(repos_dir: str) -> dict[str, str]:
    """class name → table, scanned from staged model files (`protected $table = '…'`)."""
    tables: dict[str, str] = {}
    for dirpath, _, files in os.walk(repos_dir):
        if not is_model_dir(dirpath):
            continue
        for filename in files:
            if not filename.endswith(EXTENSIONS):
                continue
            text = read(os.path.join(dirpath, filename))
            if text is None:
                continue
            table, cls = TABLE_PROP.search(text), CLASS_ANYWHERE.search(text)
            if table and cls:
                tables[cls.group(1)] = table.group(1)
    return tables
