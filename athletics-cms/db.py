import sqlite3
import os
import re

DB_PATH = os.path.join(os.path.dirname(__file__), "dev.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

# A DATABASE_URL (or POSTGRES_URL — the name a Vercel Postgres/Neon storage
# integration tends to inject) switches every connection in this app from
# local SQLite to real Postgres. This is required, not optional, the
# moment this app runs anywhere without a persistent local disk (Vercel's
# serverless functions are the reason this exists) — set neither and local
# development behaves exactly as it always has, against dev.db.
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")


def using_postgres():
    return bool(DATABASE_URL)


# --- Pure translation helpers -------------------------------------------
# Kept independent of psycopg2 (no import of it anywhere below) so this
# logic can be exercised directly — e.g. in a test harness that doesn't
# have psycopg2 installed at all — without needing a real Postgres
# connection. Only get_db()/_PGConnection actually touch psycopg2.
_NAMED_PARAM_RE = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)")
_AUTOINCREMENT_RE = re.compile(r"INTEGER PRIMARY KEY AUTOINCREMENT")
_OR_IGNORE_RE = re.compile(r"INSERT\s+OR\s+IGNORE\s+INTO", re.IGNORECASE)
_INSERT_TABLE_RE = re.compile(r"INSERT\s+INTO\s+(\w+)", re.IGNORECASE)


def _tables_with_id_column():
    """Not every table here has a surrogate `id` — a handful of
    one-row-per-key settings tables (staff_directory_settings,
    roster_settings, schedule_settings) use their own natural key as the
    primary key instead, and permissions/role_permissions are pure
    lookup/join tables. Blindly appending `RETURNING id` to an INSERT
    against one of those fails outright ("column id does not exist"), so
    this reads schema.sql once at import time — the same file
    executescript() below turns into the live schema — to know which
    tables can actually answer that RETURNING clause."""
    with open(SCHEMA_PATH) as f:
        script = f.read()
    tables = set()
    for name, body in zip(
        re.findall(r"CREATE TABLE (\w+)\s*\(", script),
        re.split(r"CREATE TABLE \w+\s*\(", script)[1:],
    ):
        if re.search(r"^\s*id\s+INTEGER", body, re.MULTILINE):
            tables.add(name)
    return tables


_TABLES_WITH_ID = _tables_with_id_column()


def _translate(sql, params):
    """Every query elsewhere in this app was written against sqlite3's
    placeholder conventions — positional `?` with a list/tuple of params,
    or named `:key` with a dict — never psycopg2's `%s`/`%(key)s`.
    Translating here, once, means none of the ~260 call sites across
    app.py need to change. This is exactly as naive as sqlite3's own
    placeholder handling (a positional count, nothing smarter) — safe
    here because nothing in this app's SQL text has a literal `?`,
    `:word`, or `%` typed directly into the query string; every
    `%`-bearing value (e.g. a LIKE pattern) is always built into a bound
    *parameter*, never the SQL text itself."""
    sql = _OR_IGNORE_RE.sub("INSERT INTO", sql)
    if isinstance(params, dict):
        return _NAMED_PARAM_RE.sub(r"%(\1)s", sql), params
    return sql.replace("?", "%s"), (params if params is not None else [])


def _pg_schema_script(script):
    """schema.sql is pure DDL (no parameters), so — unlike every other
    query in this app — there's no placeholder translation to do here,
    just the handful of SQLite-only spellings: PRAGMA (Postgres always
    enforces foreign keys, nothing to opt into) and
    `INTEGER PRIMARY KEY AUTOINCREMENT` (Postgres spells the same
    "auto-incrementing integer PK" idea `SERIAL PRIMARY KEY`). Everything
    else in schema.sql — UNIQUE/REFERENCES/CHECK/DEFAULT CURRENT_TIMESTAMP
    — is standard SQL both databases already agree on."""
    lines = [
        line for line in script.splitlines()
        if not line.strip().upper().startswith("PRAGMA")
    ]
    return _AUTOINCREMENT_RE.sub("SERIAL PRIMARY KEY", "\n".join(lines))


if using_postgres():
    import psycopg2
    import psycopg2.extras

    class _PGCursor:
        """Wraps a psycopg2 cursor (RealDictCursor, so rows already behave
        like plain dicts — `row['col']`, `dict(row)`, `.get()` all just
        work, same as this app already relies on from sqlite3.Row) to also
        emulate sqlite3's `cursor.lastrowid` after an INSERT, which
        psycopg2 has no native equivalent for. Most tables in schema.sql
        use a plain integer `id` primary key, so appending `RETURNING id`
        to an INSERT against one of those (see _TABLES_WITH_ID — a few
        natural-key settings/lookup tables don't have an `id` at all)
        resolves it — the 5 call sites elsewhere that read `.lastrowid`
        need no changes."""

        def __init__(self, cursor):
            self._cursor = cursor
            self.lastrowid = None

        def execute(self, sql, params=()):
            sql, bound = _translate(sql, params)
            stripped = sql.strip()
            table_match = _INSERT_TABLE_RE.match(stripped)
            wants_returning = (
                table_match
                and table_match.group(1) in _TABLES_WITH_ID
                and "RETURNING" not in stripped.upper()
            )
            if wants_returning:
                sql = stripped + " RETURNING id"
            self._cursor.execute(sql, bound)
            if wants_returning:
                try:
                    row = self._cursor.fetchone()
                    self.lastrowid = row["id"] if row else None
                except psycopg2.ProgrammingError:
                    # An "INSERT ... ON CONFLICT DO NOTHING" that ended up
                    # not inserting anything has nothing to RETURN.
                    self.lastrowid = None
            return self

        def executemany(self, sql, seq_of_params):
            seq = list(seq_of_params)
            if not seq:
                return self
            sql, _ = _translate(sql, seq[0])
            self._cursor.executemany(sql, seq)
            return self

        def fetchone(self):
            return self._cursor.fetchone()

        def fetchall(self):
            return self._cursor.fetchall()

        def __iter__(self):
            return iter(self._cursor)

        @property
        def rowcount(self):
            return self._cursor.rowcount

        @property
        def description(self):
            return self._cursor.description

    class _PGConnection:
        """Enough of sqlite3.Connection's surface for this app: `execute`/
        `executemany` as connection-level shortcuts (sqlite3's own
        convenience methods), a real `.cursor()`, `.executescript()` for
        the schema file, and `.commit()`/`.close()`."""

        def __init__(self, conn):
            self._conn = conn

        def _new_cursor(self):
            return _PGCursor(self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor))

        def execute(self, sql, params=()):
            return self._new_cursor().execute(sql, params)

        def executemany(self, sql, seq_of_params):
            return self._new_cursor().executemany(sql, seq_of_params)

        def executescript(self, script):
            # psycopg2, unlike sqlite3, is happy to run a whole
            # `;`-separated script through one plain execute() — the only
            # work needed is _pg_schema_script's SQLite->Postgres spelling
            # fixes above.
            with self._conn.cursor() as cur:
                cur.execute(_pg_schema_script(script))

        def commit(self):
            self._conn.commit()

        def close(self):
            self._conn.close()

        def cursor(self):
            return self._new_cursor()


def get_db():
    if using_postgres():
        raw = psycopg2.connect(DATABASE_URL)
        return _PGConnection(raw)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()


if __name__ == "__main__":
    if using_postgres():
        init_db()
        print("Initialized Postgres database from DATABASE_URL/POSTGRES_URL")
    else:
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        init_db()
        print(f"Initialized fresh database at {DB_PATH}")
