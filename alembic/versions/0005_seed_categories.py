"""seed default categories

Revision ID: 0005_seed_categories
Revises: 0004_reports_agent_columns
Create Date: 2026-04-16
"""

from __future__ import annotations

from alembic import op

revision = "0005_seed_categories"
down_revision = "0004_reports_agent_columns"
branch_labels = None
depends_on = None

# fmt: off
CATEGORIES = [
    # --- Ausgaben: Fix ---
    ("miete",             "Miete & Nebenkosten",          "fix"),
    ("strom-gas",         "Strom & Gas",                  "fix"),
    ("internet-telefon",  "Internet & Telefon",           "fix"),
    ("versicherungen",    "Versicherungen",               "fix"),
    ("auto-fix",          "Auto (Steuer, Versicherung)",  "fix"),
    ("oepnv-abo",         "ÖPNV-Abo",                    "fix"),
    ("abos-streaming",    "Abos & Streaming",             "fix"),
    ("mitgliedschaften",  "Mitgliedschaften",             "fix"),
    ("kredit-tilgung",    "Kredit & Tilgung",             "fix"),
    ("gez",               "Rundfunkbeitrag",              "fix"),
    # --- Ausgaben: Variabel ---
    ("lebensmittel",      "Lebensmittel",                 "variabel"),
    ("drogerie",          "Drogerie & Hygiene",           "variabel"),
    ("tanken",            "Tanken & Laden",               "variabel"),
    ("auto-variabel",     "Auto (Werkstatt, Parken)",     "variabel"),
    ("gesundheit",        "Gesundheit & Apotheke",        "variabel"),
    ("haushalt",          "Haushalt & Wohnung",           "variabel"),
    # --- Ausgaben: Diskretionär ---
    ("restaurant-cafe",   "Restaurant & Café",            "diskretionaer"),
    ("bars-ausgehen",     "Bars & Ausgehen",              "diskretionaer"),
    ("kleidung",          "Kleidung & Schuhe",            "diskretionaer"),
    ("elektronik",        "Elektronik & Software",        "diskretionaer"),
    ("freizeit",          "Freizeit & Hobby",             "diskretionaer"),
    ("reisen",            "Reisen & Urlaub",              "diskretionaer"),
    ("geschenke",         "Geschenke & Spenden",          "diskretionaer"),
    ("bildung",           "Bildung & Bücher",             "diskretionaer"),
    # --- Einkommen ---
    ("gehalt",            "Gehalt & Lohn",                "fix"),
    ("nebeneinkuenfte",   "Nebeneinkünfte",               "variabel"),
    ("kapitalertraege",   "Kapitalerträge",               "variabel"),
    ("erstattungen",      "Erstattungen & Rückzahlungen", "variabel"),
    # --- Investitionen & Transfers ---
    ("etf-sparplan",      "ETF-Sparplan",                 "fix"),
    ("wertpapierkauf",    "Wertpapierkauf",               "diskretionaer"),
    ("umbuchung",         "Umbuchung",                    "fix"),
]
# fmt: on


def upgrade() -> None:
    for cat_id, name, cat_type in CATEGORIES:
        op.execute(
            f"INSERT INTO categories (id, name, type) "
            f"VALUES ('{cat_id}', '{name}', '{cat_type}') "
            f"ON CONFLICT (id) DO NOTHING"
        )


def downgrade() -> None:
    ids = ", ".join(f"'{c[0]}'" for c in CATEGORIES)
    op.execute(f"DELETE FROM categories WHERE id IN ({ids})")
