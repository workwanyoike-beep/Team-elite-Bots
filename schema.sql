-- ============================================================
-- AUTO-SUPERVISOR WORKFORCE ECOSYSTEM - DATABASE SCHEMA
-- Run this in your Supabase SQL editor
-- ============================================================

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- TABLE: workers
-- ============================================================
CREATE TABLE workers (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  telegram_username TEXT NOT NULL UNIQUE,
  telegram_chat_id  BIGINT UNIQUE,
  mpesa_number      TEXT,
  mpesa_name        TEXT,
  created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- TABLE: pcs
-- ============================================================
CREATE TABLE pcs (
  id        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  hwid      TEXT NOT NULL UNIQUE,         -- Hardware ID of the PC
  label     TEXT,                          -- e.g. "PC-01", "PC-02"
  status    TEXT NOT NULL DEFAULT 'vacant'
              CHECK (status IN ('vacant','occupied')),
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- TABLE: shifts
-- ============================================================
CREATE TABLE shifts (
  id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  worker_id    UUID NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
  pc_id        UUID NOT NULL REFERENCES pcs(id) ON DELETE CASCADE,
  start_time   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  end_time     TIMESTAMPTZ,
  password_pin TEXT NOT NULL,              -- 6-digit PIN hashed with bcrypt
  status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','completed','denied')),
  created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- Prevent two active shifts on same PC (enforced by partial unique index)
CREATE UNIQUE INDEX one_active_shift_per_pc
  ON shifts (pc_id)
  WHERE status = 'active';

-- Prevent a worker from having two active shifts
CREATE UNIQUE INDEX one_active_shift_per_worker
  ON shifts (worker_id)
  WHERE status = 'active';

-- ============================================================
-- TABLE: performance_logs
-- ============================================================
CREATE TABLE performance_logs (
  id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  worker_id          UUID NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
  shift_id           UUID NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
  -- Stats at shift start
  start_sent         INTEGER NOT NULL DEFAULT 0,
  start_received     INTEGER NOT NULL DEFAULT 0,
  -- Stats at shift end
  end_sent           INTEGER,
  end_received       INTEGER,
  -- Calculated: ((end_received - start_received) / (end_sent - start_sent)) * 100
  final_percentage   NUMERIC(5,2),
  -- Rolling 15-min nudge data (JSONB array of {ts, score})
  rolling_scores     JSONB DEFAULT '[]',
  created_at         TIMESTAMPTZ DEFAULT NOW(),
  updated_at         TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- TABLE: unlock_signals
-- For Supabase Realtime — bot writes here, desktop client listens
-- ============================================================
CREATE TABLE unlock_signals (
  id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  pc_hwid     TEXT NOT NULL,
  worker_id   UUID NOT NULL REFERENCES workers(id),
  shift_id    UUID NOT NULL REFERENCES shifts(id),
  action      TEXT NOT NULL CHECK (action IN ('unlock','lock','deny')),
  reason      TEXT,
  consumed    BOOLEAN DEFAULT FALSE,
  created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- ROW LEVEL SECURITY (RLS)
-- Workers can only see their OWN data
-- ============================================================

ALTER TABLE workers         ENABLE ROW LEVEL SECURITY;
ALTER TABLE shifts          ENABLE ROW LEVEL SECURITY;
ALTER TABLE performance_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE pcs             ENABLE ROW LEVEL SECURITY;
ALTER TABLE unlock_signals  ENABLE ROW LEVEL SECURITY;

-- Workers can read/update only their own row
CREATE POLICY "workers_self_read"
  ON workers FOR SELECT
  USING (telegram_chat_id = (current_setting('request.jwt.claims', true)::json->>'chat_id')::bigint);

CREATE POLICY "workers_self_update"
  ON workers FOR UPDATE
  USING (telegram_chat_id = (current_setting('request.jwt.claims', true)::json->>'chat_id')::bigint);

-- Workers see only their own shifts
CREATE POLICY "shifts_self"
  ON shifts FOR SELECT
  USING (worker_id = (
    SELECT id FROM workers
    WHERE telegram_chat_id = (current_setting('request.jwt.claims', true)::json->>'chat_id')::bigint
  ));

-- Workers see only their own performance logs
CREATE POLICY "perf_self"
  ON performance_logs FOR SELECT
  USING (worker_id = (
    SELECT id FROM workers
    WHERE telegram_chat_id = (current_setting('request.jwt.claims', true)::json->>'chat_id')::bigint
  ));

-- Service role (bot/server) bypasses RLS — set service_role key on server side
-- PCs: only service role can read/write
CREATE POLICY "pcs_service_only" ON pcs FOR ALL USING (FALSE);
CREATE POLICY "signals_service_only" ON unlock_signals FOR ALL USING (FALSE);

-- ============================================================
-- REALTIME: enable for unlock_signals table
-- ============================================================
ALTER PUBLICATION supabase_realtime ADD TABLE unlock_signals;

-- ============================================================
-- INDEXES for performance
-- ============================================================
CREATE INDEX idx_shifts_worker   ON shifts(worker_id);
CREATE INDEX idx_shifts_pc       ON shifts(pc_id);
CREATE INDEX idx_perf_worker     ON performance_logs(worker_id);
CREATE INDEX idx_perf_shift      ON performance_logs(shift_id);
CREATE INDEX idx_signals_hwid    ON unlock_signals(pc_hwid, consumed);

-- ============================================================
-- FUNCTION: auto-update updated_at
-- ============================================================
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_perf_updated_at
  BEFORE UPDATE ON performance_logs
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
