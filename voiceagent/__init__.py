"""Apollonia's ElevenLabs voice agent (elevenlabs-voice-roadmap.md §4.2).

Runs on the apollonia-voice VM next to Asterisk. Per call:

  Asterisk --AudioSocket--> agent --caller audio (G.711 μ-law)--> gpt-realtime
  gpt-realtime --text--> agent --spelled-out text--> ElevenLabs (eleven_v4_turbo)
  ElevenLabs --pcm_8000--> agent --AudioSocket--> Asterisk --> caller

Everything that is not audio (tenant, prompt, tools, lead email) is Render's,
over /internal/voice/* (backend/call/agent_api.py).

Entry point: python -m voiceagent (systemd: apollonia-agent.service).
This folder is deployed as-is; pbx/ next to it is the Asterisk side.
"""
