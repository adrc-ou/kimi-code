def probe(full):
    # Exercise the same TLS, bearer auth and API helper used by the agent.
    import comfyctl

    stats = comfyctl.request_json("GET", "/system_stats")
    if not isinstance(stats, dict) or "system" not in stats:
        raise ValueError("invalid ComfyUI stats")
    if full:
        schema = comfyctl.request_json("GET", "/object_info/EmptyImage")
        if "EmptyImage" not in schema:
            raise ValueError("missing EmptyImage node")
    return "authenticated stats" + (" and node schema" if full else "")
