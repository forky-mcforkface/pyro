# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

import contextlib

import funsor

from pyro.distributions.util import copy_docs_from
from pyro.infer import TraceEnum_ELBO as _OrigTraceEnum_ELBO

from pyro.contrib.funsor import to_data, to_funsor
from pyro.contrib.funsor.handlers import enum, plate, replay, trace
from pyro.contrib.funsor.infer.elbo import ELBO, Jit_ELBO


def terms_from_trace(tr):
    """Helper function to extract elbo components from execution traces."""
    # data structure containing densities, measures, scales, and identification
    # of free variables as either product (plate) variables or sum (measure) variables
    terms = {"log_factors": [], "log_measures": [], "scale": to_funsor(1.),
             "plate_vars": frozenset(), "measure_vars": frozenset(), "plate_to_step": dict()}
    for name, node in tr.nodes.items():
        # add markov dimensions to the plate_to_step dictionary
        if node["type"] == "markov_chain":
            terms["plate_to_step"][node["name"]] = node["value"]
        if node["type"] != "sample" or type(node["fn"]).__name__ == "_Subsample" or \
                node["infer"].get("_do_not_score", False):
            continue
        # grab plate dimensions from the cond_indep_stack
        terms["plate_vars"] |= frozenset(f.name for f in node["cond_indep_stack"] if f.vectorized)
        # grab the log-measure, found only at sites that are not replayed or observed
        if node["funsor"].get("log_measure", None) is not None:
            terms["log_measures"].append(node["funsor"]["log_measure"])
            # sum (measure) variables: the fresh non-plate variables at a site
            terms["measure_vars"] |= (frozenset(node["funsor"]["value"].inputs) | {name}) - terms["plate_vars"]
        # grab the scale, assuming a common subsampling scale
        if node.get("replay_active", False) and set(node["funsor"]["log_prob"].inputs) & terms["measure_vars"] and \
                float(to_data(node["funsor"]["scale"])) != 1.:
            # model site that depends on enumerated variable: common scale
            terms["scale"] = node["funsor"]["scale"]
        else:  # otherwise: default scale behavior
            node["funsor"]["log_prob"] = node["funsor"]["log_prob"] * node["funsor"]["scale"]
        # grab the log-density, found at all sites except those that are not replayed
        if node["is_observed"] or not node.get("replay_skipped", False):
            terms["log_factors"].append(node["funsor"]["log_prob"])
    # add plate dimensions to the plate_to_step dictionary
    terms["plate_to_step"].update({plate: terms["plate_to_step"].get(plate, {}) for plate in terms["plate_vars"]})
    return terms


@copy_docs_from(_OrigTraceEnum_ELBO)
class TraceMarkovEnum_ELBO(ELBO):

    def differentiable_loss(self, model, guide, *args, **kwargs):

        # get batched, enumerated, to_funsor-ed traces from the guide and model
        with plate(size=self.num_particles) if self.num_particles > 1 else contextlib.ExitStack(), \
                enum(first_available_dim=(-self.max_plate_nesting-1) if self.max_plate_nesting else None):
            guide_tr = trace(guide).get_trace(*args, **kwargs)
            model_tr = trace(replay(model, trace=guide_tr)).get_trace(*args, **kwargs)

        # extract from traces all metadata that we will need to compute the elbo
        guide_terms = terms_from_trace(guide_tr)
        model_terms = terms_from_trace(model_tr)

        # build up a lazy expression for the elbo
        with funsor.interpreter.interpretation(funsor.terms.lazy):
            # identify and contract out auxiliary variables in the model with partial_sum_product
            contracted_factors, uncontracted_factors = [], []
            for f in model_terms["log_factors"]:
                if model_terms["measure_vars"].intersection(f.inputs):
                    contracted_factors.append(f)
                else:
                    uncontracted_factors.append(f)
            # incorporate the effects of subsampling and handlers.scale through a common scale factor
            markov_dims = frozenset({
                    plate for plate, step in model_terms["plate_to_step"].items() if step})
            contracted_costs = [model_terms["scale"] * f for f in funsor.sum_product.modified_partial_sum_product(
                funsor.ops.logaddexp, funsor.ops.add,
                model_terms["log_measures"] + contracted_factors,
                plate_to_step=model_terms["plate_to_step"],
                eliminate=model_terms["measure_vars"] | markov_dims
            )]

            costs = contracted_costs + uncontracted_factors  # model costs: logp
            costs += [-f for f in guide_terms["log_factors"]]  # guide costs: -logq

        # finally, integrate out guide variables in the elbo and all plates
        guide_markov_dims = frozenset(plate for plate, step in guide_terms["plate_to_step"].items() if step)
        # first compute all marginal logqs eagerly in a single forward-backward pass
        # we create dummy factors for each model cost to ensure all required marginals are computed
        targets = []
        for cost in contracted_costs + uncontracted_factors:
            target = funsor.Tensor(funsor.ops.new_zeros(funsor.tensor.get_default_prototype(), ()).expand(
                tuple(v.size for v in cost.inputs.values())), cost.inputs, cost.dtype)
            targets.append(target)
        targets += guide_terms["log_measures"]
        with funsor.interpreter.interpretation(funsor.terms.lazy):
            logzq = sum(funsor.sum_product.modified_partial_sum_product(
                funsor.ops.logaddexp, funsor.ops.add,
                targets,
                plate_to_step=guide_terms["plate_to_step"],
                eliminate=guide_terms["measure_vars"] | guide_markov_dims
            ))

        with funsor.adjoint.AdjointTape() as tape:
            logzq = funsor.optimizer.apply_optimizer(logzq)
        log_qs = tuple(tape.adjoint(funsor.ops.logaddexp, funsor.ops.add, logzq, tuple(targets)).values())

        with funsor.interpreter.interpretation(funsor.terms.lazy):
            elbo = to_funsor(0, output=funsor.Real)
            for cost in costs:
                # look up the marginal logq in the guide corresponding to this cost term
                log_prob = next(iter(log_q for log_q in log_qs if dict(log_q.inputs) == dict(cost.inputs)))
                # compute the expected cost term E_q[logp] or E_q[-logq] using the marginal logq for q
                elbo_term = funsor.Integrate(
                    log_prob, cost, frozenset(cost.inputs) - frozenset(guide_terms["plate_to_step"]))
                elbo += elbo_term.reduce(funsor.ops.add)

        # evaluate the elbo, using memoize to share tensor computation where possible
        with funsor.memoize.memoize():
            return -to_data(funsor.optimizer.apply_optimizer(elbo))


@copy_docs_from(_OrigTraceEnum_ELBO)
class TraceEnum_ELBO(ELBO):

    def differentiable_loss(self, model, guide, *args, **kwargs):

        # get batched, enumerated, to_funsor-ed traces from the guide and model
        with plate(size=self.num_particles) if self.num_particles > 1 else contextlib.ExitStack(), \
                enum(first_available_dim=(-self.max_plate_nesting-1) if self.max_plate_nesting else None):
            guide_tr = trace(guide).get_trace(*args, **kwargs)
            model_tr = trace(replay(model, trace=guide_tr)).get_trace(*args, **kwargs)

        # extract from traces all metadata that we will need to compute the elbo
        guide_terms = terms_from_trace(guide_tr)
        model_terms = terms_from_trace(model_tr)

        # build up a lazy expression for the elbo
        with funsor.interpreter.interpretation(funsor.terms.lazy):
            # identify and contract out auxiliary variables in the model with partial_sum_product
            contracted_factors, uncontracted_factors = [], []
            for f in model_terms["log_factors"]:
                if model_terms["measure_vars"].intersection(f.inputs):
                    contracted_factors.append(f)
                else:
                    uncontracted_factors.append(f)
            # incorporate the effects of subsampling and handlers.scale through a common scale factor
            contracted_costs = [model_terms["scale"] * f for f in funsor.sum_product.partial_sum_product(
                funsor.ops.logaddexp, funsor.ops.add,
                model_terms["log_measures"] + contracted_factors,
                plates=model_terms["plate_vars"], eliminate=model_terms["measure_vars"]
            )]

            costs = contracted_costs + uncontracted_factors  # model costs: logp
            costs += [-f for f in guide_terms["log_factors"]]  # guide costs: -logq

            # finally, integrate out guide variables in the elbo and all plates
            plate_vars = guide_terms["plate_vars"] | model_terms["plate_vars"]
            elbo = to_funsor(0, output=funsor.Real)
            for cost in costs:
                # compute the marginal logq in the guide corresponding to this cost term
                log_prob = funsor.sum_product.sum_product(
                    funsor.ops.logaddexp, funsor.ops.add,
                    guide_terms["log_measures"],
                    plates=plate_vars,
                    eliminate=(plate_vars | guide_terms["measure_vars"]) - frozenset(cost.inputs)
                )
                # compute the expected cost term E_q[logp] or E_q[-logq] using the marginal logq for q
                elbo_term = funsor.Integrate(log_prob, cost, guide_terms["measure_vars"] & frozenset(cost.inputs))
                elbo += elbo_term.reduce(funsor.ops.add, plate_vars & frozenset(cost.inputs))

        # evaluate the elbo, using memoize to share tensor computation where possible
        with funsor.memoize.memoize():
            return -to_data(funsor.optimizer.apply_optimizer(elbo))


class JitTraceEnum_ELBO(Jit_ELBO, TraceEnum_ELBO):
    pass
