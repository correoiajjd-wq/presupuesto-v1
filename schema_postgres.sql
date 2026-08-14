-- Esquema PostgreSQL — Sistema de Presupuestación V1
-- Traducción del modelo conceptual del doc 03 §39/§40.
--
-- Decisiones:
--   * INPUT y CALCULATED viven en tablas distintas: un valor calculado nunca
--     puede ser editado ni confundido con una carga (doc 03 §40).
--   * La versión guarda un snapshot de configuración: una versión histórica no
--     depende de la configuración vigente (doc 03 §45).
--   * audit_event es append-only; no hay UPDATE ni DELETE sobre esa tabla.
--   * La inmutabilidad de una versión aprobada se defiende en la base con un
--     trigger, no sólo en la aplicación (doc 04 §1.5).

CREATE TYPE version_status  AS ENUM ('DRAFT','IN_REVIEW','APPROVED','REJECTED');
CREATE TYPE config_status   AS ENUM ('DRAFT','IN_REVIEW','APPROVED','LOCKED');
CREATE TYPE task_status     AS ENUM ('NOT_STARTED','DRAFT','SUBMITTED','IN_REVIEW','APPROVED','REJECTED');
CREATE TYPE input_status    AS ENUM ('DRAFT','SUBMITTED','IN_REVIEW','APPROVED','REJECTED');
CREATE TYPE input_source    AS ENUM ('MANUAL','IMPORT','SCENARIO');
CREATE TYPE alert_status    AS ENUM ('PENDING','RESOLVED','ACCEPTED');

-- ---------------------------------------------------------------- identidad
CREATE TABLE organization (
    id              UUID PRIMARY KEY,
    name            TEXT NOT NULL
);

CREATE TABLE app_user (
    id              UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organization(id),
    email           TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    active          BOOLEAN NOT NULL DEFAULT TRUE
);

-- Autorización declarativa: capacidades + alcance, sin roles hardcodeados
-- en el código de negocio (doc 03 §3/§46).
CREATE TABLE capability_grant (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES app_user(id),
    capability      TEXT NOT NULL,            -- budget.expense.load, budget.approve, ...
    scope_type      TEXT NOT NULL,            -- COMPANY | BUSINESS_UNIT | BRANCH | OPERATION | SUPPORT_UNIT | COST_CENTER
    scope_id        TEXT,                     -- NULL = transversal
    -- El alcance sobre un contenedor alcanza a lo contenido: quien tiene la
    -- sucursal tiene sus operaciones; quien tiene la unidad, las suyas.
    UNIQUE (user_id, capability, scope_type, scope_id)
);

-- ------------------------------------------------------------- presupuesto
CREATE TABLE budget (
    id                  UUID PRIMARY KEY,
    organization_id     UUID NOT NULL REFERENCES organization(id),
    name                TEXT NOT NULL,
    fiscal_year_start   DATE NOT NULL,
    fiscal_year_end     DATE NOT NULL,
    presentation_currency CHAR(3) NOT NULL,
    current_version_id  UUID,                 -- puntero, no estado (doc 04 §53)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (fiscal_year_end > fiscal_year_start)
);

CREATE TABLE budget_version (
    id                  UUID PRIMARY KEY,
    budget_id           UUID NOT NULL REFERENCES budget(id),
    number              INTEGER NOT NULL,
    status              version_status NOT NULL DEFAULT 'DRAFT',
    config_status       config_status  NOT NULL DEFAULT 'DRAFT',
    source_version_id   UUID REFERENCES budget_version(id),
    configuration_snapshot JSONB NOT NULL,    -- contrato del modelo, congelado
    created_by          UUID NOT NULL REFERENCES app_user(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_by         UUID REFERENCES app_user(id),
    approved_at         TIMESTAMPTZ,
    row_version         INTEGER NOT NULL DEFAULT 1,  -- optimistic locking (doc 04 §56)
    UNIQUE (budget_id, number)
);
ALTER TABLE budget ADD CONSTRAINT fk_current_version
    FOREIGN KEY (current_version_id) REFERENCES budget_version(id);

-- Tipos de cambio: se congelan con la versión (doc 02 §30)
CREATE TABLE fx_rate (
    version_id      UUID NOT NULL REFERENCES budget_version(id),
    currency        CHAR(3) NOT NULL,
    rate_date       DATE NOT NULL,
    rate_to_presentation NUMERIC(20,10) NOT NULL,
    PRIMARY KEY (version_id, currency, rate_date),
    CHECK (rate_to_presentation > 0)
);

-- ------------------------------------------------------------------ inputs
CREATE TABLE input_value (
    id              BIGSERIAL PRIMARY KEY,
    version_id      UUID NOT NULL REFERENCES budget_version(id),
    concept         TEXT NOT NULL,            -- SALES_QTY, EXPENSE_AMOUNT, ...
    -- OPERATION es la combinación unidad x sucursal: la unidad mínima del
    -- presupuesto. Ventas y dotación se cargan siempre ahí.
    scope_type      TEXT NOT NULL,            -- COMPANY | BUSINESS_UNIT | BRANCH | OPERATION | SUPPORT_UNIT | COST_CENTER
    scope_id        TEXT,
    product_id      TEXT,
    family_id       TEXT,
    expense_id      TEXT,
    area_id         TEXT,
    capex_category_id TEXT,
    balance_item_id TEXT,
    period          CHAR(7),                  -- 'YYYY-MM', período de carga
    effective_date  DATE,
    change_type     TEXT,
    amount          NUMERIC(20,4) NOT NULL,
    currency        CHAR(3),
    status          input_status NOT NULL DEFAULT 'DRAFT',
    source          input_source NOT NULL DEFAULT 'MANUAL',
    import_batch_id UUID,
    loaded_by       UUID REFERENCES app_user(id),
    loaded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    comment         TEXT,
    UNIQUE (version_id, concept, scope_type, scope_id, product_id, family_id,
            expense_id, area_id, capex_category_id, balance_item_id, period,
            effective_date, change_type)
);
CREATE INDEX ix_input_version_concept ON input_value (version_id, concept);
CREATE INDEX ix_input_scope ON input_value (version_id, scope_type, scope_id);

-- ------------------------------------------------------------- calculados
-- Tabla separada, sin columna editable por usuario. Guarda las dependencias
-- que produjeron el valor, para poder explicarlo y recalcular sólo lo afectado.
CREATE TABLE calculated_value (
    id              BIGSERIAL PRIMARY KEY,
    version_id      UUID NOT NULL REFERENCES budget_version(id),
    scenario_id     UUID,                     -- NULL = presupuesto base
    node_key        TEXT NOT NULL,            -- METRICA|AMBITO|PERIODO
    metric          TEXT NOT NULL,
    scope_key       TEXT NOT NULL,
    period          CHAR(7) NOT NULL,         -- o 'FY'
    amount          NUMERIC(20,6),            -- NULL = no calculable (≠ 0)
    currency        CHAR(3) NOT NULL,
    depends_on      TEXT[] NOT NULL DEFAULT '{}',
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (version_id, scenario_id, node_key)
);
CREATE INDEX ix_calc_lookup ON calculated_value (version_id, metric, scope_key, period);

-- ---------------------------------------------------------------- workflow
CREATE TABLE task (
    id              UUID PRIMARY KEY,
    version_id      UUID NOT NULL REFERENCES budget_version(id),
    concept         TEXT NOT NULL,
    scope_type      TEXT NOT NULL,
    scope_id        TEXT,
    label           TEXT NOT NULL,
    status          task_status NOT NULL DEFAULT 'NOT_STARTED',
    assignee_id     UUID REFERENCES app_user(id),
    due_date        DATE
);

CREATE TABLE task_transition (
    id              BIGSERIAL PRIMARY KEY,
    task_id         UUID NOT NULL REFERENCES task(id),
    from_status     task_status,
    to_status       task_status NOT NULL,
    actor_id        UUID REFERENCES app_user(id),
    comment         TEXT,
    at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE approval (
    id              UUID PRIMARY KEY,
    version_id      UUID NOT NULL REFERENCES budget_version(id),
    task_id         UUID REFERENCES task(id),
    approver_id     UUID NOT NULL REFERENCES app_user(id),
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    invalidated_at  TIMESTAMPTZ,
    invalidated_reason TEXT
);

-- ----------------------------------------------------------------- alertas
CREATE TABLE alert (
    id              UUID PRIMARY KEY,
    version_id      UUID NOT NULL REFERENCES budget_version(id),
    code            TEXT NOT NULL,
    message         TEXT NOT NULL,
    entity          TEXT,
    status          alert_status NOT NULL DEFAULT 'PENDING',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_by     UUID REFERENCES app_user(id),
    resolved_at     TIMESTAMPTZ,
    comment         TEXT
);

-- --------------------------------------------------------------- escenarios
CREATE TABLE scenario (
    id              UUID PRIMARY KEY,
    version_id      UUID NOT NULL REFERENCES budget_version(id),
    name            TEXT NOT NULL,
    created_by      UUID NOT NULL REFERENCES app_user(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (version_id, name)
);

CREATE TABLE scenario_adjustment (
    id              BIGSERIAL PRIMARY KEY,
    scenario_id     UUID NOT NULL REFERENCES scenario(id) ON DELETE CASCADE,
    concept         TEXT NOT NULL,            -- siempre un INPUT, nunca un calculado
    scope_type      TEXT NOT NULL,
    scope_id        TEXT,
    variation_type  TEXT NOT NULL,            -- PERCENTAGE | ABSOLUTE
    variation       NUMERIC(20,6) NOT NULL
);

-- -------------------------------------------------------------- importación
CREATE TABLE import_batch (
    id              UUID PRIMARY KEY,
    version_id      UUID NOT NULL REFERENCES budget_version(id),
    import_type     TEXT NOT NULL,
    file_uri        TEXT NOT NULL,            -- el archivo va a object storage, no acá
    status          TEXT NOT NULL,            -- PENDING | COMMITTED | REJECTED
    idempotency_key TEXT UNIQUE,              -- doc 04 §37
    rows_committed  INTEGER NOT NULL DEFAULT 0,
    created_by      UUID NOT NULL REFERENCES app_user(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE import_error (
    id              BIGSERIAL PRIMARY KEY,
    batch_id        UUID NOT NULL REFERENCES import_batch(id) ON DELETE CASCADE,
    row_number      INTEGER NOT NULL,
    column_name     TEXT NOT NULL,
    raw_value       TEXT,
    error           TEXT NOT NULL,
    expected        TEXT NOT NULL
);

-- --------------------------------------------------------------- auditoría
CREATE TABLE audit_event (
    id              BIGSERIAL PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organization(id),
    version_id      UUID REFERENCES budget_version(id),
    actor_id        UUID REFERENCES app_user(id),
    action          TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    before_value    TEXT,
    after_value     TEXT,
    comment         TEXT,
    correlation_id  UUID,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_lookup ON audit_event (version_id, entity_type, occurred_at DESC);

REVOKE UPDATE, DELETE ON audit_event FROM PUBLIC;

-- ------------------------------------------- inmutabilidad de versión aprobada
CREATE OR REPLACE FUNCTION assert_version_mutable() RETURNS trigger AS $$
DECLARE st version_status;
BEGIN
    SELECT status INTO st FROM budget_version
     WHERE id = COALESCE(NEW.version_id, OLD.version_id);
    IF st = 'APPROVED' THEN
        RAISE EXCEPTION 'VERSION_IMMUTABLE: la versión aprobada no puede modificarse'
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_input_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON input_value
    FOR EACH ROW EXECUTE FUNCTION assert_version_mutable();

CREATE TRIGGER trg_fx_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON fx_rate
    FOR EACH ROW EXECUTE FUNCTION assert_version_mutable();
