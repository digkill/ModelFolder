from __future__ import annotations

MODELS = [
    {"id": "claude-opus-5", "provider": "kie", "kind": "chat", "title": "Claude Opus 5", "docs": "https://kie.ai/claude-opus-5", "kie_model": "claude-opus-5", "chat_path": "/claude-opus-5/v1/chat/completions", "roles": ["orchestrator", "design", "code"]},
    {"id": "claude-sonnet-5", "provider": "kie", "kind": "chat", "title": "Claude Sonnet 5", "docs": "https://kie.ai/claude-sonnet-5", "kie_model": "claude-sonnet-5", "chat_path": "/claude-sonnet-5/v1/chat/completions", "roles": ["orchestrator", "code"]},
    {"id": "claude-fable-5", "provider": "kie", "kind": "chat", "title": "Claude Fable 5", "docs": "https://kie.ai/claude-fable-5", "kie_model": "claude-fable-5", "chat_path": "/claude-fable-5/v1/chat/completions", "roles": ["orchestrator", "design"]},
    {"id": "claude-opus-4-8", "provider": "kie", "kind": "chat", "title": "Claude Opus 4.8", "docs": "https://kie.ai/claude-opus-4-8", "kie_model": "claude-opus-4-8", "chat_path": "/claude-opus-4-8/v1/chat/completions", "roles": ["orchestrator"]},
    {"id": "gpt-5-6-sol", "provider": "kie", "kind": "chat", "title": "GPT-5.6 Sol", "docs": "https://kie.ai/gpt-5-6", "kie_model": "gpt-5-6-sol", "chat_path": "/gpt-5-6-sol/v1/chat/completions", "roles": ["orchestrator", "code"]},
    {"id": "gpt-5-6-terra", "provider": "kie", "kind": "chat", "title": "GPT-5.6 Terra", "docs": "https://kie.ai/gpt-5-6", "kie_model": "gpt-5-6-terra", "roles": ["design"]},
    {"id": "gpt-5-6-luna", "provider": "kie", "kind": "chat", "title": "GPT-5.6 Luna", "docs": "https://kie.ai/gpt-5-6", "kie_model": "gpt-5-6-luna", "roles": ["fast"]},
    {"id": "gpt-5.3-codex", "provider": "kie", "kind": "chat", "title": "GPT-5.3 Codex", "docs": "https://kie.ai/codex", "kie_model": "gpt-5.3-codex", "roles": ["code"]},
    {"id": "grok-4-6", "provider": "kie", "kind": "chat", "title": "Grok 4.6", "docs": "https://kie.ai/grok-4-6", "kie_model": "grok-4-6", "chat_path": "/grok-4-6/v1/chat/completions", "roles": ["orchestrator", "design"]},
    {"id": "gemini-3-7-flash", "provider": "kie", "kind": "chat", "title": "Gemini 3.7 Flash", "docs": "https://kie.ai/gemini-3-7-flash", "kie_model": "gemini-3-7-flash", "roles": ["fast", "code"]},
    {"id": "gemini-3-6-flash", "provider": "kie", "kind": "chat", "title": "Gemini 3.6 Flash", "docs": "https://kie.ai/gemini-3-6-flash", "kie_model": "gemini-3-6-flash", "roles": ["fast"]},
    {"id": "gemini-3-5-flash", "provider": "kie", "kind": "chat", "title": "Gemini 3.5 Flash", "docs": "https://kie.ai/gemini-3-5-flash", "kie_model": "gemini-3-5-flash", "roles": ["fast"]},
    {"id": "openai-gpt", "provider": "openai", "kind": "chat", "title": "OpenAI Chat", "docs": "https://platform.openai.com", "roles": ["orchestrator", "fallback"]},
    {"id": "anthropic-claude", "provider": "anthropic", "kind": "chat", "title": "Anthropic Claude", "docs": "https://platform.claude.com", "roles": ["orchestrator", "fallback"]},
    {"id": "xai-grok", "provider": "grok", "kind": "chat", "title": "xAI Grok", "docs": "https://docs.x.ai", "roles": ["orchestrator", "fallback"]},
    {"id": "gpt-image-1.5", "provider": "kie", "kind": "image", "title": "GPT Image 1.5", "docs": "https://kie.ai/gpt-image-1.5", "kie_model": "gpt-image-1.5", "roles": ["concept", "ui"]},
    {"id": "gpt-image-1", "provider": "kie", "kind": "image", "title": "GPT Image 1 / 4o", "docs": "https://kie.ai/4o-image-api", "kie_model": "gpt-image-1", "roles": ["concept"]},
    {"id": "nano-banana-2-lite", "provider": "kie", "kind": "image", "title": "Nano Banana 2 Lite", "docs": "https://kie.ai/nano-banana-2-lite", "kie_model": "nano-banana-2-lite", "roles": ["concept", "fast"]},
    {"id": "flux-3", "provider": "kie", "kind": "image", "title": "FLUX 3", "docs": "https://kie.ai/flux-3", "kie_model": "flux-3", "roles": ["concept"]},
    {"id": "grok-imagine-video-1.5", "provider": "kie", "kind": "video", "title": "Grok Imagine Video 1.5", "docs": "https://kie.ai/grok-imagine-video-1.5", "kie_model": "grok-imagine-video-1.5", "roles": ["cinematic"]},
    {"id": "kling-o3", "provider": "kie", "kind": "video", "title": "Kling O3", "docs": "https://kie.ai/kling-o3", "kie_model": "kling-o3", "roles": ["cinematic"]},
    {"id": "gemini-omni", "provider": "kie", "kind": "video", "title": "Gemini Omni", "docs": "https://kie.ai/gemini-omni", "kie_model": "gemini-omni", "roles": ["cinematic"]},
    {"id": "runway", "provider": "kie", "kind": "video", "title": "Runway Gen-4", "docs": "https://kie.ai/runway-api", "kie_model": "runway", "roles": ["cinematic"]},
    {"id": "hailuo", "provider": "kie", "kind": "video", "title": "Hailuo / MiniMax", "docs": "https://kie.ai/market/hailuo", "kie_model": "hailuo", "roles": ["cinematic"]},
    {"id": "elevenlabs-tts", "provider": "kie", "kind": "audio", "title": "ElevenLabs TTS", "docs": "https://kie.ai/elevenlabs-tts", "kie_model": "elevenlabs-tts", "roles": ["voice"]},
    {"id": "elevenlabs-dialogue-v3", "provider": "kie", "kind": "audio", "title": "ElevenLabs Dialogue V3", "docs": "https://kie.ai/elevenlabs/text-to-dialogue-v3", "kie_model": "elevenlabs/text-to-dialogue-v3", "roles": ["voice", "dialogue"]},
    {"id": "suno", "provider": "kie", "kind": "music", "title": "Suno", "docs": "https://kie.ai/suno-api", "kie_model": "suno", "roles": ["music"]},
    {"id": "meshy-text-to-3d", "provider": "meshy", "kind": "mesh", "title": "Meshy Text to 3D", "docs": "https://www.meshy.ai", "roles": ["mesh"]},
    {"id": "catalog-search", "provider": "catalog", "kind": "catalog", "title": "ModelFolder catalog", "docs": "/api/catalog/search", "roles": ["assets"]},
]


def find_model(model_id: str) -> dict:
    for item in MODELS:
        if item["id"] == model_id:
            return item
    return {"id": model_id, "provider": "kie", "kie_model": model_id, "kind": "chat", "title": model_id, "roles": []}
