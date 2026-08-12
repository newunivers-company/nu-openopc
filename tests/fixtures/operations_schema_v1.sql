CREATE TABLE operations_schema (
    component TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO operations_schema(component, version, updated_at)
VALUES ('operating_kernel', 1, '2026-01-01T00:00:00+00:00');

CREATE TABLE goal_contracts (
    goal_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    organization_id TEXT DEFAULT '',
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO goal_contracts(
    goal_id, project_id, organization_id, title, status, version,
    payload, created_at, updated_at
) VALUES (
    'legacy-goal',
    'default',
    '',
    'Legacy goal',
    'active',
    1,
    '{"acceptance_criteria":[{"criterion_id":"legacy","description":"Survives migration"}],"created_at":"2026-01-01T00:00:00+00:00","goal_id":"legacy-goal","objective":"Prove v1 to v2 migration","organization_id":"","project_id":"default","schema_version":1,"status":"active","title":"Legacy goal","updated_at":"2026-01-01T00:00:00+00:00","version":1}',
    '2026-01-01T00:00:00+00:00',
    '2026-01-01T00:00:00+00:00'
);
