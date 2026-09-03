# NRP-provided LLM + LiteLLM + Kimi Code

This repo contains all you need to get coding agentically with OU's LiteLLM access to open-weight models provided by NRP. It's designed to be a good community member by inherently respecting concurrency limits.

## Instructions

1. Create a [virtual key](https://litellm.lib.ou.edu/ui/?page=api-keys) in LiteLLM and give it access to the model you want.
2. Create a `.env` file (use `.env.example` for reference) and paste in the virtual key you just generated as `LITELLM_API_KEY`. The other settings should be fine as-is, but you're welcome to experiment with other models besides our current recommendation, which is Qwen3.8-Flash-Next. The identifier to use as `LITELLM_MODEL_ID` is actually the human-readable model name in the LiteLLM dashboard; in this case, we're using "Qwen3.5 397B" but that is **mislabeled** in LiteLLM and actually does point to NRP's Quen3.8-Flash-Next model.
3. Run `./start.sh`. This script will build the Docker container the first time you run it, then run it and start Kimi Code within it. Docker is used here to sandbox the work surface the AI agent will have access to, for security. Of course, these models are crafty... so never assume it's completely safe! Always monitor what your agent is doing.

Kimi Code starts up a web server and prints a URL in your terminal once it's running. This web UI is the primary way to use your coding agent. Pull it up in your favorite browser and get to work!
