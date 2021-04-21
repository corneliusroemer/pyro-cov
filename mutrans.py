#!/usr/bin/env python

import argparse
import functools
import logging
import os
import re
from timeit import default_timer

import torch

from pyrocov import mutrans

logger = logging.getLogger(__name__)
logging.basicConfig(format="%(relativeCreated) 9d %(message)s", level=logging.INFO)


def cached(filename):
    def decorator(fn):
        @functools.wraps(fn)
        def cached_fn(*args, **kwargs):
            f = filename(*args, **kwargs) if callable(filename) else filename
            if not os.path.exists(f):
                result = fn(*args, **kwargs)
                logger.info(f"saving {f}")
                torch.save(result, f)
            else:
                logger.info(f"loading cached {f}")
                result = torch.load(f, map_location=torch.empty(()).device)
            return result

        return cached_fn

    return decorator


def _safe_str(v):
    v = str(v)
    v = re.sub("[^A-Za-x0-9-]", "_", v)
    return v


def _load_data_filename(args, **kwargs):
    parts = ["data", str(args.max_feature_order)]
    for k, v in sorted(kwargs.get("include", {}).items()):
        parts.append(f"I{k}={_safe_str(v)}")
    for k, v in sorted(kwargs.get("exclude", {}).items()):
        parts.append(f"E{k}={_safe_str(v)}")
    return "results/mutrans.{}.pt".format(".".join(parts))


@cached(_load_data_filename)
def load_data(args, **kwargs):
    return mutrans.load_gisaid_data(
        max_feature_order=args.max_feature_order, device=args.device, **kwargs
    )


def _fit_filename(name, *args):
    strs = [name]
    for arg in args[2:]:
        if isinstance(arg, tuple):
            strs.append("-".join(f"{k}={_safe_str(v)}" for k, v in arg))
        else:
            strs.append(str(arg))
    return "results/mutrans.{}.pt".format(".".join(strs))


@cached(lambda *args: _fit_filename("svi", *args))
def fit_svi(
    args,
    dataset,
    guide_type="mvn_dependent",
    n=1001,
    lr=0.01,
    lrd=0.1,
    holdout=(),
):
    start_time = default_timer()
    result = mutrans.fit_svi(
        dataset,
        guide_type=guide_type,
        num_steps=n,
        learning_rate=lr,
        learning_rate_decay=lrd,
        log_every=args.log_every,
        seed=args.seed,
    )
    result["walltime"] = default_timer() - start_time

    result["args"] = args
    return result


@cached(lambda *args: _fit_filename("mcmc", *args))
def fit_mcmc(
    args,
    dataset,
    model_type="dependent",
    num_steps=10001,
    num_warmup=1000,
    num_samples=1000,
    max_tree_depth=10,
    holdout=(),
):
    svi_params = fit_svi(
        args,
        dataset,
        "mvn_dependent",
        num_steps,
        0.01,
        0.1,
        holdout,
    )["params"]

    start_time = default_timer()
    result = mutrans.fit_mcmc(
        dataset,
        svi_params,
        model_type=model_type,
        num_warmup=num_warmup,
        num_samples=num_samples,
        max_tree_depth=max_tree_depth,
        log_every=args.log_every,
        seed=args.seed,
    )
    result["walltime"] = default_timer() - start_time

    result["args"] = args
    return result


def main(args):
    torch.set_default_dtype(torch.double)
    if args.cuda:
        torch.set_default_tensor_type(torch.cuda.DoubleTensor)
    if args.debug:
        torch.autograd.set_detect_anomaly(True)

    # Run MCMC.
    if args.mcmc:
        dataset = load_data(args)
        fit_mcmc(
            args,
            dataset,
            args.model_type,
            args.num_steps,
            args.num_warmup,
            args.num_samples,
            args.max_tree_depth,
        )
        return

    # Configure guides.
    svi_config = (
        args.guide_type,
        args.num_steps,
        args.learning_rate,
        args.learning_rate_decay,
    )
    if args.svi:
        dataset = load_data(args)
        fit_svi(args, dataset, *svi_config)
        return
    # guide_type, n, lr, lrd
    inference_configs = [
        svi_config,
        (
            "mcmc",
            "naive",
            args.num_steps,
            args.num_warmup,
            args.num_samples,
            args.max_tree_depth,
        ),
        (
            "mcmc",
            "dependent",
            args.num_steps,
            args.num_warmup,
            args.num_samples,
            args.max_tree_depth,
        ),
        (
            "mcmc",
            "conditioned",
            args.num_steps,
            args.num_warmup,
            args.num_samples,
            args.max_tree_depth,
        ),
        (
            "mcmc",
            "preconditioned",
            args.num_steps,
            args.num_warmup,
            args.num_samples,
            args.max_tree_depth,
        ),
        ("map", 1001, 0.05, 1.0),
        ("normal_delta", 2001, 0.05, 0.1),
        ("normal", 2001, 0.05, 0.1),
        ("mvn_delta", 10001, 0.01, 0.1),
        ("mvn_normal", 10001, 0.01, 0.1),
        ("mvn_delta_dependent", 10001, 0.01, 0.1),
        ("mvn_normal_dependent", 10001, 0.01, 0.1),
    ]

    # Configure data holdouts.
    empty_holdout = ()
    holdouts = [
        {"exclude": {"location": "^Europe / United Kingdom"}},
        {"exclude": {"location": "^North America / USA"}},
        {"include": {"location": "^Europe / United Kingdom"}},
        {"include": {"location": "^North America / USA"}},
        {"include": {"virus_name": "^hCoV-19/USA/..-CDC-"}},
        {"include": {"virus_name": "^hCoV-19/USA/..-CDC-2-"}},
    ]

    configs = [c + (empty_holdout,) for c in inference_configs]
    for holdout in holdouts:
        holdout = tuple(
            (k, tuple(sorted(v.items()))) for k, v in sorted(holdout.items())
        )
        configs.append(svi_config + (holdout,))

    # Sequentially fit models.
    result = {}
    for config in configs:
        logger.info(f"Config: {config}")
        holdout = {k: dict(v) for k, v in config[-1]}
        dataset = load_data(args, **holdout)
        if config[0] == "mcmc":
            result[config] = fit_mcmc(args, dataset, *config[1:])
        else:
            result[config] = fit_svi(args, dataset, *config)
        result[config]["mutations"] = dataset["mutations"]
    logger.info("saving results/mutrans.pt")
    torch.save(result, "results/mutrans.pt")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fit mutation-transmissibility models")
    parser.add_argument("--max-feature-order", default=0, type=int)
    parser.add_argument("--svi", action="store_true", help="run only SVI inference")
    parser.add_argument("--mcmc", action="store_true", help="run only MCMC inference")
    parser.add_argument("-g", "--guide-type", default="mvn_dependent")
    parser.add_argument("-m", "--model-type", default="dependent")
    parser.add_argument("-n", "--num-steps", default=10001, type=int)
    parser.add_argument("-lr", "--learning-rate", default=0.01, type=float)
    parser.add_argument("-lrd", "--learning-rate-decay", default=0.1, type=float)
    parser.add_argument("--num-warmup", default=1000, type=int)
    parser.add_argument("--num-samples", default=1000, type=int)
    parser.add_argument("--max-tree-depth", default=10, type=int)
    parser.add_argument(
        "--cuda", action="store_true", default=torch.cuda.is_available()
    )
    parser.add_argument("--cpu", dest="cuda", action="store_false")
    parser.add_argument("--seed", default=20210319, type=int)
    parser.add_argument("-l", "--log-every", default=50, type=int)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    args.device = "cuda" if args.cuda else "cpu"
    main(args)
