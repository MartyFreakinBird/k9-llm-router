# Orbitron Dual-Instance Architecture — Path B

## Instance Map

| Instance | Project Ref | Role |
|---|---|---|
| Sovereign Plane | `ziqenqqgnqxqrazmjohs` | Production: trading signals, platform integrations, external API ingress |
| Cloud CB Plane | `logkumycxztvnnatheup` | Cognitive Bus: cb_messages, agent adapters, text-to-SQL, JEPA telemetry |

## Data Flow

```
External platforms          Lovable Cloud (CB Plane)        Original (Sovereign Plane)
TradeMaster, Replit    →    orbitron-bus edge fn        →   (no direct writes)
K-9 :8765              →    cb_messages table           →   cb-bridge → /external-integration
Base44 Vectos          →    agent adapters              →   signal-aggregator (authenticated)
```

## cb-bridge Routing Rules

| CB Type | Forwarded? | Condition |
|---|---|---|
| `execution_request` | ✅ Yes | Always — requires `signature` field or 403 |
| `alert` | ✅ Yes | Always |
| `audit` | ✅ Yes | Always |
| `inference` | ✅ Yes | Only if `confidence >= 0.65` |
| `observation` | ❌ No | CB-internal only |
| `heartbeat` | ❌ No | CB-internal only |

## Secrets Required on Cloud Instance

Set via Lovable dashboard → Edge Functions → Secrets:

```
ORIGINAL_SUPABASE_URL      = https://ziqenqqgnqxqrazmjohs.supabase.co
ORIGINAL_SUPABASE_ANON_KEY = <anon key — already stored in Base44 secrets>
ORIGINAL_INTEGRATION_KEY   = ork_live_... (mint from original instance, optional)
```

## Deploy Command

```bash
supabase functions deploy cb-bridge --project-ref logkumycxztvnnatheup
```

## Getting ORIGINAL_INTEGRATION_KEY (optional, enables authenticated forwarding)

```bash
curl -X POST https://ziqenqqgnqxqrazmjohs.supabase.co/functions/v1/integration-auth \
  -H "apikey: <service_role_key>" \
  -H "Content-Type: application/json" \
  -d '{"name": "cb-bridge", "permissions": ["event_write"]}'
# Returns: { "key": "ork_live_..." }
# Store in Cloud instance secrets as ORIGINAL_INTEGRATION_KEY
```

## Next Steps After Deploy

1. Deploy `cb-bridge` to Cloud instance
2. Test: POST a `{"type": "alert", ...}` CB envelope to `cb-bridge`
3. Verify it appears in original instance's event log
4. (Optional) mint `ORIGINAL_INTEGRATION_KEY` for authenticated forwarding
5. Wire K-9 `:8765` to POST `inference` + `execution_request` to `orbitron-bus`
