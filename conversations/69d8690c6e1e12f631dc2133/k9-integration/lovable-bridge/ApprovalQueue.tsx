/**
 * ApprovalQueue — Lovable React component
 * 
 * CB-9 Human-in-the-Loop Approval Queue for the K-9 autonomous decision pipeline.
 * Renders inside the ai-yield-whisperer Lovable app.
 * 
 * Two modes:
 *   1. DIRECT — polls k9-llm-router /reason/approvals directly (homelab network)
 *   2. SUPABASE — reads pending approvals from cb_messages table (cloud/remote)
 * 
 * In SUPABASE mode, approve/reject actions POST to an edge function that writes
 * the decision back to cb_messages. The k9-llm-router polls cb_messages for
 * decision updates and processes them.
 * 
 * Usage in Lovable app:
 *   import { ApprovalQueue } from '@/components/k9/ApprovalQueue';
 *   <ApprovalQueue mode="supabase" />
 *   // or
 *   <ApprovalQueue mode="direct" routerUrl="http://localhost:8765" />
 */

import React, { useState, useEffect, useCallback } from 'react';

// ── Types ────────────────────────────────────────────────────────────────────

interface ApprovalTask {
  approval_id: string;
  trace_id: string;
  timestamp: number;
  question: string;
  answer: string;
  confidence: number;
  task_class: string;
  risk_level: string;
  handover_reason: string;
  reasoning_trace: string[];
  evidence: string[];
  state: string;
  approved_by: string;
  approved_at: number;
  note: string;
  dispatch_result: string;
  dispatch_service: string;
  dispatch_latency_ms: number;
  dispatch_error: string;
  created_at: string;
  expires_at: string;
}

interface ApprovalStats {
  total: number;
  pending: number;
  approved: number;
  rejected: number;
  expired: number;
  executed: number;
  execution_failed: number;
  approval_rate: number;
  avg_ttl_seconds: number;
}

// ── Configuration ─────────────────────────────────────────────────────────────

const REFRESH_MS = 5000;

const stateColors: Record<string, string> = {
  pending: '#f59e0b',
  approved: '#4ade80',
  rejected: '#f87171',
  expired: '#64748b',
  executed: '#22d3ee',
  execution_failed: '#f87171',
};

const riskColors: Record<string, string> = {
  read_only: '#4ade80',
  idempotent: '#22d3ee',
  state_changing: '#f59e0b',
  high_risk: '#f87171',
};

// ── Component ──────────────────────────────────────────────────────────────────

export const ApprovalQueue: React.FC<{
  mode?: 'direct' | 'supabase';
  routerUrl?: string;
  supabaseUrl?: string;
  supabaseAnonKey?: string;
  edgeFunctionUrl?: string;
  integrationKey?: string;
}> = ({
  mode = 'supabase',
  routerUrl = 'http://localhost:8765',
  supabaseUrl,
  supabaseAnonKey,
  edgeFunctionUrl,
  integrationKey,
}) => {
  const [pending, setPending] = useState<ApprovalTask[]>([]);
  const [recent, setRecent] = useState<ApprovalTask[]>([]);
  const [stats, setStats] = useState<ApprovalStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdate, setLastUpdate] = useState<string>('never');
  const [actingId, setActingId] = useState<string | null>(null);
  const [showRecent, setShowRecent] = useState(false);

  // ── Direct mode: poll k9-llm-router ──────────────────────────────────────
  const fetchDirect = useCallback(async () => {
    try {
      const [pendingRes, statsRes] = await Promise.all([
        fetch(`${routerUrl}/reason/approvals?limit=20`).then(r => r.ok ? r.json() : null),
        fetch(`${routerUrl}/reason/approvals/stats`).then(r => r.ok ? r.json() : null),
      ]);

      if (pendingRes?.pending) setPending(pendingRes.pending);
      if (statsRes) setStats(statsRes);
      setError(null);
    } catch (e: any) {
      setError(e?.message || 'Failed to connect to k9-llm-router');
    } finally {
      setLoading(false);
      setLastUpdate(new Date().toLocaleTimeString());
    }
  }, [routerUrl]);

  const fetchRecentDirect = useCallback(async () => {
    try {
      const res = await fetch(`${routerUrl}/reason/approvals/recent?limit=30`).then(r => r.ok ? r.json() : null);
      if (res?.tasks) setRecent(res.tasks);
    } catch (e) {
      // silent
    }
  }, [routerUrl]);

  // ── Supabase mode: read from cb_messages ─────────────────────────────────
  const fetchSupabase = useCallback(async () => {
    const sbUrl = supabaseUrl || (typeof window !== 'undefined' 
      ? (window as any).__SUPABASE_URL || localStorage.getItem('k9_supabase_url') || ''
      : '');
    const sbKey = supabaseAnonKey || (typeof window !== 'undefined'
      ? (window as any).__SUPABASE_ANON_KEY || localStorage.getItem('k9_supabase_anon_key') || ''
      : '');

    if (!sbUrl || !sbKey) {
      setError('Supabase URL and anon key required. Set via props or localStorage.');
      setLoading(false);
      return;
    }

    try {
      // Query cb_messages for approval events from k9-approval source
      const url = `${sbUrl}/rest/v1/cb_messages?` + new URLSearchParams({
        select: '*',
        'source': 'eq.k9-approval',
        order: 'created_at.desc',
        limit: '50',
      });

      const res = await fetch(url, {
        headers: {
          'apikey': sbKey,
          'Authorization': `Bearer ${sbKey}`,
          'Content-Type': 'application/json',
        },
      });

      if (!res.ok) throw new Error(`Supabase query failed: ${res.status}`);
      const rows = await res.json();

      // Parse rows into ApprovalTask shape
      const parseRow = (r: any): ApprovalTask => ({
        approval_id: r.payload?.approval_id || r.message_id || r.id,
        trace_id: r.trace_id || '',
        timestamp: new Date(r.created_at).getTime() / 1000,
        question: r.payload?.question || '',
        answer: r.payload?.answer || '',
        confidence: r.confidence || 0,
        task_class: r.ontology_tags?.[0] || 'unknown',
        risk_level: r.ontology_tags?.[1] || 'unknown',
        handover_reason: r.payload?.handover_reason || '',
        reasoning_trace: r.payload?.reasoning_trace || [],
        evidence: r.payload?.evidence || [],
        state: r.payload?.state || 'pending',
        approved_by: r.payload?.approved_by || '',
        approved_at: r.payload?.approved_at || 0,
        note: r.payload?.note || '',
        dispatch_result: r.payload?.dispatch_result || '',
        dispatch_service: r.payload?.dispatch_service || '',
        dispatch_latency_ms: r.payload?.dispatch_latency_ms || 0,
        dispatch_error: r.payload?.dispatch_error || '',
        created_at: r.created_at || '',
        expires_at: r.payload?.expires_at || '',
      });

      const allTasks = rows.map(parseRow);
      setPending(allTasks.filter(t => t.state === 'pending'));
      setRecent(allTasks.filter(t => t.state !== 'pending').slice(0, 20));

      // Compute stats
      const states = allTasks.map(t => t.state);
      const statCount = (s: string) => states.filter(x => x === s).length;
      setStats({
        total: allTasks.length,
        pending: statCount('pending'),
        approved: statCount('approved'),
        rejected: statCount('rejected'),
        expired: statCount('expired'),
        executed: statCount('executed'),
        execution_failed: statCount('execution_failed'),
        approval_rate: allTasks.length > 0 
          ? (statCount('approved') + statCount('executed')) / allTasks.length 
          : 0,
        avg_ttl_seconds: 3600,
      });

      setError(null);
    } catch (e: any) {
      setError(e?.message || 'Failed to query Supabase');
    } finally {
      setLoading(false);
      setLastUpdate(new Date().toLocaleTimeString());
    }
  }, [supabaseUrl, supabaseAnonKey]);

  // ── Actions ──────────────────────────────────────────────────────────────
  const handleApprove = useCallback(async (taskId: string) => {
    setActingId(taskId);
    try {
      if (mode === 'direct') {
        await fetch(`${routerUrl}/reason/approvals/${taskId}/approve?approved_by=lovable-ui`, { method: 'POST' });
      } else {
        const efUrl = edgeFunctionUrl || `${supabaseUrl || ''}/functions/v1/approval-bridge`;
        await fetch(efUrl, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...(integrationKey ? { 'x-integration-key': integrationKey } : {}),
          },
          body: JSON.stringify({
            approval_id: taskId,
            action: 'approve',
            approved_by: 'lovable-ui',
          }),
        });
      }
      setTimeout(mode === 'direct' ? fetchDirect : fetchSupabase, 500);
    } catch (e: any) {
      setError(e?.message || 'Approve failed');
    } finally {
      setActingId(null);
    }
  }, [mode, routerUrl, edgeFunctionUrl, supabaseUrl, integrationKey, fetchDirect, fetchSupabase]);

  const handleReject = useCallback(async (taskId: string, note?: string) => {
    setActingId(taskId);
    try {
      if (mode === 'direct') {
        const url = `${routerUrl}/reason/approvals/${taskId}/reject?approved_by=lovable-ui&note=${encodeURIComponent(note || '')}`;
        await fetch(url, { method: 'POST' });
      } else {
        const efUrl = edgeFunctionUrl || `${supabaseUrl || ''}/functions/v1/approval-bridge`;
        await fetch(efUrl, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...(integrationKey ? { 'x-integration-key': integrationKey } : {}),
          },
          body: JSON.stringify({
            approval_id: taskId,
            action: 'reject',
            approved_by: 'lovable-ui',
            note: note || '',
          }),
        });
      }
      setTimeout(mode === 'direct' ? fetchDirect : fetchSupabase, 500);
    } catch (e: any) {
      setError(e?.message || 'Reject failed');
    } finally {
      setActingId(null);
    }
  }, [mode, routerUrl, edgeFunctionUrl, supabaseUrl, integrationKey, fetchDirect, fetchSupabase]);

  // ── Polling ───────────────────────────────────────────────────────────────
  const fetchData = mode === 'direct' ? fetchDirect : fetchSupabase;

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, REFRESH_MS);
    return () => clearInterval(interval);
  }, [fetchData]);

  useEffect(() => {
    if (showRecent && mode === 'direct') {
      fetchRecentDirect();
    }
  }, [showRecent, mode, fetchRecentDirect]);

  // ── Render ───────────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div style={{ padding: '24px', fontFamily: 'monospace', color: '#64748b', fontSize: '12px' }}>
        CB-9 Approval Queue — connecting ({mode})…
      </div>
    );
  }

  return (
    <div style={{
      fontFamily: "'JetBrains Mono', monospace",
      fontSize: '12px',
      color: '#e2e8f0',
      background: '#080c10',
      padding: '16px',
      minHeight: '100%',
      display: 'flex',
      flexDirection: 'column',
      gap: '12px',
    }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span style={{ fontSize: '14px', fontWeight: 600 }}>⊘ CB-9 Approval Queue</span>
          <span style={{
            padding: '2px 8px', borderRadius: '3px', fontSize: '10px',
            background: pending.length > 0 ? 'rgba(245,158,11,0.15)' : 'rgba(74,222,128,0.15)',
            color: pending.length > 0 ? '#f59e0b' : '#4ade80',
          }}>
            {pending.length} pending
          </span>
        </div>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <span style={{ color: '#64748b', fontSize: '10px' }}>↻ {lastUpdate}</span>
          <button
            onClick={() => setShowRecent(!showRecent)}
            style={{
              padding: '3px 10px', fontSize: '10px',
              background: showRecent ? 'rgba(34,211,238,0.1)' : 'transparent',
              color: '#22d3ee', border: '1px solid rgba(34,211,238,0.3)',
              borderRadius: '3px', cursor: 'pointer', fontFamily: 'monospace',
            }}
          >
            {showRecent ? '⬡ Pending' : '⏱ History'}
          </button>
        </div>
      </div>

      {/* Stats Strip */}
      {stats && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: '8px' }}>
          <StatTile label="Total" value={stats.total} color="#22d3ee" />
          <StatTile label="Pending" value={stats.pending} color="#f59e0b" />
          <StatTile label="Approved" value={stats.approved} color="#4ade80" />
          <StatTile label="Rejected" value={stats.rejected} color="#f87171" />
          <StatTile label="Executed" value={stats.executed} color="#22d3ee" />
          <StatTile
            label="Approval Rate"
            value={`${(stats.approval_rate * 100).toFixed(0)}%`}
            color={stats.approval_rate > 0.7 ? '#4ade80' : '#f59e0b'}
          />
        </div>
      )}

      {/* Error Banner */}
      {error && (
        <div style={{
          padding: '8px 12px', background: 'rgba(248,113,113,0.1)',
          border: '1px solid rgba(248,113,113,0.3)', borderRadius: '4px',
          color: '#f87171', fontSize: '11px',
        }}>
          ⚠ {error}
        </div>
      )}

      {/* Task List */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', flex: 1 }}>
        {(showRecent ? recent : pending).length === 0 ? (
          <div style={{
            padding: '32px', textAlign: 'center', color: '#64748b', fontSize: '12px',
          }}>
            {showRecent ? 'No recent approvals' : '✓ No pending approvals — system is autonomous'}
          </div>
        ) : (
          (showRecent ? recent : pending).map((task) => (
            <ApprovalCard
              key={task.approval_id}
              task={task}
              onApprove={() => handleApprove(task.approval_id)}
              onReject={(note) => handleReject(task.approval_id, note)}
              acting={actingId === task.approval_id}
              showActions={!showRecent}
            />
          ))
        )}
      </div>
    </div>
  );
};

// ── Sub-components ────────────────────────────────────────────────────────────

const StatTile: React.FC<{ label: string; value: React.ReactNode; color: string; sub?: string }> = ({
  label, value, color, sub,
}) => (
  <div style={{
    background: 'rgba(15,20,28,0.8)', border: '1px solid rgba(255,255,255,0.06)',
    borderRadius: '4px', padding: '8px 10px',
  }}>
    <div style={{ color: '#64748b', fontSize: '9px', textTransform: 'uppercase', letterSpacing: '0.5px' }}>{label}</div>
    <div style={{ color, fontSize: '18px', fontWeight: 600, marginTop: '2px' }}>{value}</div>
    {sub && <div style={{ color: '#475569', fontSize: '9px', marginTop: '1px' }}>{sub}</div>}
  </div>
);

const ApprovalCard: React.FC<{
  task: ApprovalTask;
  onApprove: () => void;
  onReject: (note?: string) => void;
  acting: boolean;
  showActions: boolean;
}> = ({ task, onApprove, onReject, acting, showActions }) => {
  const [expanded, setExpanded] = useState(false);
  const [rejectNote, setRejectNote] = useState('');
  const [showReject, setShowReject] = useState(false);

  const conf = (task.confidence * 100).toFixed(0);
  const ageSec = Math.floor((Date.now() / 1000) - task.timestamp);
  const ageStr = ageSec < 60 ? `${ageSec}s` : ageSec < 3600 ? `${Math.floor(ageSec / 60)}m` : `${Math.floor(ageSec / 3600)}h`;
  const stateColor = stateColors[task.state] || '#64748b';
  const riskColor = riskColors[task.risk_level] || '#64748b';

  return (
    <div style={{
      background: 'rgba(15,20,28,0.9)',
      border: `1px solid ${showActions ? 'rgba(245,158,11,0.2)' : 'rgba(255,255,255,0.06)'}`,
      borderRadius: '6px', padding: '12px',
      cursor: 'pointer',
    }} onClick={() => setExpanded(!expanded)}>
      {/* Top row: badges + age */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' }}>
        <span style={{
          padding: '2px 8px', borderRadius: '3px', fontSize: '9px', fontWeight: 600,
          background: `${stateColor}20`, color: stateColor, textTransform: 'uppercase',
        }}>
          {task.state}
        </span>
        <span style={{
          padding: '1px 6px', borderRadius: '3px', fontSize: '9px',
          background: `${riskColor}15`, color: riskColor,
        }}>
          {task.risk_level}
        </span>
        <span style={{ color: '#64748b', fontSize: '10px' }}>{task.task_class}</span>
        <span style={{ marginLeft: 'auto', color: '#475569', fontSize: '10px' }}>{ageStr} ago</span>
      </div>

      {/* Question + Answer */}
      <div style={{ marginBottom: '4px' }}>
        <span style={{ color: '#94a3b8', fontSize: '10px' }}>Q: </span>
        <span style={{ color: '#e2e8f0' }}>{task.question}</span>
      </div>
      <div style={{ marginBottom: '6px' }}>
        <span style={{ color: '#94a3b8', fontSize: '10px' }}>A: </span>
        <span style={{ color: '#cbd5e1' }}>{task.answer.slice(0, 120)}{task.answer.length > 120 ? '…' : ''}</span>
      </div>

      {/* Confidence + handover reason */}
      <div style={{ display: 'flex', gap: '12px', fontSize: '10px', color: '#64748b' }}>
        <span>conf: <span style={{ color: '#a78bfa' }}>{conf}%</span></span>
        <span>reason: <span style={{ color: '#94a3b8' }}>{task.handover_reason}</span></span>
        {task.dispatch_result && (
          <span>dispatch: <span style={{ color: stateColors[task.dispatch_result] || '#64748b' }}>{task.dispatch_result}</span></span>
        )}
      </div>

      {/* Expanded detail */}
      {expanded && (
        <div style={{ marginTop: '8px', paddingTop: '8px', borderTop: '1px solid rgba(255,255,255,0.06)' }}>
          {task.reasoning_trace.length > 0 && (
            <div style={{ marginBottom: '6px' }}>
              <div style={{ color: '#64748b', fontSize: '9px', textTransform: 'uppercase', marginBottom: '3px' }}>Reasoning Trace</div>
              {task.reasoning_trace.map((r, i) => (
                <div key={i} style={{ color: '#94a3b8', fontSize: '11px', marginLeft: '8px', marginBottom: '2px' }}>→ {r}</div>
              ))}
            </div>
          )}
          <div style={{ display: 'flex', gap: '12px', fontSize: '10px', color: '#475569' }}>
            <span>trace: {task.trace_id.slice(0, 8)}</span>
            <span>id: {task.approval_id.slice(0, 8)}</span>
            {task.approved_by && <span>by: {task.approved_by}</span>}
            {task.dispatch_error && <span style={{ color: '#f87171' }}>err: {task.dispatch_error}</span>}
          </div>
        </div>
      )}

      {/* Action buttons */}
      {showActions && (
        <div style={{ marginTop: '8px', display: 'flex', gap: '8px' }} onClick={(e) => e.stopPropagation()}>
          {!showReject ? (
            <>
              <button
                onClick={onApprove}
                disabled={acting}
                style={{
                  padding: '5px 16px', fontSize: '11px', fontWeight: 600,
                  background: acting ? 'rgba(74,222,128,0.1)' : 'rgba(74,222,128,0.15)',
                  color: '#4ade80', border: '1px solid rgba(74,222,128,0.3)',
                  borderRadius: '3px', cursor: acting ? 'wait' : 'pointer', fontFamily: 'monospace',
                }}
              >
                {acting ? '…' : '✓ Approve & Dispatch'}
              </button>
              <button
                onClick={() => setShowReject(true)}
                disabled={acting}
                style={{
                  padding: '5px 16px', fontSize: '11px',
                  background: 'rgba(248,113,113,0.1)',
                  color: '#f87171', border: '1px solid rgba(248,113,113,0.3)',
                  borderRadius: '3px', cursor: 'pointer', fontFamily: 'monospace',
                }}
              >
                ✕ Reject
              </button>
            </>
          ) : (
            <div style={{ display: 'flex', gap: '6px', width: '100%' }}>
              <input
                value={rejectNote}
                onChange={(e) => setRejectNote(e.target.value)}
                placeholder="Rejection reason (optional)"
                style={{
                  flex: 1, padding: '4px 8px', fontSize: '11px',
                  background: 'rgba(15,20,28,0.8)', color: '#e2e8f0',
                  border: '1px solid rgba(255,255,255,0.1)', borderRadius: '3px',
                  fontFamily: 'monospace',
                }}
              />
              <button
                onClick={() => { onReject(rejectNote); setShowReject(false); setRejectNote(''); }}
                disabled={acting}
                style={{
                  padding: '4px 12px', fontSize: '11px',
                  background: 'rgba(248,113,113,0.15)', color: '#f87171',
                  border: '1px solid rgba(248,113,113,0.3)', borderRadius: '3px',
                  cursor: 'pointer', fontFamily: 'monospace',
                }}
              >
                Confirm Reject
              </button>
              <button
                onClick={() => setShowReject(false)}
                style={{
                  padding: '4px 10px', fontSize: '11px',
                  background: 'transparent', color: '#64748b',
                  border: '1px solid rgba(255,255,255,0.1)', borderRadius: '3px',
                  cursor: 'pointer', fontFamily: 'monospace',
                }}
              >
                Cancel
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default ApprovalQueue;
