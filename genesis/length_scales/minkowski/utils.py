"""
utilities for making statistical fits of models to minkowski scales
"""
import numpy as np
import matplotlib.pyplot as plt
import xarray as xr

def cdf(v, ax=None):
    y = 1.0/(np.arange(len(v))+1.)
    x = np.sort(v)[::-1]
    if ax is None:
        ax = plt.gca()
    ax.plot(x, y, marker='.')
    ax.set_title('cdf')

def rank(v, ax):
    y = np.sort(v)
    x = np.arange(len(y))
    ax.plot(x, y, marker='.')
    ax.set_title('rank')

def fixed_bin_hist(v, dv, ax, **kwargs):
    vmin = np.floor(v.min()/dv)*dv
    vmax = np.ceil(v.max()/dv)*dv
    nbins = int((vmax-vmin)/dv)
    ax.hist(v, range=(vmin, vmax), bins=nbins, **kwargs)

def dist_plot(v, dv_bin, fit=None, axes=None, log_dists=True, **kwargs):
    da_v = None
    if isinstance(v, xr.DataArray):
        da_v = v
        v = da_v.values

    if axes is None:
        fig, axes = plt.subplots(ncols=4, figsize=(16, 4))
    else:
        fig = axes[0].figure
    ax = axes[0]
    fixed_bin_hist(v=v, dv=dv_bin, ax=ax, density=False, **kwargs)
    ax.set_title('hist')

    ax = axes[1]
    fixed_bin_hist(v=v, dv=dv_bin, ax=ax, density=True, **kwargs)
    ax.set_title('pdf')

    if fit:
        if fit[0] == 'exp':
            beta, vrange_fit = fit[1:]
            beta_std = None
            if type(beta) == tuple:
                beta, beta_std = beta
            Ntot = len(v)
            C = np.exp(vrange_fit[0]/beta)
            v_ = np.linspace(*vrange_fit, 100)
            ax.plot(v_, C/beta*np.exp(-v_/beta), color='red')
            ax.axvline(beta+vrange_fit[0], linestyle='--', color='red')
            if da_v is not None:
                units = da_v.units
            else:
                units = ''
            if beta_std is None:
                ax.text(0.9, 0.3, r"$\beta={:.0f}{}$".format(beta, units),
                        transform=ax.transAxes, horizontalalignment='right')
            else:
                ax.text(0.9, 0.3, r"$\beta={:.0f}\pm{:.0f}{}$".format(
                            beta, beta_std, units),
                        transform=ax.transAxes, horizontalalignment='right')
        else:
            raise NotImplementedError(fit)

    ax = axes[2]
    cdf(v, ax=ax)

    ax = axes[3]
    rank(v, ax=ax)

    if log_dists:
        [ax.set_yscale('log') for ax in axes[:2]]

    if da_v is not None:
        labels = [
            ('{} [{}]'.format(da_v.name, da_v.units), 'num objects [1]'),
            ('{} [{}]'.format(da_v.name, da_v.units), 'density [1/{}]'.format(da_v.units)),
            ('{} [{}]'.format(da_v.name, da_v.units), 'fraction of objects'),
            ('object num', '{} [{}]'.format(da_v.name, da_v.units)),
        ]

        for ax, (xl, yl) in zip(axes, labels):
            ax.set_xlabel(xl)
            ax.set_ylabel(yl)

    return fig, axes