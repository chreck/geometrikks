# External database

GeoMetrikks ships with a TimescaleDB container, but nothing in the app
requires it to be that one. Every connection parameter is configurable, so
the app can talk to a database you already run: another host, a shared
cluster, or a managed TimescaleDB service.

## What the database must provide

The traffic history is not stored in plain PostgreSQL tables. Three
extensions are mandatory, and the app enables them at startup:

| Extension | Used for |
|---|---|
| `timescaledb` | Hypertables, continuous aggregates and retention policies |
| `timescaledb_toolkit` | The percentile and counter helpers behind the analytics views |
| `postgis` | `geo_locations.geographic_point` and every spatial query |

They must be *available* on the server; the app runs
`CREATE EXTENSION IF NOT EXISTS`, which needs no special privilege once an
administrator has installed the extension in the target database. If the
extensions are missing entirely, startup fails with a clear error rather
than silently degrading.

That rules out managed PostgreSQL offerings without TimescaleDB (Amazon
RDS for PostgreSQL, Cloud SQL, Azure Database for PostgreSQL). What works:

- The `timescale/timescaledb-ha` image on a host of your choice — the
  variant the project is tested against, and the only one that carries the
  toolkit.
- Timescale Cloud, where all three extensions are available.
- A self-managed PostgreSQL with TimescaleDB, the toolkit and PostGIS
  installed from packages.

The application role needs `CONNECT` on the database and `CREATE` on the
`public` schema; migrations create and alter its tables. Nothing needs
superuser once the extensions exist.

## Pointing the app at it

Two equivalent ways. Individual variables:

```bash
DB_HOST=pg.example.com
DB_PORT=5432
DB_USER=geouser
DB_PASSWORD=...
DB_DATABASE=geometrikks
```

Or one connection string, which is what most managed providers hand you:

```bash
DB_CONNECTION_STRING=postgresql://geouser:pass@pg.example.com:5432/geometrikks?sslmode=verify-full
```

`DATABASE_URL` is accepted as a synonym, so a provider's own variable can
be passed straight through.

The connection string wins over the individual variables for every
component it carries — it is one atomic address, and a deployment should
not have to unset the `DB_HOST` an image or compose file already provides.
Variables it does not cover still apply, so `DB_POOL_SIZE`,
`DB_MIGRATE_ON_STARTUP` and the TLS settings work either way.

Query parameters are split up the same way libpq does: `sslmode`,
`sslrootcert`, `sslcert`, `sslkey` and `sslpassword` configure TLS, and
anything else (`application_name`, `options`) becomes a PostgreSQL startup
parameter. `DB_SERVER_SETTINGS` sets those directly as a JSON object, and
its entries win over the ones in the string.

Unix-socket connections are not supported; the app connects over TCP.

## TLS

`DB_SSLMODE` follows libpq's vocabulary:

| Mode | Encrypted | Server identity checked |
|---|---|---|
| `disable` | no | – |
| `allow` | if the server insists | no |
| `prefer` (default) | if the server offers it | no |
| `require` | yes | no |
| `verify-ca` | yes | certificate must be signed by a trusted CA |
| `verify-full` | yes | CA **and** the hostname must match |

Only the `verify-*` modes protect against a machine-in-the-middle; the
others just encrypt. For a database on another host, `verify-full` is the
setting worth the small amount of extra work.

Certificates are configured as file paths:

```bash
DB_SSLMODE=verify-full
DB_SSLROOTCERT=/etc/geometrikks/tls/ca.pem   # CA that signed the server certificate
DB_SSLCERT=/etc/geometrikks/tls/client.pem   # only for certificate authentication
DB_SSLKEY=/etc/geometrikks/tls/client.key
DB_SSLPASSWORD=...                           # only if the key is encrypted
```

Without `DB_SSLROOTCERT`, `verify-ca` and `verify-full` verify against the
system trust store, which is what a managed provider with a publicly
signed certificate needs. Mount the file into the container (read-only)
and make sure it is readable by `PUID`:`PGID`. A path that does not exist
fails startup instead of silently falling back to an unverified
connection.

## Secrets from files

Every variable the app understands also reads from a file when you append
`_FILE` to its name and point that at a path:

```bash
DB_PASSWORD_FILE=/run/secrets/db_password
DB_CONNECTION_STRING_FILE=/run/secrets/db_dsn
APP_ADMIN_PASSWORD_FILE=/run/secrets/admin_password
MAXMINDDB_LICENSE_KEY_FILE=/run/secrets/maxmind_key
```

This is how Docker secrets, Podman secrets and Kubernetes secret volumes
hand credentials to a container, and it keeps them out of `docker
inspect`, `/proc/<pid>/environ` and crash reports. A trailing newline is
stripped, so `echo` and an editor both produce a usable file.

If both `DB_PASSWORD` and `DB_PASSWORD_FILE` are set, the file wins: it is
the deliberate choice, while the plain variable is usually a default
inherited from a compose file. A file that cannot be read fails startup
naming the variable, rather than falling back to a default password.

## Compose without the bundled database

`docker-compose.external-db.yml` is the user-facing compose file with the
`timescale_db` service removed:

```bash
curl -LO https://raw.githubusercontent.com/GilbN/geometrikks/main/docker-compose.external-db.yml
curl -Lo .env https://raw.githubusercontent.com/GilbN/geometrikks/main/.env.example
$EDITOR .env      # DB_CONNECTION_STRING or DB_HOST/DB_USER/DB_PASSWORD, admin password, log path
docker compose -f docker-compose.external-db.yml up -d
```

The app starts in degraded mode when the database is unreachable and
recovers on its own once it answers, so a database restart does not need a
container restart.

## Connection pooling

`DB_POOL_SIZE` (default 5) plus `DB_MAX_OVERFLOW` (10) bound what one
GeoMetrikks process opens, and the live-events backend adds one long-lived
`LISTEN` connection. Size the server's `max_connections` accordingly when
the database is shared.

Do not put a transaction-pooling proxy (PgBouncer in `transaction` mode,
Supabase's pooler port, RDS Proxy) in front of it. The live feed relies on
session-level `LISTEN/NOTIFY`, and asyncpg's prepared statements need the
same backend across statements; both break under transaction pooling.
Session pooling works, as does connecting directly.

## Migrations

By default the container migrates the schema at startup
(`DB_MIGRATE_ON_STARTUP=true`), which is also what creates the extensions
and TimescaleDB objects. For a deployment that runs migrations as its own
step, see [Migration ownership](deployment.md#migration-ownership); the
TLS and connection settings apply unchanged to `litestar database
upgrade`.
