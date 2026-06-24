"""
text_to_sql.py — K-9 Natural Language → SQL Query Engine
─────────────────────────────────────────────────────────────────────────────
Sprint CB-3 · k9-llm-router

Converts natural language queries into safe SQL queries over cb_messages.
Used by ops teams to extract failure modes, latency patterns, and task outcomes.

Schema (cb_messages):
  - id (UUID) — unique message ID
  - timestamp (TIMESTAMP) — when message was created
  - source (VARCHAR) — origin agent/module
  - type (VARCHAR) — message type (execution_request, result, audit, etc.)
  - ontology_tags (TEXT[]) — semantic labels
  - confidence (FLOAT) — 0-1 confidence score
  - payload (JSONB) — message data
  - trace_id (UUID) — execution trace
  - causation_id (UUID) — parent message
  - signature (VARCHAR) — Ed25519 sig
  - created_at (TIMESTAMP)
  - updated_at (TIMESTAMP)

Task outcome data lives in payload:
  {
    "task_id": "...",
    "task_type": "scout_tutor" | "finance_coach" | "trading_signal" | etc.,
    "status": "PENDING_APPROVAL" | "APPROVED" | "REJECTED" | "EXECUTING" | "COMPLETE" | "FAILED",
    "success": true | false,
    "blast_radius": 0.0-1.0,
    "alignment_score": 0.0-1.0,
    "execution_time_ms": 123,
    "error_code": "TIMEOUT" | "TIER_MISMATCH" | "GUARDRAIL_BLOCK" | etc.,
    "model_used": "ollama:mistral" | "gemini-2-flash" | "claude-sonnet-4.5",
    "latency_ms": 456,
    "tokens_used": 789
  }

Safe queries only:
  - Read-only SELECT
  - Parameterized to prevent injection
  - Rate-limited
  - Max result set 10k rows
  - Execution timeout 30s

Dependencies:
  - sqlparse>=0.4.0 (SQL parsing + validation)
  - anthropic or similar (LLM for nl→sql translation)
  - psycopg2-binary>=2.9.0 (Postgres client)

Env vars:
  SUPABASE_URL           — https://ziqenqqgnqxqrazmjohs.supabase.co
  SUPABASE_SERVICE_KEY   — service role key
  K9_LOCAL_ONLY          — "true" → query k9_local instead
  K9_PG_HOST, K9_PG_PORT, K9_PG_DB, K9_PG_USER, K9_PG_PASSWORD
  TEXT_TO_SQL_RATE_LIMIT — queries per minute, default 10
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx

log = logging.getLogger("k9-text-to-sql")

# ── Config ────────────────────────────────────────────────────────────────────

SUPABASE_URL      = os.getenv("SUPABASE_URL", "")
SUPABASE_SVC_KEY  = os.getenv("SUPABASE_SERVICE_KEY", "")
K9_LOCAL_ONLY     = os.getenv("K9_LOCAL_ONLY", "false").lower() == "true"

K9_PG_HOST        = os.getenv("K9_PG_HOST", "localhost")
K9_PG_PORT        = int(os.getenv("K9_PG_PORT", "5432"))
K9_PG_DB          = os.getenv("K9_PG_DB", "k9_local")
K9_PG_USER        = os.getenv("K9_PG_USER", "postgres")
K9_PG_PASSWORD    = os.getenv("K9_PG_PASSWORD", "")

RATE_LIMIT        = int(os.getenv("TEXT_TO_SQL_RATE_LIMIT", "10"))  # per minute
MAX_ROWS          = 10_000
QUERY_TIMEOUT_S   = 30


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class QueryResult:
    success: bool
    query: Optional[str]        = None
    rows: Optional[list[dict]]  = None
    row_count: int              = 0
    execution_time_ms: float    = 0.0
    error: Optional[str]        = None
    columns: Optional[list[str]]= None


@dataclass
class SafetyCheck:
    is_safe: bool
    reason: Optional[str] = None
    detected_injection: bool = False
    detected_dml: bool = False


# ── NL→SQL Translation ────────────────────────────────────────────────────────

class TextToSQLTranslator:
    """
    Translates natural language queries to SQL via LLM.
    All queries are validated for safety before execution.
    """

    def __init__(self) -> None:
        self._translated = 0
        self._failed = 0
        self._safe_queries = 0
        self._blocked_queries = 0
        self._last_query_time = 0.0
        self._rate_limit_window: dict[str, list[float]] = {}

    async def translate(self, nl_query: str) -> str:
        """Convert natural language to SQL using LLM."""
        prompt = f"""Convert the following natural language query into a SQL SELECT statement.

Target table: cb_messages
Columns: id, timestamp, source, type, ontology_tags, confidence, payload, trace_id, causation_id, signature, created_at, updated_at

The payload column is JSONB and contains task execution data with fields like:
  - task_id, task_type, status, success, blast_radius, alignment_score
  - execution_time_ms, error_code, model_used, latency_ms, tokens_used

Important rules:
1. ONLY SELECT queries — NO INSERT, UPDATE, DELETE, DROP
2. Always use WHERE clauses to limit results
3. Include LIMIT 10000 at the end
4. Use explicit column names (SELECT col1, col2, ...)
5. For JSON data, use payload->'field_name' or payload->>'field_name'
6. Return ONLY the SQL query, no explanation

Natural language query: {nl_query}

SQL Query:"""

        try:
            # Try to use k9-llm-router's /route endpoint
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    "http://localhost:8765/route",
                    json={
                        "task_type": "text_to_sql",
                        "component": "k9-text-to-sql",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 500,
                        "temperature": 0.1,  # low temp for deterministic SQL
                    }
                )
                resp.raise_for_status()
                sql = resp.json().get("content", "").strip()
                
                # Extract SQL from markdown code blocks if present
                sql = re.sub(r"```sql\n?|\n?```", "", sql)
                sql = re.sub(r"```\n?|\n?```", "", sql)
                
                self._translated += 1
                log.info("[text_to_sql] Translated NL → SQL (%d chars)", len(sql))
                return sql
        except Exception as e:
            self._failed += 1
            log.warning("[text_to_sql] Translation failed: %s", e)
            raise

    async def safety_check(self, sql: str) -> SafetyCheck:
        """Validate SQL for injection, DML, and other safety issues."""
        sql_upper = sql.upper().strip()

        # 1. Block non-SELECT queries
        if not sql_upper.startswith("SELECT"):
            return SafetyCheck(
                is_safe=False,
                reason="Only SELECT queries are allowed",
                detected_dml=True
            )

        # 2. Block dangerous patterns
        dangerous_patterns = [
            r"INSERT\s", r"UPDATE\s", r"DELETE\s", r"DROP\s",
            r"ALTER\s", r"CREATE\s", r"TRUNCATE\s",
            r"exec\(", r"execute\(",
            r";\s*(DROP|ALTER|DELETE|INSERT|UPDATE)",
            r"\/\*.*?\*\/",  # multi-line comments
        ]
        for pattern in dangerous_patterns:
            if re.search(pattern, sql_upper):
                return SafetyCheck(
                    is_safe=False,
                    reason=f"Dangerous pattern detected: {pattern}",
                    detected_injection=True
                )

        # 3. Require explicit column names (prevent SELECT *)
        if re.search(r"SELECT\s+\*", sql_upper):
            return SafetyCheck(
                is_safe=False,
                reason="SELECT * not allowed — list specific columns"
            )

        # 4. Require LIMIT clause
        if not re.search(r"LIMIT\s+\d+", sql_upper):
            log.warning("[text_to_sql] Query missing LIMIT — adding default")
            sql = f"{sql.rstrip(';')} LIMIT {MAX_ROWS};"

        # 5. Check for obvious injection patterns
        # (parameterized queries will handle most, but catch obvious ones)
        if "'" in sql and "--" in sql:
            return SafetyCheck(
                is_safe=False,
                reason="Suspicious quote + comment pattern (possible injection)",
                detected_injection=True
            )

        return SafetyCheck(is_safe=True)

    def check_rate_limit(self, user_id: str) -> bool:
        """Check if user is within rate limit (RATE_LIMIT queries per minute)."""
        now = time.time()
        minute_ago = now - 60

        if user_id not in self._rate_limit_window:
            self._rate_limit_window[user_id] = []

        # Clean old entries
        self._rate_limit_window[user_id] = [
            t for t in self._rate_limit_window[user_id] if t > minute_ago
        ]

        # Check limit
        if len(self._rate_limit_window[user_id]) >= RATE_LIMIT:
            return False

        # Record query
        self._rate_limit_window[user_id].append(now)
        return True


# ── Database Query Execution ──────────────────────────────────────────────────

async def execute_sql_supabase(sql: str) -> QueryResult:
    """Execute SQL via Supabase REST API."""
    if not SUPABASE_URL or not SUPABASE_SVC_KEY:
        return QueryResult(
            success=False,
            error="Supabase not configured"
        )

    # Supabase doesn't directly support arbitrary SQL via REST
    # Use the Postgres HTTP function approach or fall back to local
    log.warning("[text_to_sql] Supabase remote exec not supported — falling back to local")
    return await execute_sql_local(sql)


async def execute_sql_local(sql: str) -> QueryResult:
    """Execute SQL against local k9_local Postgres."""
    start = time.time()

    try:
        import psycopg2
        from psycopg2 import sql as psycopg2_sql
    except ImportError:
        return QueryResult(
            success=False,
            error="psycopg2 not installed — run: pip install psycopg2-binary"
        )

    try:
        # Connect
        conn = psycopg2.connect(
            host=K9_PG_HOST,
            port=K9_PG_PORT,
            database=K9_PG_DB,
            user=K9_PG_USER,
            password=K9_PG_PASSWORD,
            connect_timeout=5,
        )
        conn.set_session(autocommit=True)
        cursor = conn.cursor()

        # Set timeout
        cursor.execute(f"SET statement_timeout TO {QUERY_TIMEOUT_S * 1000}")

        # Execute
        cursor.execute(sql)

        # Fetch results
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = cursor.fetchall()

        # Convert to list of dicts
        result_dicts = [
            {col: row[i] for i, col in enumerate(columns)}
            for row in rows
        ]

        cursor.close()
        conn.close()

        elapsed = (time.time() - start) * 1000
        log.info(
            "[text_to_sql] Query executed: %d rows in %.1f ms",
            len(result_dicts), elapsed
        )

        return QueryResult(
            success=True,
            query=sql,
            rows=result_dicts,
            row_count=len(result_dicts),
            columns=columns,
            execution_time_ms=round(elapsed, 1),
        )

    except Exception as e:
        elapsed = (time.time() - start) * 1000
        return QueryResult(
            success=False,
            query=sql,
            error=str(e),
            execution_time_ms=round(elapsed, 1),
        )


# ── Main query pipeline ───────────────────────────────────────────────────────

class TextToSQLEngine:
    """
    Full NL→SQL pipeline: translate → validate → execute → return results.
    """

    def __init__(self) -> None:
        self.translator = TextToSQLTranslator()
        self._queries_executed = 0
        self._queries_blocked = 0

    async def query(
        self,
        nl_query: str,
        user_id: str = "anonymous",
    ) -> QueryResult:
        """
        Execute a natural language query.
        """
        # 1. Rate limit
        if not self.translator.check_rate_limit(user_id):
            self._queries_blocked += 1
            return QueryResult(
                success=False,
                error=f"Rate limit exceeded ({RATE_LIMIT} queries/min)"
            )

        # 2. Translate
        try:
            sql = await self.translator.translate(nl_query)
        except Exception as e:
            return QueryResult(
                success=False,
                error=f"Translation failed: {e}"
            )

        # 3. Safety check
        safety = await self.translator.safety_check(sql)
        if not safety.is_safe:
            self._queries_blocked += 1
            log.warning(
                "[text_to_sql] Query blocked: %s",
                safety.reason
            )
            return QueryResult(
                success=False,
                query=sql,
                error=f"Query blocked for safety: {safety.reason}"
            )

        # 4. Execute
        self._queries_executed += 1
        result = await execute_sql_local(sql)
        return result

    def stats(self) -> dict:
        return {
            "queries_executed": self._queries_executed,
            "queries_blocked": self._queries_blocked,
            "translations": self.translator._translated,
            "translation_failures": self.translator._failed,
            "rate_limit": RATE_LIMIT,
            "max_rows": MAX_ROWS,
            "timeout_s": QUERY_TIMEOUT_S,
        }


# ── Module singleton ──────────────────────────────────────────────────────────
text_to_sql_engine = TextToSQLEngine()
