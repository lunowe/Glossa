from glossa.pricing import compute_cost_usd, get_price


class TestGetPrice:
    def test_known_model(self):
        price = get_price("claude-sonnet-4-6")
        assert price.input_per_million == 3.00
        assert price.output_per_million == 15.00

    def test_unknown_model_falls_back_to_zero(self):
        price = get_price("totally-fake-model")
        assert price.input_per_million == 0.0
        assert price.output_per_million == 0.0


class TestComputeCost:
    def test_opus_4_7_basic(self):
        # 1M input + 1M output at Opus 4.7 = $5 + $25 = $30
        cost = compute_cost_usd(
            model="claude-opus-4-7",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        assert cost == 30.0

    def test_sonnet_4_6_with_cache(self):
        # 100k input + 100k cache_creation + 100k cache_read + 50k output
        # = 0.1 * 3.00 + 0.1 * 3.75 + 0.1 * 0.30 + 0.05 * 15.00 = 0.30 + 0.375 + 0.03 + 0.75 = 1.455
        cost = compute_cost_usd(
            model="claude-sonnet-4-6",
            input_tokens=100_000,
            output_tokens=50_000,
            cache_creation_input_tokens=100_000,
            cache_read_input_tokens=100_000,
        )
        assert cost == 1.455

    def test_zero_tokens_zero_cost(self):
        assert compute_cost_usd(model="claude-opus-4-7", input_tokens=0, output_tokens=0) == 0.0

    def test_unknown_model_zero_cost(self):
        assert compute_cost_usd(model="totally-fake-model", input_tokens=1_000_000, output_tokens=1_000_000) == 0.0

    def test_haiku_45_cheaper_than_opus(self):
        kwargs = {"input_tokens": 100_000, "output_tokens": 50_000}
        haiku = compute_cost_usd(model="claude-haiku-4-5", **kwargs)
        opus = compute_cost_usd(model="claude-opus-4-7", **kwargs)
        assert haiku < opus
        # haiku = 0.1 * 1.0 + 0.05 * 5.0 = 0.35
        assert haiku == 0.35
