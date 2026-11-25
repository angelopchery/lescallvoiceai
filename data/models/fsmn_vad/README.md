# FSMN-VAD (vendored, FunASR ONNX export)

Primary VAD for `VAD_ENGINE=fsmn` (deploy/PLAN-vad-stack.md), loaded by
`app/voice/vad_stack.py` via `funasr_onnx.Fsmn_vad_online`.

- Source: Hugging Face `funasr/fsmn-vad-onnx` (model.onnx,
  model_quant.onnx) + `funasr/FSMN-VAD` (config.yaml, am.mvn — the
  onnx repo's vad.yaml lacks the `model_conf` block the loader needs).
  Downloaded 2026-07-30.
- License: **Apache-2.0** (both repo cards checked 2026-07-30).
  FunASR code (funasr_onnx) is MIT.
- Benchmarked on THIS VM (2-core, contended), 100ms hops,
  `intra_op_num_threads=1`:
  full model p50 1.6ms / p95 2.4ms / max 8.1ms — the FULL model is
  FASTER than the quantized one here (dequant overhead beats the gains
  on a 0.4M-param model), which is why vad_stack loads `quantize=False`.
  `model_quant.onnx` is kept for constrained environments.
- Trained on zh-CN speech; speech/non-speech generalizes to Indic
  telephony per our recordings-corpus checks. Fallback if it ever
  disappoints: Silero-VAD slots into the same adapter (plan §risks).

Do NOT re-download at boot; do not add `intra_op_num_threads>1` on this
host — the ORT default of 4 threads thrashes the 2-core box (p95 jumped
to 20ms with 49ms spikes).
