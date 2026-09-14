"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-14

Squashes what were `0001`-`0008` into one migration. Nothing has been deployed yet — no
environment holds applied revision history to protect — so this collapses straight to the
final v0 schema rather than replaying `0003`'s `coverage NOT NULL DEFAULT 'global'` and then
`0007`'s later fix, or creating `catalogs` without `identifier` and adding it in `0008`. Both
land in their final form directly.

Existing local/CI databases at any of the old revisions must be reset (`make clean` drops the
volume) before this applies; their `alembic_version` no longer matches anything in this
history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUMS = {
    "catalog_status": ("suggested", "active"),
    "catalog_kind": ("open", "public", "academic", "school", "specialized"),
    "catalog_color": ("gray", "red", "yellow", "blue", "green", "purple", "orange", "pink"),
    "publication_type": (
        "ebook",
        "audiobook",
        "comic",
        "newspaper",
        "magazine",
        "journal",
        "article",
    ),
    "coverage_scope": ("global", "country", "subdivisions", "local"),
    "link_rel": (
        "self",
        "catalog",
        "shelf",
        "icon",
        "authenticate",
        "alternate",
        "profile",
        "search",
    ),
}

#: (alpha2, alpha3, numeric3). ISO 3166-1, 249 entries.
COUNTRIES = [
    ("AD", "AND", "020"),
    ("AE", "ARE", "784"),
    ("AF", "AFG", "004"),
    ("AG", "ATG", "028"),
    ("AI", "AIA", "660"),
    ("AL", "ALB", "008"),
    ("AM", "ARM", "051"),
    ("AO", "AGO", "024"),
    ("AQ", "ATA", "010"),
    ("AR", "ARG", "032"),
    ("AS", "ASM", "016"),
    ("AT", "AUT", "040"),
    ("AU", "AUS", "036"),
    ("AW", "ABW", "533"),
    ("AX", "ALA", "248"),
    ("AZ", "AZE", "031"),
    ("BA", "BIH", "070"),
    ("BB", "BRB", "052"),
    ("BD", "BGD", "050"),
    ("BE", "BEL", "056"),
    ("BF", "BFA", "854"),
    ("BG", "BGR", "100"),
    ("BH", "BHR", "048"),
    ("BI", "BDI", "108"),
    ("BJ", "BEN", "204"),
    ("BL", "BLM", "652"),
    ("BM", "BMU", "060"),
    ("BN", "BRN", "096"),
    ("BO", "BOL", "068"),
    ("BQ", "BES", "535"),
    ("BR", "BRA", "076"),
    ("BS", "BHS", "044"),
    ("BT", "BTN", "064"),
    ("BV", "BVT", "074"),
    ("BW", "BWA", "072"),
    ("BY", "BLR", "112"),
    ("BZ", "BLZ", "084"),
    ("CA", "CAN", "124"),
    ("CC", "CCK", "166"),
    ("CD", "COD", "180"),
    ("CF", "CAF", "140"),
    ("CG", "COG", "178"),
    ("CH", "CHE", "756"),
    ("CI", "CIV", "384"),
    ("CK", "COK", "184"),
    ("CL", "CHL", "152"),
    ("CM", "CMR", "120"),
    ("CN", "CHN", "156"),
    ("CO", "COL", "170"),
    ("CR", "CRI", "188"),
    ("CU", "CUB", "192"),
    ("CV", "CPV", "132"),
    ("CW", "CUW", "531"),
    ("CX", "CXR", "162"),
    ("CY", "CYP", "196"),
    ("CZ", "CZE", "203"),
    ("DE", "DEU", "276"),
    ("DJ", "DJI", "262"),
    ("DK", "DNK", "208"),
    ("DM", "DMA", "212"),
    ("DO", "DOM", "214"),
    ("DZ", "DZA", "012"),
    ("EC", "ECU", "218"),
    ("EE", "EST", "233"),
    ("EG", "EGY", "818"),
    ("EH", "ESH", "732"),
    ("ER", "ERI", "232"),
    ("ES", "ESP", "724"),
    ("ET", "ETH", "231"),
    ("FI", "FIN", "246"),
    ("FJ", "FJI", "242"),
    ("FK", "FLK", "238"),
    ("FM", "FSM", "583"),
    ("FO", "FRO", "234"),
    ("FR", "FRA", "250"),
    ("GA", "GAB", "266"),
    ("GB", "GBR", "826"),
    ("GD", "GRD", "308"),
    ("GE", "GEO", "268"),
    ("GF", "GUF", "254"),
    ("GG", "GGY", "831"),
    ("GH", "GHA", "288"),
    ("GI", "GIB", "292"),
    ("GL", "GRL", "304"),
    ("GM", "GMB", "270"),
    ("GN", "GIN", "324"),
    ("GP", "GLP", "312"),
    ("GQ", "GNQ", "226"),
    ("GR", "GRC", "300"),
    ("GS", "SGS", "239"),
    ("GT", "GTM", "320"),
    ("GU", "GUM", "316"),
    ("GW", "GNB", "624"),
    ("GY", "GUY", "328"),
    ("HK", "HKG", "344"),
    ("HM", "HMD", "334"),
    ("HN", "HND", "340"),
    ("HR", "HRV", "191"),
    ("HT", "HTI", "332"),
    ("HU", "HUN", "348"),
    ("ID", "IDN", "360"),
    ("IE", "IRL", "372"),
    ("IL", "ISR", "376"),
    ("IM", "IMN", "833"),
    ("IN", "IND", "356"),
    ("IO", "IOT", "086"),
    ("IQ", "IRQ", "368"),
    ("IR", "IRN", "364"),
    ("IS", "ISL", "352"),
    ("IT", "ITA", "380"),
    ("JE", "JEY", "832"),
    ("JM", "JAM", "388"),
    ("JO", "JOR", "400"),
    ("JP", "JPN", "392"),
    ("KE", "KEN", "404"),
    ("KG", "KGZ", "417"),
    ("KH", "KHM", "116"),
    ("KI", "KIR", "296"),
    ("KM", "COM", "174"),
    ("KN", "KNA", "659"),
    ("KP", "PRK", "408"),
    ("KR", "KOR", "410"),
    ("KW", "KWT", "414"),
    ("KY", "CYM", "136"),
    ("KZ", "KAZ", "398"),
    ("LA", "LAO", "418"),
    ("LB", "LBN", "422"),
    ("LC", "LCA", "662"),
    ("LI", "LIE", "438"),
    ("LK", "LKA", "144"),
    ("LR", "LBR", "430"),
    ("LS", "LSO", "426"),
    ("LT", "LTU", "440"),
    ("LU", "LUX", "442"),
    ("LV", "LVA", "428"),
    ("LY", "LBY", "434"),
    ("MA", "MAR", "504"),
    ("MC", "MCO", "492"),
    ("MD", "MDA", "498"),
    ("ME", "MNE", "499"),
    ("MF", "MAF", "663"),
    ("MG", "MDG", "450"),
    ("MH", "MHL", "584"),
    ("MK", "MKD", "807"),
    ("ML", "MLI", "466"),
    ("MM", "MMR", "104"),
    ("MN", "MNG", "496"),
    ("MO", "MAC", "446"),
    ("MP", "MNP", "580"),
    ("MQ", "MTQ", "474"),
    ("MR", "MRT", "478"),
    ("MS", "MSR", "500"),
    ("MT", "MLT", "470"),
    ("MU", "MUS", "480"),
    ("MV", "MDV", "462"),
    ("MW", "MWI", "454"),
    ("MX", "MEX", "484"),
    ("MY", "MYS", "458"),
    ("MZ", "MOZ", "508"),
    ("NA", "NAM", "516"),
    ("NC", "NCL", "540"),
    ("NE", "NER", "562"),
    ("NF", "NFK", "574"),
    ("NG", "NGA", "566"),
    ("NI", "NIC", "558"),
    ("NL", "NLD", "528"),
    ("NO", "NOR", "578"),
    ("NP", "NPL", "524"),
    ("NR", "NRU", "520"),
    ("NU", "NIU", "570"),
    ("NZ", "NZL", "554"),
    ("OM", "OMN", "512"),
    ("PA", "PAN", "591"),
    ("PE", "PER", "604"),
    ("PF", "PYF", "258"),
    ("PG", "PNG", "598"),
    ("PH", "PHL", "608"),
    ("PK", "PAK", "586"),
    ("PL", "POL", "616"),
    ("PM", "SPM", "666"),
    ("PN", "PCN", "612"),
    ("PR", "PRI", "630"),
    ("PS", "PSE", "275"),
    ("PT", "PRT", "620"),
    ("PW", "PLW", "585"),
    ("PY", "PRY", "600"),
    ("QA", "QAT", "634"),
    ("RE", "REU", "638"),
    ("RO", "ROU", "642"),
    ("RS", "SRB", "688"),
    ("RU", "RUS", "643"),
    ("RW", "RWA", "646"),
    ("SA", "SAU", "682"),
    ("SB", "SLB", "090"),
    ("SC", "SYC", "690"),
    ("SD", "SDN", "729"),
    ("SE", "SWE", "752"),
    ("SG", "SGP", "702"),
    ("SH", "SHN", "654"),
    ("SI", "SVN", "705"),
    ("SJ", "SJM", "744"),
    ("SK", "SVK", "703"),
    ("SL", "SLE", "694"),
    ("SM", "SMR", "674"),
    ("SN", "SEN", "686"),
    ("SO", "SOM", "706"),
    ("SR", "SUR", "740"),
    ("SS", "SSD", "728"),
    ("ST", "STP", "678"),
    ("SV", "SLV", "222"),
    ("SX", "SXM", "534"),
    ("SY", "SYR", "760"),
    ("SZ", "SWZ", "748"),
    ("TC", "TCA", "796"),
    ("TD", "TCD", "148"),
    ("TF", "ATF", "260"),
    ("TG", "TGO", "768"),
    ("TH", "THA", "764"),
    ("TJ", "TJK", "762"),
    ("TK", "TKL", "772"),
    ("TL", "TLS", "626"),
    ("TM", "TKM", "795"),
    ("TN", "TUN", "788"),
    ("TO", "TON", "776"),
    ("TR", "TUR", "792"),
    ("TT", "TTO", "780"),
    ("TV", "TUV", "798"),
    ("TW", "TWN", "158"),
    ("TZ", "TZA", "834"),
    ("UA", "UKR", "804"),
    ("UG", "UGA", "800"),
    ("UM", "UMI", "581"),
    ("US", "USA", "840"),
    ("UY", "URY", "858"),
    ("UZ", "UZB", "860"),
    ("VA", "VAT", "336"),
    ("VC", "VCT", "670"),
    ("VE", "VEN", "862"),
    ("VG", "VGB", "092"),
    ("VI", "VIR", "850"),
    ("VN", "VNM", "704"),
    ("VU", "VUT", "548"),
    ("WF", "WLF", "876"),
    ("WS", "WSM", "882"),
    ("YE", "YEM", "887"),
    ("YT", "MYT", "175"),
    ("ZA", "ZAF", "710"),
    ("ZM", "ZMB", "894"),
    ("ZW", "ZWE", "716"),
]

#: (code, country_alpha2, parent_code, subdivision_type)
SUBDIVISIONS = [
    ("BE-BRU", "BE", None, "region"),
    ("BE-VLG", "BE", None, "region"),
    ("BE-WAL", "BE", None, "region"),
]

_URN_UUID_PATTERN = (
    r"^urn:uuid:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _enum(name: str) -> postgresql.ENUM:
    """Reference a type created earlier in this same migration without recreating it."""
    return postgresql.ENUM(name=name, create_type=False)


def _child_table(name: str, value_column: sa.Column, *extra: sa.schema.SchemaItem) -> None:
    """The four value collections share a shape: composite PK, cascade, index on catalog_id."""
    op.create_table(
        name,
        sa.Column("catalog_id", postgresql.UUID(as_uuid=True), nullable=False),
        value_column,
        sa.PrimaryKeyConstraint("catalog_id", value_column.name, name=op.f(f"pk_{name}")),
        sa.ForeignKeyConstraint(
            ["catalog_id"],
            ["catalogs.id"],
            ondelete="CASCADE",
            name=op.f(f"fk_{name}_catalog_id_catalogs"),
        ),
        *extra,
    )
    op.create_index(f"ix_{name}_catalog_id", name, ["catalog_id"])


def upgrade() -> None:
    for name, values in ENUMS.items():
        sa.Enum(*values, name=name).create(op.get_bind())

    op.create_table(
        "countries",
        sa.Column("alpha2", sa.CHAR(length=2), nullable=False),
        sa.Column("alpha3", sa.CHAR(length=3), nullable=False),
        sa.Column("numeric3", sa.CHAR(length=3), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.PrimaryKeyConstraint("alpha2", name=op.f("pk_countries")),
        sa.UniqueConstraint("alpha3", name=op.f("uq_countries_alpha3")),
    )
    op.create_table(
        "subdivisions",
        sa.Column("code", sa.String(length=6), nullable=False),
        sa.Column("country_alpha2", sa.CHAR(length=2), nullable=False),
        sa.Column("parent_code", sa.String(length=6), nullable=True),
        sa.Column("subdivision_type", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_subdivisions")),
        sa.ForeignKeyConstraint(
            ["country_alpha2"],
            ["countries.alpha2"],
            name=op.f("fk_subdivisions_country_alpha2_countries"),
        ),
        sa.ForeignKeyConstraint(
            ["parent_code"],
            ["subdivisions.code"],
            name=op.f("fk_subdivisions_parent_code_subdivisions"),
        ),
    )

    op.create_table(
        "catalogs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("status", _enum("catalog_status"), server_default="suggested", nullable=False),
        sa.Column("recommended", sa.Boolean(), server_default="false", nullable=False),
        # Q1 (ADR-036): the stable, externally-assigned upsert match key. `id` above is
        # gen_random_uuid()'d fresh per environment and cannot serve that role.
        sa.Column("identifier", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("color", _enum("catalog_color"), server_default="gray", nullable=False),
        sa.Column("country_code", sa.CHAR(length=2), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        # Nullable, no default (ADR-032). NULL means *not declared*; `global` means
        # *worldwide* — defaulting would assert reach no catalog claimed.
        sa.Column("coverage", _enum("coverage_scope"), nullable=True),
        sa.Column("submitter_name", sa.Text(), nullable=True),
        sa.Column("submitter_email", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalogs")),
        sa.ForeignKeyConstraint(
            ["country_code"], ["countries.alpha2"], name=op.f("fk_catalogs_country_code_countries")
        ),
        sa.UniqueConstraint("identifier", name=op.f("uq_catalogs_identifier")),
        sa.CheckConstraint("length(trim(title)) > 0", name=op.f("ck_catalogs_title_not_blank")),
        sa.CheckConstraint(
            "country_code = upper(country_code)", name=op.f("ck_catalogs_country_uppercase")
        ),
        sa.CheckConstraint(
            "status <> 'active' OR published_at IS NOT NULL",
            name=op.f("ck_catalogs_published_when_active"),
        ),
        sa.CheckConstraint(
            f"identifier ~ '{_URN_UUID_PATTERN}'",
            name=op.f("ck_catalogs_identifier_is_urn_uuid"),
        ),
    )
    # Partial: the top-level feed's only query. Right shape over ten rows, still right at
    # ten thousand.
    op.create_index(
        "ix_catalogs_recommended",
        "catalogs",
        ["recommended"],
        postgresql_where=sa.text("recommended AND status = 'active'"),
    )
    op.create_index("ix_catalogs_country_code", "catalogs", ["country_code"])

    _child_table("catalog_kinds", sa.Column("kind", _enum("catalog_kind"), nullable=False))
    _child_table(
        "catalog_publication_types",
        sa.Column("publication_type", _enum("publication_type"), nullable=False),
    )
    _child_table(
        "catalog_languages",
        sa.Column("language_tag", sa.String(length=35), nullable=False),
        sa.CheckConstraint(
            "language_tag = lower(language_tag)",
            name=op.f("ck_catalog_languages_language_tag_lowercase"),
        ),
    )
    _child_table(
        "catalog_subdivisions",
        sa.Column("subdivision_code", sa.String(length=6), nullable=False),
        sa.CheckConstraint(
            "subdivision_code = upper(subdivision_code)",
            name=op.f("ck_catalog_subdivisions_subdivision_code_uppercase"),
        ),
        sa.ForeignKeyConstraint(
            ["subdivision_code"],
            ["subdivisions.code"],
            name=op.f("fk_catalog_subdivisions_subdivision_code_subdivisions"),
        ),
    )

    op.create_table(
        "links",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("catalog_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("href", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=True),
        sa.Column("rel", _enum("link_rel"), nullable=False),
        sa.Column("templated", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("authentication", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_links")),
        sa.ForeignKeyConstraint(
            ["catalog_id"],
            ["catalogs.id"],
            ondelete="CASCADE",
            name=op.f("fk_links_catalog_id_catalogs"),
        ),
        # Satisfies the schema's `uniqueItems` on the links array.
        sa.UniqueConstraint("catalog_id", "rel", "href", name=op.f("uq_links_catalog_rel_href")),
        sa.CheckConstraint(
            "NOT templated OR rel = 'search'", name=op.f("ck_links_templated_only_search")
        ),
    )
    op.create_index("ix_links_catalog_id", "links", ["catalog_id"])

    # A trigger rather than an ORM `onupdate`: it also fires for the direct SQL the seed
    # script and any future data migration perform. Autogenerate does not produce triggers.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN NEW.updated_at = now(); RETURN NEW; END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER catalogs_set_updated_at
        BEFORE UPDATE ON catalogs
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )

    # ISO 3166-1 in full, and the ISO 3166-2 subdivisions actually referenced by catalog
    # data. A data migration rather than a seed script so every environment has countries
    # without an extra step — `catalogs.country_code` is a foreign key.
    op.bulk_insert(
        sa.table(
            "countries",
            sa.column("alpha2", sa.CHAR(2)),
            sa.column("alpha3", sa.CHAR(3)),
            sa.column("numeric3", sa.CHAR(3)),
        ),
        [{"alpha2": a2, "alpha3": a3, "numeric3": n3} for a2, a3, n3 in COUNTRIES],
    )
    op.bulk_insert(
        sa.table(
            "subdivisions",
            sa.column("code", sa.String(6)),
            sa.column("country_alpha2", sa.CHAR(2)),
            sa.column("parent_code", sa.String(6)),
            sa.column("subdivision_type", sa.String()),
        ),
        [
            {
                "code": code,
                "country_alpha2": country,
                "parent_code": parent,
                "subdivision_type": kind,
            }
            for code, country, parent, kind in SUBDIVISIONS
        ],
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS catalogs_set_updated_at ON catalogs;")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at();")
    op.drop_table("links")
    for name in (
        "catalog_subdivisions",
        "catalog_languages",
        "catalog_publication_types",
        "catalog_kinds",
    ):
        op.drop_table(name)
    op.drop_table("catalogs")
    op.drop_table("subdivisions")
    op.drop_table("countries")
    for name in reversed(ENUMS):
        sa.Enum(name=name).drop(op.get_bind())
