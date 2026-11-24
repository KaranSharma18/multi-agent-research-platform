"""The remote Web Research Agent, served over A2A v1.0.

It is deliberately a separate service on a different framework from the
orchestrator: the Claude Agent SDK runs its research loop, and the orchestrator
only ever sees its Agent Card, task states and the brief it returns. That
opacity is the point of A2A.

Engines (RESEARCH_ENGINE):
  claude  the Claude Agent SDK, limited to WebSearch and WebFetch, with a
          domain hook, `dontAsk` permissions, a turn cap and a budget cap.
  gemini  Gemini with Google Search grounding, for setups with only a Gemini key.
  stub    scripted behaviours (complete, fail, stall, input-required, inject)
          for CI and eval fault cases. No network.
  auto    claude if an Anthropic key is present, else gemini if a Google key
          is present, else stub.
"""
