"""SQLite database, seed data and *enforced* per-server table isolation.

One shared file (meridian.db, WAL mode). Isolation is enforced by SQLite itself: every server opens
its connection with `set_authorizer()`, a callback SQLite runs while compiling each statement. It
allows reads/writes only on that server's own tables, so e.g. insurance_server running
"SELECT * FROM accounts" fails with 'not authorized' - even if someone writes a buggy query.

    python database.py --seed      # (re)build and seed meridian.db
"""
import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone

from guardrails import redact_for_logging
from logging_config import get_logger, trace

logger = get_logger("database")


def _redact_db(args):
    """SQL text is safe to log (it uses ? placeholders); parameter VALUES may be PII, so log only how many."""
    safe = redact_for_logging({k: v for k, v in args.items() if k != "params"})
    safe["params"] = f"<{len(args.get('params', ()))} values>"
    return safe

# server -> (tables it may read, tables it may also write)
ACCESS = {
    "banking_server": ({"accounts", "transactions"}, set()),
    "insurance_server": ({"policies", "claims"}, set()),
    "wealth_server": ({"portfolios", "investment_products"}, set()),
    "concierge_ops_server": ({"customers", "interaction_log", "escalation_flags", "audit_log"},
                             {"interaction_log", "audit_log"}),
    "admin": (None, None),  # seeding only
}

# sqlite3 authorizer action codes (stable numbers from the SQLite C API)
_READ, _INSERT, _UPDATE, _DELETE = 20, 18, 23, 9
_SAFE_ACTIONS = {21, 22, 31, 32, 33}  # SELECT, TRANSACTION, FUNCTION, SAVEPOINT, RECURSIVE
_OK, _DENY = sqlite3.SQLITE_OK, sqlite3.SQLITE_DENY


def db_path():
    return os.getenv("NEXUS_DB", "meridian.db")


def _authorizer(readable, writable):
    def check(action, arg1, arg2, db_name, source):
        if action == _READ:
            return _OK if arg1 in readable | writable else _DENY
        if action in (_INSERT, _UPDATE, _DELETE):
            return _OK if arg1 in writable else _DENY
        return _OK if action in _SAFE_ACTIONS else _DENY  # denies ATTACH, DDL, PRAGMA, ...

    return check


def connect(service):
    if service not in ACCESS:
        raise ValueError(f"Unknown service {service}")
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    readable, writable = ACCESS[service]
    if service != "admin":
        conn.set_authorizer(_authorizer(readable, writable))
    return conn


@trace(logger, redact=_redact_db)
def fetch_all(service, sql, params=()):
    conn = connect(service)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


@trace(logger, redact=_redact_db)
def fetch_one(service, sql, params=()):
    rows = fetch_all(service, sql, params)
    return rows[0] if rows else None


@trace(logger, redact=_redact_db)
def execute(service, sql, params=()):
    """Run one write statement; returns the new row id."""
    conn = connect(service)
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ schema
SCHEMA = """
CREATE TABLE customers (customer_id TEXT PRIMARY KEY, name TEXT, email TEXT, phone TEXT, tier TEXT,
    kyc_status TEXT, kyc_verified_on TEXT, kyc_document_ref TEXT, known_product_lines TEXT);
CREATE TABLE accounts (account_id TEXT PRIMARY KEY, customer_id TEXT, account_type TEXT, balance REAL,
    currency TEXT, status TEXT, auto_debit_allowed INTEGER, opened_on TEXT);
CREATE TABLE transactions (txn_id INTEGER PRIMARY KEY, account_id TEXT, txn_date TEXT, description TEXT,
    amount REAL, balance_after REAL);
CREATE TABLE policies (policy_id TEXT PRIMARY KEY, customer_id TEXT, policy_type TEXT, premium_amount REAL,
    premium_frequency TEXT, payment_methods TEXT, status TEXT, start_date TEXT, end_date TEXT,
    coverage_clauses TEXT);
CREATE TABLE claims (claim_id TEXT PRIMARY KEY, policy_id TEXT, status TEXT, filed_on TEXT, last_update TEXT);
CREATE TABLE portfolios (portfolio_id TEXT PRIMARY KEY, customer_id TEXT, risk_profile TEXT, total_value REAL,
    currency TEXT, status TEXT, holdings_json TEXT);
CREATE TABLE investment_products (product_id TEXT PRIMARY KEY, name TEXT, category TEXT, risk_level INTEGER,
    min_investment REAL, description TEXT);
CREATE TABLE interaction_log (id INTEGER PRIMARY KEY, ts TEXT, customer_id TEXT, channel TEXT, summary TEXT);
CREATE TABLE escalation_flags (id INTEGER PRIMARY KEY, customer_id TEXT, flag_type TEXT, active INTEGER,
    note TEXT, created_on TEXT);
CREATE TABLE audit_log (id INTEGER PRIMARY KEY, timestamp TEXT, action_type TEXT, performed_by TEXT,
    customer_id TEXT, outcome TEXT);
"""

TERMS = {
    "auto_debit_payment": {"covered": True, "clause": "4.2",
                           "text": "Premiums may be paid by monthly auto-debit from an eligible Meridian account."},
    "theft": {"covered": True, "clause": "2.1", "text": "Theft of insured items is covered."},
    "flood_damage": {"covered": False, "clause": "3.4", "text": "Flood damage is excluded."},
}


def _seed(conn):
    many = conn.executemany
    many("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?)", [
        ("CUS-20077", "Arjun Mehta", "arjun.mehta@example.com", "+91 98765 43210", "gold", "verified", "2025-03-14", "KYC-DOC-77-A1", "banking,insurance"),
        ("CUS-20078", "Meera Iyer", "meera.iyer@example.com", "+91 98765 43211", "silver", "verified", "2025-05-02", "KYC-DOC-78-B2", "banking,insurance"),
        ("CUS-20079", "Rahul Nair", "rahul.nair@example.com", "+91 98765 43212", "standard", "verified", "2024-11-20", "KYC-DOC-79-C3", "banking,insurance"),
        ("CUS-20080", "Sunita Rao", "sunita.rao@example.com", "+91 98765 43213", "standard", "verified", "2024-08-09", "KYC-DOC-80-D4", "banking"),
        ("CUS-20081", "Vikram Shah", "vikram.shah@example.com", "+91 98765 43214", "standard", "pending", None, "KYC-DOC-81-E5", "banking"),
    ])
    many("INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?)", [
        ("ACC-20077", "CUS-20077", "savings", 184500, "INR", "active", 1, "2021-06-01"),
        ("ACC-20078", "CUS-20078", "savings", 92300, "INR", "restricted", 0, "2022-02-11"),
        ("ACC-20079", "CUS-20079", "savings", 8400, "INR", "active", 1, "2023-01-23"),
        ("ACC-20080", "CUS-20080", "savings", 51200, "INR", "active", 1, "2020-09-30"),
        ("ACC-20081", "CUS-20081", "savings", 12000, "INR", "active", 1, "2024-07-04"),
    ])
    many("INSERT INTO transactions (account_id, txn_date, description, amount, balance_after) VALUES (?,?,?,?,?)", [
        ("ACC-20077", "2026-09-28", "Salary credit", 95000, 184500),
        ("ACC-20077", "2026-09-25", "Electricity bill", -3200, 89500),
        ("ACC-20077", "2026-09-20", "Grocery store", -4100, 92700),
        ("ACC-20077", "2026-09-15", "Insurance premium (manual)", -1250, 96800),
        ("ACC-20077", "2026-09-10", "Restaurant", -1800, 98050),
        ("ACC-20077", "2026-09-01", "Rent", -22000, 99850),
        ("ACC-20078", "2026-09-27", "Salary credit", 60000, 92300),
    ])
    clauses = json.dumps(TERMS)
    many("INSERT INTO policies VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("POL-IN-30091", "CUS-20077", "home", 1250, "monthly", "card,bank_transfer,auto_debit", "active", "2025-04-01", "2027-03-31", clauses),
        ("POL-IN-30092", "CUS-20078", "home", 980, "monthly", "card,bank_transfer,auto_debit", "active", "2025-06-01", "2027-05-31", clauses),
        ("POL-IN-30093", "CUS-20079", "motor", 1500, "monthly", "card,bank_transfer,auto_debit", "active", "2025-01-15", "2027-01-14", clauses),
    ])
    many("INSERT INTO claims VALUES (?,?,?,?,?)", [
        ("CLM-50001", "POL-IN-30091", "under_review", "2026-09-02", "2026-09-30"),
        ("CLM-50002", "POL-IN-30092", "settled", "2026-06-10", "2026-07-21"),
    ])
    many("INSERT INTO portfolios VALUES (?,?,?,?,?,?,?)", [
        ("PORT-20077", "CUS-20077", "moderate", 750000, "INR", "active",
         json.dumps([{"product_id": "PROD-MF-03", "units": 1200, "value": 450000},
                     {"product_id": "PROD-DB-02", "units": 300, "value": 300000}])),
    ])
    many("INSERT INTO investment_products VALUES (?,?,?,?,?,?)", [
        ("PROD-EQ-01", "Meridian Growth Equity Fund", "equity", 5, 5000, "High-growth equity fund with high volatility."),
        ("PROD-DB-02", "Meridian Secure Debt Fund", "debt", 2, 10000, "Short-duration debt fund for capital stability."),
        ("PROD-MF-03", "Meridian Balanced Fund", "hybrid", 3, 5000, "Balanced equity and debt allocation."),
    ])
    conn.execute("INSERT INTO escalation_flags (customer_id, flag_type, active, note, created_on) VALUES (?,?,?,?,?)",
                 ("CUS-20080", "safeguarding_concern", 1, "Branch reported possible financial abuse", "2026-09-18"))
    conn.execute("INSERT INTO audit_log (timestamp, action_type, performed_by, customer_id, outcome) VALUES (?,?,?,?,?)",
                 (now_iso(), "database_seeded", "database.py", "CUS-00000", "ok"))


@trace(logger)
def seed():
    """Delete and rebuild meridian.db with the fixtures used by the demo and tests."""
    path = db_path()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    conn = connect("admin")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _seed(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def ensure_seeded():
    if not os.path.exists(db_path()):
        seed()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Meridian database tools")
    parser.add_argument("--seed", action="store_true", help="rebuild and seed the database")
    if parser.parse_args().seed:
        print("seeded", seed())
