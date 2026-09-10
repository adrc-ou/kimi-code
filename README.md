# NRP-provided LLM + LiteLLM + Kimi Code

This repo contains all you need to get coding agentically with OU's LiteLLM access to open-weight models provided by NRP. It's designed to be a good community member by inherently respecting concurrency limits. Because this harness is containerized with Docker, it's portable for cross-platform compatibility and somewhat sandboxed to protect your system from overzealous agents. Even so, always monitor what your agents are doing!

This configuration is currently policy-profiled for NRP's qwen3 service (Qwen3.8-Flash-Next). Do not switch models without updating and validating the context/concurrency policy values.

## Instructions

1. Create a [virtual key](https://litellm.lib.ou.edu/ui/?page=api-keys) in LiteLLM and give it access to the model you want.
2. Create a `.env` file (use `.env.example` for reference) and paste in the virtual key you just generated as `LITELLM_API_KEY`. The other settings should be fine as-is, but you're welcome to experiment with other models besides our current recommendation, which is Qwen3.8-Flash-Next. The identifier to use as `LITELLM_MODEL_ID` is actually the human-readable model name in the LiteLLM dashboard.
3. Run `./workspace-init.sh` to set up the agentic tools within the sandboxed workspace.
4. Run `./start.sh` to start Kimi Code.

Kimi Code starts up a web server and prints a URL in your terminal once it's running. This web UI is the primary way to use your coding agent. Pull it up in your favorite browser and get to work!
