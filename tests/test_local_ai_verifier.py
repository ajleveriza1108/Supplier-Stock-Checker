from core.local_ai_verifier import LocalAIChangeVerifier


def test_extract_plain_json():
    result = LocalAIChangeVerifier._extract_json(
        '{"verdict":"CONFIRM","normalized_stock":"IN_STOCK"}'
    )
    assert result["verdict"] == "CONFIRM"


def test_extract_fenced_json():
    result = LocalAIChangeVerifier._extract_json(
        '```json\n{"verdict":"REJECT","normalized_stock":"OOS"}\n```'
    )
    assert result["verdict"] == "REJECT"


def test_low_inventory_maps_to_oos():
    assert LocalAIChangeVerifier._stock_bucket("Limited Stock") == "OOS"
    assert LocalAIChangeVerifier._stock_bucket("Only 2 Remaining") == "OOS"
    assert LocalAIChangeVerifier._stock_bucket("Backorder") == "OOS"


def test_normal_stock_mapping():
    assert LocalAIChangeVerifier._stock_bucket("In Stock") == "IN_STOCK"
    assert LocalAIChangeVerifier._stock_bucket("Unable to Verify") == "UNKNOWN"
