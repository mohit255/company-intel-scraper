"""
PostgreSQL connection + schema for the company intelligence data.

Connection comes from the DATABASE_URL env var, defaulting to the local
company_intel database. Point it at RDS/Aurora in production:
    export DATABASE_URL=postgresql://user:pass@host:5432/company_intel
"""

import os
from email.utils import parsedate_to_datetime

import psycopg
from psycopg.rows import dict_row

DB_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/company_intel")

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    name    TEXT PRIMARY KEY,
    field   TEXT,
    website TEXT
);

CREATE TABLE IF NOT EXISTS news (
    id         BIGSERIAL PRIMARY KEY,
    company    TEXT NOT NULL,
    field      TEXT NOT NULL,
    title      TEXT NOT NULL,
    link       TEXT NOT NULL UNIQUE,
    source     TEXT,
    published  TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    search     tsvector GENERATED ALWAYS AS
               (to_tsvector('english', coalesce(title, ''))) STORED
);
CREATE INDEX IF NOT EXISTS news_field_idx     ON news (field);
CREATE INDEX IF NOT EXISTS news_company_idx   ON news (company);
CREATE INDEX IF NOT EXISTS news_published_idx ON news (published DESC);
CREATE INDEX IF NOT EXISTS news_search_idx    ON news USING GIN (search);

CREATE TABLE IF NOT EXISTS jobs (
    id         BIGSERIAL PRIMARY KEY,
    company    TEXT NOT NULL,
    field      TEXT NOT NULL,
    title      TEXT NOT NULL,
    location   TEXT,
    url        TEXT NOT NULL UNIQUE,
    posted_at  TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_field_idx    ON jobs (field);
CREATE INDEX IF NOT EXISTS jobs_company_idx  ON jobs (company);
CREATE INDEX IF NOT EXISTS jobs_location_idx ON jobs (location);

CREATE TABLE IF NOT EXISTS products (
    id           BIGSERIAL PRIMARY KEY,
    company      TEXT NOT NULL,
    field        TEXT NOT NULL,
    url          TEXT NOT NULL UNIQUE,
    page_title   TEXT,
    prices       JSONB NOT NULL DEFAULT '[]',
    text_snippet TEXT,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS products_field_idx   ON products (field);
CREATE INDEX IF NOT EXISTS products_company_idx ON products (company);

ALTER TABLE news      ADD COLUMN IF NOT EXISTS source_url TEXT;
ALTER TABLE products  ADD COLUMN IF NOT EXISTS image      TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS image      TEXT;
"""


def connect():
    return psycopg.connect(DB_URL, row_factory=dict_row)


def init_db(conn):
    conn.execute(SCHEMA)
    conn.commit()


def parse_pubdate(value):
    """RSS pubDate (RFC 2822) -> datetime, or None if unparseable."""
    try:
        return parsedate_to_datetime(value)
    except Exception:
        return None
