-- ============================================================
-- K-9 Cognitive Bus — cb_messages table
-- Sprint: Orbitron-CB-1
-- Run in Supabase SQL Editor → or via: supabase db push
-- ============================================================

-- 1. Create table
CREATE TABLE IF NOT EXISTS public.cb_messages (
  id               BIGSERIAL PRIMARY KEY,
  message_id       UUID        NOT NULL UNIQUE,
  trace_id         UUID        NOT NULL,
  causation_id     UUID,
  source           TEXT        NOT NULL,
  target           JSONB,                    -- string or string[]
  type             TEXT        NOT NULL,
  ontology_tags    TEXT[]      NOT NULL DEFAULT '{}',
  confidence       FLOAT       NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  payload          JSONB       NOT NULL DEFAULT '{}',
  "timestamp"      TIMESTAMPTZ NOT NULL,
  signature        TEXT,
  aeg_proof_hash   TEXT,
  alignment_score  INT         CHECK (alignment_score >= 0 AND alignment_score <= 10000),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  user_id          UUID        REFERENCES auth.users(id) ON DELETE CASCADE
);

-- 2. Indexes
CREATE INDEX IF NOT EXISTS idx_cb_messages_trace    ON public.cb_messages (trace_id);
CREATE INDEX IF NOT EXISTS idx_cb_messages_type     ON public.cb_messages (type);
CREATE INDEX IF NOT EXISTS idx_cb_messages_source   ON public.cb_messages (source);
CREATE INDEX IF NOT EXISTS idx_cb_messages_ts       ON public.cb_messages ("timestamp" DESC);
CREATE INDEX IF NOT EXISTS idx_cb_messages_tags     ON public.cb_messages USING GIN (ontology_tags);
CREATE INDEX IF NOT EXISTS idx_cb_messages_user     ON public.cb_messages (user_id);

-- 3. Enable RLS
ALTER TABLE public.cb_messages ENABLE ROW LEVEL SECURITY;

-- 4. RLS Policies
-- Users see only their own messages
CREATE POLICY "Users read own cb_messages"
  ON public.cb_messages
  FOR SELECT
  USING (auth.uid() = user_id);

-- Users insert their own messages
CREATE POLICY "Users insert own cb_messages"
  ON public.cb_messages
  FOR INSERT
  WITH CHECK (auth.uid() = user_id OR user_id IS NULL);

-- Admin role sees all (for audit / governance)
CREATE POLICY "Admin reads all cb_messages"
  ON public.cb_messages
  FOR SELECT
  USING (auth.jwt() ->> 'role' = 'admin');

-- Service role bypasses RLS (used by edge functions)
-- (service role key already bypasses RLS by default in Supabase)

-- 5. Enable realtime publication
ALTER PUBLICATION supabase_realtime ADD TABLE public.cb_messages;

-- 6. Grant permissions
GRANT SELECT, INSERT ON public.cb_messages TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.cb_messages TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.cb_messages_id_seq TO authenticated;

-- ============================================================
-- Verification query
-- ============================================================
-- SELECT table_name, rls_enabled
-- FROM information_schema.tables t
-- JOIN pg_class c ON c.relname = t.table_name
-- WHERE t.table_name = 'cb_messages';
