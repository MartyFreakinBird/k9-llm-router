/**
 * SecurityPanel — Lovable React component
 * 
 * CB-9 Security Reinforcement UI for the K-9 autonomous decision pipeline.
 * Renders inside the ai-yield-whisperer Lovable app.
 * 
 * Shows:
 *   - Kill switch status + activate/deactivate
 *   - Behavioral anomaly detection (EMA baselines, z-score anomalies)
 *   - Egress allowlist (permitted services + rate limits + remove)
 *   - Forensic capture (suspicious activity records)
 *   - Security posture summary (signatures, observations, halted state)
 * 
 * Two modes:
 *   1. DIRECT — polls k9-llm-router /reason/security/* directly (homelab network)
 *   2. SUPABASE — reads security status from cb_messages (cloud/remote)
 * 
 * Usage in Lovable app:
 *   import { SecurityPanel } from '@/components/k9/SecurityPanel';
 *   <SecurityPanel mode="direct" routerUrl="http://localhost:8765" />
 */

import React, { useState, useEffect, useCallback } from 'react';

// ── Types ────────────────────────────────────────────────────────────────────

interface KillSwitchStatus {
  active: boolean;
  reason: string;
  activated_by: string;
  activated_at: number;
  credentials_flagged: string[];
  history: { action: string; reason?: string; by: string; timestamp: number }[];
}

interface BehavioralStats {
  dispatch_rate_ema: number;
  dispatch_rate_std: number;
  answer_len_ema: number;
  confidence_ema: Record<string, number>;
  task_class_ema: Record<string, number>;
  total_observations: number;
  halted: boolean;
  halt_reason: string;
}

interface EgressService {
  url: string;
  methods: string[];
  paths: string[];
  rate_limit: number;
  current_rate: number;
}

interface ForensicRecord {
  timestamp: number;
  event_type: string;
  service: string;
  task_class: string;
  question: string;
  answer: string;
  confidence: number;
  reason: string;
  context: Record<string, unknown>;
}

interface Anomaly {
  type: string;
  z_score?: number;
  current_rate?: number;
  baseline_rate?: number;
  severity: string;
  timestamp: number;
}

interface SecurityStatus {
  enabled: boolean;
  kill_switch: KillSwitchStatus;
  behavioral: BehavioralStats;
  egress: Record<string, EgressService>;
  signatures_configured: boolean;
  forensics: { total_captured: number; buffer_size: number; by_type: Record<string, number> };
  recent_anomalies: Anomaly[];
  recent_forensics: ForensicRecord[];
}

// ── Configuration ─────────────────────────────────────────────────────────────

const REFRESH_MS = 5000;

const stateColors: Record<string, string> = {
  critical: '#f87171',
  warning: '#f59e0b',
  info: '#22d3ee',
  emergency: '#f87171',
};

const forensicTypeColors: Record<string, string> = {
  blocked_dispatch: '#f87171',
  anomaly: '#f59e0b',
  unsigned_request: '#f87171',
  kill_switch: '#a78bfa',
};

// ── Component ──────────────────────────────────────────────────────────────────

export const SecurityPanel: React.FC<{
  mode?: 'direct' | 'supabase';
  routerUrl?: string;
}> = ({
  mode = 'direct',
  routerUrl = 'http://localhost:8765',
}) => {
  const [status, setStatus] = useState<SecurityStatus | null>(null);
  const [anomalies, setAnomalies] = useState<Anomaly[]>([]);
  const [forensics, setForensics] = useState<ForensicRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdate, setLastUpdate] = useState<string>('never');
  const [acting, setActing] = useState(false);

  // ── Fetch security status ────────────────────────────────────────────────
  const fetchStatus = useCallback(async () => {
    try {
      const res = await fetch(`${routerUrl}/reason/security/status`, {
        signal: AbortSignal.timeout(5000),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setStatus(data);
      if (data.recent_anomalies) setAnomalies(data.recent_anomalies);
      if (data.recent_forensics) setForensics(data.recent_forensics);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to connect');
    } finally {
      setLoading(false);
      setLastUpdate(new Date().toLocaleTimeString());
    }
  }, [routerUrl]);

  // ── Actions ──────────────────────────────────────────────────────────────
  const activateKillSwitch = useCallback(async () => {
    if (!confirm('Activate KILL SWITCH? This halts ALL auto-execution immediately.')) return;
    setActing(true);
    try {
      await fetch(`${routerUrl}/reason/security/kill-switch/activate?reason=lovable-ui`, {
        method: 'POST',
        signal: AbortSignal.timeout(5000),
      });
      setTimeout(fetchStatus, 500);
    } catch (e) {
      setError('Kill switch activation failed');
    } finally {
      setActing(false);
    }
  }, [routerUrl, fetchStatus]);

  const deactivateKillSwitch = useCallback(async () => {
    setActing(true);
    try {
      await fetch(`${routerUrl}/reason/security/kill-switch/deactivate?authorized_by=lovable-ui`, {
        method: 'POST',
        signal: AbortSignal.timeout(5000),
      });
      setTimeout(fetchStatus, 500);
    } catch (e) {
      setError('Kill switch deactivation failed');
    } finally {
      setActing(false);
    }
  }, [routerUrl, fetchStatus]);

  const removeEgressService = useCallback(async (name: string) => {
    if (!confirm(`Remove '${name}' from egress allowlist?`)) return;
    try {
      await fetch(`${routerUrl}/reason/security/egress/${name}/remove`, {
        method: 'POST',
        signal: AbortSignal.timeout(5000),
      });
      setTimeout(fetchStatus, 500);
    } catch (e) {
      setError('Egress removal failed');
    }
  }, [routerUrl, fetchStatus]);

  // ── Polling ───────────────────────────────────────────────────────────────
  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, REFRESH_MS);
    return () => clearInterval(interval);
  }, [fetchStatus]);

  // ── Render ───────────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div style={{ padding: '24px', fontFamily: 'monospace', color: '#64748b', fontSize: '12px' }}>
        Security Panel — connecting…
      </div>
    );
  }

  const ks = status?.kill_switch;
  const bl = status?.behavioral;
  const egress = status?.egress || {};
  const fs = status?.forensics;

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
          <span style={{ fontSize: '14px', fontWeight: 600 }}>⊘ Security Reinforcement</span>
          <span style={{
            padding: '2px 8px', borderRadius: '3px', fontSize: '10px',
            background: ks?.active ? 'rgba(248,113,113,0.15)' : 'rgba(74,222,128,0.15)',
            color: ks?.active ? '#f87171' : '#4ade80',
          }}>
            {ks?.active ? 'KILL SWITCH ACTIVE' : 'Operational'}
          </span>
          {status?.signatures_configured && (
            <span style={{
              padding: '1px 6px', borderRadius: '3px', fontSize: '9px',
              background: 'rgba(34,211,238,0.1)', color: '#22d3ee',
            }}>
              HMAC ✓
            </span>
          )}
        </div>
        <span style={{ color: '#64748b', fontSize: '10px' }}>↻ {lastUpdate}</span>
      </div>

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

      {/* Kill Switch Card */}
      <div style={{
        background: ks?.active ? 'rgba(248,113,113,0.08)' : 'rgba(15,20,28,0.6)',
        border: `1px solid ${ks?.active ? '#f87171' : 'rgba(74,222,128,0.2)'}`,
        borderRadius: '6px', padding: '12px',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
          <div style={{
            width: '10px', height: '10px', borderRadius: '50%',
            background: ks?.active ? '#f87171' : '#4ade80',
            boxShadow: ks?.active ? '0 0 6px #f87171' : 'none',
          }} />
          <span style={{ fontSize: '11px', fontWeight: 600 }}>
            {ks?.active ? 'KILL SWITCH ACTIVE' : 'Kill Switch — Standby'}
          </span>
          <span style={{ color: '#64748b', fontSize: '9px', marginLeft: 'auto' }}>
            {ks?.reason || 'auto-execution active'}
          </span>
        </div>
        <div style={{ display: 'flex', gap: '8px' }}>
          {!ks?.active ? (
            <button
              onClick={activateKillSwitch}
              disabled={acting}
              style={{
                padding: '5px 14px', fontSize: '10px', fontWeight: 600,
                background: 'rgba(248,113,113,0.15)', color: '#f87171',
                border: '1px solid rgba(248,113,113,0.4)', borderRadius: '3px',
                cursor: acting ? 'wait' : 'pointer', fontFamily: 'monospace',
              }}
            >
              ⛔ Activate Kill Switch
            </button>
          ) : (
            <>
              <button
                onClick={deactivateKillSwitch}
                disabled={acting}
                style={{
                  padding: '5px 14px', fontSize: '10px', fontWeight: 600,
                  background: 'rgba(74,222,128,0.1)', color: '#4ade80',
                  border: '1px solid rgba(74,222,128,0.3)', borderRadius: '3px',
                  cursor: acting ? 'wait' : 'pointer', fontFamily: 'monospace',
                }}
              >
                ✓ Deactivate
              </button>
              {ks?.credentials_flagged && ks.credentials_flagged.length > 0 && (
                <span style={{ fontSize: '8px', color: '#64748b', alignSelf: 'center' }}>
                  Credentials flagged: {ks.credentials_flagged.join(', ')}
                </span>
              )}
            </>
          )}
        </div>
      </div>

      {/* Behavioral Baseline Grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '8px' }}>
        <BaselineTile label="Dispatch Rate" value={bl ? `${bl.dispatch_rate_ema.toFixed(1)}/min` : '—'} />
        <BaselineTile label="Observations" value={bl?.total_observations || 0} />
        <BaselineTile label="Answer Length" value={bl ? `${Math.round(bl.answer_len_ema)}` : '—'} />
        <BaselineTile
          label="Status"
          value={bl?.halted ? 'HALTED' : (bl?.total_observations || 0) > 100 ? 'stable' : 'learning'}
          color={bl?.halted ? '#f87171' : '#4ade80'}
        />
      </div>

      {/* Two-column: Anomalies + Egress */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
        {/* Anomalies */}
        <div style={{
          background: 'rgba(15,20,28,0.8)', border: '1px solid rgba(255,255,255,0.06)',
          borderRadius: '6px', padding: '12px',
        }}>
          <div style={{ fontSize: '10px', fontWeight: 600, color: '#64748b', textTransform: 'uppercase', marginBottom: '8px' }}>
            ⚠ Behavioral Anomalies
          </div>
          {anomalies.length === 0 ? (
            <div style={{ padding: '16px', textAlign: 'center', color: '#64748b', fontSize: '11px' }}>
              No anomalies — baseline stable
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
              {anomalies.map((a, i) => {
                const color = stateColors[a.severity] || '#64748b';
                return (
                  <div key={i} style={{
                    display: 'flex', alignItems: 'center', gap: '6px',
                    padding: '6px 8px', borderRadius: '4px',
                    borderLeft: `2px solid ${color}`,
                    background: `${color}10`,
                  }}>
                    <span style={{ fontSize: '9px', fontWeight: 600, textTransform: 'uppercase', color }}>
                      {a.type.replace(/_/g, ' ')}
                    </span>
                    {a.z_score && (
                      <span style={{ color: '#64748b', fontSize: '8px' }}>z={a.z_score}</span>
                    )}
                    <span style={{ marginLeft: 'auto', color: '#475569', fontSize: '8px' }}>
                      {timeAgo(a.timestamp)}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Egress Allowlist */}
        <div style={{
          background: 'rgba(15,20,28,0.8)', border: '1px solid rgba(255,255,255,0.06)',
          borderRadius: '6px', padding: '12px',
        }}>
          <div style={{ fontSize: '10px', fontWeight: 600, color: '#64748b', textTransform: 'uppercase', marginBottom: '8px' }}>
            ⊘ Egress Allowlist
          </div>
          {Object.keys(egress).length === 0 ? (
            <div style={{ padding: '16px', textAlign: 'center', color: '#64748b', fontSize: '11px' }}>
              No egress services configured
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
              {Object.entries(egress).map(([name, svc]) => {
                const rate = svc.current_rate || 0;
                const limit = svc.rate_limit || 20;
                const isHigh = rate > limit * 0.8;
                return (
                  <div key={name} style={{
                    display: 'flex', alignItems: 'center', gap: '6px',
                    padding: '5px 8px', borderRadius: '3px',
                    background: 'rgba(255,255,255,0.02)', fontSize: '9px',
                  }}>
                    <span style={{ fontWeight: 600, color: '#e2e8f0' }}>{name}</span>
                    <span style={{ color: '#475569', fontSize: '8px' }}>
                      {svc.url.replace('http://', '')}
                    </span>
                    <span style={{
                      marginLeft: 'auto', fontSize: '8px', padding: '1px 5px', borderRadius: '2px',
                      background: isHigh ? 'rgba(245,158,11,0.15)' : 'rgba(34,211,238,0.1)',
                      color: isHigh ? '#f59e0b' : '#22d3ee',
                    }}>
                      {rate}/{limit}
                    </span>
                    <span
                      onClick={() => removeEgressService(name)}
                      style={{
                        fontSize: '8px', color: '#f87171', cursor: 'pointer',
                        padding: '1px 4px', borderRadius: '2px',
                        border: '1px solid rgba(248,113,113,0.2)',
                      }}
                    >
                      ✕
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {/* Forensic Capture */}
      <div style={{
        background: 'rgba(15,20,28,0.8)', border: '1px solid rgba(255,255,255,0.06)',
        borderRadius: '6px', padding: '12px',
      }}>
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px',
        }}>
          <span style={{ fontSize: '10px', fontWeight: 600, color: '#64748b', textTransform: 'uppercase' }}>
            🔍 Forensic Capture
          </span>
          <span style={{ fontSize: '8px', color: '#475569' }}>
            {fs?.total_captured || 0} records captured
          </span>
        </div>
        {forensics.length === 0 ? (
          <div style={{ padding: '16px', textAlign: 'center', color: '#64748b', fontSize: '11px' }}>
            No forensic records — no blocked activity
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '3px', maxHeight: '150px', overflowY: 'auto' }}>
            {forensics.map((r, i) => {
              const color = forensicTypeColors[r.event_type] || '#64748b';
              return (
                <div key={i} style={{
                  display: 'flex', alignItems: 'center', gap: '6px',
                  padding: '4px 8px', borderRadius: '3px',
                  background: 'rgba(255,255,255,0.02)', fontSize: '9px',
                }}>
                  <span style={{
                    fontSize: '8px', fontWeight: 600, padding: '1px 4px',
                    borderRadius: '2px', textTransform: 'uppercase',
                    background: `${color}20`, color,
                  }}>
                    {r.event_type}
                  </span>
                  <span style={{ color: '#94a3b8', flex: 1, fontSize: '8px' }}>
                    {(r.reason || '').substring(0, 80)}
                  </span>
                  <span style={{ color: '#475569', fontSize: '8px' }}>
                    {timeAgo(r.timestamp)}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Footer */}
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        fontSize: '9px', color: '#475569', paddingTop: '4px',
      }}>
        <span>CB-9 Security · poll 5s · {mode} mode</span>
        <span>
          {status?.enabled ? 'security enabled' : 'security disabled'}
          {status?.signatures_configured ? ' · HMAC-SHA256' : ' · unsigned'}
        </span>
      </div>
    </div>
  );
};

// ── Sub-components ────────────────────────────────────────────────────────────

const BaselineTile: React.FC<{ label: string; value: React.ReactNode; color?: string }> = ({
  label, value, color = '#e2e8f0',
}) => (
  <div style={{
    padding: '8px 10px', borderRadius: '4px',
    background: 'rgba(255,255,255,0.02)',
    border: '1px solid rgba(255,255,255,0.04)',
  }}>
    <div style={{ color: '#64748b', fontSize: '8px', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
      {label}
    </div>
    <div style={{ color, fontSize: '14px', fontWeight: 600, marginTop: '2px' }}>
      {value}
    </div>
  </div>
);

function timeAgo(ts: number): string {
  if (!ts) return '';
  const s = Math.floor((Date.now() / 1000) - ts);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export default SecurityPanel;
