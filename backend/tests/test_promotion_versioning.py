from app.promotion.versioning import (
    LEGACY_PROMOTION_MODEL_VERSION,
    PromotionRuntimeMode,
    get_promotion_model_identity,
)


def test_invalid_runtime_mode_fails_closed_to_legacy():
    assert PromotionRuntimeMode.parse("unexpected") is PromotionRuntimeMode.LEGACY


def test_default_identity_keeps_legacy_champion():
    identity = get_promotion_model_identity()
    assert identity.active_model_version == LEGACY_PROMOTION_MODEL_VERSION
    assert identity.runtime_mode is PromotionRuntimeMode.LEGACY
    assert identity.challenger_enabled is False
    payload = identity.as_payload()
    assert payload["production_source"] == "champion"
    assert payload["feature_version"]
    assert payload["data_version"]
