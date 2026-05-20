"""Tests for pricing and cost estimation."""

from decimal import Decimal

from costguard.models import CostEstimateRequest, Provider
from costguard.pricing import PricingManager


class TestPricingManager:
    """Tests for PricingManager."""

    def setup_method(self) -> None:
        """Setup for each test."""
        from costguard.pricing import reset_pricing_manager

        reset_pricing_manager()
        self.manager = PricingManager()

    def test_get_pricing_openai(self) -> None:
        """Test getting OpenAI model pricing."""
        pricing = self.manager.get_pricing("gpt-4o")
        assert pricing is not None
        assert pricing.provider == Provider.OPENAI
        assert pricing.model_id == "gpt-4o"
        assert pricing.input_price_per_mtok == Decimal("2.50")
        assert pricing.output_price_per_mtok == Decimal("10.00")

    def test_get_pricing_anthropic(self) -> None:
        """Test getting Anthropic model pricing."""
        pricing = self.manager.get_pricing("claude-opus-4-7")
        assert pricing is not None
        assert pricing.provider == Provider.ANTHROPIC
        assert pricing.model_id == "claude-opus-4-7"
        assert pricing.input_price_per_mtok == Decimal("5.00")
        assert pricing.output_price_per_mtok == Decimal("25.00")

    def test_get_pricing_openrouter(self) -> None:
        """Test getting OpenRouter model pricing."""
        pricing = self.manager.get_pricing("openai/gpt-4o")
        assert pricing is not None
        assert pricing.provider == Provider.OPENROUTER
        assert pricing.model_id == "openai/gpt-4o"
        # Should include 5.5% platform fee
        assert pricing.input_price_per_mtok == Decimal("2.64")

    def test_get_pricing_unknown(self) -> None:
        """Test getting pricing for unknown model."""
        pricing = self.manager.get_pricing("unknown-model")
        assert pricing is None

    def test_estimate_tokens(self) -> None:
        """Test token estimation."""
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello, world!"},
        ]

        tokens = self.manager.estimate_tokens(messages, "gpt-4o")
        # Should be > 0 and reasonable
        assert tokens > 0
        assert tokens < 1000  # Reasonable upper bound for short message

    def test_estimate_cost(self) -> None:
        """Test cost estimation."""
        request = CostEstimateRequest(
            provider=Provider.OPENAI,
            model_id="gpt-4o",
            messages=[
                {"role": "user", "content": "Hello"},
            ],
            estimated_output_tokens=100,
        )

        estimate = self.manager.estimate_cost(request)
        assert estimate is not None
        assert estimate.estimated_input_tokens > 0
        assert estimate.estimated_output_tokens == 100
        assert estimate.estimated_total_tokens == estimate.estimated_input_tokens + 100
        assert estimate.estimated_cost > Decimal("0.00")
        assert estimate.pricing_used.model_id == "gpt-4o"

    def test_calculate_actual_cost(self) -> None:
        """Test actual cost calculation."""
        cost = self.manager.calculate_actual_cost("gpt-4o", 1000, 500)
        assert cost is not None
        # Expected: (1000 * 2.50 / 1M) + (500 * 10.00 / 1M) = 0.0025 + 0.005 = 0.0075
        expected = Decimal("0.0075")
        assert cost == expected

    def test_list_available_models(self) -> None:
        """Test listing available models."""
        models = self.manager.list_available_models()
        assert len(models) > 0

        # Filter by provider
        openai_models = self.manager.list_available_models(Provider.OPENAI)
        assert all(m.provider == Provider.OPENAI for m in openai_models)

    def test_add_custom_pricing(self) -> None:
        """Test adding custom pricing."""
        from costguard.models import ProviderPricing

        custom = ProviderPricing(
            provider=Provider.OPENAI,
            model_id="custom-model",
            model_name="Custom Model",
            input_price_per_mtok=Decimal("1.00"),
            output_price_per_mtok=Decimal("2.00"),
            context_window=8000,
        )

        self.manager.add_custom_pricing(custom)

        retrieved = self.manager.get_pricing("custom-model")
        assert retrieved is not None
        assert retrieved.model_id == "custom-model"
        assert retrieved.input_price_per_mtok == Decimal("1.00")


class TestPricingModels:
    """Tests for specific model pricing."""

    def setup_method(self) -> None:
        """Setup for each test."""
        from costguard.pricing import reset_pricing_manager

        reset_pricing_manager()
        self.manager = PricingManager()

    def test_gpt_5_5_pricing(self) -> None:
        """Test GPT-5.5 pricing (April 2026)."""
        pricing = self.manager.get_pricing("gpt-5.5")
        assert pricing is not None
        assert pricing.input_price_per_mtok == Decimal("5.00")
        assert pricing.output_price_per_mtok == Decimal("30.00")

    def test_gpt_5_4_pricing(self) -> None:
        """Test GPT-5.4 pricing (April 2026)."""
        pricing = self.manager.get_pricing("gpt-5.4")
        assert pricing is not None
        assert pricing.input_price_per_mtok == Decimal("2.50")
        assert pricing.output_price_per_mtok == Decimal("15.00")

    def test_claude_opus_4_7_pricing(self) -> None:
        """Test Claude Opus 4.7 pricing (April 2026)."""
        pricing = self.manager.get_pricing("claude-opus-4-7")
        assert pricing is not None
        assert pricing.input_price_per_mtok == Decimal("5.00")
        assert pricing.output_price_per_mtok == Decimal("25.00")

    def test_claude_sonnet_4_6_pricing(self) -> None:
        """Test Claude Sonnet 4.6 pricing (April 2026)."""
        pricing = self.manager.get_pricing("claude-sonnet-4-6")
        assert pricing is not None
        assert pricing.input_price_per_mtok == Decimal("3.00")
        assert pricing.output_price_per_mtok == Decimal("15.00")

    def test_claude_haiku_4_5_pricing(self) -> None:
        """Test Claude Haiku 4.5 pricing (April 2026)."""
        pricing = self.manager.get_pricing("claude-haiku-4-5")
        assert pricing is not None
        assert pricing.input_price_per_mtok == Decimal("1.00")
        assert pricing.output_price_per_mtok == Decimal("5.00")
