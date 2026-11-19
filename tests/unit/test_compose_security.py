"""Roadmap M4.1a, step 7: what the files that start PostgreSQL must and must not contain."""
import re

from rag import config

COMPOSE = (config.ROOT / "docker-compose.yml").read_text(encoding="utf-8")
HBA = (config.ROOT / "docker" / "postgres" / "pg_hba.conf").read_text(encoding="utf-8")
START = (config.ROOT / "start.bat").read_text(encoding="utf-8")
IGNORE = (config.ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def active_lines(text):
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def test_the_compose_file_holds_no_password_and_reads_it_from_a_secret_file():
    code = "\n".join(active_lines(COMPOSE))
    assert not re.search(r"POSTGRES_PASSWORD\s*:", code)  # only POSTGRES_PASSWORD_FILE
    assert "POSTGRES_PASSWORD_FILE: /run/secrets/db_bootstrap_password" in code
    assert "POSTGRES_USER: rag_bootstrap" in code and "POSTGRES_USER: rag\n" not in code + "\n"
    assert not re.search(r"\brag:rag\b|password\s*[:=]\s*rag\b", code, re.IGNORECASE)


def test_the_secret_file_comes_from_a_directory_that_must_be_named_outside_the_project():
    assert "${RAG_SECRETS_DIR:?" in COMPOSE and "./secrets" not in COMPOSE and "../" not in COMPOSE


def test_postgres_and_qdrant_listen_on_this_computer_only():
    ports = re.findall(r'^\s*-\s*"([^"]+:\d+:\d+)"', COMPOSE, re.MULTILINE)
    assert ports and all(port.startswith("127.0.0.1:") for port in ports)


def test_the_server_is_told_to_use_the_mounted_access_rules():
    assert "hba_file=/etc/postgresql/pg_hba.conf" in COMPOSE
    assert "./docker/postgres/pg_hba.conf:/etc/postgresql/pg_hba.conf:ro" in COMPOSE
    assert "no-new-privileges:true" in COMPOSE and "password_encryption=scram-sha-256" in COMPOSE


def test_the_access_rules_open_nothing_but_the_two_application_roles():
    rules = [line.split() for line in active_lines(HBA)]
    assert rules == [["local", "all", "all", "trust"],
                     ["host", "all", "rag_bootstrap", "all", "reject"],
                     ["host", "rag", "rag_app,rag_migrator", "all", "scram-sha-256"],
                     ["host", "all", "all", "all", "reject"]]
    assert "trust" not in " ".join(" ".join(r) for r in rules if r[0] == "host")


def test_the_start_script_makes_the_secrets_before_the_database_and_never_shows_one():
    assert START.index("db-secrets") < START.index("docker compose up") < START.index("db-setup")
    assert "RAG_DB_HARDENED=1" in START
    assert not re.search(r"PASSWORD", START, re.IGNORECASE) or "RAG_SECRETS_DIR" in START
    assert not re.search(r"POSTGRES_PASSWORD|db_.*_password", START)


def test_the_places_secrets_could_end_up_in_by_accident_are_ignored_by_git():
    for pattern in (".env", ".env.*", "*.secret", "secrets/"):
        assert pattern in IGNORE
