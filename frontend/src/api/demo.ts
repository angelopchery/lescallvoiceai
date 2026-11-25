// Synthetic fallback data so the UI renders fully even when the backend
// (Linux-only, port 8080) is not reachable from this machine. Numbers mirror
// the shapes returned by /api/* — NOT real customer data.

const wave = (n: number, base: number, amp: number) =>
  Array.from({ length: n }, (_, i) => Math.round(base + amp * Math.sin(i / 1.7) + amp * 0.4 * Math.cos(i / 0.9)))

export const demoOverview = {
  today: { calls: 1, active: 0, trend: 40, cost: 0, stt_cost: 0, tts_cost: 0, stt_seconds: 0, tts_chars: 0 },
  week: { calls: 5, cost: 0 },
  month: { calls: 8541, cost: 1014.6587 },
  totals: { agents: 0, campaigns: 1, leads: 0, calls: 2708, inbound: 2703, minutes_used: 1840 },
  hourly: wave(24, 22, 16).map((v) => Math.max(0, v)),
  calls_7d: wave(7, 5, 3).map((v) => Math.max(0, v)),
  kpis: { total: 1, answered: 1, missed: 0, inbound: 0, outbound: 1, appointments: 0, followups: 1, minutes_used: 0 },
  spark: {
    agents: wave(16, 4, 2), campaigns: wave(16, 3, 1.4), leads: wave(16, 5, 3),
    calls: wave(16, 40, 14), today: wave(16, 6, 4), inbound: wave(16, 38, 12), minutes: wave(16, 30, 10),
  },
  runtime: 'degraded' as const,
}

// Neutral fallback shown only while the real /api/health is still loading —
// must NOT claim a service is down (that was a hardcoded false alarm).
export const demoHealth = {
  overall: 'healthy' as const,
  services: [
    { name: 'System', ok: true },
    { name: 'AI Pipeline', ok: true },
    { name: 'Telephony', ok: true },
    { name: 'STT (Sarvam)', ok: true },
    { name: 'TTS (Bulbul)', ok: true },
  ],
  liveCount: 0,
}
