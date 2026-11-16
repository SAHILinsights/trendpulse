"""Generate docs/data_dictionary.md from the SQLAlchemy schema (single source of truth)."""

from __future__ import annotations

from pathlib import Path

from trendpulse.catalog import COL_DOC, LAYER, TABLE_DOC
from trendpulse.storage import schema as s


def main() -> None:
    out = ["# Data dictionary", "", "Generated from `src/trendpulse/storage/schema.py` by "
           "`scripts/generate_data_dictionary.py`. All timestamps are `TIMESTAMP WITH TIME ZONE` in UTC. "
           "In PostgreSQL every table lives in schema `trendpulse`.", ""]
    order = sorted(s.metadata.tables.values(), key=lambda t: (
        ["Bronze", "Silver", "Gold"].index(LAYER[t.name].split(" ")[0]) if t.name in LAYER else 9, t.name))
    out.append("| Table | Layer | Purpose |\n|---|---|---|")
    for t in order:
        out.append(f"| [`{t.name}`](#{t.name}) | {LAYER.get(t.name, 'Operational')} | {TABLE_DOC.get(t.name, '')} |")
    for t in order:
        pk = [c.name for c in t.primary_key.columns]
        out += ["", f"## {t.name}", "", f"*{LAYER.get(t.name, 'Operational')}* — {TABLE_DOC.get(t.name, '')}", "",
                f"Primary key: `({', '.join(pk)})`", ""]
        uniq = [c for c in t.constraints if c.__class__.__name__ == "UniqueConstraint"]
        for u in uniq:
            out.append(f"Unique: `({', '.join(col.name for col in u.columns)})`  ")
        if t.indexes:
            out.append("Indexes: " + ", ".join(f"`{i.name}({', '.join(c.name for c in i.columns)})`"
                                                for i in sorted(t.indexes, key=lambda i: i.name)))
            out.append("")
        out.append("| Column | Type | Null | Description |\n|---|---|---|---|")
        for c in t.columns:
            out.append(f"| `{c.name}` | {str(c.type).replace('DATETIME', 'TIMESTAMPTZ')} | "
                       f"{'yes' if c.nullable else 'no'} | {COL_DOC.get(c.name, '')} |")
    Path(__file__).resolve().parents[1].joinpath("docs", "data_dictionary.md").write_text("\n".join(out) + "\n")
    print("wrote docs/data_dictionary.md")


if __name__ == "__main__":
    main()
