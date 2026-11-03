CREATE TABLE tenants (
    id   INTEGER PRIMARY KEY,
    name VARCHAR(80) NOT NULL UNIQUE
);

CREATE TABLE sessions (
    id         INTEGER PRIMARY KEY,
    tenant_id  INTEGER NOT NULL REFERENCES tenants (id),
    state      VARCHAR(16) NOT NULL DEFAULT 'queued',
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE results (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions (id),
    path       TEXT NOT NULL
);

CREATE INDEX idx_sessions_tenant ON sessions (tenant_id, created_at);
