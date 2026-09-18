# Migrations (Alembic)

This app now has migration tooling. It did not before — every schema
change had to go out as a new `Column(...)` in `models/models.py` and
hope `Base.metadata.create_all()` (which only creates *missing* tables,
never alters existing ones) was enough. That stops being safe the moment
a change needs to modify an existing table on a database that already
has real student data in it — which is exactly where this project is now.

## One-time setup on your LIVE Supabase database

`alembic/versions/..._baseline.py` describes every table as it exists
today. Your Supabase database already has these tables (created by
`create_all()` on every deploy so far) — so the baseline must be
**stamped**, not **run**, or Alembic will try to `CREATE TABLE` things
that already exist and fail:

```bash
export DATABASE_URL="<your real Supabase connection string>"
pip install -r requirements.txt -r requirements-dev.txt --break-system-packages
alembic stamp head
```

`stamp` just tells Alembic "the database is already at this point," with
zero DDL executed. Do this once, on production, before your next schema
change.

## Making a future schema change

1. Edit `models/models.py` as usual (add a column, a table, etc).
2. Generate a migration: `alembic revision --autogenerate -m "add whatever"`.
3. **Read the generated file in `alembic/versions/`** — autogenerate is a
   good first draft, not a guarantee (it won't catch a renamed column, for
   instance, and will generate a drop+add instead, which loses data).
4. Apply it: `alembic upgrade head`.

`main.py`'s startup still runs `Base.metadata.create_all()` too, so
brand-new tables keep appearing automatically with zero extra steps —
that part hasn't changed. Alembic is specifically for changes
`create_all()` can't do: altering or dropping a column, renaming
something, adding a NOT NULL constraint to existing rows, etc.

## Local dev

The SQLite fallback (no `DATABASE_URL` set) doesn't need any of this —
`create_all()` builds it fresh every time, and pytest's fixtures do the
same for each test. Alembic mainly matters for the real, persistent
Postgres database.
