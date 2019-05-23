import numpy as np
import xarray as xr

from .metrics import POSITIVELY_ORIENTED_METRICS
from .prediction import (compute_hindcast, compute_perfect_model,
                         compute_persistence)
from .stats import DPP, varweighted_mean_period


def _distribution_to_ci(ds, ci_low, ci_high, dim='bootstrap'):
    """Get confidence intervals from bootstrapped distribution.

    Needed for bootstrapping confidence intervals and p_values of a metric.

    Args:
        ds (xarray object): distribution.
        ci_low (float): low confidence interval.
        ci_high (float): high confidence interval.
        dim (str): dimension to apply xr.quantile to. Default: 'bootstrap'

    Returns:
        uninit_hind (xarray object): uninitialize hindcast with hind.coords.
    """
    # TODO: get rid of try, except logic
    try:  # incase of lazy results compute
        if len(ds.chunks) >= 1:
            ds = ds.compute()
    except:
        pass
    ds_ci = ds.quantile(q=[ci_low, ci_high], dim=dim)
    return ds_ci


def _pvalue_from_distributions(simple_fct, init, metric='pearson_r'):
    """Get probability that skill of a simple forecast (e.g., persistence or
    uninitlaized skill) is larger than initialized skill.

    Needed for bootstrapping confidence intervals and p_values of a metric in
    the hindcast framework. Checks whether a simple forecast like persistence
    or uninitialized performs better than initialized forecast. Need to keep in
    mind the orientation of metric (whether larger values are better or worse
    than smaller ones.)

    Args:
        simple_fct (xarray object): persistence or uninit skill.
        init (xarray object): hindcast skill.
        metric (str): name of metric

    Returns:
        pv (xarray object): probability that simple forecast performs better
                            than initialized forecast.
    """
    pv = ((simple_fct - init) > 0).sum('bootstrap') / init.bootstrap.size
    if metric not in POSITIVELY_ORIENTED_METRICS:
        pv = 1 - pv
    return pv


def bootstrap_uninitialized_ensemble(hind, hist):
    """Resample uninitialized hindcast from historical members.

    Needed for bootstrapping confidence intervals and p_values of a metric in
    the hindcast framework. Takes hind.lead.size timesteps from historical at
    same forcing and rearranges them into ensemble and member dimensions.

    Args:
        hind (xarray object): hindcast.
        hist (xarray object): historical uninitialized.

    Returns:
        uninit_hind (xarray object): uninitialize hindcast with hind.coords.
    """
    # find range for bootstrapping
    if 'member' not in hist.dims:
        raise ValueError(
            'Please supply a historical ensemble with a member dimension.')

    first_init = max(hist.time.min().values, hind['init'].min().values)
    last_init = min(hist.time.max().values - hind['lead'].size,
                    hind['init'].max().values)
    hind = hind.sel(init=slice(first_init, last_init))

    uninit_hind = []
    for init in hind.init.values:
        random_members = np.random.choice(hist.member.values, hind.member.size)
        # take random uninitialized members from hist at init forcing
        # (Goddard allows 5 year forcing range here)
        uninit_at_one_init_year = hist.sel(
            time=slice(init + 1, init + hind['lead'].size),
            member=random_members).rename({'time': 'lead'})
        uninit_at_one_init_year['lead'] = np.arange(
            1, 1 + uninit_at_one_init_year['lead'].size)
        uninit_at_one_init_year['member'] = np.arange(1,
                                                      1 + len(random_members))
        uninit_hind.append(uninit_at_one_init_year)
    uninit_hind = xr.concat(uninit_hind, 'init')
    uninit_hind['init'] = hind['init'].values
    return uninit_hind


def bootstrap_uninit_pm_ensemble_from_control(ds, control):
    """
    Create a pseudo-ensemble from control run.

    Needed for block bootstrapping confidence intervals of a metric in perfect
    model framework. Takes randomly segments of length of ensemble dataset from
    control and rearranges them into ensemble and member dimensions.

    Args:
        ds (xarray object): ensemble simulation.
        control (xarray object): control simulation.

    Returns:
        ds_e (xarray object): pseudo-ensemble generated from control run.
    """
    nens = ds.init.size
    nmember = ds.member.size
    length = ds.lead.size
    c_start = 0
    c_end = control['time'].size
    lead_time = ds['lead']

    def isel_years(control, year_s, length):
        new = control.isel(time=slice(year_s, year_s + length))
        new = new.rename({'time': 'lead'})
        new['lead'] = lead_time
        return new

    def create_pseudo_members(control):
        startlist = np.random.randint(c_start, c_end - length - 1, nmember)
        return xr.concat(
            (isel_years(control, start, length) for start in startlist),
            'member')

    return xr.concat((create_pseudo_members(control) for _ in range(nens)),
                     'init')


def DPP_threshold(control, sig=95, bootstrap=500, **dpp_kwargs):
    """Calc DPP from re-sampled dataset.

    Reference:
    * Feng, X., T. DelSole, and P. Houser. “Bootstrap Estimated Seasonal
        Potential Predictability of Global Temperature and Precipitation.”
        Geophysical Research Letters 38, no. 7 (2011).
        https://doi.org/10/ft272w.

    """
    bootstraped_results = []
    time = control.time.values
    for _ in range(bootstrap):
        smp_time = np.random.choice(time, len(time))
        smp_control = control.sel(time=smp_time)
        smp_control['time'] = time
        bootstraped_results.append(DPP(smp_control, **dpp_kwargs))
    threshold = xr.concat(bootstraped_results,
                          'bootstrap').quantile(sig / 100, 'bootstrap')
    return threshold


def varweighted_mean_period_threshold(control,
                                      sig=95,
                                      bootstrap=500,
                                      **vwmp_kwargs):
    """Calc vwmp from re-sampled dataset.

    """
    bootstraped_results = []
    time = control.time.values
    for _ in range(bootstrap):
        smp_time = np.random.choice(time, len(time))
        smp_control = control.sel(time=smp_time)
        smp_control['time'] = time
        bootstraped_results.append(
            varweighted_mean_period(smp_control, **vwmp_kwargs))
    threshold = xr.concat(bootstraped_results,
                          'bootstrap').quantile(sig / 100, 'bootstrap')
    return threshold


def bootstrap_compute(hind,
                      reference,
                      hist=None,
                      metric='pearson_r',
                      comparison='m2e',
                      sig=95,
                      bootstrap=500,
                      pers_sig=None,
                      compute=compute_hindcast,
                      resample_uninit=bootstrap_uninitialized_ensemble):
    """Bootstrap compute with replacement.

    Reference:
      * Goddard, L., A. Kumar, A. Solomon, D. Smith, G. Boer, P.
            Gonzalez, V. Kharin, et al. “A Verification Framework for
            Interannual-to-Decadal Predictions Experiments.” Climate
            Dynamics 40, no. 1–2 (January 1, 2013): 245–72.
            https://doi.org/10/f4jjvf.

    Args:
        hind (xr.Dataset): prediction ensemble.
        reference (xr.Dataset): reference simulation.
        hist (xr.Dataset): historical/uninitialized simulation.
        metric (str): `metric`. Defaults to 'pearson_r'.
        comparison (str): `comparison`. Defaults to 'm2e'.
        sig (int): Significance level for uninitialized and
                   initialized skill. Defaults to 95.
        bootstrap (int): number of resampling iterations (bootstrap
                         with replacement). Defaults to 500.
        compute_uninitialized_skill (bool): Defaults to True.
        compute_persistence_skill (bool): Defaults to True.
        nlags (type): number of lags persistence forecast skill.
                      Defaults to hind.lead.size.

    Returns:
        results: (xr.Dataset): bootstrapped results
        ...contains...
        init_ci (xr.Dataset): confidence levels of init_skill
        uninit_ci (xr.Dataset): confidence levels of uninit_skill
        p_uninit_over_init (xr.Dataset): p-value of the hypothesis
                                         that the difference of
                                         skill between the
                                         initialized and uninitialized
                                         simulations is smaller or
                                         equal to zero based on
                                         bootstrapping with
                                         replacement.
                                         Defaults to None.
        pers_ci (xr.Dataset): confidence levels of pers_skill
        p_pers_over_init (xr.Dataset): p-value of the hypothesis
                                       that the difference of
                                       skill between the
                                       initialized and persistence
                                       simulations is smaller or
                                       equal to zero based on
                                       bootstrapping with
                                       replacement.
                                       Defaults to None.

    """
    if pers_sig is None:
        pers_sig = sig

    p = (100 - sig) / 100  # 0.05
    ci_low = p / 2  # 0.025
    ci_high = 1 - p / 2  # 0.975
    p_pers = (100 - pers_sig) / 100  # 0.5
    ci_low_pers = p_pers / 2
    ci_high_pers = 1 - p_pers / 2

    inits = hind.init.values
    init = []
    uninit = []
    pers = []
    # resample with replacement
    # DoTo: parallelize loop
    for _ in range(bootstrap):
        smp = np.random.choice(inits, len(inits))
        smp_hind = hind.sel(init=smp)
        # compute init skill
        init.append(
            compute(smp_hind, reference, metric=metric, comparison=comparison))
        # generate uninitialized ensemble from hist
        if hist is None:  # PM path, use reference = control
            hist = reference
        uninit_hind = resample_uninit(hind, hist)
        # compute uninit skill
        uninit.append(
            compute(uninit_hind,
                    reference,
                    metric=metric,
                    comparison=comparison))
        # compute persistence skill
        pers.append(compute_persistence(smp_hind, reference, metric=metric))
    init = xr.concat(init, dim='bootstrap')
    uninit = xr.concat(uninit, dim='bootstrap')
    pers = xr.concat(pers, dim='bootstrap')

    if set(pers.coords) != set(init.coords):
        init, pers = xr.broadcast(init, pers)

    init_ci = _distribution_to_ci(init, ci_low, ci_high)
    uninit_ci = _distribution_to_ci(uninit, ci_low, ci_high)
    pers_ci = _distribution_to_ci(pers, ci_low_pers, ci_high_pers)

    p_uninit_over_init = _pvalue_from_distributions(uninit, init)
    p_pers_over_init = _pvalue_from_distributions(pers, init)

    # calc skill
    init_skill = compute(hind, reference, metric=metric, comparison=comparison)
    uninit_skill = uninit.mean('bootstrap')
    pers_skill = compute_persistence(hind, reference, metric=metric)

    if set(pers_skill.coords) != set(init_skill.coords):
        init_skill, pers_skill = xr.broadcast(init_skill, pers_skill)

    # wrap results together in one dataarray
    skill = xr.concat([init_skill, uninit_skill, pers_skill], 'kind')
    skill['kind'] = ['init', 'uninit', 'pers']

    # probability that i beats init
    p = xr.concat([p_uninit_over_init, p_pers_over_init], 'kind')
    p['kind'] = ['uninit', 'pers']

    # ci for each skill
    ci = xr.concat([init_ci, uninit_ci, pers_ci],
                   'kind').rename({'quantile': 'results'})
    ci['kind'] = ['init', 'uninit', 'pers']

    results = xr.concat([skill, p], 'results')
    results['results'] = ['skill', 'p']
    if set(results.coords) != set(ci.coords):
        res_drop = [c for c in results.coords if c not in ci.coords]
        ci_drop = [c for c in ci.coords if c not in results.coords]
        results = results.drop(res_drop)
        ci = ci.drop(ci_drop)
    results = xr.concat([results, ci], 'results')
    return results


def bootstrap_hindcast(hind,
                       hist,
                       reference,
                       metric='pearson_r',
                       comparison='e2r',
                       sig=95,
                       bootstrap=500,
                       pers_sig=None):
    """Wrapper for bootstrap_compute for hindcasts."""
    return bootstrap_compute(hind,
                             reference,
                             hist=hist,
                             metric=metric,
                             comparison=comparison,
                             sig=sig,
                             bootstrap=bootstrap,
                             pers_sig=pers_sig,
                             compute=compute_hindcast,
                             resample_uninit=bootstrap_uninitialized_ensemble)


def bootstrap_perfect_model(ds,
                            control,
                            metric='pearson_r',
                            comparison='m2e',
                            sig=95,
                            bootstrap=500,
                            pers_sig=None):
    """Wrapper for bootstrap_compute for perfect-model in steady state."""
    return bootstrap_compute(
        ds,
        control,
        hist=None,
        metric=metric,
        comparison=comparison,
        sig=sig,
        bootstrap=bootstrap,
        pers_sig=pers_sig,
        compute=compute_perfect_model,
        resample_uninit=bootstrap_uninit_pm_ensemble_from_control)
