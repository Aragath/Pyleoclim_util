#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tue Feb 25 08:14:31 2020

@author: deborahkhider

Functions concerning wavelet analysis
"""


__all__ = [
    'wwz',
    'xwc',
]

import numpy as np
import statsmodels.api as sm
from scipy import optimize, signal
from pathos.multiprocessing import ProcessingPool as Pool
import numba as nb
from numba.errors import NumbaPerformanceWarning
import warnings
import collections
import scipy.fftpack as fft

from .tsutils import standardize as std
from .tsutils import gaussianize as gauss
from .tsutils import clean_ts
from .tsmodel import ar1_sim

warnings.filterwarnings("ignore", category=NumbaPerformanceWarning)

#---------------
#Wrapper functions
#---------------

#----------------
#Main Functions
#----------------

class AliasFilter(object):
    '''Performing anti-alias filter on a psd @author: fzhu
    '''

    def alias_filter(self, freq, pwr, fs, fc, f_limit, avgs):
        ''' anti_alias filter

        Args
        ----

        freq : array
            vector of frequencies in power spectrum
        pwr : array
            vector of spectral power corresponding to frequencies "freq"
        fs : float
            sampling frequency
        fc : float
            corner frequency for 1/f^2 steepening of power spectrum
        f_limit : float
            lower frequency limit for estimating misfit of model-plus-alias spectrum vs. measured power
        avgs : int
            flag for whether spectrum is derived from instantaneous point measurements (avgs<>1)
            OR from measurements averaged over each sampling interval (avgs==1)

        Returns
        -------

        alpha : float
            best-fit exponent of power-law model
        filtered_pwr : array
            vector of alias-filtered spectral power
        model_pwr : array
            vector of modeled spectral power
        aliased_pwr : array
            vector of modeled spectral power, plus aliases

        References
        ----------

        1. Kirchner, J. W. Aliasing in 1/f(alpha) noise spectra: origins, consequences, and remedies.
                Phys Rev E Stat Nonlin Soft Matter Phys 71, 66110 (2005).

        '''
        log_pwr = np.log(pwr)
        freq_mask = (freq > f_limit)*1  # convert True & False to 1 & 0

        alpha_upper_bound = 5

        if avgs == 1:
            alpha_lower_bound = -2.9  # if measurements are time-averaged
        else:
            alpha_lower_bound = -0.9  # if measurements are point samples

        alpha = optimize.fminbound(self.misfit, alpha_lower_bound, alpha_upper_bound,
                                   args=(fs, fc, freq, log_pwr, freq_mask, avgs), xtol=1e-4)

        model_pwr, aliased_pwr, RMSE = self.alias(alpha, fs, fc, freq, log_pwr, freq_mask, avgs)
        filtered_pwr = pwr * model_pwr / aliased_pwr

        return alpha, filtered_pwr, model_pwr, aliased_pwr

    def misfit(self, alpha, fs, fc, freq, log_pwr, freq_mask, avgs):
        model, aliased_pwr, RMSE = self.alias(alpha, fs, fc, freq, log_pwr, freq_mask, avgs)
        return RMSE

    def alias(self, alpha, fs, fc, freq, log_pwr, freq_mask, avgs):
        model_pwr = self.model(alpha, fs, fc, freq, avgs)
        aliased_pwr = np.copy(model_pwr)
        if avgs == 1:
            aliased_pwr = aliased_pwr * np.sinc(freq/fs) ** 2

        for k in range(1, 11):
            alias_minus = self.model(alpha, fs, fc, k*fs-freq, avgs)
            if avgs == 1:
                alias_minus = alias_minus * np.sinc((k*fs-freq)/fs) ** 2

            aliased_pwr = aliased_pwr + alias_minus

            alias_plus = self.model(alpha, fs, fc, k*fs+freq, avgs)  # notice the + in (k*fs+freq)
            if avgs == 1:
                alias_plus = alias_plus * np.sinc((k*fs+freq)/fs) ** 2

            aliased_pwr = aliased_pwr + alias_plus

        if avgs == 1:
            beta = alpha + 3
            const = 1 / (2*np.pi**2*beta/fs)
        else:
            beta = alpha + 1
            const = 1 / (beta*fs)

        zo_minus = (11*fs-freq)**(-beta)
        dz_minus = zo_minus / 20

        for j in range(1, 21):
            aliased_pwr = aliased_pwr + const / ((j*dz_minus)**(2/beta) + 1/fc**2)*dz_minus

        zo_plus = (11*fs+freq)**(-beta)
        dz_plus = zo_plus / 20

        for j in range(1, 21):
            aliased_pwr = aliased_pwr + const / ((j*dz_plus)**(2/beta) + 1/fc**2)*dz_plus

        log_aliased = np.log(aliased_pwr)

        prefactor = np.sum((log_pwr - log_aliased) * freq_mask) / np.sum(freq_mask)

        log_aliased = log_aliased + prefactor
        aliased_pwr = aliased_pwr * np.exp(prefactor)
        model_pwr = model_pwr * np.exp(prefactor)

        RMSE = np.sqrt(np.sum((log_aliased-log_pwr)*(log_aliased-log_pwr)*freq_mask)) / np.sum(freq_mask)

        return model_pwr, aliased_pwr, RMSE

    def model(self, alpha, fs, fc, freq, avgs):
        spectr = freq**(-alpha) / (1 + (freq/fc)**2)

        return spectr

def tau_estimation(ys, ts, detrend=False, params=["default", 4, 0, 1], 
                   gaussianize=False, standardize=True):
    ''' Return the estimated persistence of a givenevenly/unevenly spaced time series.

    Args
    ----

    ys : array
        a time series
    ts : array
        time axis of the time series
    detrend : string
        'linear' - a linear least-squares fit to `ys` is subtracted;
        'constant' - the mean of `ys` is subtracted
        'savitzy-golay' - ys is filtered using the Savitzky-Golay filters and the resulting filtered series is subtracted from y.
    params : list
        The paramters for the Savitzky-Golay filters. The first parameter
        corresponds to the window size (default it set to half of the data)
        while the second parameter correspond to the order of the filter
        (default is 4). The third parameter is the order of the derivative
        (the default is zero, which means only smoothing.)
    gaussianize : bool
        If True, gaussianizes the timeseries
    standardize : bool
        If True, standardizes the timeseries

    Returns
    -------

    tau_est : float
        the estimated persistence

    References
    ----------

    Mudelsee, M. TAUEST: A Computer Program for Estimating Persistence in Unevenly Spaced Weather/Climate Time Series.
        Comput. Geosci. 28, 69–72 (2002).

    '''
    pd_ys = preprocess(ys, ts, detrend=detrend, params=params, gaussianize=gaussianize, standardize=standardize)
    dt = np.diff(ts)
    #  assert dt > 0, "The time points should be increasing!"

    def ar1_fun(a):
        return np.sum((pd_ys[1:] - pd_ys[:-1]*a**dt)**2)

    a_est = optimize.minimize_scalar(ar1_fun, bounds=[0, 1], method='bounded').x
    #  a_est = optimize.minimize_scalar(ar1_fun, method='brent').x

    tau_est = -1 / np.log(a_est)

    return tau_est

def preprocess(ys, ts, detrend=False, params=["default", 4, 0, 1], 
               gaussianize=False, standardize=True):
    ''' Return the processed time series using detrend and standardization.

    Args
    ----

    ys : array
        a time series
    ts : array
        The time axis for the timeseries. Necessary for use with
        the Savitzky-Golay filters method since the series should be evenly spaced.
    detrend : string
        'none'/False/None - no detrending will be applied;
        'linear' - a linear least-squares fit to `ys` is subtracted;
        'constant' - the mean of `ys` is subtracted
        'savitzy-golay' - ys is filtered using the Savitzky-Golay filters and the resulting filtered series is subtracted from y.
    params : list
        The paramters for the Savitzky-Golay filters. The first parameter
        corresponds to the window size (default it set to half of the data)
        while the second parameter correspond to the order of the filter
        (default is 4). The third parameter is the order of the derivative
        (the default is zero, which means only smoothing.)
    gaussianize : bool
        If True, gaussianizes the timeseries
    standardize : bool
        If True, standardizes the timeseries

    Returns
    -------

    res : array
        the processed time series

    '''

    if detrend == 'none' or detrend is False or detrend is None:
        ys_d = ys
    else:
        ys_d = detrend(ys, ts, method=detrend, params=params)

    if standardize:
        res, _, _ = std(ys_d)
    else:
        res = ys_d

    if gaussianize:
        res = gauss(res)

    return res

def assertPositiveInt(*args):
    ''' Assert that the args are all positive integers.
    '''
    for arg in args:
        assert isinstance(arg, int) and arg >= 1

def wwz_basic(ys, ts, freq, tau, c=1/(8*np.pi**2), Neff=3, nproc=1, detrend=False, params=['default', 4, 0, 1],
              gaussianize=False, standardize=True):
    ''' Return the weighted wavelet amplitude (WWA).

    Original method from Foster. Not multiprocessing.

    Args
    ----

    ys : array
        a time series
    ts : array
        time axis of the time series
    freq : array
        vector of frequency
    tau : array
        the evenly-spaced time points, namely the time shift for wavelet analysis
    c : float
        the decay constant
    Neff : int
        the threshold of the number of effective degree of freedom
    nproc :int
        fake argument, just for convenience
    detrend : string
        None - the original time series is assumed to have no trend;
        'linear' - a linear least-squares fit to `ys` is subtracted;
        'constant' - the mean of `ys` is subtracted
        'savitzy-golay' - ys is filtered using the Savitzky-Golay filters and the resulting filtered series is subtracted from y.
    params : list
        The paramters for the Savitzky-Golay filters. The first parameter
        corresponds to the window size (default it set to half of the data)
        while the second parameter correspond to the order of the filter
        (default is 4). The third parameter is the order of the derivative
        (the default is zero, which means only smoothing.)
    gaussianize : bool
        If True, gaussianizes the timeseries
    standardize : bool
        If True, standardizes the timeseries

    Returns
    -------

    wwa : array
        the weighted wavelet amplitude
    phase : array
        the weighted wavelet phase
    Neffs : array
        the matrix of effective number of points in the time-scale coordinates
    coeff : array
        the wavelet transform coefficients (a0, a1, a2)

    References
    ----------

    Foster, G. Wavelets for period analysis of unevenly sampled time series. The Astronomical Journal 112, 1709 (1996).
    Witt, A. & Schumann, A. Y. Holocene climate variability on millennial scales recorded in Greenland ice cores.
        Nonlinear Processes in Geophysics 12, 345–352 (2005).

    '''
    assert nproc == 1, "wwz_basic() only supports nproc=1"
    assertPositiveInt(Neff)

    nt = np.size(tau)
    nf = np.size(freq)

    pd_ys = preprocess(ys, ts, detrend=detrend, params=params, gaussianize=gaussianize, standardize=standardize)

    omega = make_omega(ts, freq)

    Neffs = np.ndarray(shape=(nt, nf))
    ywave_1 = np.ndarray(shape=(nt, nf))
    ywave_2 = np.ndarray(shape=(nt, nf))
    ywave_3 = np.ndarray(shape=(nt, nf))

    S = np.zeros(shape=(3, 3))

    for k in range(nf):
        for j in range(nt):
            dz = omega[k] * (ts - tau[j])
            weights = np.exp(-c*dz**2)

            sum_w = np.sum(weights)
            Neffs[j, k] = sum_w**2 / np.sum(weights**2)  # local number of effective dof

            if Neffs[j, k] <= Neff:
                ywave_1[j, k] = np.nan  # the coefficients cannot be estimated reliably when Neff_loc <= Neff
                ywave_2[j, k] = np.nan
                ywave_3[j, k] = np.nan
            else:
                phi2 = np.cos(dz)
                phi3 = np.sin(dz)

                S[0, 0] = 1
                S[1, 1] = np.sum(weights*phi2*phi2) / sum_w
                S[2, 2] = np.sum(weights*phi3*phi3) / sum_w
                S[1, 0] = S[0, 1] = np.sum(weights*phi2) / sum_w
                S[2, 0] = S[0, 2] = np.sum(weights*phi3) / sum_w
                S[2, 1] = S[1, 2] = np.sum(weights*phi2*phi3) / sum_w

                S_inv = np.linalg.pinv(S)

                weighted_phi1 = np.sum(weights*pd_ys) / sum_w
                weighted_phi2 = np.sum(weights*phi2*pd_ys) / sum_w
                weighted_phi3 = np.sum(weights*phi3*pd_ys) / sum_w

                ywave_1[j, k] = S_inv[0, 0]*weighted_phi1 + S_inv[0, 1]*weighted_phi2 + S_inv[0, 2]*weighted_phi3
                ywave_2[j, k] = S_inv[1, 0]*weighted_phi1 + S_inv[1, 1]*weighted_phi2 + S_inv[1, 2]*weighted_phi3
                ywave_3[j, k] = S_inv[2, 0]*weighted_phi1 + S_inv[2, 1]*weighted_phi2 + S_inv[2, 2]*weighted_phi3

    wwa = np.sqrt(ywave_2**2 + ywave_3**2)
    phase = np.arctan2(ywave_3, ywave_2)
    #  coeff = ywave_2 + ywave_3*1j
    coeff = (ywave_1, ywave_2, ywave_3)

    return wwa, phase, Neffs, coeff

def wwz_nproc(ys, ts, freq, tau, c=1/(8*np.pi**2), Neff=3, nproc=8,  detrend=False, params=['default', 4, 0, 1],
              gaussianize=False, standardize=True):
    ''' Return the weighted wavelet amplitude (WWA).

    Original method from Foster. Supports multiprocessing.

    Args
    ----

    ys : array
        a time series
    ts : array
        time axis of the time series
    freq : array
        vector of frequency
    tau : array
        the evenly-spaced time points, namely the time shift for wavelet analysis
    c : float
        the decay constant
    Neff : int
        the threshold of the number of effective degree of freedom
    nproc : int
        the number of processes for multiprocessing
    detrend : string
        None - the original time series is assumed to have no trend;
        'linear' - a linear least-squares fit to `ys` is subtracted;
        'constant' - the mean of `ys` is subtracted
        'savitzy-golay' - ys is filtered using the Savitzky-Golay filters and the resulting filtered series is subtracted from y.
    params : list
        The paramters for the Savitzky-Golay filters. The first parameter
        corresponds to the window size (default it set to half of the data)
        while the second parameter correspond to the order of the filter
        (default is 4). The third parameter is the order of the derivative
        (the default is zero, which means only smoothing.)
    gaussianize : bool
        If True, gaussianizes the timeseries
    standardize : bool
        If True, standardizes the timeseries

    Returns
    -------

    wwa : array
        the weighted wavelet amplitude
    phase : array
        the weighted wavelet phase
    Neffs : array
        the matrix of effective number of points in the time-scale coordinates
    coeff : array
        the wavelet transform coefficients (a0, a1, a2)

    '''
    assert nproc >= 2, "wwz_nproc() should use nproc >= 2, if want serial run, please use wwz_basic()"
    assertPositiveInt(Neff)

    nt = np.size(tau)
    nf = np.size(freq)

    pd_ys = preprocess(ys, ts, detrend=detrend, params=params, gaussianize=gaussianize, standardize=standardize)

    omega = make_omega(ts, freq)

    Neffs = np.ndarray(shape=(nt, nf))
    ywave_1 = np.ndarray(shape=(nt, nf))
    ywave_2 = np.ndarray(shape=(nt, nf))
    ywave_3 = np.ndarray(shape=(nt, nf))

    def wwa_1g(tau, omega):
        dz = omega * (ts - tau)
        weights = np.exp(-c*dz**2)

        sum_w = np.sum(weights)
        Neff_loc = sum_w**2 / np.sum(weights**2)

        S = np.zeros(shape=(3, 3))

        if Neff_loc <= Neff:
            ywave_2_1g = np.nan
            ywave_3_1g = np.nan
        else:
            phi2 = np.cos(dz)
            phi3 = np.sin(dz)

            S[0, 0] = 1
            S[1, 1] = np.sum(weights*phi2*phi2) / sum_w
            S[2, 2] = np.sum(weights*phi3*phi3) / sum_w
            S[1, 0] = S[0, 1] = np.sum(weights*phi2) / sum_w
            S[2, 0] = S[0, 2] = np.sum(weights*phi3) / sum_w
            S[2, 1] = S[1, 2] = np.sum(weights*phi2*phi3) / sum_w

            S_inv = np.linalg.pinv(S)

            weighted_phi1 = np.sum(weights*pd_ys) / sum_w
            weighted_phi2 = np.sum(weights*phi2*pd_ys) / sum_w
            weighted_phi3 = np.sum(weights*phi3*pd_ys) / sum_w

            ywave_1_1g = S_inv[0, 0]*weighted_phi1 + S_inv[0, 1]*weighted_phi2 + S_inv[0, 2]*weighted_phi3
            ywave_2_1g = S_inv[1, 0]*weighted_phi1 + S_inv[1, 1]*weighted_phi2 + S_inv[1, 2]*weighted_phi3
            ywave_3_1g = S_inv[2, 0]*weighted_phi1 + S_inv[2, 1]*weighted_phi2 + S_inv[2, 2]*weighted_phi3

        return Neff_loc, ywave_1_1g, ywave_2_1g, ywave_3_1g

    tf_mesh = np.meshgrid(tau, omega)
    list_of_grids = list(zip(*(grid.flat for grid in tf_mesh)))
    tau_grids, omega_grids = zip(*list_of_grids)

    with Pool(nproc) as pool:
        res = pool.map(wwa_1g, tau_grids, omega_grids)
        res_array = np.asarray(res)
        Neffs = res_array[:, 0].reshape((np.size(omega), np.size(tau))).T
        ywave_1 = res_array[:, 1].reshape((np.size(omega), np.size(tau))).T
        ywave_2 = res_array[:, 2].reshape((np.size(omega), np.size(tau))).T
        ywave_3 = res_array[:, 3].reshape((np.size(omega), np.size(tau))).T

    wwa = np.sqrt(ywave_2**2 + ywave_3**2)
    phase = np.arctan2(ywave_3, ywave_2)
    #  coeff = ywave_2 + ywave_3*1j
    coeff = (ywave_1, ywave_2, ywave_3)

    return wwa, phase, Neffs, coeff

def kirchner_basic(ys, ts, freq, tau, c=1/(8*np.pi**2), Neff=3, nproc=1, detrend=False, params=["default", 4, 0, 1],
                   gaussianize=False, standardize=True):
    ''' Return the weighted wavelet amplitude (WWA) modified by Kirchner.

    Method modified by Kirchner. No multiprocessing.

    Args
    ----

    ys : array
        a time series
    ts : array
        time axis of the time series
    freq : array
        vector of frequency
    tau : array
        the evenly-spaced time points, namely the time shift for wavelet analysis
    c : float
        the decay constant
    Neff : int
        the threshold of the number of effective degree of freedom
    nproc : int
        fake argument for convenience, for parameter consistency between functions, does not need to be specified
    detrend : string
        None - the original time series is assumed to have no trend;
        'linear' - a linear least-squares fit to `ys` is subtracted;
        'constant' - the mean of `ys` is subtracted
        'savitzy-golay' - ys is filtered using the Savitzky-Golay filters and the resulting filtered series is subtracted from y.
    params : list
        The paramters for the Savitzky-Golay filters. The first parameter
        corresponds to the window size (default it set to half of the data)
        while the second parameter correspond to the order of the filter
        (default is 4). The third parameter is the order of the derivative
        (the default is zero, which means only smoothing.)
    gaussianize : bool
        If True, gaussianizes the timeseries
    standardize : bool
        If True, standardizes the timeseries

    Returns
    -------

    wwa : array
        the weighted wavelet amplitude
    phase : array
        the weighted wavelet phase
    Neffs : array
        the matrix of effective number of points in the time-scale coordinates
    coeff : array
        the wavelet transform coefficients (a0, a1, a2)

    References
    ----------

    Foster, G. Wavelets for period analysis of unevenly sampled time series. The Astronomical Journal 112, 1709 (1996).
    Witt, A. & Schumann, A. Y. Holocene climate variability on millennial scales recorded in Greenland ice cores.
    Nonlinear Processes in Geophysics 12, 345–352 (2005).

    '''
    assert nproc == 1, "wwz_basic() only supports nproc=1"
    assertPositiveInt(Neff)

    nt = np.size(tau)
    nts = np.size(ts)
    nf = np.size(freq)

    pd_ys = preprocess(ys, ts, detrend=detrend, params=params, gaussianize=gaussianize, standardize=standardize)

    omega = make_omega(ts, freq)

    Neffs = np.ndarray(shape=(nt, nf))
    a0 = np.ndarray(shape=(nt, nf))
    a1 = np.ndarray(shape=(nt, nf))
    a2 = np.ndarray(shape=(nt, nf))

    for k in range(nf):
        for j in range(nt):
            dz = omega[k] * (ts - tau[j])
            weights = np.exp(-c*dz**2)

            sum_w = np.sum(weights)
            Neffs[j, k] = sum_w**2 / np.sum(weights**2)  # local number of effective dof

            if Neffs[j, k] <= Neff:
                a0[j, k] = np.nan  # the coefficients cannot be estimated reliably when Neff_loc <= Neff
                a1[j, k] = np.nan
                a2[j, k] = np.nan
            else:
                def w_prod(xs, ys):
                    return np.sum(weights*xs*ys) / sum_w

                sin_basis = np.sin(omega[k]*ts)
                cos_basis = np.cos(omega[k]*ts)
                one_v = np.ones(nts)

                sin_one = w_prod(sin_basis, one_v)
                cos_one = w_prod(cos_basis, one_v)
                sin_cos = w_prod(sin_basis, cos_basis)
                sin_sin = w_prod(sin_basis, sin_basis)
                cos_cos = w_prod(cos_basis, cos_basis)

                numerator = 2 * (sin_cos - sin_one * cos_one)
                denominator = (cos_cos - cos_one**2) - (sin_sin - sin_one**2)
                time_shift = np.arctan2(numerator, denominator) / (2*omega[k])  # Eq. (S5)

                sin_shift = np.sin(omega[k]*(ts - time_shift))
                cos_shift = np.cos(omega[k]*(ts - time_shift))
                sin_tau_center = np.sin(omega[k]*(time_shift - tau[j]))
                cos_tau_center = np.cos(omega[k]*(time_shift - tau[j]))

                ys_cos_shift = w_prod(pd_ys, cos_shift)
                ys_sin_shift = w_prod(pd_ys, sin_shift)
                ys_one = w_prod(pd_ys, one_v)
                cos_shift_one = w_prod(cos_shift, one_v)
                sin_shift_one = w_prod(sin_shift, one_v)

                A = 2*(ys_cos_shift-ys_one*cos_shift_one)
                B = 2*(ys_sin_shift-ys_one*sin_shift_one)

                a0[j, k] = ys_one
                a1[j, k] = cos_tau_center*A - sin_tau_center*B  # Eq. (S6)
                a2[j, k] = sin_tau_center*A + cos_tau_center*B  # Eq. (S7)

    wwa = np.sqrt(a1**2 + a2**2)
    phase = np.arctan2(a2, a1)
    #  coeff = a1 + a2*1j
    coeff = (a0, a1, a2)

    return wwa, phase, Neffs, coeff
def kirchner_nproc(ys, ts, freq, tau, c=1/(8*np.pi**2), Neff=3, nproc=8, detrend=False, params=['default', 4, 0, 1],
                   gaussianize=False, standardize=True):
    ''' Return the weighted wavelet amplitude (WWA) modified by Kirchner.

    Method modified by kirchner. Supports multiprocessing.

    Args
    ----

    ys : array
        a time series
    ts : array
        time axis of the time series
    freq : array
        vector of frequency
    tau : array
        the evenly-spaced time points, namely the time shift for wavelet analysis
    c : float
        the decay constant
    Neff : int
        the threshold of the number of effective degree of freedom
    nproc : int
        the number of processes for multiprocessing
    detrend : string
        None - the original time series is assumed to have no trend;
        'linear' - a linear least-squares fit to `ys` is subtracted;
        'constant' - the mean of `ys` is subtracted
        'savitzy-golay' - ys is filtered using the Savitzky-Golay filters and the resulting filtered series is subtracted from y.
    params : list
        The paramters for the Savitzky-Golay filters. The first parameter
        corresponds to the window size (default it set to half of the data)
        while the second parameter correspond to the order of the filter
        (default is 4). The third parameter is the order of the derivative
        (the default is zero, which means only smoothing.)
    gaussianize : bool
        If True, gaussianizes the timeseries
    standardize : bool
        If True, standardizes the timeseries

    Returns
    -------

    wwa (array): the weighted wavelet amplitude
    phase (array): the weighted wavelet phase
    Neffs (array): the matrix of effective number of points in the time-scale coordinates
    coeff (array): the wavelet transform coefficients (a0, a1, a2)

    '''
    assert nproc >= 2, "wwz_nproc() should use nproc >= 2, if want serial run, please use wwz_basic()"
    assertPositiveInt(Neff)

    nt = np.size(tau)
    nts = np.size(ts)
    nf = np.size(freq)

    pd_ys = preprocess(ys, ts, detrend=detrend, params=params, gaussianize=gaussianize, standardize=standardize)

    omega = make_omega(ts, freq)

    Neffs = np.ndarray(shape=(nt, nf))
    a0 = np.ndarray(shape=(nt, nf))
    a1 = np.ndarray(shape=(nt, nf))
    a2 = np.ndarray(shape=(nt, nf))

    def wwa_1g(tau, omega):
        dz = omega * (ts - tau)
        weights = np.exp(-c*dz**2)

        sum_w = np.sum(weights)
        Neff_loc = sum_w**2 / np.sum(weights**2)

        if Neff_loc <= Neff:
            a0_1g = np.nan  # the coefficients cannot be estimated reliably when Neff_loc <= Neff
            a1_1g = np.nan  # the coefficients cannot be estimated reliably when Neff_loc <= Neff
            a2_1g = np.nan
        else:
            def w_prod(xs, ys):
                return np.sum(weights*xs*ys) / sum_w

            sin_basis = np.sin(omega*ts)
            cos_basis = np.cos(omega*ts)
            one_v = np.ones(nts)

            sin_one = w_prod(sin_basis, one_v)
            cos_one = w_prod(cos_basis, one_v)
            sin_cos = w_prod(sin_basis, cos_basis)
            sin_sin = w_prod(sin_basis, sin_basis)
            cos_cos = w_prod(cos_basis, cos_basis)

            numerator = 2*(sin_cos - sin_one*cos_one)
            denominator = (cos_cos - cos_one**2) - (sin_sin - sin_one**2)
            time_shift = np.arctan2(numerator, denominator) / (2*omega)  # Eq. (S5)

            sin_shift = np.sin(omega*(ts - time_shift))
            cos_shift = np.cos(omega*(ts - time_shift))
            sin_tau_center = np.sin(omega*(time_shift - tau))
            cos_tau_center = np.cos(omega*(time_shift - tau))

            ys_cos_shift = w_prod(pd_ys, cos_shift)
            ys_sin_shift = w_prod(pd_ys, sin_shift)
            ys_one = w_prod(pd_ys, one_v)
            cos_shift_one = w_prod(cos_shift, one_v)
            sin_shift_one = w_prod(sin_shift, one_v)

            A = 2*(ys_cos_shift - ys_one*cos_shift_one)
            B = 2*(ys_sin_shift - ys_one*sin_shift_one)

            a0_1g = ys_one
            a1_1g = cos_tau_center*A - sin_tau_center*B  # Eq. (S6)
            a2_1g = sin_tau_center*A + cos_tau_center*B  # Eq. (S7)

        return Neff_loc, a0_1g, a1_1g, a2_1g

    tf_mesh = np.meshgrid(tau, omega)
    list_of_grids = list(zip(*(grid.flat for grid in tf_mesh)))
    tau_grids, omega_grids = zip(*list_of_grids)

    with Pool(nproc) as pool:
        res = pool.map(wwa_1g, tau_grids, omega_grids)
        res_array = np.asarray(res)
        Neffs = res_array[:, 0].reshape((np.size(omega), np.size(tau))).T
        a0 = res_array[:, 1].reshape((np.size(omega), np.size(tau))).T
        a1 = res_array[:, 2].reshape((np.size(omega), np.size(tau))).T
        a2 = res_array[:, 3].reshape((np.size(omega), np.size(tau))).T

    wwa = np.sqrt(a1**2 + a2**2)
    phase = np.arctan2(a2, a1)
    #  coeff = a1 + a2*1j
    coeff = (a0, a1, a2)

    return wwa, phase, Neffs, coeff

def kirchner_numba(ys, ts, freq, tau, c=1/(8*np.pi**2), Neff=3, detrend=False, params=["default", 4, 0, 1],
                   gaussianize=False, standardize=True, nproc=1):
    ''' Return the weighted wavelet amplitude (WWA) modified by Kirchner.

    Using numba.

    Args
    ----

    ys : array
        a time series
    ts : array
        time axis of the time series
    freq : array
        vector of frequency
    tau : array
        the evenly-spaced time points, namely the time shift for wavelet analysis
    c : float
        the decay constant
    Neff : int
        the threshold of the number of effective degree of freedom
    nproc : int
        fake argument, just for convenience
    detrend : string
        None - the original time series is assumed to have no trend;
        'linear' - a linear least-squares fit to `ys` is subtracted;
        'constant' - the mean of `ys` is subtracted
        'savitzy-golay' - ys is filtered using the Savitzky-Golay filters and the resulting filtered series is subtracted from y.
    params : list
        The paramters for the Savitzky-Golay filters. The first parameter
        corresponds to the window size (default it set to half of the data)
        while the second parameter correspond to the order of the filter
        (default is 4). The third parameter is the order of the derivative
        (the default is zero, which means only smoothing.)
    gaussianize : bool
        If True, gaussianizes the timeseries
    standardize : bool
        If True, standardizes the timeseries

    Returns
    -------

    wwa : array
        the weighted wavelet amplitude
    phase : array
        the weighted wavelet phase
    Neffs : array
        the matrix of effective number of points in the time-scale coordinates
    coeff : array
        the wavelet transform coefficients (a0, a1, a2)

    References
    ----------

    Foster, G. Wavelets for period analysis of unevenly sampled time series. The Astronomical Journal 112, 1709 (1996).
    Witt, A. & Schumann, A. Y. Holocene climate variability on millennial scales recorded in Greenland ice cores.
        Nonlinear Processes in Geophysics 12, 345–352 (2005).

    '''
    assertPositiveInt(Neff)
    nt = np.size(tau)
    nts = np.size(ts)
    nf = np.size(freq)

    pd_ys = preprocess(ys, ts, detrend=detrend, params=params, gaussianize=gaussianize, standardize=standardize)

    omega = make_omega(ts, freq)

    Neffs = np.ndarray(shape=(nt, nf))
    a0 = np.ndarray(shape=(nt, nf))
    a1 = np.ndarray(shape=(nt, nf))
    a2 = np.ndarray(shape=(nt, nf))

    @nb.jit(nopython=True, parallel=True, fastmath=True)
    def loop_over(nf, nt, Neffs, a0, a1, a2):
        def wwa_1g(tau, omega):
            dz = omega * (ts - tau)
            weights = np.exp(-c*dz**2)

            sum_w = np.sum(weights)
            Neff_loc = sum_w**2 / np.sum(weights**2)

            if Neff_loc <= Neff:
                a0_1g = np.nan  # the coefficients cannot be estimated reliably when Neff_loc <= Neff
                a1_1g = np.nan  # the coefficients cannot be estimated reliably when Neff_loc <= Neff
                a2_1g = np.nan
            else:
                def w_prod(xs, ys):
                    return np.sum(weights*xs*ys) / sum_w

                sin_basis = np.sin(omega*ts)
                cos_basis = np.cos(omega*ts)
                one_v = np.ones(nts)

                sin_one = w_prod(sin_basis, one_v)
                cos_one = w_prod(cos_basis, one_v)
                sin_cos = w_prod(sin_basis, cos_basis)
                sin_sin = w_prod(sin_basis, sin_basis)
                cos_cos = w_prod(cos_basis, cos_basis)

                numerator = 2*(sin_cos - sin_one*cos_one)
                denominator = (cos_cos - cos_one**2) - (sin_sin - sin_one**2)
                time_shift = np.arctan2(numerator, denominator) / (2*omega)  # Eq. (S5)

                sin_shift = np.sin(omega*(ts - time_shift))
                cos_shift = np.cos(omega*(ts - time_shift))
                sin_tau_center = np.sin(omega*(time_shift - tau))
                cos_tau_center = np.cos(omega*(time_shift - tau))

                ys_cos_shift = w_prod(pd_ys, cos_shift)
                ys_sin_shift = w_prod(pd_ys, sin_shift)
                ys_one = w_prod(pd_ys, one_v)
                cos_shift_one = w_prod(cos_shift, one_v)
                sin_shift_one = w_prod(sin_shift, one_v)

                A = 2*(ys_cos_shift - ys_one*cos_shift_one)
                B = 2*(ys_sin_shift - ys_one*sin_shift_one)

                a0_1g = ys_one
                a1_1g = cos_tau_center*A - sin_tau_center*B  # Eq. (S6)
                a2_1g = sin_tau_center*A + cos_tau_center*B  # Eq. (S7)

            return Neff_loc, a0_1g, a1_1g, a2_1g

        for k in nb.prange(nf):
            for j in nb.prange(nt):
                Neffs[j, k], a0[j, k], a1[j, k], a2[j, k] = wwa_1g(tau[j], omega[k])

        return Neffs, a0, a1, a2

    Neffs, a0, a1, a2 = loop_over(nf, nt, Neffs, a0, a1, a2)

    wwa = np.sqrt(a1**2 + a2**2)
    phase = np.arctan2(a2, a1)
    #  coeff = a1 + a2*1j
    coeff = (a0, a1, a2)

    return wwa, phase, Neffs, coeff

def kirchner_f2py(ys, ts, freq, tau, c=1/(8*np.pi**2), Neff=3, nproc=8, detrend=False, params=['default', 4, 0, 1],
                  gaussianize=False, standardize=True):
    ''' Return the weighted wavelet amplitude (WWA) modified by Kirchner.

    Fastest method. Calls Fortran libraries.

    Args
    ----

    ys : array
        a time series
    ts : array
        time axis of the time series
    freq : array
        vector of frequency
    tau : array
        the evenly-spaced time points, namely the time shift for wavelet analysis
    c : float
        the decay constant
    Neff : int
        the threshold of the number of effective degree of freedom
    nproc : int
        fake argument, just for convenience
    detrend : string
        None - the original time series is assumed to have no trend;
        'linear' - a linear least-squares fit to `ys` is subtracted;
        'constant' - the mean of `ys` is subtracted
        'savitzy-golay' - ys is filtered using the Savitzky-Golay filters and the resulting filtered series is subtracted from y.
    params : list
        The paramters for the Savitzky-Golay filters. The first parameter
        corresponds to the window size (default it set to half of the data)
        while the second parameter correspond to the order of the filter
        (default is 4). The third parameter is the order of the derivative
        (the default is zero, which means only smoothing.)
    gaussianize : bool
        If True, gaussianizes the timeseries
    standardize : bool
        If True, standardizes the timeseries

    Returns
    -------

    wwa : array
        the weighted wavelet amplitude
    phase : array
        the weighted wavelet phase
    Neffs : array
        the matrix of effective number of points in the time-scale coordinates
    coeff : array
        the wavelet transform coefficients (a0, a1, a2)

    '''
    from . import f2py_wwz as f2py
    assertPositiveInt(Neff, nproc)

    nt = np.size(tau)
    nts = np.size(ts)
    nf = np.size(freq)

    pd_ys = preprocess(ys, ts, detrend=detrend, params=params, gaussianize=gaussianize, standardize=standardize)

    omega = make_omega(ts, freq)

    Neffs, a0, a1, a2 = f2py.f2py_wwz.wwa(tau, omega, c, Neff, ts, pd_ys, nproc, nts, nt, nf)

    undef = -99999.
    a0[a0 == undef] = np.nan
    a1[a1 == undef] = np.nan
    a2[a2 == undef] = np.nan
    wwa = np.sqrt(a1**2 + a2**2)
    phase = np.arctan2(a2, a1)

    #  coeff = a1 + a2*1j
    coeff = (a0, a1, a2)

    return wwa, phase, Neffs, coeff

def make_coi(tau, Neff=3):
    ''' Return the cone of influence.

    Args
    ----

    tau : array
        the evenly-spaced time points, namely the time shift for wavelet analysis
    Neff : int
        the threshold of the number of effective samples

    Returns
    -------

        coi : array
            cone of influence

    References
    ----------

    wave_signif() in http://paos.colorado.edu/research/wavelets/wave_python/waveletFunctions.py

    '''
    assert isinstance(Neff, int) and Neff >= 1
    nt = np.size(tau)

    fourier_factor = 4*np.pi / (Neff+np.sqrt(2+Neff**2))
    coi_const = fourier_factor / np.sqrt(2)

    dt = np.median(np.diff(tau))
    nt_half = (nt+1)//2 - 1

    A = np.append(0.00001, np.arange(nt_half)+1)
    B = A[::-1]

    if nt % 2 == 0:
        C = np.append(A, B)
    else:
        C = np.append(A, B[1:])

    coi = coi_const * dt * C

    return coi

def make_omega(ts, freq):
    ''' Return the angular frequency based on the time axis and given frequency vector

    Args
    ----

    ys : array
        a time series
    ts : array
        time axis of the time series
    freq : array
        vector of frequency

    Returns
    -------


    omega : array
        the angular frequency vector

    '''
    # for the frequency band larger than f_Nyquist, the wwa will be marked as NaNs
    f_Nyquist = 0.5 / np.median(np.diff(ts))
    freq_with_nan = np.copy(freq)
    freq_with_nan[freq > f_Nyquist] = np.nan
    omega = 2*np.pi*freq_with_nan

    return omega

def wwa2psd(wwa, ts, Neffs, freq=None, Neff=3, anti_alias=False, avgs=2):
    """ Return the power spectral density (PSD) using the weighted wavelet amplitude (WWA).

    Args
    ----

    wwa : array
        the weighted wavelet amplitude.
    ts : array
        the time points, should be pre-truncated so that the span is exactly what is used for wwz
    Neffs : array
        the matrix of effective number of points in the time-scale coordinates obtained from wwz from wwz
    freq : array
        vector of frequency from wwz
    Neff : int
        the threshold of the number of effective samples
    anti_alias : bool
        whether to apply anti-alias filter
    avgs : int
        flag for whether spectrum is derived from instantaneous point measurements (avgs<>1) OR from measurements averaged over each sampling interval (avgs==1)

    Returns
    -------

    psd : array
        power spectral density

    References
    ----------

    Kirchner's C code for weighted psd calculation

    """
    af = AliasFilter()

    # weighted psd calculation start
    power = wwa**2 * 0.5 * (np.max(ts)-np.min(ts))/np.size(ts) * Neffs

    Neff_diff = Neffs - Neff
    Neff_diff[Neff_diff < 0] = 0

    sum_power = np.nansum(power * Neff_diff, axis=0)
    sum_eff = np.nansum(Neff_diff, axis=0)

    psd = sum_power / sum_eff
    # weighted psd calculation end

    if anti_alias:
        assert freq is not None, "freq is required for alias filter!"
        dt = np.median(np.diff(ts))
        f_sampling = 1/dt
        psd_copy = psd[1:]
        freq_copy = freq[1:]
        alpha, filtered_pwr, model_pwer, aliased_pwr = af.alias_filter(
            freq_copy, psd_copy, f_sampling, f_sampling*1e3, np.min(freq), avgs)

        psd[1:] = np.copy(filtered_pwr)

    return psd

def wwz(ys, ts, tau=None, freq=None, c=1/(8*np.pi**2), Neff=3, Neff_coi=3,
        nMC=200, nproc=8, detrend=False, params=['default', 4, 0, 1],
        gaussianize=False, standardize=True, method='default', len_bd=0,
        bc_mode='reflect', reflect_type='odd'):
    ''' Return the weighted wavelet amplitude (WWA) with phase, AR1_q, and cone of influence, as well as WT coefficients

    Args
    ----

    ys : array
        a time series, NaNs will be deleted automatically
    ts : array
        the time points, if `ys` contains any NaNs, some of the time points will be deleted accordingly
    tau : array
        the evenly-spaced time points
    freq : array
        vector of frequency
    c : float
        the decay constant, the default value 1/(8*np.pi**2) is good for most of the cases
    Neff : int
        effective number of points
    nMC : int
        the number of Monte-Carlo simulations
    nproc : int
        the number of processes for multiprocessing
    detrend : str
        None - the original time series is assumed to have no trend;
        'linear' - a linear least-squares fit to `ys` is subtracted;
        'constant' - the mean of `ys` is subtracted
        'savitzy-golay' - ys is filtered using the Savitzky-Golay
               filters and the resulting filtered series is subtracted from y.
    params : list
        The paramters for the Savitzky-Golay filters. The first parameter
        corresponds to the window size (default it set to half of the data)
        while the second parameter correspond to the order of the filter
        (default is 4). The third parameter is the order of the derivative
        (the default is zero, which means only smoothing.)
    method : string
        'Foster' - the original WWZ method;
        'Kirchner' - the method Kirchner adapted from Foster;
        'Kirchner_f2py' - the method Kirchner adapted from Foster with f2py
    len_bd : int
        the number of the ghost grids want to creat on each boundary
    bc_mode : string
        {'constant', 'edge', 'linear_ramp', 'maximum', 'mean', 'median', 'minimum', 'reflect' , 'symmetric', 'wrap'}
        For more details, see np.lib.pad()
    reflect_type : string
         {‘even’, ‘odd’}, optional
         Used in ‘reflect’, and ‘symmetric’. The ‘even’ style is the default with an unaltered reflection around the edge value.
         For the ‘odd’ style, the extented part of the array is created by subtracting the reflected values from two times the edge value.
         For more details, see np.lib.pad()

    Returns
    -------

    wwa : array
        the weighted wavelet amplitude.
    AR1_q : array
        AR1 simulations
    coi : array
        cone of influence
    freq : array
        vector of frequency
    tau : array
        the evenly-spaced time points, namely the time shift for wavelet analysis
    Neffs : array
        the matrix of effective number of points in the time-scale coordinates
    coeff : array
        the wavelet transform coefficents

    '''
    assert isinstance(nMC, int) and nMC >= 0, "nMC should be larger than or equal to 0."

    ys_cut, ts_cut, freq, tau = prepare_wwz(
        ys, ts, freq=freq, tau=tau, len_bd=len_bd,
        bc_mode=bc_mode, reflect_type=reflect_type
    )

    wwz_func = get_wwz_func(nproc, method)
    wwa, phase, Neffs, coeff = wwz_func(ys_cut, ts_cut, freq, tau, Neff=Neff, c=c, nproc=nproc,
                                        detrend=detrend, params=params,
                                        gaussianize=gaussianize, standardize=standardize)

    # Monte-Carlo simulations of AR1 process
    nt = np.size(tau)
    nf = np.size(freq)

    wwa_red = np.ndarray(shape=(nMC, nt, nf))
    AR1_q = np.ndarray(shape=(nt, nf))

    if nMC >= 1:
        #  tauest = wa.tau_estimation(ys_cut, ts_cut, detrend=detrend, gaussianize=gaussianize, standardize=standardize)

        for i in tqdm(range(nMC), desc='Monte-Carlo simulations'):
            r = ar1_sim(ys_cut, np.size(ts_cut), 1, ts=ts_cut)
            wwa_red[i, :, :], _, _, _ = wwz_func(r, ts_cut, freq, tau, c=c, Neff=Neff, nproc=nproc,
                                                 detrend=detrend, params=params,
                                                 gaussianize=gaussianize, standardize=standardize)

        for j in range(nt):
            for k in range(nf):
                AR1_q[j, k] = mquantiles(wwa_red[:, j, k], 0.95)

    else:
        AR1_q = None

    # calculate the cone of influence
    coi = make_coi(tau, Neff=Neff_coi)

    Results = collections.namedtuple('Results', ['amplitude', 'phase', 'AR1_q', 'coi', 'freq', 'time', 'Neffs', 'coeff'])
    res = Results(amplitude=wwa, phase=phase, AR1_q=AR1_q, coi=coi, freq=freq, time=tau, Neffs=Neffs, coeff=coeff)

    return res
def xwc(ys1, ts1, ys2, ts2, smooth_factor=0.25,
        tau=None, freq=None, c=1/(8*np.pi**2), Neff=3, nproc=8, detrend=False,
        nMC=200, params=['default', 4, 0, 1],
        gaussianize=False, standardize=True, method='default'):
    ''' Return the cross-wavelet coherence of two time series.

    Args
    ----

    ys1 : array
        first of two time series
    ys2 : array
        second of the two time series
    ts1 : array
        time axis of first time series
    ts2 : array
        time axis of the second time series
    tau : array
        the evenly-spaced time points
    freq : array
        vector of frequency
    c : float
        the decay constant, the default value 1/(8*np.pi**2) is good for most of the cases
    Neff : int
        effective number of points
    nproc : int
        the number of processes for multiprocessing
    nMC : int
        the number of Monte-Carlo simulations
    detrend : string
        None - the original time series is assumed to have no trend;
        'linear' - a linear least-squares fit to `ys` is subtracted;
        'constant' - the mean of `ys` is subtracted
        'savitzy-golay' - ys is filtered using the Savitzky-Golay
               filters and the resulting filtered series is subtracted from y.
    params : list
        The paramters for the Savitzky-Golay filters. The first parameter
        corresponds to the window size (default it set to half of the data)
        while the second parameter correspond to the order of the filter
        (default is 4). The third parameter is the order of the derivative
        (the default is zero, which means only smoothing.)
    gaussianize : bool
        If True, gaussianizes the timeseries
    standardize : bool
        If True, standardizes the timeseries
    method : string
        'Foster' - the original WWZ method;
        'Kirchner' - the method Kirchner adapted from Foster;
        'Kirchner_f2py' - the method Kirchner adapted from Foster with f2py

    Returns
    -------

    res : dict
        contains the cross wavelet coherence, cross-wavelet phase,
        vector of frequency, evenly-spaced time points, AR1 sims, cone of influence

    '''
    assert isinstance(nMC, int) and nMC >= 0, "nMC should be larger than or eaqual to 0."

    if tau is None:
        lb1, ub1 = np.min(ts1), np.max(ts1)
        lb2, ub2 = np.min(ts2), np.max(ts2)
        lb = np.max([lb1, lb2])
        ub = np.min([ub1, ub2])

        inside = ts1[(ts1>=lb) & (ts1<=ub)]
        tau = np.linspace(lb, ub, np.size(inside)//10)
        print(f'Setting tau={tau[:3]}...{tau[-3:]}, ntau={np.size(tau)}')

    if freq is None:
        s0 = 2*np.median(np.diff(ts1))
        nv = 12
        a0 = 2**(1/nv)
        noct = np.floor(np.log2(np.size(ts1)))-1
        scale = s0*a0**(np.arange(noct*nv+1))
        freq = 1/scale[::-1]
        print(f'Setting freq={freq[:3]}...{freq[-3:]}, nfreq={np.size(freq)}')

    ys1_cut, ts1_cut, freq1, tau1 = prepare_wwz(ys1, ts1, freq=freq, tau=tau)
    ys2_cut, ts2_cut, freq2, tau2 = prepare_wwz(ys2, ts2, freq=freq, tau=tau)

    if np.any(tau1 != tau2):
        print('inconsistent `tau`, recalculating...')
        tau_min = np.min([np.min(tau1), np.min(tau2)])
        tau_max = np.max([np.max(tau1), np.max(tau2)])
        ntau = np.max([np.size(tau1), np.size(tau2)])
        tau = np.linspace(tau_min, tau_max, ntau)
    else:
        tau = tau1

    if np.any(freq1 != freq2):
        print('inconsistent `freq`, recalculating...')
        freq_min = np.min([np.min(freq1), np.min(freq2)])
        freq_max = np.max([np.max(freq1), np.max(freq2)])
        nfreq = np.max([np.size(freq1), np.size(freq2)])
        freq = np.linspace(freq_min, freq_max, nfreq)
    else:
        freq = freq1

    if freq[0] == 0:
        freq = freq[1:] # delete 0 frequency if present

    res_wwz1 = wwz(ys1_cut, ts1_cut, tau=tau, freq=freq, c=c, Neff=Neff, nMC=0,
                   nproc=nproc, detrend=detrend, params=params,
                   gaussianize=gaussianize, standardize=standardize, method=method)
    res_wwz2 = wwz(ys2_cut, ts2_cut, tau=tau, freq=freq, c=c, Neff=Neff, nMC=0,
                   nproc=nproc, detrend=detrend, params=params,
                   gaussianize=gaussianize, standardize=standardize, method=method)

    wt_coeff1 = res_wwz1.coeff[1] - res_wwz1.coeff[2]*1j
    wt_coeff2 = res_wwz2.coeff[1] - res_wwz2.coeff[2]*1j

    xw_coherence, xw_phase = wavelet_coherence(wt_coeff1, wt_coeff2, freq, tau, smooth_factor=smooth_factor)
    xwt, xw_amplitude, _ = cross_wt(wt_coeff1, wt_coeff2)

    # Monte-Carlo simulations of AR1 process
    nt = np.size(tau)
    nf = np.size(freq)

    coherence_red = np.ndarray(shape=(nMC, nt, nf))
    AR1_q = np.ndarray(shape=(nt, nf))

    if nMC >= 1:

        for i in tqdm(range(nMC), desc='Monte-Carlo simulations'):
            r1 = ar1_sim(ys1_cut, np.size(ts1_cut), 1, ts=ts1_cut)
            r2 = ar1_sim(ys2_cut, np.size(ts2_cut), 1, ts=ts2_cut)
            res_wwz_r1 = wwz(r1, ts1_cut, tau=tau, freq=freq, c=c, Neff=Neff, nMC=0, nproc=nproc,
                                                     detrend=detrend, params=params,
                                                     gaussianize=gaussianize, standardize=standardize)
            res_wwz_r2 = wwz(r2, ts2_cut, tau=tau, freq=freq, c=c, Neff=Neff, nMC=0, nproc=nproc,
                                                     detrend=detrend, params=params,
                                                     gaussianize=gaussianize, standardize=standardize)

            wt_coeffr1 = res_wwz_r1.coeff[1] - res_wwz_r2.coeff[2]*1j
            wt_coeffr2 = res_wwz_r1.coeff[1] - res_wwz_r2.coeff[2]*1j
            coherence_red[i, :, :], phase_red = wavelet_coherence(wt_coeffr1, wt_coeffr2, freq, tau, smooth_factor=smooth_factor)

        for j in range(nt):
            for k in range(nf):
                AR1_q[j, k] = mquantiles(coherence_red[:, j, k], 0.95)

    else:
        AR1_q = None

    coi = make_coi(tau, Neff=Neff)
    Results = collections.namedtuple('Results', ['xw_coherence', 'xw_amplitude', 'xw_phase', 'xwt', 'freq', 'time', 'AR1_q', 'coi'])
    res = Results(xw_coherence=xw_coherence, xw_amplitude=xw_amplitude, xw_phase=xw_phase, xwt=xwt,
                  freq=freq, time=tau, AR1_q=AR1_q, coi=coi)

    return res
def freq_vector_lomb_scargle(ts, nf=None, ofac=4, hifac=1):
    ''' Return the frequency vector based on the Lomb-Scargle algorithm.

    Args
    ----

    ts : array
        time axis of the time series
    ofac : float
        Oversampling rate that influences the resolution of the frequency axis,
                 when equals to 1, it means no oversamling (should be >= 1).
                 The default value 4 is usaually a good value.
    hifac : float
        fhi/fnyq (should be >= 1), where fhi is the highest frequency that
        can be analyzed by the Lomb-Scargle algorithm and fnyq is the Nyquist frequency.

    Returns
    -------

    freq : array
        the frequency vector

    References
    ----------

    Trauth, M. H. MATLAB® Recipes for Earth Sciences. (Springer, 2015). pp 181.

    '''
    assert ofac >= 1 and hifac <= 1, "`ofac` should be >= 1, and `hifac` should be <= 1"

    dt = np.median(np.diff(ts))
    flo = (1/(2*dt)) / (np.size(ts)*ofac)
    fhi = hifac / (2*dt)

    if nf is None:
        df = flo
        nf = (fhi - flo) / df + 1

    freq = np.linspace(flo, fhi, nf)

    return freq

def freq_vector_welch(ts):
    ''' Return the frequency vector based on the Welch's method.

    Args
    ----

    ts : array
        time axis of the time series

    Returns
    -------

    freq : array
        the frequency vector

    References
    ----------

    https://github.com/scipy/scipy/blob/v0.14.0/scipy/signal/Spectral.py

    '''
    nt = np.size(ts)
    dt = np.median(np.diff(ts))
    fs = 1 / dt
    if nt % 2 == 0:
        n_freq = nt//2 + 1
    else:
        n_freq = (nt+1) // 2

    freq = np.arange(n_freq) * fs / nt

    return freq

def freq_vector_nfft(ts):
    ''' Return the frequency vector based on NFFT

    Args
    ----

    ts : array
        time axis of the time series

    Returns
    -------

    freq : array
        the frequency vector

    '''
    nt = np.size(ts)
    dt = np.median(np.diff(ts))
    fs = 1 / dt
    n_freq = nt//2 + 1

    freq = np.linspace(0, fs/2, n_freq)

    return freq

def make_freq_vector(ts, method = 'nfft', **kwargs):
    ''' Make frequency vector- Selector function.

    This function selects among various methods to obtain the frequency
    vector.

    Args
    ----

    ts : array): time axis of the time series
    method : string
        The method to use. Options are 'nfft' (default), 'Lomb-Scargle', 'Welch'
    kwargs : dict, optional
            For Lomb_Scargle, additional parameters may be passed:
            - nf (int): number of frequency points
            - ofac (float): Oversampling rate that influences the resolution of the frequency axis,
                 when equals to 1, it means no oversamling (should be >= 1).
                 The default value 4 is usaually a good value.
            - hifac (float): fhi/fnyq (should be >= 1), where fhi is the highest frequency that
                  can be analyzed by the Lomb-Scargle algorithm and fnyq is the Nyquist frequency.

    Returns
    -------

    freq : array
        the frequency vector

    '''

    if method == 'Lomb-Scargle':
        freq = freq_vector_lomb_scargle(ts,**kwargs)
    elif method == 'Welch':
        freq = freq_vector_welch(ts)
    else:
        freq = freq_vector_nfft(ts)
    #  freq = freq[1:]  # discard the first element 0

    return freq

def beta_estimation(psd, freq, fmin=None, fmax=None):
    ''' Estimate the power slope of a 1/f^beta process.

    Args
    ----

    psd : array
        the power spectral density
    freq : array
        the frequency vector
    fmin : float
        the min of frequency range for beta estimation
    fmax : float
        the max of frequency range for beta estimation

    Returns
    -------

    beta : float
        the estimated slope
    f_binned : array
        binned frequency vector
    psd_binned : array
        binned power spectral density
    Y_reg : array
        prediction based on linear regression

    '''
    # drop the PSD at frequency zero
    if freq[0] == 0:
        psd = psd[1:]
        freq = freq[1:]

    if fmin is None or fmin == 0:
        fmin = np.min(freq)

    if fmax is None:
        fmax = np.max(freq)

    Results = collections.namedtuple('Results', ['beta', 'f_binned', 'psd_binned', 'Y_reg', 'std_err'])
    if np.max(freq) < fmax or np.min(freq) > fmin:
        print(fmin, fmax)
        print(np.min(freq), np.max(freq))
        print('WRONG')
        res = Results(beta=np.nan, f_binned=np.nan, psd_binned=np.nan, Y_reg=np.nan, std_err=np.nan)
        return res

    # frequency binning start
    fminindx = np.where(freq >= fmin)[0][0]
    fmaxindx = np.where(freq <= fmax)[0][-1]

    if fminindx >= fmaxindx:
        res = Results(beta=np.nan, f_binned=np.nan, psd_binned=np.nan, Y_reg=np.nan, std_err=np.nan)
        return res

    logf = np.log(freq)
    logf_step = logf[fminindx+1] - logf[fminindx]
    logf_start = logf[fminindx]
    logf_end = logf[fmaxindx]
    logf_binedges = np.arange(logf_start, logf_end+logf_step, logf_step)

    n_intervals = np.size(logf_binedges)-1
    logpsd_binned = np.empty(n_intervals)
    logf_binned = np.empty(n_intervals)

    logpsd = np.log(psd)

    for i in range(n_intervals):
        lb = logf_binedges[i]
        ub = logf_binedges[i+1]
        q = np.where((logf > lb) & (logf <= ub))

        logpsd_binned[i] = np.nanmean(logpsd[q])
        logf_binned[i] = (ub + lb) / 2

    f_binned = np.exp(logf_binned)
    psd_binned = np.exp(logpsd_binned)
    # frequency binning end

    # linear regression below
    Y = np.log10(psd_binned)
    X = np.log10(f_binned)
    X_ex = sm.add_constant(X)

    model = sm.OLS(Y, X_ex)
    results = model.fit()

    if np.size(results.params) < 2:
        beta = np.nan
        Y_reg = np.nan
        std_err = np.nan
    else:
        beta = -results.params[1]  # the slope we want
        Y_reg = 10**model.predict(results.params)  # prediction based on linear regression
        std_err = results.bse[1]

    res = Results(beta=beta, f_binned=f_binned, psd_binned=psd_binned, Y_reg=Y_reg, std_err=std_err)

    return res

def beta2HurstIndex(beta):
    ''' Translate psd slope to Hurst index

    Args
    ----

    beta : float
        the estimated slope of a power spectral density curve

    Returns
    -------

    H : float
        Hurst index, should be in (0, 1)

    References
    ----------

    Equation 2 in http://www.bearcave.com/misl/misl_tech/wavelets/hurst/

    '''
    H = (beta-1)/2

    return H

def psd_ar(var_noise, freq, ar_params, f_sampling):
    ''' Return the theoretical power spectral density (PSD) of an autoregressive model

    Args
    ----

    var_noise : float
        the variance of the noise of the AR process
    freq : array
        vector of frequency
    ar_params : array
        autoregressive coefficients, not including zero-lag
    f_sampling : float
        sampling frequency

    Returns
    -------

    psd : array
        power spectral density

    '''
    p = np.size(ar_params)

    tmp = np.ndarray(shape=(p, np.size(freq)), dtype=complex)
    for k in range(p):
        tmp[k, :] = np.exp(-1j*2*np.pi*(k+1)*freq/f_sampling)

    psd = var_noise / np.absolute(1-np.sum(ar_params*tmp, axis=0))**2

    return psd

def fBMsim(N=128, H=0.25):
    '''Simple method to generate fractional Brownian Motion

    Args
    ----

    N : int
        the length of the simulated time series
    H : float
        Hurst index, should be in (0, 1). The relationship between H and the scaling exponent beta is
        H = (beta-1) / 2

    Returns
    -------

    xfBm : array
        the simulated fractional Brownian Motion time series

    References
    ----------

    1. http://cours-physique.lps.ens.fr/index.php/TD11_Correlated_Noise_2011
    2. https://www.wikiwand.com/en/Fractional_Brownian_motion

    @authors: jeg, fzhu
    '''
    assert isinstance(N, int) and N >= 1
    assert H > 0 and H < 1, "H should be in (0, 1)!"

    HH = 2 * H

    ns = N-1  # number of steps
    covariance = np.ones((ns, ns))

    for i in range(ns):
        for j in range(i, ns):
            x = np.abs(i-j)
            covariance[i, j] = covariance[j, i] = (np.abs(x-1)**HH + (x+1)**HH - 2*x**HH) / 2.

    w, v = np.linalg.eig(covariance)

    A = np.zeros((ns, ns))
    for i in range(ns):
        for j in range(i, ns):
            A[i, j] = A[j, i] = np.sum(np.sqrt(w) * v[i, :] * v[j, :])

    xi = np.random.randn((ns))
    eta = np.dot(A, xi)

    xfBm = np.zeros(N)
    xfBm[0] = 0
    for i in range(1, N):
        xfBm[i] = xfBm[i-1] + eta[i-1]

    return xfBm

def psd_fBM(freq, ts, H):
    ''' Return the theoretical psd of a fBM

    Args
    ----

    freq : array
        vector of frequency
    ts : array
        the time axis of the time series
    H : float
        Hurst index, should be in (0, 1)

    Returns
    --------

    psd : array
        power spectral density

    References
    ----------

    Flandrin, P. On the spectrum of fractional Brownian motions.
        IEEE Transactions on Information Theory 35, 197–199 (1989).

    '''
    nf = np.size(freq)
    psd = np.ndarray(shape=(nf))
    T = np.max(ts) - np.min(ts)

    omega = 2 * np.pi * freq

    for k in range(nf):
        tmp = 2 * omega[k] * T
        psd[k] = (1 - 2**(1 - 2*H)*np.sin(tmp)/tmp) / np.abs(omega[k])**(1 + 2*H)

    return psd

def get_wwz_func(nproc, method):
    ''' Return the wwz function to use.

    Args
    ----

    nproc : int
        the number of processes for multiprocessing
    method : string
        'Foster' - the original WWZ method;
        'Kirchner' - the method Kirchner adapted from Foster;
        'Kirchner_f2py' - the method Kirchner adapted from Foster with f2py (default)

    Returns
    -------

    wwz_func : function
        the wwz function to use

    '''
    assertPositiveInt(nproc)

    if method == 'Foster':
        if nproc == 1:
            wwz_func = wwz_basic
        else:
            wwz_func = wwz_nproc

    elif method == 'Kirchner':
        if nproc == 1:
            wwz_func = kirchner_basic
        else:
            wwz_func = kirchner_nproc
    elif method == 'Kirchner_f2py':
        wwz_func = kirchner_f2py
    else:
        # default method; Kirchner's algorithm with Numba support for acceleration
        wwz_func = kirchner_numba

    return wwz_func

def prepare_wwz(ys, ts, freq=None, tau=None, len_bd=0, bc_mode='reflect', reflect_type='odd', **kwargs):
    ''' Return the truncated time series with NaNs deleted and estimate frequency vector and tau

    Args
    ----

    ys : array
        a time series, NaNs will be deleted automatically
    ts : array
        the time points, if `ys` contains any NaNs, some of the time points will be deleted accordingly
    freq : array
        vector of frequency. If None, use the nfft method.If using Lomb-Scargle, additional parameters
        may be set. See make_freq_vector
    tau : array
        the evenly-spaced time points, namely the time shift for wavelet analysis
        if the boundaries of tau are not exactly on two of the time axis points, then tau will be adjusted to be so
    len_bd : int
        the number of the ghost grids want to create on each boundary
    bc_mode : string
        {'constant', 'edge', 'linear_ramp', 'maximum', 'mean', 'median', 'minimum', 'reflect' , 'symmetric', 'wrap'}
        For more details, see np.lib.pad()
    reflect_type : string
         {‘even’, ‘odd’}, optional
         Used in ‘reflect’, and ‘symmetric’. The ‘even’ style is the default with an unaltered reflection around the edge value.
         For the ‘odd’ style, the extented part of the array is created by subtracting the reflected values from two times the edge value.
         For more details, see np.lib.pad()

    Returns
    -------

    ys_cut : array
        the truncated time series with NaNs deleted
    ts_cut : array
        the truncated time axis of the original time series with NaNs deleted
    freq : array
        vector of frequency
    tau : array
        the evenly-spaced time points, namely the time shift for wavelet analysis

    '''
    ys, ts = clean_ts(ys, ts)

    if tau is None:
        med_res = int(np.size(ts) // np.median(np.diff(ts)))
        tau = np.linspace(np.min(ts), np.max(ts), np.max([int(np.size(ts)//10), 50, med_res]))

    elif np.isnan(tau).any():
        warnings.warn("The input tau contains some NaNs." +
                      "It will be regenerated using the boundarys of the time axis of the time series with NaNs deleted," +
                      "with the length of the size of the input tau.")
        tau = np.linspace(np.min(ts), np.max(ts), np.size(tau))

    elif np.min(tau) < np.min(ts) and np.max(tau) > np.max(ts):
        warnings.warn("tau should be within the time span of the time series." +
                      "Note that sometimes if the leading points of the time series are NaNs," +
                      "they will be deleted and cause np.min(tau) < np.min(ts)." +
                      "A new tau with the same size of the input tau will be generated.")
        tau = np.linspace(np.min(ts), np.max(ts), np.size(tau))

    elif np.min(tau) not in ts or np.max(tau) not in ts:
        warnings.warn("The boundaries of tau are not exactly on two of the time axis points," +
                      "and it will be adjusted to be so.")
        tau_lb = np.min(ts[ts > np.min(tau)])
        tau_ub = np.max(ts[ts < np.max(tau)])
        tau = np.linspace(tau_lb, tau_ub, np.size(tau))

    # boundary condition
    if len_bd > 0:
        dt = np.median(np.diff(ts))
        dtau = np.median(np.diff(tau))
        len_bd_tau = len_bd*dt//dtau

        if bc_mode in ['reflect', 'symmetric']:
            ys = np.lib.pad(ys, (len_bd, len_bd), bc_mode, reflect_type=reflect_type)
        else:
            ys = np.lib.pad(ys, (len_bd, len_bd), bc_mode)

        ts_left_bd = np.linspace(ts[0]-dt*len_bd, ts[0]-dt, len_bd)
        ts_right_bd = np.linspace(ts[-1]+dt, ts[-1]+dt*len_bd, len_bd)
        ts = np.concatenate((ts_left_bd, ts, ts_right_bd))

        warnings.warn("The tau will be regenerated to fit the boundary condition.")
        tau_left_bd = np.linspace(tau[0]-dtau*len_bd_tau, tau[0]-dtau, len_bd_tau)
        tau_right_bd = np.linspace(tau[-1]+dtau, tau[-1]+dtau*len_bd_tau, len_bd_tau)
        tau = np.concatenate((tau_left_bd, tau, tau_right_bd))

    # truncate the time series when the range of tau is smaller than that of the time series
    ts_cut = ts[(np.min(tau) <= ts) & (ts <= np.max(tau))]
    ys_cut = ys[(np.min(tau) <= ts) & (ts <= np.max(tau))]

    if freq is None:
        freq = make_freq_vector(ts_cut, method='nfft')

    # remove 0 in freq vector
    freq = freq[freq != 0]

    return ys_cut, ts_cut, freq, tau

def cross_wt(coeff1, coeff2):
    ''' Return the cross wavelet transform.

    Args
    ----

    coeff1 : array
        the first of two sets of wavelet transform coefficients **in the form of a1 + a2*1j**
    coeff2 : array
        the second of two sets of wavelet transform coefficients **in the form of a1 + a2*1j**
    freq : array
        vector of frequency
    tau : array'
        the evenly-spaced time points, namely the time shift for wavelet analysis

    Returns
    -------

    xw_amplitude : array
        the cross wavelet amplitude
    xw_phase : array
        the cross wavelet phase

    References
    ----------

    1.Grinsted, A., Moore, J. C. & Jevrejeva, S. Application of the cross wavelet transform and
        wavelet coherence to geophysical time series. Nonlin. Processes Geophys. 11, 561–566 (2004).

    '''
    xwt = coeff1 * np.conj(coeff2)
    xw_amplitude = np.sqrt(xwt.real**2 + xwt.imag**2)
    xw_phase = np.arctan2(xwt.imag, xwt.real)

    return xwt, xw_amplitude, xw_phase

def wavelet_coherence(coeff1, coeff2, freq, tau, smooth_factor=0.25):
    ''' Return the cross wavelet coherence.

    Args
    ----

    coeff1 : array
        the first of two sets of wavelet transform coefficients **in the form of a1 + a2*1j**
    coeff2 : array
        the second of two sets of wavelet transform coefficients **in the form of a1 + a2*1j**
    freq : array
        vector of frequency
    tau : array'
        the evenly-spaced time points, namely the time shift for wavelet analysis

    Returns
    -------

    xw_coherence : array
        the cross wavelet coherence

    References
    ----------

    1. Grinsted, A., Moore, J. C. & Jevrejeva, S. Application of the cross wavelet transform and
        wavelet coherence to geophysical time series. Nonlin. Processes Geophys. 11, 561–566 (2004).
    2. Matlab code by Grinsted (https://github.com/grinsted/wavelet-coherence)
    3. Python code by Sebastian Krieger (https://github.com/regeirk/pycwt)

    '''
    def rect(length, normalize=False):
        """ Rectangular function adapted from https://github.com/regeirk/pycwt/blob/master/pycwt/helpers.py

        Args:
            length (int): length of the rectangular function
            normalize (bool): normalize or not

        Returns:
            rect (array): the (normalized) rectangular function

        """
        rect = np.zeros(length)
        rect[0] = rect[-1] = 0.5
        rect[1:-1] = 1

        if normalize:
            rect /= rect.sum()

        return rect

    def smoothing(coeff, snorm, dj, smooth_factor=smooth_factor):
        """ Smoothing function adapted from https://github.com/regeirk/pycwt/blob/master/pycwt/helpers.py

        Args
        ----

        coeff : array
            the wavelet coefficients get from wavelet transform **in the form of a1 + a2*1j**
        snorm : array
            normalized scales
        dj : float
            it satisfies the equation [ Sj = S0 * 2**(j*dj) ]

        Returns
        -------

        rect : array
            the (normalized) rectangular function

        """
        def fft_kwargs(signal, **kwargs):
            return {'n': np.int(2 ** np.ceil(np.log2(len(signal))))}

        W = coeff.transpose()
        m, n = np.shape(W)

        # Smooth in time
        k = 2 * np.pi * fft.fftfreq(fft_kwargs(W[0, :])['n'])
        k2 = k ** 2
        # Notes by Smoothing by Gaussian window (absolute value of wavelet function)
        # using the convolution theorem: multiplication by Gaussian curve in
        # Fourier domain for each scale, outer product of scale and frequency
        F = np.exp(-smooth_factor * (snorm[:, np.newaxis] ** 2) * k2)  # Outer product
        smooth = fft.ifft(F * fft.fft(W, axis=1, **fft_kwargs(W[0, :])),
                          axis=1,  # Along Fourier frequencies
                          **fft_kwargs(W[0, :], overwrite_x=True))
        T = smooth[:, :n]  # Remove possibly padded region due to FFT
        if np.isreal(W).all():
            T = T.real

        # Smooth in scale
        wsize = 0.6 / dj * 2
        win = rect(np.int(np.round(wsize)), normalize=True)
        T = signal.convolve2d(T, win[:, np.newaxis], 'same')
        S = T.transpose()

        return S

    xwt = coeff1 * np.conj(coeff2)
    power1 = np.abs(coeff1)**2
    power2 = np.abs(coeff2)**2

    scales = 1/freq  # `scales` here is the `Period` axis in the wavelet plot
    dt = np.median(np.diff(tau))
    snorm = scales / dt  # normalized scales

    # with WWZ method, we don't have a constant dj, so we will just take the average over the whole scale range
    N = np.size(scales)
    s0 = scales[-1]
    sN = scales[0]
    dj = np.log2(sN/s0) / N

    S12 = smoothing(xwt/scales, snorm, dj)
    S1 = smoothing(power1/scales, snorm, dj)
    S2 = smoothing(power2/scales, snorm, dj)
    xw_coherence = np.abs(S12)**2 / (S1*S2)
    wcs = S12 / (np.sqrt(S1)*np.sqrt(S2))
    xw_phase = np.angle(wcs)

    return xw_coherence, xw_phase

def reconstruct_ts(coeff, freq, tau, t, len_bd=0):
    ''' Reconstruct the normalized time series from the wavelet coefficients.

    Args
    ----

    coeff : array
        the coefficients of the corresponding basis functions (a0, a1, a2)
    freq : array
        vector of frequency of the basis functions
    tau : array
        the evenly-spaced time points of the basis functions
    t : array
        the specified evenly-spaced time points of the reconstructed time series
    len_bd : int
        the number of the ghost grids want to creat on each boundary

    Returns
    -------

    rec_ts : array
        the reconstructed normalized time series
    t : array
        the evenly-spaced time points of the reconstructed time series
    '''
    omega = 2*np.pi*freq
    nf = np.size(freq)

    dt = np.median(np.diff(t))
    if len_bd > 0:
        t_left_bd = np.linspace(t[0]-dt*len_bd, t[0]-dt, len_bd)
        t_right_bd = np.linspace(t[-1]+dt, t[-1]+dt*len_bd, len_bd)
        t = np.concatenate((t_left_bd, t, t_right_bd))

    ntau = np.size(tau)
    a_0, a_1, a_2 = coeff

    rec_ts = np.zeros(np.size(t))
    for k in range(nf):
        for j in range(ntau):
            if np.isnan(a_0[j, k]) or np.isnan(a_1[j, k]) or np.isnan(a_1[j, k]):
                continue
            else:
                dz = omega[k] * (t - tau[j])
                phi_1 = np.cos(dz)
                phi_2 = np.sin(dz)

                rec_ts += (a_0[j, k] + a_1[j, k]*phi_1 + a_2[j, k]*phi_2)

    rec_ts = preprocess(rec_ts, t, detrend=False, gaussianize=False, standardize=True)

    return rec_ts, t

def wavelet_evenly():
    #TODO
    return
