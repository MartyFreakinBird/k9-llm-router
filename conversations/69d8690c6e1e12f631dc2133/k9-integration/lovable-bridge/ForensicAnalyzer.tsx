/**
 * ForensicAnalyzer — Lovable React component
 * 
 * LLM-powered forensic analysis using local Ollama.
 * Analyzes captured security events for attack patterns.
 * 
 * Why local? Commercial models (Claude, GPT) refuse to analyze attack
 * payloads due to safety filters. The Hugging Face incident proved
 * open-source local models are required for forensic analysis.
 * 
 * API:
 *   POST /reason/security/forensics/analyze  — run analysis
 *   GET  /reason/security/forensics/analysis — last result
 *   GET  /reason/security/forensics/analyzer-status — config
 *   POST /reason/security/forensics/auto-analyze — toggle auto mode
 */

import React, { useState, useEffect, useCallback } from 'react';

interface ThreatAssessment {
  timestamp: number;
  records_analyzed: number;
  attack_classification: string;
  confidence: number;
  recommended_action: string;
  pattern_summary: string;
  indicators: string[];
  mitre_mapping: string[];
  model_used: string;
  analysis_latency_ms: number;
  error: string;
}

interface AnalyzerStatus {
  model: string;
  ollama_url: string;
  auto_mode: boolean;
  analysis_count: number;
  last_analysis: ThreatAssessment | null;
  trigger_threshold: number;
  auto_interval: number;
}

const classificationColors: Record<string, string> = {
  none: '#4ade80',
  prompt_injection: '#f87171',
  lateral_movement: '#f87171',
  data_exfiltration: '#f87171',
  credential_theft: '#f87171',
  sandbox_escape: '#f87171',
  rate_flood: '#f87171',
  suspicious_but_inconclusive: '#f59e0b',
};

const actionColors: Record<string, string> = {
  monitor: '#4ade80',
  investigate: '#f59e0b',
  halt: '#f87171',
  kill_switch: '#f87171',
};

export const ForensicAnalyzer: React.FC<{
  routerUrl?: string;
}> = ({
  routerUrl = 'http://localhost:8765',
}) => {
  const [assessment, setAssessment] = useState<ThreatAssessment | null>(null);
  const [status, setStatus] = useState<AnalyzerStatus | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchStatus = useCallback(async () => {
    try {
      const res = await fetch(`${routerUrl}/reason/security/forensics/analyzer-status`, {
        signal: AbortSignal.timeout(5000),
      });
      if (!res.ok) return;
      const data = await res.json();
      setStatus(data);
      if (data.last_analysis) setAssessment(data.last_analysis);
    } catch { /* offline */ }
  }, [routerUrl]);

  const runAnalysis = useCallback(async () => {
    setAnalyzing(true);
    setError(null);
    try {
      const res = await fetch(`${routerUrl}/reason/security/forensics/analyze?limit=50`, {
        method: 'POST',
        signal: AbortSignal.timeout(60000),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setAssessment(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Analysis failed');
    } finally {
      setAnalyzing(false);
    }
  }, [routerUrl]);

  const toggleAuto = useCallback(async () => {
    if (!status) return;
    const enable = !status.auto_mode;
    try {
      await fetch(`${routerUrl}/reason/security/forensics/auto-analyze?enable=${enable}`, {
        method: 'POST',
        signal: AbortSignal.timeout(5000),
      });
      setTimeout(fetchStatus, 500);
    } catch (e) {
      setError('Toggle failed');
    }
  }, [routerUrl, status, fetchStatus]);

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 10000);
    return () => clearInterval(interval);
  }, [fetchStatus]);

  const classification = assessment?.attack_classification || 'none';
  const clsColor = classificationColors[classification] || '#64748b';
  const action = assessment?.recommended_action || 'monitor';
  const actColor = actionColors[action] || '#64748b';
  const cardBorder = classification === 'none' ? 'rgba(74,222,128,0.2)'
    : classification === 'suspicious_but_inconclusive' ? 'rgba(245,158,11,0.3)'
    : '#f87171';
  const cardBg = classification === 'none' ? 'rgba(74,222,128,0.03)'
    : classification === 'suspicious_but_inconclusive' ? 'rgba(245,158,11,0.04)'
    : 'rgba(248,113,113,0.08)';

  return (
    <div style={{
      fontFamily: "'JetBrains Mono', monospace",
      fontSize: '12px',
      color: '#e2e8f0',
      background: '#080c10',
      padding: '16px',
      borderRadius: '6px',
      display: 'flex',
      flexDirection: 'column',
      gap: '12px',
    }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span style={{ fontSize: '14px', fontWeight: 600 }}>🔍 LLM Forensic Analyzer</span>
          <span style={{
            fontSize: '9px', color: '#64748b',
            background: 'rgba(168,85,247,0.1)', padding: '1px 6px', borderRadius: '2px',
          }}>
            local Ollama — no commercial safety filters
          </span>
        </div>
        <div style={{ display: 'flex', gap: '6px' }}>
          <button
            onClick={runAnalysis}
            disabled={analyzing}
            style={{
              padding: '4px 12px', fontSize: '10px', fontWeight: 600,
              background: 'rgba(34,211,238,0.1)', color: '#22d3ee',
              border: '1px solid rgba(34,211,238,0.3)', borderRadius: '3px',
              cursor: analyzing ? 'wait' : 'pointer', fontFamily: 'monospace',
            }}
          >
            {analyzing ? '⏳ Analyzing...' : '▶ Analyze Now'}
          </button>
          <button
            onClick={toggleAuto}
            style={{
              padding: '4px 12px', fontSize: '10px', fontWeight: 600,
              background: status?.auto_mode ? 'rgba(245,158,11,0.15)' : 'rgba(34,211,238,0.1)',
              color: status?.auto_mode ? '#f59e0b' : '#22d3ee',
              border: `1px solid ${status?.auto_mode ? 'rgba(245,158,11,0.3)' : 'rgba(34,211,238,0.3)'}`,
              borderRadius: '3px', cursor: 'pointer', fontFamily: 'monospace',
            }}
          >
            ⏱ Auto: {status?.auto_mode ? 'ON' : 'OFF'}
          </button>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div style={{
          padding: '6px 10px', background: 'rgba(248,113,113,0.1)',
          border: '1px solid rgba(248,113,113,0.3)', borderRadius: '3px',
          color: '#f87171', fontSize: '11px',
        }}>
          ⚠ {error}
        </div>
      )}

      {/* Assessment Card */}
      <div style={{
        background: cardBg,
        border: `1px solid ${cardBorder}`,
        borderRadius: '6px',
        padding: '12px',
      }}>
        {/* Classification + confidence + action */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
          <span style={{
            fontSize: '10px', fontWeight: 700, textTransform: 'uppercase',
            padding: '2px 8px', borderRadius: '3px',
            background: `${clsColor}20`, color: clsColor,
          }}>
            {classification.replace(/_/g, ' ')}
          </span>
          {assessment && (
            <span style={{ color: '#64748b', fontSize: '9px' }}>
              confidence: {(assessment.confidence * 100).toFixed(0)}%
            </span>
          )}
          <span style={{
            marginLeft: 'auto', fontSize: '9px', fontWeight: 600,
            textTransform: 'uppercase', padding: '1px 6px', borderRadius: '2px',
            background: `${actColor}15`, color: actColor,
          }}>
            {action.replace(/_/g, ' ')}
          </span>
        </div>

        {/* Summary */}
        <div style={{ color: '#94a3b8', fontSize: '10px', lineHeight: 1.5 }}>
          {assessment?.pattern_summary || 'No analysis performed yet. Click "Analyze Now" to run LLM forensic analysis on captured security events.'}
        </div>

        {/* Indicators */}
        {assessment?.indicators && assessment.indicators.length > 0 && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', marginTop: '8px' }}>
            {assessment.indicators.map((ind, i) => (
              <span key={i} style={{
                fontSize: '8px', padding: '1px 5px', borderRadius: '2px',
                background: 'rgba(255,255,255,0.05)', color: '#22d3ee',
              }}>
                {ind.replace(/_/g, ' ')}
              </span>
            ))}
          </div>
        )}

        {/* MITRE */}
        {assessment?.mitre_mapping && assessment.mitre_mapping.length > 0 && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', marginTop: '6px' }}>
            {assessment.mitre_mapping.map((m, i) => (
              <span key={i} style={{
                fontSize: '8px', padding: '1px 5px', borderRadius: '2px',
                fontFamily: 'monospace', background: 'rgba(168,85,247,0.1)', color: '#a78bfa',
              }}>
                {m}
              </span>
            ))}
          </div>
        )}

        {/* Meta */}
        <div style={{
          display: 'flex', gap: '12px', fontSize: '8px', color: '#475569', marginTop: '8px',
        }}>
          {assessment?.model_used && <span>model: {assessment.model_used}</span>}
          {assessment && <span>records: {assessment.records_analyzed}</span>}
          {assessment && <span>latency: {assessment.analysis_latency_ms}ms</span>}
          {assessment?.error && (
            <span style={{ color: '#f87171' }}>err: {assessment.error.substring(0, 80)}</span>
          )}
        </div>
      </div>

      {/* Analyzer Status */}
      {status && (
        <div style={{
          display: 'flex', justifyContent: 'space-between', fontSize: '9px', color: '#475569',
        }}>
          <span>
            model: {status.model} · {status.ollama_url.replace('http://', '')}
          </span>
          <span>
            {status.analysis_count} analyses · auto every {status.auto_interval}s
          </span>
        </div>
      )}
    </div>
  );
};

export default ForensicAnalyzer;
