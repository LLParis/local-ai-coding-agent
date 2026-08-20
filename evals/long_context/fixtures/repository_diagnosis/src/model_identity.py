def model_matches(launched: str, observed: str) -> bool:
    return observed.split(":", 1)[0] == launched.split(":", 1)[0]
