/**
 * CB-8 MonitorPanel — Lovable React component
 * 
 * Real-time monitoring dashboard for the K-9 autonomous decision pipeline.
 * Renders inside the ai-yield-whisperer Lovable app.
 * 
 * Polls the k9-llm-router /reason/monitor/* endpoints every 5 seconds.
 * Shows: circuit breaker status, active alerts, decision counts,
 * dispatch error rate, confidence baseline, and recent journal entries.
 * 
 * Usage in Lovable app:
 *   import { MonitorPanel } from '@/components/k9/MonitorPanel';
 *   <MonitorPanel routerUrl="http://localhost:8765" />
 * 
 * Or as an iframe-embeddable standalone page.
 */

import React, { useState, useEffect, useCallback } from 'react';

interface MonitorStatus {
  monitoring_active: boolean;
  total_evaluations: number;
  last_evaluation_ts: number;
  decision_counts: {
    auto_execute: number;
    human_review: number;
    reject: number;
    total: number;
  };
  dispatch_stats: {
    success: number;
    error: number;
    error_rate: number;
  };
  confidence: {
    baseline: number;
    baseline_samples: number;
    drift_threshold: number;
  };
  circuit_breaker: {
    tripped: boolean;
    consecutive_errors: number;
    trip_reason: string;
    trip_timestamp: number;
    total_trips: number;
    threshold: number;
  };
  active_alerts: number;
  total_alerts: number;
  thresholds: Record<string, number>;
}

interface Alert {
  alert_id: string;
  alert_type: string;
  level: 'info' | 'warning' | 'critical' | 'emergency';
  message: string;
  metric_value: number;
  threshold: number;
  timestamp: number;
  acknowledged: boolean;
  metadata: Record<string, any>;
}

interface JournalEntry {
  trace_id: string;
  timestamp: number;
  question: string;
  answer: string;
  confidence: number;
  iterations: number;
  task_class: string;
  decision: string;
  risk_level: string;
  handover_reason: string;
  dispatch_result: string;
  dispatch_service: string;
  dispatch_latency_ms: number;
  dispatch_error: string;
  summary: string;
}

const REFRESH_MS = 5000;

const levelColors: Record<string, string> = {
  info: '#22d3ee',
  warning: '#f59e0b',
  critical: '#f87171',
  emergency: '#f87171',
};

const badgeColors: Record<string, string> = {
  auto_execute: '#4ade80',
  human_review: '#f59e0b',
  reject: '#f87171',
};

export const MonitorPanel: React.FC<{ routerUrl?: string }> = ({
  routerUrl = 'http://localhost:8765',
}) => {
  const [status, setStatus] = useState<MonitorStatus | null>(null);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [journal, setJournal] = useState<JournalEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdate, setLastUpdate] = useState<string>('never');

  const fetchData = useCallback(async () => {
    try {
      const [statusRes, alertsRes, journalRes] = await Promise.all([
        fetch(`${routerUrl}/reason/monitor/status`).then(r => r.ok ? r.json() : null),
        fetch(`${routerUrl}/reason/monitor/alerts?limit=15`).then(r => r.ok ? r.json() : null),
        fetch(`${routerUrl}/reason/journal?limit=10`).then(r => r.ok ? r.json() : null),
      ]);

      if (statusRes) setStatus(statusRes);
      if (alertsRes?.alerts) setAlerts(alertsRes.alerts);
      if (journalRes?.entries) setJournal(journalRes.entries);
      setError(null);
      setLastUpdate(new Date().toLocaleTimeString());
    } catch (e: any) {
      setError(e?.message || 'Failed to connect to k9-llm-router');
    } finally {
      setLoading(false);
    }
  }, [routerUrl]);

  const resetCircuitBreaker = useCallback(async () => {
    try {
      await fetch(`${routerUrl}/reason/monitor/circuit-breaker/reset`, { method: 'POST' });
      setTimeout(fetchData, 500);
    } catch (e) {
      console.error('Reset failed:', e);
    }
  }, [routerUrl, fetchData]);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, REFRESH_MS);
    return () => clearInterval(interval);
  }, [fetchData]);

  if (loading) {
    return (
      <div style={{ padding: '24px', fontFamily: 'monospace', color: '#64748b', fontSize: '12px' }}>
        CB-8 Monitor — connecting to {routerUrl}…
      </div>
    );
  }

  if (error && !status) {
    return (
      <div style={{ padding: '24px', fontFamily: 'monospace', color: '#f87171', fontSize: '12px' }}>
        <div style={{ marginBottom: '8px' }}>⚠ Connection Error</div>
        <div style={{ color: '#64748b' }}>{error}</div>
        <button
          onClick={fetchData}
          style={{
            marginTop: '12px', padding: '6px 14px', fontSize: '11px',
            background: 'transparent', color: '#22d3ee', border: '1px solid #22d3ee',
            borderRadius: '3px', cursor: 'pointer', fontFamily: 'monospace',
          }}
        >
          ↻ Retry
        </button>
      </div>
    );
  }

  const dc = status?.decision_counts || {};
  const ds = status?.dispatch_stats || {};
  const cf = status?.confidence || {};
  const cb = status?.circuit_breaker || {};
  const errRate = (ds.error_rate || 0) * 100;

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
      {/* Stat Strip */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(6, 1fr)',
        gap: '8px',
      }}>
        <StatTile label="Auto-Execute" value={dc.auto_execute || 0} color="#4ade80" sub={`${dc.total || 0} total`} />
        <StatTile label="Human Review" value={dc.human_review || 0} color="#f59e0b" sub="pending" />
        <StatTile label="Rejected" value={dc.reject || 0} color="#f87171" sub="below threshold" />
        <StatTile label="Error Rate" value={`${errRate.toFixed(1)}%`} color={errRate > 30 ? '#f87171' : errRate > 10 ? '#f59e0b' : '#4ade80'} sub={`${ds.error || 0} errors`} />
        <StatTile label="Confidence" value={(cf.baseline || 0).toFixed(3)} color="#a78bfa" sub={`${cf.baseline_samples || 0} samples`} />
        <StatTile label="Alerts" value={status?.active_alerts || 0} color={status?.active_alerts ? '#f87171' : '#22d3ee'} sub={`${status?.total_alerts || 0} total`} />
      </div>

      {/* Main Grid: Circuit Breaker | Alerts | Journal */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: '1fr 1fr 1fr',
        gap: '12px',
        flex: 1,
        minHeight: '300px',
      }}>
        {/* Circuit Breaker */}
        <div style={{
          background: '#0d1117',
          border: '1px solid rgba(255,255,255,0.06)',
          borderRadius: '4px',
          padding: '14px',
          display: 'flex',
          flexDirection: 'column',
          gap: '10px',
        }}>
          <div style={{ fontSize: '9px', textTransform: 'uppercase', letterSpacing: '0.14em', color: '#334155', marginBottom: '6px' }}>
            ⬡ Circuit Breaker
          </div>
          <div style={{
            display: 'flex',
            alignItems: 'center',
            gap: '10px',
            padding: '10px',
            borderRadius: '3px',
            border: cb.tripped ? '1px solid rgba(248,113,113,0.3)' : '1px solid rgba(74,222,128,0.2)',
            background: cb.tripped ? 'rgba(248,113,113,0.1)' : 'rgba(74,222,128,0.06)',
          }}>
            <div style={{
              width: '10px', height: '10px', borderRadius: '50%',
              background: cb.tripped ? '#f87171' : '#4ade80',
              boxShadow: cb.tripped ? '0 0 8px rgba(248,113,113,0.5)' : '0 0 6px rgba(74,222,128,0.4)',
            }} />
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: '10px' }}>
                {cb.tripped ? 'TRIPPED — auto-execution halted' : 'Operational — auto-execution active'}
              </div>
              <div style={{ fontSize: '8.5px', color: '#64748b', marginTop: '2px' }}>
                {cb.tripped ? cb.trip_reason : `Consecutive errors: ${cb.consecutive_errors || 0}`}
              </div>
            </div>
          </div>
          {cb.tripped && (
            <button
              onClick={resetCircuitBreaker}
              style={{
                fontSize: '9px', padding: '5px 10px',
                border: '1px solid rgba(248,113,113,0.4)',
                borderRadius: '3px', background: 'rgba(248,113,113,0.1)',
                color: '#f87171', cursor: 'pointer',
                textTransform: 'uppercase', letterSpacing: '0.08em',
              }}
            >
              ↻ Reset Circuit Breaker
            </button>
          )}
          <div style={{ marginTop: 'auto', fontSize: '9px', color: '#64748b', lineHeight: 1.6 }}>
            <div>Consecutive errors: <span style={{ color: '#e2e8f0' }}>{cb.consecutive_errors || 0}</span></div>
            <div>Threshold: <span style={{ color: '#e2e8f0' }}>{cb.threshold || 10}</span></div>
            <div>Total trips: <span style={{ color: '#e2e8f0' }}>{cb.total_trips || 0}</span></div>
          </div>
        </div>

        {/* Active Alerts */}
        <div style={{
          background: '#0d1117',
          border: '1px solid rgba(255,255,255,0.06)',
          borderRadius: '4px',
          padding: '14px',
          display: 'flex',
          flexDirection: 'column',
          gap: '6px',
          overflow: 'hidden',
        }}>
          <div style={{ fontSize: '9px', textTransform: 'uppercase', letterSpacing: '0.14em', color: '#334155', marginBottom: '6px' }}>
            ⚠ Active Alerts
          </div>
          <div style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '4px' }}>
            {alerts.length === 0 ? (
              <div style={{ color: '#334155', fontSize: '9px', textAlign: 'center', padding: '20px 0' }}>
                No alerts — system nominal
              </div>
            ) : alerts.map((a) => (
              <div key={a.alert_id} style={{
                display: 'flex', alignItems: 'flex-start', gap: '8px',
                padding: '7px 9px', borderRadius: '3px',
                borderLeft: `2px solid ${levelColors[a.level] || '#334155'}`,
                background: '#111820', fontSize: '9.5px', lineHeight: 1.4,
              }}>
                <span style={{
                  fontSize: '8px', textTransform: 'uppercase', letterSpacing: '0.08em',
                  padding: '1px 5px', borderRadius: '2px', flexShrink: 0,
                  background: `${levelColors[a.level] || '#334155'}22`,
                  color: levelColors[a.level] || '#64748b',
                }}>
                  {a.level}
                </span>
                <div style={{ flex: 1 }}>
                  <div style={{ color: '#e2e8f0' }}>{a.message}</div>
                  <div style={{ color: '#334155', fontSize: '8px', marginTop: '2px' }}>
                    {a.alert_type.replace(/_/g, ' ')}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Decision Journal */}
        <div style={{
          background: '#0d1117',
          border: '1px solid rgba(255,255,255,0.06)',
          borderRadius: '4px',
          padding: '14px',
          display: 'flex',
          flexDirection: 'column',
          gap: '6px',
          overflow: 'hidden',
        }}>
          <div style={{ fontSize: '9px', textTransform: 'uppercase', letterSpacing: '0.14em', color: '#334155', marginBottom: '6px' }}>
            ⊛ Decision Journal
          </div>
          <div style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '3px' }}>
            {journal.length === 0 ? (
              <div style={{ color: '#334155', fontSize: '9px', textAlign: 'center', padding: '20px 0' }}>
                No decisions recorded yet
              </div>
            ) : journal.map((e) => (
              <div key={e.trace_id} style={{
                display: 'flex', alignItems: 'center', gap: '6px',
                padding: '5px 8px', background: '#111820',
                borderRadius: '2px', fontSize: '9px',
              }}>
                <span style={{
                  fontSize: '7.5px', padding: '1px 4px', borderRadius: '2px',
                  textTransform: 'uppercase', letterSpacing: '0.06em', flexShrink: 0,
                  background: `${badgeColors[e.decision] || '#334155'}22`,
                  color: badgeColors[e.decision] || '#64748b',
                }}>
                  {e.decision === 'auto_execute' ? 'auto' : e.decision === 'human_review' ? 'human' : 'reject'}
                </span>
                <span style={{
                  color: '#64748b', flex: 1,
                  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                }}>
                  {(e.question || '').substring(0, 40)}
                  {e.dispatch_result === 'executed' && e.dispatch_service ? ` → ${e.dispatch_service}` : ''}
                </span>
                <span style={{ fontSize: '8px', color: '#334155', flexShrink: 0 }}>
                  conf={(e.confidence || 0).toFixed(2)}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Footer */}
      <div style={{
        fontSize: '9px', color: '#334155',
        display: 'flex', alignItems: 'center', gap: '16px',
        borderTop: '1px solid rgba(255,255,255,0.06)',
        paddingTop: '8px',
      }}>
        <span>CB-8 Monitoring · poll 5s</span>
        <span>Last update: {lastUpdate}</span>
        {error && <span style={{ color: '#f87171' }}>⚠ {error}</span>}
      </div>
    </div>
  );
};

const StatTile: React.FC<{ label: string; value: any; color: string; sub?: string }> = ({
  label, value, color, sub,
}) => (
  <div style={{
    background: '#0d1117',
    border: '1px solid rgba(255,255,255,0.06)',
    borderRadius: '4px',
    padding: '10px 12px',
    display: 'flex',
    flexDirection: 'column',
    gap: '3px',
  }}>
    <div style={{ fontSize: '8.5px', textTransform: 'uppercase', letterSpacing: '0.12em', color: '#334155' }}>
      {label}
    </div>
    <div style={{ fontFamily: "'Syne', sans-serif", fontWeight: 700, fontSize: '18px', color }}>
      {value}
    </div>
    {sub && <div style={{ fontSize: '8.5px', color: '#64748b' }}>{sub}</div>}
  </div>
);

export default MonitorPanel;
