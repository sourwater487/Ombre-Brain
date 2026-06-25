SELF_ANCHOR_TAG = "自我"
SELF_ANCHOR_LEGACY_TAGS = {
    "self_anchor",
    "self_identity",
    "self-identity",
    "first_person_anchor",
    "first-person-anchor",
}
SELF_ANCHOR_KIND_KEYS = {SELF_ANCHOR_TAG, *SELF_ANCHOR_LEGACY_TAGS}


def _tag_key(value: object) -> str:
    return str(value or "").strip()


def _tag_match(value: object) -> bool:
    text = _tag_key(value)
    return text == SELF_ANCHOR_TAG or text.lower() in SELF_ANCHOR_LEGACY_TAGS


def is_self_anchor_metadata(meta: dict | None) -> bool:
    if not isinstance(meta, dict):
        return False
    if bool(meta.get("self_anchor")):
        return True
    tags = list(meta.get("tags", []) or []) + list(meta.get("bucket_tags", []) or [])
    if any(_tag_match(tag) for tag in tags):
        return True
    for key in ("profile_kind", "bucket_profile_kind", "anchor_kind", "kind", "source"):
        value = _tag_key(meta.get(key))
        if value == SELF_ANCHOR_TAG or value.lower() in SELF_ANCHOR_KIND_KEYS:
            return True
    return False


def is_self_anchor_bucket(bucket: dict | None) -> bool:
    if not isinstance(bucket, dict):
        return False
    meta = bucket.get("metadata", {}) if isinstance(bucket.get("metadata"), dict) else {}
    return is_self_anchor_metadata(meta)
