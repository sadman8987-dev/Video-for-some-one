"""Smart, bounded retry profiles for YouTube extraction."""

def youtube_attempt_profiles(has_cookies: bool) -> list[tuple[str, dict]]:
    profiles = [
        ("default", {}),
        ("mweb-pot", {"extractor_args": {"youtube": {"player_client": ["mweb"]}}}),
        ("android-vr", {"extractor_args": {"youtube": {"player_client": ["android_vr"]}}}),
        ("embedded", {"extractor_args": {"youtube": {"player_client": ["web_embedded"]}}}),
    ]
    return profiles[:2] if has_cookies else profiles


def merge_extractor_options(base: dict, attempt: dict) -> dict:
    merged = dict(base)
    args = {key: dict(value) for key, value in (base.get("extractor_args") or {}).items()}
    for key, value in (attempt.get("extractor_args") or {}).items():
        args[key] = {**args.get(key, {}), **value}
    if args:
        merged["extractor_args"] = args
    return merged
