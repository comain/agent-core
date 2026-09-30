"""Operator commands; benchmark HTTP is restricted to the refresh operation."""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict

from .cache import CatalogUnavailable
from .configuration import load_selection_config
from .refresh import refresh_catalog
from .runtime import DiscoveryRuntime
from .sources import SourceError


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("refresh", "explain"))
    parser.add_argument("--config", required=True, help="absolute operator-owned JSON path")
    args = parser.parse_args(argv)
    try:
        config = load_selection_config(args.config, environ=os.environ)
        if args.operation == "refresh":
            output = asyncio.run(refresh_catalog(config, environ=os.environ))
        else:
            result = DiscoveryRuntime(config, environ=os.environ).resolve(require_available=False)
            output = {"application": config.policy.application_id,
                      "minimum_coding_score": config.policy.minimum_coding_score,
                      "ranking_strategy": config.policy.ranking_strategy,
                      "threshold_source": config.threshold_source,
                      "attribution": "Artificial Analysis https://artificialanalysis.ai/",
                      "pricing_attribution": "OpenRouter https://openrouter.ai/",
                      "pricing_discounts": {p.id: p.pricing_discount for p in config.providers},
                      "price_weights": config.price_weights.model_dump(mode="json"),
                      "models": [c.model_dump(mode="json") for c in result.candidates],
                      "decisions": [asdict(d) for d in result.decisions]}
        print(json.dumps(output, sort_keys=True))
        return 0
    except (ValueError, OSError, TimeoutError) as error:
        if isinstance(error, (CatalogUnavailable, SourceError)) or type(error) is ValueError:
            message = str(error)
        else:
            message = type(error).__name__
        print("model selection failed: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
