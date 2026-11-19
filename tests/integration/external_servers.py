"""Real external database servers in throw-away Docker containers, for the connector's integration tests: PostgreSQL
(several versions, with and without TLS) and SQL Server (with and without TLS). Everything is removed again by
`stop()`.

The same sample schema goes into both kinds: two schemas, a composite primary key, a composite foreign key, a table
without a primary key, a unique constraint, an index, a view, and a row of "private" data that must never reach the
application's database.
"""

import base64
import datetime
import ipaddress
import subprocess
import time
import uuid

from tests import stack as stack_tools

# a value in an external table; it must never be copied into the internal database
SECRET_ROW = "hidden-row-value-xyz-9137"
ADMIN_PASSWORD = "Adm1n!-Passw0rd#x9Zq"
READER_PASSWORD = "Rd!Str0ng#Pass42-qq"
WRITER_PASSWORD = "Wr!Str0ng#Pass42-qq"

PG_SAMPLE = f"""
CREATE SCHEMA sales; CREATE SCHEMA hr;
CREATE TABLE sales.customer (id int PRIMARY KEY, name text NOT NULL, secret_note text, balance numeric(12,2));
INSERT INTO sales.customer VALUES (1, 'Hidden Customer Name', '{SECRET_ROW}', 10.5);
CREATE TABLE sales.order_line (order_id int, line_no int, customer_id int NOT NULL REFERENCES sales.customer(id),
    qty int NOT NULL, PRIMARY KEY (order_id, line_no));
CREATE TABLE sales.order_note (order_id int, line_no int, text varchar(200),
    FOREIGN KEY (order_id, line_no) REFERENCES sales.order_line(order_id, line_no));
CREATE TABLE hr.employee (id int PRIMARY KEY, email text NOT NULL UNIQUE);
CREATE TABLE hr.no_pk (a int, b timestamptz);
CREATE VIEW sales.big_orders AS SELECT order_id, qty FROM sales.order_line WHERE qty > 10;
CREATE MATERIALIZED VIEW sales.order_totals AS SELECT customer_id, sum(qty) AS total FROM sales.order_line GROUP BY 1;
CREATE TABLE sales.events (id int, at date) PARTITION BY RANGE (at);
CREATE TABLE sales.events_2026 PARTITION OF sales.events FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
CREATE INDEX ix_order_line_customer ON sales.order_line (customer_id);
CREATE ROLE reader LOGIN PASSWORD '{READER_PASSWORD}';
GRANT USAGE ON SCHEMA sales, hr TO reader;
GRANT SELECT ON ALL TABLES IN SCHEMA sales, hr TO reader;
CREATE ROLE writer LOGIN PASSWORD '{WRITER_PASSWORD}';
GRANT USAGE, CREATE ON SCHEMA sales TO writer;
GRANT ALL ON ALL TABLES IN SCHEMA sales TO writer;
"""

MSSQL_SAMPLE = f"""
CREATE DATABASE rag_ext;
GO
USE rag_ext;
GO
CREATE SCHEMA sales;
GO
CREATE SCHEMA hr;
GO
CREATE TABLE sales.customer (id INT NOT NULL PRIMARY KEY, name NVARCHAR(100) NOT NULL, secret_note VARCHAR(MAX) NULL,
    balance DECIMAL(12,2) NULL);
CREATE TABLE sales.order_line (order_id INT NOT NULL, line_no INT NOT NULL, customer_id INT NOT NULL, qty INT NOT NULL,
    CONSTRAINT pk_order_line PRIMARY KEY (order_id, line_no),
    CONSTRAINT fk_ol_customer FOREIGN KEY (customer_id) REFERENCES sales.customer(id));
CREATE TABLE sales.order_note (order_id INT NOT NULL, line_no INT NOT NULL, text NVARCHAR(200),
    CONSTRAINT fk_note_line FOREIGN KEY (order_id, line_no) REFERENCES sales.order_line(order_id, line_no));
CREATE TABLE hr.employee (id INT NOT NULL, email NVARCHAR(100) NOT NULL, CONSTRAINT pk_emp PRIMARY KEY (id),
    CONSTRAINT uq_emp_email UNIQUE (email));
CREATE TABLE hr.no_pk (a INT, b DATETIME2(3));
CREATE INDEX ix_order_line_customer ON sales.order_line (customer_id);
GO
CREATE VIEW sales.big_orders AS SELECT order_id, qty FROM sales.order_line WHERE qty > 10;
GO
INSERT INTO sales.customer VALUES (1, N'Hidden Customer Name', '{SECRET_ROW}', 10.5);
GO
CREATE LOGIN reader WITH PASSWORD = '{READER_PASSWORD}', CHECK_POLICY = OFF;
CREATE USER reader FOR LOGIN reader;
ALTER ROLE db_datareader ADD MEMBER reader;
CREATE LOGIN writer WITH PASSWORD = '{WRITER_PASSWORD}', CHECK_POLICY = OFF;
CREATE USER writer FOR LOGIN writer;
ALTER ROLE db_owner ADD MEMBER writer;
CREATE LOGIN blind WITH PASSWORD = '{READER_PASSWORD}', CHECK_POLICY = OFF;
CREATE USER blind FOR LOGIN blind;
GO
"""


def docker(*args, check=True, input_text=None):
    done = subprocess.run(["docker", *args], capture_output=True, text=True, input=input_text)
    if check and done.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {done.stderr.strip()[:300]}")
    return done.stdout.strip()


def make_certificates(names=("localhost",), addresses=("127.0.0.1",)):
    """(CA certificate PEM, server certificate PEM, server key PEM): a CA of its own and a certificate it signed."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    def key():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key, server_key = key(), key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rag test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    alt = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(a)) for a in addresses]
    server = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    return (
        ca.public_bytes(pem).decode(),
        server.public_bytes(pem).decode(),
        server_key.private_bytes(
            pem, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        ).decode(),
    )


class Postgres:
    def __init__(self, image="postgres:17-alpine", tls=None):
        self.image, self.tls = image, tls  # tls: (certificate PEM, key PEM) or None
        self.name = f"ragext-pg-{uuid.uuid4().hex[:8]}"
        self.port = stack_tools.free_port()

    def start(self):
        env = ["-e", f"POSTGRES_PASSWORD={ADMIN_PASSWORD}", "-e", "POSTGRES_DB=ext"]
        if self.tls:
            certificate, key = self.tls
            command = (
                "echo $CRT | base64 -d > /var/lib/postgresql/server.crt; echo $KEY | base64 -d > "
                "/var/lib/postgresql/server.key; chown postgres:postgres /var/lib/postgresql/server.*; "
                "chmod 600 /var/lib/postgresql/server.key; exec docker-entrypoint.sh postgres -c ssl=on "
                "-c ssl_cert_file=/var/lib/postgresql/server.crt -c ssl_key_file=/var/lib/postgresql/server.key"
            )
            docker(
                "run",
                "-d",
                "--rm",
                "--name",
                self.name,
                *env,
                "-e",
                f"CRT={base64.b64encode(certificate.encode()).decode()}",
                "-e",
                f"KEY={base64.b64encode(key.encode()).decode()}",
                "-p",
                f"127.0.0.1:{self.port}:5432",
                "--entrypoint",
                "sh",
                self.image,
                "-c",
                command,
            )
        else:
            docker("run", "-d", "--rm", "--name", self.name, *env, "-p", f"127.0.0.1:{self.port}:5432", self.image)
        deadline = time.time() + 90
        while time.time() < deadline:
            probe = subprocess.run(
                ["docker", "exec", self.name, "psql", "-U", "postgres", "-d", "ext", "-tAc", "SELECT 1"],
                capture_output=True,
                text=True,
            )
            if probe.returncode == 0 and probe.stdout.strip() == "1":
                break
            time.sleep(1)
        else:
            self.stop()
            raise RuntimeError("PostgreSQL did not start")
        self.sql(PG_SAMPLE)
        return self

    def sql(self, text):
        docker(
            "exec",
            "-i",
            self.name,
            "psql",
            "-U",
            "postgres",
            "-d",
            "ext",
            "-v",
            "ON_ERROR_STOP=1",
            "-q",
            input_text=text,
        )

    def scalar(self, text):
        return docker("exec", self.name, "psql", "-U", "postgres", "-d", "ext", "-tAc", text)

    def checksum(self):
        """Numbers that change when any user data does: a count and a hash of every row of every user table."""
        return self.scalar(
            "SELECT (SELECT count(*) FROM sales.customer) || '/' || (SELECT count(*) FROM sales.order_line)"
            " || '/' || (SELECT count(*) FROM hr.employee) || '/' || (SELECT md5(string_agg(name || "
            "coalesce(secret_note, ''), ',' ORDER BY id)) FROM sales.customer)"
        )

    def stop(self):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)


class SqlServer:
    IMAGE = "mcr.microsoft.com/mssql/server:2022-latest"

    def __init__(self):
        self.name = f"ragext-mssql-{uuid.uuid4().hex[:8]}"
        self.port = stack_tools.free_port()

    def sqlcmd(self, *args, text=None):
        return docker(
            "exec",
            "-i",
            self.name,
            "/opt/mssql-tools18/bin/sqlcmd",
            "-C",
            "-S",
            "localhost",
            "-U",
            "sa",
            "-P",
            ADMIN_PASSWORD,
            *args,
            check=False,
            input_text=text,
        )

    def _wait(self, seconds=150):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if "1" in self.sqlcmd("-h", "-1", "-Q", "SET NOCOUNT ON; SELECT 1"):
                return
            time.sleep(2)
        raise RuntimeError("SQL Server did not start")

    def start(self):
        docker(
            "run",
            "-d",
            "--name",
            self.name,
            "-e",
            "ACCEPT_EULA=Y",
            "-e",
            f"MSSQL_SA_PASSWORD={ADMIN_PASSWORD}",
            "-p",
            f"127.0.0.1:{self.port}:1433",
            self.IMAGE,
        )
        try:
            self._wait()
            self.script(MSSQL_SAMPLE)
        except Exception:
            self.stop()
            raise
        return self

    def script(self, text):
        out = self.sqlcmd("-b", text=text)
        if "Msg " in out:
            raise RuntimeError(f"SQL Server script failed: {out[:300]}")

    def scalar(self, query):
        return self.sqlcmd("-d", "rag_ext", "-h", "-1", "-W", "-Q", "SET NOCOUNT ON; " + query).strip().splitlines()[0]

    def checksum(self):
        return self.scalar(
            "SELECT CAST((SELECT count(*) FROM sales.customer) AS varchar(10)) + '/' + CAST((SELECT "
            "count(*) FROM sales.order_line) AS varchar(10)) + '/' + CONVERT(varchar(40), HASHBYTES("
            "'MD5', (SELECT name + ISNULL(secret_note, '') FROM sales.customer FOR XML PATH(''))), 2)"
        )

    def force_tls(self, certificate, key):
        """From now on the server REQUIRES encryption and presents `certificate` (restarts it)."""
        docker("exec", "-u", "0", self.name, "sh", "-c", "mkdir -p /var/opt/mssql/certs")
        for path, content, mode in (("server.crt", certificate, "644"), ("server.key", key, "600")):
            docker(
                "exec",
                "-u",
                "0",
                "-i",
                self.name,
                "sh",
                "-c",
                f"cat > /var/opt/mssql/certs/{path} && chown mssql:root /var/opt/mssql/certs/{path} && chmod {mode} "
                f"/var/opt/mssql/certs/{path}",
                input_text=content,
            )
        conf = (
            "[network]\ntlscert = /var/opt/mssql/certs/server.crt\ntlskey = /var/opt/mssql/certs/server.key\n"
            "tlsprotocols = 1.2\nforceencryption = 1\n"
        )
        docker(
            "exec",
            "-u",
            "0",
            "-i",
            self.name,
            "sh",
            "-c",
            "cat > /var/opt/mssql/mssql.conf && chown mssql:root /var/opt/mssql/mssql.conf",
            input_text=conf,
        )
        docker("restart", self.name)
        self._wait()
        deadline = time.time() + 90
        while time.time() < deadline:  # the server answers before the user databases have been recovered
            state = "SET NOCOUNT ON; SELECT state_desc FROM sys.databases WHERE name = 'rag_ext'"
            if "ONLINE" in self.sqlcmd("-h", "-1", "-W", "-Q", state) and self._reader_can_log_in():
                return
            time.sleep(2)
        raise RuntimeError("the sample database did not come back online")

    def _reader_can_log_in(self):
        """A login of the sample database works (it may lag behind the database being ONLINE after a restart)."""
        out = docker(
            "exec",
            "-i",
            self.name,
            "/opt/mssql-tools18/bin/sqlcmd",
            "-C",
            "-S",
            "localhost",
            "-U",
            "reader",
            "-P",
            READER_PASSWORD,
            "-d",
            "rag_ext",
            "-h",
            "-1",
            "-Q",
            "SET NOCOUNT ON; SELECT 1",
            check=False,
        )
        return out.strip().startswith("1")

    def stop(self):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
