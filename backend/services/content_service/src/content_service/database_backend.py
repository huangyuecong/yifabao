"""Database construction without loading application settings or opening a connection."""

import os
from collections.abc import Mapping
from pathlib import Path
from types import MethodType

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.engine.default import DefaultDialect


def _integer(values: Mapping[str, str], name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(values.get(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def database_options(url: str, environ: Mapping[str, str] | None = None) -> dict:
    values = os.environ if environ is None else environ
    try:
        parsed = make_url(url)
    except Exception:
        raise ValueError(
            "CONTENT_DATABASE_URL is invalid; no connection values were logged"
        ) from None
    backend = values.get("CONTENT_DATABASE_BACKEND", "sqlite").strip().lower()
    if backend not in {"sqlite", "dm"}:
        raise ValueError("CONTENT_DATABASE_BACKEND must be sqlite or dm")
    if parsed.get_backend_name() != backend:
        raise ValueError("Database URL and CONTENT_DATABASE_BACKEND differ; explicitly select dm")
    options = {"pool_pre_ping": True, "hide_parameters": True}
    if backend == "sqlite":
        options["connect_args"] = {"check_same_thread": False}
        return options
    if parsed.drivername != "dm+dmPython" or not parsed.host or not parsed.username:
        raise ValueError("Dameng requires dm+dmPython URL with user and host (or service name)")
    # Do not accept driver options that enable autocommit, tracing, alternate schemas,
    # or arbitrary driver behavior through a pasted JDBC-style query string.
    if parsed.query:
        raise ValueError(
            "Dameng URL query parameters are unsupported; use documented CONTENT_DM_* keys"
        )
    args = {
        "autoCommit": False,
        "login_timeout": _integer(values, "CONTENT_DM_LOGIN_TIMEOUT_MS", 5000),
    }
    schema = values.get("CONTENT_DM_SCHEMA", "").strip()
    if schema:
        if parsed.database and parsed.database != schema:
            raise ValueError("Dameng URL schema and CONTENT_DM_SCHEMA differ")
        args["schema"] = schema
    svc_dir = values.get("CONTENT_DM_SVC_PATH", "").strip()
    service = values.get("CONTENT_DM_SERVICE_NAME", "").strip()
    if bool(svc_dir) != bool(service):
        raise ValueError(
            "CONTENT_DM_SVC_PATH and CONTENT_DM_SERVICE_NAME must be configured together"
        )
    if service:
        if not Path(svc_dir, "dm_svc.conf").is_file():
            raise ValueError("CONTENT_DM_SVC_PATH must contain dm_svc.conf")
        if parsed.host != service or parsed.port is not None:
            raise ValueError(
                "For a service connection, URL host must equal the service name without a port"
            )
        # The vendor dialect otherwise synthesizes host:5236, which bypasses aliases.
        args.update(dmsvc_path=str(Path(svc_dir).resolve()))
    options.update(
        connect_args=args,
        pool_size=_integer(values, "CONTENT_DATABASE_POOL_SIZE", 5),
        max_overflow=_integer(values, "CONTENT_DATABASE_MAX_OVERFLOW", 5, 0),
        pool_timeout=_integer(values, "CONTENT_DATABASE_POOL_TIMEOUT", 30),
        pool_recycle=_integer(values, "CONTENT_DATABASE_POOL_RECYCLE", 1800),
    )
    return options


def create_database_engine(url: str, environ: Mapping[str, str] | None = None, **overrides):
    options = database_options(url, environ)
    options.update(overrides)
    try:
        engine = create_engine(url, **options)
        values = os.environ if environ is None else environ
        if engine.dialect.name == "dm":
            _configure_dm_batch_inserts(engine)
            # dmSQLAlchemy 2.0.17's bulk constraint query uses USER_CONSTRAINTS
            # without an owner predicate for the current schema. A login can own
            # several schemas, so same-named tables can acquire another schema's
            # PK/FK/unique metadata. The vendor's single-table queries explicitly
            # filter by owner and retain foreign-key reference schemas. Use the
            # SQLAlchemy bulk adapters on this engine only; keep the vendor's
            # column/type reflection and actual constraints unchanged.
            for method_name in (
                "get_multi_pk_constraint",
                "get_multi_foreign_keys",
                "get_multi_unique_constraints",
            ):
                setattr(
                    engine.dialect,
                    method_name,
                    MethodType(getattr(DefaultDialect, method_name), engine.dialect),
                )
        if engine.dialect.name == "dm" and values.get("CONTENT_DM_SERVICE_NAME", "").strip():
            # dmPython documents service names for host/server, while dsn is host:port.
            # Remove the dialect's synthesized dsn so the driver uses host + dm_svc.conf.
            @event.listens_for(engine, "do_connect")
            def use_service_name(dialect, connection_record, cargs, cparams):
                cparams.pop("dsn", None)

        return engine
    except (ImportError, ModuleNotFoundError):
        raise RuntimeError(
            "Database driver is missing; install the matching offline requirements"
        ) from None


def _configure_dm_batch_inserts(engine) -> None:
    # dmSQLAlchemy 2.0.17 advertises ordered batch RETURNING, but the deployed
    # driver can return fewer UUID rows than the ORM inserted. Let Python
    # defaults use single INSERTs; no writes are committed or retried here.
    engine.dialect.insert_executemany_returning = False
    engine.dialect.insert_executemany_returning_sort_by_parameter_order = False
    engine.dialect.use_insertmanyvalues = False
    engine.dialect.use_insertmanyvalues_wo_returning = False

    @event.listens_for(engine, "do_executemany")
    def insert_without_bulk_returning(cursor, statement, parameters, context):
        if (context is None or not context.isinsert or context.compiled is None
                or context.compiled.effective_returning or context.compiled.has_out_parameters
                or context.out_parameters):
            return None
        # Explicit UUIDs and Core batches still reach executemany. The vendor
        # adds RETURNING even when none was requested, so execute each row on
        # this same cursor/transaction and retain bound primary-key values.
        if hasattr(cursor, "output_stream"):
            cursor.output_stream = 0
        rowcount = 0
        for values in parameters:
            engine.dialect.do_execute(cursor, statement, list(values), context)
            count = cursor.rowcount
            rowcount = rowcount + count if rowcount >= 0 and count >= 0 else -1
        context._rowcount = rowcount
        context.inserted_primary_key_rows = context._setup_ins_pk_from_empty()
        return True


def auto_migrate_enabled(url: str, environ: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    default = "true" if make_url(url).get_backend_name() == "sqlite" else "false"
    value = values.get("CONTENT_DATABASE_AUTO_MIGRATE", default).strip().lower()
    if value not in {"true", "false"}:
        raise ValueError("CONTENT_DATABASE_AUTO_MIGRATE must be true or false")
    return value == "true"
