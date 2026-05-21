"""Provider pricing database and cost estimation engine."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import tiktoken

from costguard.models import CostEstimateRequest, CostEstimateResponse, Provider, ProviderPricing

# Provider pricing data aligned to API model naming as of May 2026.
DEFAULT_PRICING: dict[str, dict[str, Any]] = {
    # OpenAI Models
    "gpt-4.1": {
        "provider": "openai",
        "model_name": "GPT-4.1",
        "input_price_per_mtok": "2.00",
        "output_price_per_mtok": "8.00",
        "context_window": 128000,
    },
    "gpt-4.1-mini": {
        "provider": "openai",
        "model_name": "GPT-4.1 Mini",
        "input_price_per_mtok": "0.40",
        "output_price_per_mtok": "1.60",
        "context_window": 128000,
    },
    "gpt-4.1-nano": {
        "provider": "openai",
        "model_name": "GPT-4.1 Nano",
        "input_price_per_mtok": "0.10",
        "output_price_per_mtok": "0.40",
        "context_window": 128000,
    },
    "gpt-4o": {
        "provider": "openai",
        "model_name": "GPT-4o",
        "input_price_per_mtok": "2.50",
        "output_price_per_mtok": "10.00",
        "context_window": 128000,
    },
    "gpt-4o-mini": {
        "provider": "openai",
        "model_name": "GPT-4o Mini",
        "input_price_per_mtok": "0.15",
        "output_price_per_mtok": "0.60",
        "context_window": 128000,
    },
    # Anthropic Models
    "claude-opus-4-20250514": {
        "provider": "anthropic",
        "model_name": "Claude Opus 4",
        "input_price_per_mtok": "5.00",
        "output_price_per_mtok": "25.00",
        "context_window": 200000,
    },
    "claude-sonnet-4-20250514": {
        "provider": "anthropic",
        "model_name": "Claude Sonnet 4",
        "input_price_per_mtok": "3.00",
        "output_price_per_mtok": "15.00",
        "context_window": 200000,
    },
    "claude-3-7-sonnet-20250219": {
        "provider": "anthropic",
        "model_name": "Claude 3.7 Sonnet",
        "input_price_per_mtok": "3.00",
        "output_price_per_mtok": "15.00",
        "context_window": 200000,
    },
    # OpenRouter aliases (map to OpenAI/Anthropic models)
    "openai/gpt-4.1": {
        "provider": "openrouter",
        "model_name": "GPT-4.1 (via OpenRouter)",
        "input_price_per_mtok": "2.11",  # +5.5% platform fee
        "output_price_per_mtok": "8.44",
        "context_window": 128000,
    },
    "openai/gpt-4.1-mini": {
        "provider": "openrouter",
        "model_name": "GPT-4.1 Mini (via OpenRouter)",
        "input_price_per_mtok": "0.42",
        "output_price_per_mtok": "1.69",
        "context_window": 128000,
    },
    "openai/gpt-4o": {
        "provider": "openrouter",
        "model_name": "GPT-4o (via OpenRouter)",
        "input_price_per_mtok": "2.64",
        "output_price_per_mtok": "10.55",
        "context_window": 128000,
    },
    "anthropic/claude-opus-4-20250514": {
        "provider": "openrouter",
        "model_name": "Claude Opus 4 (via OpenRouter)",
        "input_price_per_mtok": "5.28",
        "output_price_per_mtok": "26.38",
        "context_window": 200000,
    },
    "anthropic/claude-sonnet-4-20250514": {
        "provider": "openrouter",
        "model_name": "Claude Sonnet 4 (via OpenRouter)",
        "input_price_per_mtok": "3.17",
        "output_price_per_mtok": "15.83",
        "context_window": 200000,
    },
    "anthropic/claude-3-7-sonnet-20250219": {
        "provider": "openrouter",
        "model_name": "Claude 3.7 Sonnet (via OpenRouter)",
        "input_price_per_mtok": "3.17",
        "output_price_per_mtok": "15.83",
        "context_window": 200000,
    },
    # Current frontier models (May 2026).
    "gpt-5.5": {
        "provider": "openai",
        "model_name": "GPT-5.5",
        "input_price_per_mtok": "5.00",
        "output_price_per_mtok": "30.00",
        "context_window": 1050000,
    },
    "gpt-5.4": {
        "provider": "openai",
        "model_name": "GPT-5.4",
        "input_price_per_mtok": "2.50",
        "output_price_per_mtok": "15.00",
        "context_window": 400000,
    },
    "claude-opus-4-7": {
        "provider": "anthropic",
        "model_name": "Claude Opus 4.7",
        "input_price_per_mtok": "5.00",
        "output_price_per_mtok": "25.00",
        "context_window": 1000000,
    },
    "claude-sonnet-4-6": {
        "provider": "anthropic",
        "model_name": "Claude Sonnet 4.6",
        "input_price_per_mtok": "3.00",
        "output_price_per_mtok": "15.00",
        "context_window": 1000000,
    },
    "claude-haiku-4-5": {
        "provider": "anthropic",
        "model_name": "Claude Haiku 4.5",
        "input_price_per_mtok": "1.00",
        "output_price_per_mtok": "5.00",
        "context_window": 200000,
    },
}


class PricingManager:
    """Manages provider pricing and cost estimation."""

    def __init__(self, custom_pricing_file: Path | None = None) -> None:
        """Initialize pricing manager.

        Args:
            custom_pricing_file: Optional path to custom pricing JSON file.
        """
        self._pricing: dict[str, ProviderPricing] = {}
        self._custom_pricing_file = custom_pricing_file
        self._load_default_pricing()
        if custom_pricing_file and custom_pricing_file.exists():
            self._load_custom_pricing(custom_pricing_file)

    def _load_default_pricing(self) -> None:
        """Load default pricing data."""
        for model_id, data in DEFAULT_PRICING.items():
            self._pricing[model_id] = ProviderPricing(
                provider=Provider(data["provider"]),
                model_id=model_id,
                model_name=data["model_name"],
                input_price_per_mtok=Decimal(data["input_price_per_mtok"]),
                output_price_per_mtok=Decimal(data["output_price_per_mtok"]),
                context_window=data["context_window"],
            )

    def _load_custom_pricing(self, file_path: Path) -> None:
        """Load custom pricing from JSON file."""
        with open(file_path) as f:
            custom_data = json.load(f)
        for model_id, data in custom_data.items():
            self._pricing[model_id] = ProviderPricing(
                provider=Provider(data["provider"]),
                model_id=model_id,
                model_name=data["model_name"],
                input_price_per_mtok=Decimal(data["input_price_per_mtok"]),
                output_price_per_mtok=Decimal(data["output_price_per_mtok"]),
                context_window=data["context_window"],
            )

    def get_pricing(self, model_id: str) -> ProviderPricing | None:
        """Get pricing for a specific model.

        Args:
            model_id: Model identifier (e.g., "gpt-4o", "claude-opus-4-7")

        Returns:
            ProviderPricing if found, None otherwise.
        """
        # Direct lookup
        if model_id in self._pricing:
            return self._pricing[model_id]

        # Try common aliases
        aliases = self._get_model_aliases(model_id)
        for alias in aliases:
            if alias in self._pricing:
                return self._pricing[alias]

        return None

    def _get_model_aliases(self, model_id: str) -> list[str]:
        """Get possible aliases for a model ID."""
        aliases = []

        # OpenRouter format: provider/model
        if "/" in model_id:
            parts = model_id.split("/")
            if len(parts) == 2:
                provider, model = parts
                aliases.append(f"{provider}/{model}")
                # Also try without provider prefix
                aliases.append(model)

        # OpenAI aliases
        if model_id.startswith("gpt-"):
            aliases.append(model_id)
            # Handle version variations
            if "-" in model_id:
                base = model_id.split("-")[0]
                aliases.append(base)

        # Anthropic aliases
        if model_id.startswith("claude-"):
            aliases.append(model_id)
            # Try without date suffix
            if "-20" in model_id:
                base = model_id.split("-20")[0]
                aliases.append(base)

        return aliases

    def estimate_tokens(self, messages: list[dict[str, Any]], model_id: str) -> int:
        """Estimate token count for messages.

        Args:
            messages: List of chat messages.
            model_id: Model identifier for encoding.

        Returns:
            Estimated token count.
        """
        try:
            # Try to get appropriate encoding
            encoding = self._get_encoding(model_id)
        except Exception:
            # Fallback to cl100k_base (used by GPT-4, Claude)
            encoding = tiktoken.get_encoding("cl100k_base")

        total_tokens = 0

        for message in messages:
            # Base tokens per message
            total_tokens += 3  # <|start|>, role, <|end|>

            # Content tokens
            content = message.get("content", "")
            if content:
                total_tokens += len(encoding.encode(content))

            # Function calls / tool calls
            if "function_call" in message:
                total_tokens += len(encoding.encode(str(message["function_call"])))
            if "tool_calls" in message:
                total_tokens += len(encoding.encode(str(message["tool_calls"])))

        # Add completion tokens for assistant message
        total_tokens += 3  # <|start|>assistant<|end|>

        return total_tokens

    def _get_encoding(self, model_id: str) -> tiktoken.Encoding:
        """Get appropriate tokenizer for model."""
        # Map model IDs to tiktoken encodings
        if model_id.startswith("gpt-4") or model_id.startswith("gpt-5"):
            try:
                return tiktoken.encoding_for_model("gpt-4")
            except KeyError:
                return tiktoken.get_encoding("cl100k_base")
        elif "claude" in model_id.lower():
            # Claude uses cl100k_base
            return tiktoken.get_encoding("cl100k_base")
        else:
            # Default to cl100k_base
            return tiktoken.get_encoding("cl100k_base")

    def estimate_cost(
        self,
        request: CostEstimateRequest,
    ) -> CostEstimateResponse | None:
        """Estimate cost for a request.

        Args:
            request: Cost estimation request.

        Returns:
            Cost estimate response or None if pricing not found.
        """
        pricing = self.get_pricing(request.model_id)
        if pricing is None:
            return None

        # Estimate input tokens
        input_tokens = self.estimate_tokens(request.messages, request.model_id)

        # Use provided output estimate or default
        output_tokens = request.estimated_output_tokens

        # Calculate cost
        estimated_cost = pricing.estimate_cost(input_tokens, output_tokens)

        return CostEstimateResponse(
            estimated_input_tokens=input_tokens,
            estimated_output_tokens=output_tokens,
            estimated_total_tokens=input_tokens + output_tokens,
            estimated_cost=estimated_cost,
            pricing_used=pricing,
            safe_mode_required=False,  # Set by caller based on threshold
            safe_mode_threshold=Decimal("0.00"),  # Set by caller
        )

    def calculate_actual_cost(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
    ) -> Decimal | None:
        """Calculate actual cost from token counts.

        Args:
            model_id: Model identifier.
            input_tokens: Number of input tokens.
            output_tokens: Number of output tokens.

        Returns:
            Calculated cost or None if pricing not found.
        """
        pricing = self.get_pricing(model_id)
        if pricing is None:
            return None
        return pricing.estimate_cost(input_tokens, output_tokens)

    def list_available_models(self, provider: Provider | None = None) -> list[ProviderPricing]:
        """List available models with pricing.

        Args:
            provider: Optional provider filter.

        Returns:
            List of pricing information.
        """
        models = list(self._pricing.values())
        if provider:
            models = [m for m in models if m.provider == provider]
        return models

    def add_custom_pricing(self, pricing: ProviderPricing) -> None:
        """Add or update custom pricing.

        Args:
            pricing: Pricing information to add.
        """
        self._pricing[pricing.model_id] = pricing

        # Save to custom pricing file if configured
        if self._custom_pricing_file:
            self._save_custom_pricing()

    def _save_custom_pricing(self) -> None:
        """Save custom pricing to file."""
        if not self._custom_pricing_file:
            return

        custom_data = {}
        for model_id, pricing in self._pricing.items():
            if model_id not in DEFAULT_PRICING:
                custom_data[model_id] = {
                    "provider": pricing.provider.value,
                    "model_name": pricing.model_name,
                    "input_price_per_mtok": str(pricing.input_price_per_mtok),
                    "output_price_per_mtok": str(pricing.output_price_per_mtok),
                    "context_window": pricing.context_window,
                }

        self._custom_pricing_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._custom_pricing_file, "w") as f:
            json.dump(custom_data, f, indent=2)


# Global pricing manager instance
_pricing_manager: PricingManager | None = None


def get_pricing_manager(custom_pricing_file: Path | None = None) -> PricingManager:
    """Get or create global pricing manager instance."""
    global _pricing_manager
    if _pricing_manager is None:
        _pricing_manager = PricingManager(custom_pricing_file)
    return _pricing_manager


def reset_pricing_manager() -> None:
    """Reset global pricing manager (for testing)."""
    global _pricing_manager
    _pricing_manager = None
