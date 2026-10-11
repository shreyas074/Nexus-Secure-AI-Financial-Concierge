import sqlite3

import pytest

import database as db


def test_seed_creates_expected_data():
    assert db.fetch_one("banking_server", "SELECT COUNT(*) AS n FROM accounts")["n"] == 5
    assert db.fetch_one("concierge_ops_server", "SELECT name FROM customers WHERE customer_id='CUS-20077'")["name"] == "Arjun Mehta"
    assert db.fetch_one("insurance_server", "SELECT policy_id FROM policies WHERE policy_id='POL-IN-30091'")


def test_database_is_in_wal_mode():
    assert sqlite3.connect(db.db_path()).execute("PRAGMA journal_mode").fetchone()[0] == "wal"


@pytest.mark.parametrize("service,table", [
    ("insurance_server", "accounts"), ("insurance_server", "transactions"), ("insurance_server", "portfolios"),
    ("insurance_server", "customers"), ("banking_server", "policies"), ("banking_server", "portfolios"),
    ("banking_server", "customers"), ("wealth_server", "accounts"), ("wealth_server", "policies"),
    ("wealth_server", "customers"), ("concierge_ops_server", "accounts"), ("concierge_ops_server", "policies"),
    ("concierge_ops_server", "portfolios"),
])
def test_servers_cannot_read_other_servers_tables(service, table):
    with pytest.raises(sqlite3.DatabaseError):
        db.fetch_all(service, f"SELECT * FROM {table}")


@pytest.mark.parametrize("service,table", [
    ("banking_server", "accounts"), ("insurance_server", "claims"), ("wealth_server", "portfolios"),
    ("concierge_ops_server", "customers"), ("concierge_ops_server", "escalation_flags"),
])
def test_read_only_tables_cannot_be_written(service, table):
    with pytest.raises(sqlite3.DatabaseError):
        db.execute(service, f"DELETE FROM {table}")


def test_servers_can_read_their_own_tables():
    for service, table in [("banking_server", "accounts"), ("banking_server", "transactions"),
                           ("insurance_server", "policies"), ("insurance_server", "claims"),
                           ("wealth_server", "portfolios"), ("wealth_server", "investment_products"),
                           ("concierge_ops_server", "customers"), ("concierge_ops_server", "audit_log")]:
        db.fetch_all(service, f"SELECT * FROM {table}")


def test_cannot_attach_other_database_or_change_schema():
    for sql in ("ATTACH DATABASE 'other.db' AS o", "DROP TABLE accounts", "CREATE TABLE x (a)"):
        with pytest.raises(sqlite3.DatabaseError):
            db.fetch_all("banking_server", sql)


def test_ops_server_can_write_audit_log():
    row_id = db.execute("concierge_ops_server", "INSERT INTO audit_log (timestamp, action_type, performed_by, "
                        "customer_id, outcome) VALUES (?,?,?,?,?)", (db.now_iso(), "t", "t", "CUS-20077", "ok"))
    assert row_id > 0


def test_unknown_service_rejected():
    with pytest.raises(ValueError):
        db.connect("rogue_server")
