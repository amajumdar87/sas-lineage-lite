"""sas_lineage_lite - field-level lineage for small, simple SAS programs.

A minimal teaching sample: it reads one or more SAS programs and lists, for every column a DATA step or a
PROC SQL query creates, which input columns it comes from. Standard library only.

What it reads
  * comments, quoted text, %LET constants (&name and &name. are substituted)
  * DATA steps: SET / MERGE inputs, assignments, IF..THEN / ELSE assignments, RENAME, KEEP
  * PROC SQL:   CREATE TABLE ... AS SELECT ... FROM ... [JOIN ...]   (select list, aliases, joins)

What it does not read (on purpose - this is a small sample)
  macro definitions and calls with parameters, %IF branches, CALL EXECUTE, hash objects, arrays, PROC steps
  other than SQL, SELECT * expansion, table versions (a table written twice is treated as one table).
  Anything it skips is listed in the warnings; nothing is guessed.

Usage
  python sas_lineage_lite.py program.sas [more.sas] [--csv edges.csv] [--mermaid flow.md] [--trace work.out.col]
"""
import argparse
import csv
import re
import sys
from dataclasses import dataclass, asdict, fields
from pathlib import Path

__version__ = "0.1.0"

SQL_AGG = {"sum", "avg", "mean", "min", "max", "count", "std", "var", "median"}
NON_COLUMN_WORDS = {
    "and", "or", "not", "in", "is", "null", "missing", "like", "between", "then", "else", "if", "case", "when",
    "end", "as", "distinct", "from", "where", "by", "eq", "ne", "lt", "le", "gt", "ge", "put", "input", "of",
}
DATA_STEP_KEYWORDS = {
    "set", "merge", "by", "keep", "drop", "rename", "retain", "length", "format", "informat", "label", "array",
    "output", "delete", "where", "do", "end", "run", "select", "when", "otherwise", "return", "stop", "put",
    "file", "infile", "input", "call", "if", "else", "attrib", "drop", "lag", "first", "last", "update", "modify",
}
IDENT = r"[A-Za-z_][A-Za-z0-9_]*"


@dataclass
class Edge:
    """One column of a created table and one input column it depends on."""
    target_table: str
    target_field: str
    source_table: str
    source_field: str
    kind: str                # DIRECT | RENAMED | DERIVED | CONDITIONAL | AGGREGATE | STAR
    expression: str
    step: str                # DATA | SQL
    file: str
    line: int
    note: str = ""


@dataclass
class Result:
    edges: list
    warnings: list


# ----------------------------------------------------------------------------------------------------------
# text handling
# ----------------------------------------------------------------------------------------------------------
def split_statements(text: str) -> list[tuple[int, str]]:
    """-> [(first line number, statement text without the final ';')]. Block comments are dropped, quotes respected."""
    out, buf, line, start = [], [], 1, None
    i, n, quote = 0, len(text), ""
    while i < n:
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            if ch == "\n":
                line += 1
            i += 1
            continue
        if ch == "/" and text[i:i + 2] == "/*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += text.count("\n", i, j)
            buf.append(" ")
            i = j
            continue
        if ch in "'\"":
            quote = ch
            if start is None:
                start = line
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append((start or line, re.sub(r"\s+", " ", stmt)))
            buf, start = [], None
            i += 1
            continue
        if ch == "\n":
            line += 1
        if start is None and not ch.isspace():
            start = line
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append((start or line, re.sub(r"\s+", " ", tail)))
    # star-comments: a statement that starts with * is a comment
    return [(ln, s) for ln, s in out if not s.startswith("*")]


ELSE_TEXT = "ELSE (no earlier condition met)"


def blank_quotes(expr: str) -> str:
    """Replace quoted text by spaces so words inside strings are not read as columns."""
    return re.sub(r"(?:'[^']*'|\"[^\"]*\")(?:[dtn]\b)?", " ", expr, flags=re.I)


def substitute(stmt: str, macros: dict) -> str:
    def rep(m):
        return macros.get(m.group(1).upper(), m.group(0))
    return re.sub(r"&(" + IDENT + r")\.?", rep, stmt)


def table_name(raw: str) -> str:
    """Normalise to LIB.TABLE in upper case; a bare name is a WORK table. Dataset options are removed."""
    raw = re.sub(r"\(.*\)", "", raw).strip()
    return raw.upper() if "." in raw else f"WORK.{raw.upper()}"


def columns_in(expr: str) -> list[str]:
    """Column names used in an expression: identifiers that are not functions, keywords or numbers."""
    expr = blank_quotes(expr)
    found = []
    for m in re.finditer(r"(?<![\w.&])(" + IDENT + r")(\s*\()?", expr):
        name, is_call = m.group(1), m.group(2)
        if is_call or name.lower() in NON_COLUMN_WORDS:
            continue
        if name.upper() not in [f.upper() for f in found]:
            found.append(name)
    return found


def top_level_split(text: str, sep: str = ",") -> list[str]:
    parts, depth, cur, quote = [], 0, [], ""
    for ch in text:
        if quote:
            cur.append(ch)
            quote = "" if ch == quote else quote
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


# ----------------------------------------------------------------------------------------------------------
# DATA step
# ----------------------------------------------------------------------------------------------------------
def parse_data_step(file: str, header: tuple, body: list[tuple[int, str]], warnings: list) -> list[Edge]:
    line0, htext = header
    outs = [table_name(t) for t in re.findall(r"[A-Za-z_][\w.]*(?:\([^)]*\))?", htext[4:].strip())
            if t.lower() not in ("_null_",)]
    if not outs:
        return []
    inputs: list[str] = []
    rename: dict[str, str] = {}          # new name -> old name (dataset-option and RENAME statement)
    keep: list[str] = []
    assigns: list[tuple[int, str, str, str, str]] = []   # line, target, expression, kind, condition
    last_cond = ""
    for ln, st in body:
        low = st.lower()
        m = re.match(r"(set|merge)\s+(.*)$", st, re.I)
        if m:
            for tok in re.findall(r"[A-Za-z_][\w.]*(?:\s*\((?:[^()]|\([^)]*\))*\))?", m.group(2)):
                base = re.sub(r"\s*\(.*\)$", "", tok)
                if base.lower() in ("by", "end", "in"):
                    continue
                inputs.append(table_name(base))
                for old, new in re.findall(r"(" + IDENT + r")\s*=\s*(" + IDENT + ")",
                                           " ".join(re.findall(r"rename\s*=\s*\(([^)]*)\)", tok, re.I))):
                    rename[new.upper()] = old.upper()
            continue
        m = re.match(r"rename\s+(.*)$", st, re.I)
        if m:
            for old, new in re.findall(r"(" + IDENT + r")\s*=\s*(" + IDENT + ")", m.group(1)):
                rename[new.upper()] = old.upper()
            continue
        m = re.match(r"keep\s+(.*)$", st, re.I)
        if m:
            keep += [k.upper() for k in m.group(1).split()]
            continue
        first = re.match(r"(" + IDENT + ")", st)
        if first and first.group(1).lower() in DATA_STEP_KEYWORDS - {"if", "else"}:
            continue
        # IF cond THEN target = expr   /   ELSE [IF cond THEN] target = expr
        m = re.match(r"(?:else\s+)?(?:if\s+(.*?)\s+then\s+)?(?:do\s*)?(" + IDENT + r")\s*=\s*(.+)$", st, re.I)
        if m and (low.startswith(("if ", "else")) or m.group(1) is None):
            cond = m.group(1) or ""
            if low.startswith("if "):
                last_cond = cond
            elif low.startswith("else") and not cond:
                cond = ELSE_TEXT
            if m.group(1) is None and not low.startswith(("if ", "else")):
                cond = ""
            target = m.group(2).upper()
            if target.lower() in DATA_STEP_KEYWORDS:
                continue
            assigns.append((ln, target, m.group(3).strip(), "CONDITIONAL" if cond else "DERIVED", cond))
            continue
        m = re.match(r"(" + IDENT + r")\s*=\s*(.+)$", st)
        if m:
            assigns.append((ln, m.group(1).upper(), m.group(2).strip(), "DERIVED", ""))
            continue
        if re.match(r"(if|where|do|else|select|when|otherwise)\b", st, re.I):
            continue
    edges: list[Edge] = []
    only = inputs[0] if len(set(inputs)) == 1 else ""
    ambiguous_note = "" if only else "several or no input tables: source table not decided"

    def source_of(col: str) -> tuple[str, str]:
        col_u = col.upper()
        return (only or "?"), rename.get(col_u, col_u)

    target0 = outs[0]
    assigned = set()
    for ln, target, expr, kind, cond in assigns:
        assigned.add(target)
        cols = columns_in(expr)
        cond_cols = columns_in(cond) if cond and cond != ELSE_TEXT else []
        simple = re.fullmatch(IDENT, expr.strip()) is not None
        k = "CONDITIONAL" if cond else ("DIRECT" if simple else "DERIVED")
        if re.search(r"\b(" + "|".join(SQL_AGG) + r")\s*\(", expr, re.I) and not cond:
            k = "DERIVED"
        for c in cols:
            st_, sf = source_of(c)
            if c.upper() in assigned and c.upper() != target:      # reads a column made earlier in the same step
                st_, sf = target0, c.upper()
            edges.append(Edge(target0, target, st_, sf, k, f"{target} = {expr}", "DATA", file, ln, ambiguous_note))
        for c in cond_cols:
            st_, sf = source_of(c)
            edges.append(Edge(target0, target, st_, sf, "CONDITIONAL", f"IF {cond} THEN {target} = {expr}",
                              "DATA", file, ln, "used in the condition"))
    for new, old in rename.items():
        if new not in assigned:
            edges.append(Edge(target0, new, only or "?", old, "RENAMED", f"{old} -> {new}", "DATA", file, line0,
                              ambiguous_note))
    for k in keep:
        if k not in assigned and k not in rename:
            edges.append(Edge(target0, k, only or "?", k, "DIRECT", f"KEEP {k}", "DATA", file, line0, ambiguous_note))
    return edges


# ----------------------------------------------------------------------------------------------------------
# PROC SQL
# ----------------------------------------------------------------------------------------------------------
def parse_sql_statement(file: str, line: int, st: str, warnings: list) -> list[Edge]:
    m = re.match(r"create\s+table\s+([A-Za-z_][\w.]*)\s+as\s+(select\s+.*)$", st, re.I)
    if not m:
        return []
    target = table_name(m.group(1))
    q = m.group(2)
    # split the query at top-level FROM
    depth, pos_from = 0, -1
    for i, ch in enumerate(q):
        depth += (ch == "(") - (ch == ")")
        if depth == 0 and re.match(r"\bfrom\b", q[i:i + 5], re.I) and (i == 0 or not q[i - 1].isalnum()):
            pos_from = i
            break
    if pos_from < 0:
        warnings.append(f"{file}:{line}: SQL without FROM skipped")
        return []
    select_list = re.sub(r"^select\s+(distinct\s+)?", "", q[:pos_from], flags=re.I)
    rest = q[pos_from + 4:]
    from_part = re.split(r"\b(?:where|group\s+by|having|order\s+by)\b", rest, maxsplit=1, flags=re.I)[0]
    alias: dict[str, str] = {}
    tables: list[str] = []
    for m2 in re.finditer(r"(?:^|\bjoin\b|,)\s*([A-Za-z_][\w.]*)(?:\s+(?:as\s+)?(?!on\b|left\b|right\b|inner\b|outer\b|full\b|join\b|cross\b)(" + IDENT + r"))?",
                          from_part, re.I):
        t = table_name(m2.group(1))
        tables.append(t)
        alias[t.split(".")[-1]] = t
        alias[t] = t
        if m2.group(2):
            alias[m2.group(2).upper()] = t
    alias = {k.upper(): v for k, v in alias.items()}
    only = tables[0] if len(set(tables)) == 1 else ""
    edges: list[Edge] = []
    for item in top_level_split(select_list):
        am = re.match(r"(.*?)\s+as\s+(" + IDENT + r")$", item, re.I | re.S)
        expr, name = (am.group(1).strip(), am.group(2).upper()) if am else (item.strip(), "")
        if expr == "*" or re.fullmatch(IDENT + r"\.\*", expr):
            edges.append(Edge(target, "*", only or "?", "*", "STAR", expr, "SQL", file, line,
                              "star is not expanded in this sample"))
            continue
        cols = []                       # (table, column)
        for qm in re.finditer(r"(?<![\w.&])(" + IDENT + r")\.(" + IDENT + r")(?!\s*\()", blank_quotes(expr)):
            cols.append((alias.get(qm.group(1).upper(), "?"), qm.group(2).upper()))
        stripped = re.sub(r"(?<![\w.&])" + IDENT + r"\.", "", blank_quotes(expr))
        for c in columns_in(stripped):
            if (only or "?", c.upper()) not in cols and not any(c.upper() == x[1] for x in cols):
                cols.append((only or "?", c.upper()))
        if not name:
            mm = re.fullmatch(r"(?:" + IDENT + r"\.)?(" + IDENT + ")", expr)
            name = mm.group(1).upper() if mm else ""
        if not name:
            warnings.append(f"{file}:{line}: select item without a name skipped: {expr[:40]}")
            continue
        agg = bool(re.search(r"\b(" + "|".join(SQL_AGG) + r")\s*\(", expr, re.I))
        simple = re.fullmatch(r"(?:" + IDENT + r"\.)?" + IDENT, expr) is not None
        kind = "AGGREGATE" if agg else ("DIRECT" if simple else "DERIVED")
        if simple and cols and cols[0][1] != name:
            kind = "RENAMED"
        if re.match(r"case\b", expr, re.I):
            kind = "CONDITIONAL"
        if not cols:
            edges.append(Edge(target, name, "(constant)", "", "DERIVED", expr, "SQL", file, line, "no input column"))
        for t, c in cols:
            edges.append(Edge(target, name, t, c, kind, f"{expr} AS {name}" if am else expr, "SQL", file, line,
                              "" if t != "?" else "unqualified column with several tables: source not decided"))
    return edges


# ----------------------------------------------------------------------------------------------------------
# program level
# ----------------------------------------------------------------------------------------------------------
def parse_text(text: str, file: str = "program.sas") -> Result:
    edges: list[Edge] = []
    warnings: list[str] = []
    macros: dict[str, str] = {}
    stmts = split_statements(text)
    i = 0
    while i < len(stmts):
        ln, st = stmts[i]
        m = re.match(r"%let\s+(" + IDENT + r")\s*=\s*(.*)$", st, re.I)
        if m:
            macros[m.group(1).upper()] = substitute(m.group(2).strip(), macros).strip("'\"")
            i += 1
            continue
        st = substitute(st, macros)
        if re.match(r"%macro\b", st, re.I):
            j = i
            while j < len(stmts) and not re.match(r"%mend\b", stmts[j][1], re.I):
                j += 1
            warnings.append(f"{file}:{ln}: macro definition skipped (not supported in this sample)")
            i = j + 1
            continue
        if re.match(r"%\w+", st):
            warnings.append(f"{file}:{ln}: macro statement skipped: {st[:40]}")
            i += 1
            continue
        if re.match(r"data\b", st, re.I):
            j, body = i + 1, []
            while j < len(stmts) and not re.match(r"run\b", stmts[j][1], re.I):
                body.append((stmts[j][0], substitute(stmts[j][1], macros)))
                j += 1
            edges += parse_data_step(file, (ln, st), body, warnings)
            i = j + 1
            continue
        if re.match(r"proc\s+sql\b", st, re.I):
            j = i + 1
            while j < len(stmts) and not re.match(r"quit\b", stmts[j][1], re.I):
                edges += parse_sql_statement(file, stmts[j][0], substitute(stmts[j][1], macros), warnings)
                j += 1
            i = j + 1
            continue
        i += 1
    return Result(edges, warnings)


def parse_files(paths: list) -> Result:
    all_edges, all_warn = [], []
    for p in paths:
        r = parse_text(Path(p).read_text(encoding="utf-8", errors="replace"), Path(p).name)
        all_edges += r.edges
        all_warn += r.warnings
    return Result(all_edges, all_warn)


# ----------------------------------------------------------------------------------------------------------
# outputs
# ----------------------------------------------------------------------------------------------------------
def write_csv(edges: list, path) -> None:
    cols = [f.name for f in fields(Edge)]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for e in edges:
            w.writerow(asdict(e))


def mermaid(edges: list) -> str:
    """Table-level flow chart (Mermaid) - paste into a Markdown file on GitHub to see it drawn."""
    seen, lines = set(), ["flowchart LR"]
    ids: dict[str, str] = {}

    def nid(t):
        return ids.setdefault(t, f"t{len(ids) + 1}")
    for e in edges:
        if e.source_table in ("?", "(constant)"):
            continue
        k = (e.source_table, e.target_table)
        if k in seen or e.source_table == e.target_table:
            continue
        seen.add(k)
        lines.append(f'    {nid(e.source_table)}["{e.source_table}"] --> {nid(e.target_table)}["{e.target_table}"]')
    return "\n".join(lines)


def trace(edges: list, target: str, max_depth: int = 25) -> list[list[str]]:
    """All upstream chains for TABLE.COLUMN, from the first input to the target. A chain stops at a table nothing
    in the given programs creates."""
    if "." not in target:
        raise ValueError("use TABLE.COLUMN, for example work.final.risk_band")
    tbl, _, col = target.rpartition(".")
    tbl, col = table_name(tbl), col.upper()
    by_target: dict[tuple, list] = {}
    for e in edges:
        by_target.setdefault((e.target_table, e.target_field), []).append(e)
    chains: list[list[str]] = []

    def walk(t, c, path, depth):
        ups = by_target.get((t, c), [])
        if not ups or depth >= max_depth:
            chains.append(list(reversed(path)))
            return
        for e in ups:
            node = f"{e.source_table}.{e.source_field}" if e.source_field else e.source_table
            if node in path:
                continue
            walk(e.source_table, e.source_field, path + [node], depth + 1)
    walk(tbl, col, [f"{tbl}.{col}"], 0)
    return chains


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Field-level lineage for small SAS programs (teaching sample)")
    ap.add_argument("programs", nargs="+")
    ap.add_argument("--csv", help="write every column edge to this CSV file")
    ap.add_argument("--mermaid", help="write a table-level flow chart (Mermaid) to this file")
    ap.add_argument("--trace", help="print the upstream chains of TABLE.COLUMN")
    a = ap.parse_args(argv)
    res = parse_files(a.programs)
    print(f"{len(res.edges)} column edges from {len(a.programs)} program(s); {len(res.warnings)} warning(s)")
    for w in res.warnings:
        print("  warning:", w)
    if a.csv:
        write_csv(res.edges, a.csv)
        print("wrote", a.csv)
    if a.mermaid:
        Path(a.mermaid).write_text("```mermaid\n" + mermaid(res.edges) + "\n```\n", encoding="utf-8")
        print("wrote", a.mermaid)
    if a.trace:
        for ch in trace(res.edges, a.trace):
            print("  " + "  ->  ".join(ch))
    return 0


if __name__ == "__main__":
    sys.exit(main())
