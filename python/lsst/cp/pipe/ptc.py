# This file is part of cp_pipe.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
import numpy as np
import matplotlib.pyplot as plt
from collections import Counter

import lsst.afw.math as afwMath
import lsst.pex.config as pexConfig
import lsst.pipe.base as pipeBase
from .utils import (fitLeastSq, fitBootstrap, funcPolynomial, funcAstier)
from scipy.optimize import least_squares

import datetime

from .astierCovPtcUtils import (fftSize, CovFft, computeCovDirect, fitData)
from .linearity import LinearitySolveTask
from .photodiode import getBOTphotodiodeData

from lsst.pipe.tasks.getRepositoryData import DataRefListRunner

from lsst.ip.isr import IsrCalib

__all__ = ['MeasurePhotonTransferCurveTask',
           'MeasurePhotonTransferCurveTaskConfig',
           'PhotonTransferCurveDataset']


class MeasurePhotonTransferCurveTaskConfig(pexConfig.Config):
    """Config class for photon transfer curve measurement task"""
    ccdKey = pexConfig.Field(
        dtype=str,
        doc="The key by which to pull a detector from a dataId, e.g. 'ccd' or 'detector'.",
        default='ccd',
    )
    ptcFitType = pexConfig.ChoiceField(
        dtype=str,
        doc="Fit PTC to Eq. 16, Eq. 20 in Astier+19, or to a polynomial.",
        default="POLYNOMIAL",
        allowed={
            "POLYNOMIAL": "n-degree polynomial (use 'polynomialFitDegree' to set 'n').",
            "EXPAPPROXIMATION": "Approximation in Astier+19 (Eq. 16).",
            "FULLCOVARIANCE": "Full covariances model in Astier+19 (Eq. 20)"
        }
    )
    sigmaClipFullFitCovariancesAstier = pexConfig.Field(
        dtype=float,
        doc="sigma clip for full model fit for FULLCOVARIANCE ptcFitType ",
        default=5.0,
    )
    maxIterFullFitCovariancesAstier = pexConfig.Field(
        dtype=int,
        doc="Maximum number of iterations in full model fit for FULLCOVARIANCE ptcFitType",
        default=3,
    )
    maximumRangeCovariancesAstier = pexConfig.Field(
        dtype=int,
        doc="Maximum range of covariances as in Astier+19",
        default=8,
    )
    covAstierRealSpace = pexConfig.Field(
        dtype=bool,
        doc="Calculate covariances in real space or via FFT? (see appendix A of Astier+19).",
        default=False,
    )
    polynomialFitDegree = pexConfig.Field(
        dtype=int,
        doc="Degree of polynomial to fit the PTC, when 'ptcFitType'=POLYNOMIAL.",
        default=3,
    )
    linearity = pexConfig.ConfigurableField(
        target=LinearitySolveTask,
        doc="Task to solve the linearity."
    )

    doCreateLinearizer = pexConfig.Field(
        dtype=bool,
        doc="Calculate non-linearity and persist linearizer?",
        default=False,
    )

    binSize = pexConfig.Field(
        dtype=int,
        doc="Bin the image by this factor in both dimensions.",
        default=1,
    )
    minMeanSignal = pexConfig.Field(
        dtype=float,
        doc="Minimum value (inclusive) of mean signal (in DN) above which to consider.",
        default=0,
    )
    maxMeanSignal = pexConfig.Field(
        dtype=float,
        doc="Maximum value (inclusive) of mean signal (in DN) below which to consider.",
        default=9e6,
    )
    initialNonLinearityExclusionThresholdPositive = pexConfig.RangeField(
        dtype=float,
        doc="Initially exclude data points with a variance that are more than a factor of this from being"
            " linear in the positive direction, from the PTC fit. Note that these points will also be"
            " excluded from the non-linearity fit. This is done before the iterative outlier rejection,"
            " to allow an accurate determination of the sigmas for said iterative fit.",
        default=0.12,
        min=0.0,
        max=1.0,
    )
    initialNonLinearityExclusionThresholdNegative = pexConfig.RangeField(
        dtype=float,
        doc="Initially exclude data points with a variance that are more than a factor of this from being"
            " linear in the negative direction, from the PTC fit. Note that these points will also be"
            " excluded from the non-linearity fit. This is done before the iterative outlier rejection,"
            " to allow an accurate determination of the sigmas for said iterative fit.",
        default=0.25,
        min=0.0,
        max=1.0,
    )
    sigmaCutPtcOutliers = pexConfig.Field(
        dtype=float,
        doc="Sigma cut for outlier rejection in PTC.",
        default=5.0,
    )
    maskNameList = pexConfig.ListField(
        dtype=str,
        doc="Mask list to exclude from statistics calculations.",
        default=['SUSPECT', 'BAD', 'NO_DATA'],
    )
    nSigmaClipPtc = pexConfig.Field(
        dtype=float,
        doc="Sigma cut for afwMath.StatisticsControl()",
        default=5.5,
    )
    nIterSigmaClipPtc = pexConfig.Field(
        dtype=int,
        doc="Number of sigma-clipping iterations for afwMath.StatisticsControl()",
        default=1,
    )
    maxIterationsPtcOutliers = pexConfig.Field(
        dtype=int,
        doc="Maximum number of iterations for outlier rejection in PTC.",
        default=2,
    )
    doFitBootstrap = pexConfig.Field(
        dtype=bool,
        doc="Use bootstrap for the PTC fit parameters and errors?.",
        default=False,
    )
    doPhotodiode = pexConfig.Field(
        dtype=bool,
        doc="Apply a correction based on the photodiode readings if available?",
        default=True,
    )
    photodiodeDataPath = pexConfig.Field(
        dtype=str,
        doc="Gen2 only: path to locate the data photodiode data files.",
        default=""
    )
    instrumentName = pexConfig.Field(
        dtype=str,
        doc="Instrument name.",
        default='',
    )


class PhotonTransferCurveDataset(IsrCalib):
    """A simple class to hold the output data from the PTC task.

    The dataset is made up of a dictionary for each item, keyed by the
    amplifiers' names, which much be supplied at construction time.

    New items cannot be added to the class to save accidentally saving to the
    wrong property, and the class can be frozen if desired.

    inputExpIdPairs records the exposures used to produce the data.
    When fitPtc() or fitCovariancesAstier() is run, a mask is built up, which is by definition
    always the same length as inputExpIdPairs, rawExpTimes, rawMeans
    and rawVars, and is a list of bools, which are incrementally set to False
    as points are discarded from the fits.

    PTC fit parameters for polynomials are stored in a list in ascending order
    of polynomial term, i.e. par[0]*x^0 + par[1]*x + par[2]*x^2 etc
    with the length of the list corresponding to the order of the polynomial
    plus one.

    Parameters
    ----------
    ampNames : `list`
        List with the names of the amplifiers of the detector at hand.

    ptcFitType : `str`
        Type of model fitted to the PTC: "POLYNOMIAL", "EXPAPPROXIMATION", or "FULLCOVARIANCE".

    kwargs : `dict`, optional
        Other keyword arguments to pass to the parent init.

    Notes
    -----
    The stored attributes are:

    badAmps : `list`
        List with bad amplifiers names.
    inputExpIdPairs : `dict`, [`str`, `list`]
        Dictonary keyed by amp names containing the input exposures IDs.
    expIdMask : `dict`, [`str`, `list`]
        Dictonary keyed by amp names containing the mask produced after outlier rejection. The mask produced
        by the "FULLCOVARIANCE" option may differ from the one produced in the other two PTC fit types.
    rawExpTimes : `dict`, [`str`, `list`]
        Dictonary keyed by amp names containing the unmasked exposure times.
    rawMeans : `dict`, [`str`, `list`]
        Dictonary keyed by amp names containing the unmasked average of the means of the exposures in each
        flat pair.
    rawVars : `dict`, [`str`, `list`]
        Dictonary keyed by amp names containing the variance of the difference image of the exposure sin each
        flat pair.
    gain : `dict`, [`str`, `list`]
        Dictonary keyed by amp names containing the fitted gains.
    gainErr : `dict`, [`str`, `list`]
        Dictonary keyed by amp names containing the errors on the fitted gains.
    noise : `dict`, [`str`, `list`]
        Dictonary keyed by amp names containing the fitted noise.
    noiseErr : `dict`, [`str`, `list`]
        Dictonary keyed by amp names containing the errors on the fitted noise.
    ptcFitPars : `dict`, [`str`, `list`]
        Dictonary keyed by amp names containing the fitted parameters of the PTC model for ptcFitTye in
        ["POLYNOMIAL", "EXPAPPROXIMATION"].
    ptcFitParsError : `dict`, [`str`, `list`]
        Dictonary keyed by amp names containing the errors on the fitted parameters of the PTC model for
        ptcFitTye in ["POLYNOMIAL", "EXPAPPROXIMATION"].
    ptcFitChiSq : `dict`, [`str`, `list`]
        Dictonary keyed by amp names containing the reduced chi squared of the fit for ptcFitTye in
        ["POLYNOMIAL", "EXPAPPROXIMATION"].
    covariancesFitsWithNoB : `dict`, [`str`, `list`]
        Dictonary keyed by amp names containing CovFit objects that fit the measured
        covariances to Eq. 20 of Astier+19, with "b" set to zero.
    covariancesFits : `dict`, [`str`, `list`]
        Dictonary keyed by amp names containinging CovFit objects that fit the measured
        covariances to Eq. 20 of Astier+19.
    aMatrix : `dict`, [`str`, `list`]
        Dictonary keyed by amp names containing the "a" parameters from the model in Eq. 20 of Astier+19.
    bMatrix : `dict`, [`str`, `list`]
        Dictonary keyed by amp names containing the "b" parameters from the model in Eq. 20 of Astier+19.
    finalVars : `dict`, [`str`, `list`]
        Dictonary keyed by amp names containing the masked variance of the difference image of each flat
        pair.
    finalModelVars : `dict`, [`str`, `list`]
        Dictonary keyed by amp names containing the masked modeled variance of the difference image of each
        flat pair.
    finalMeans : `dict`, [`str`, `list`]
        Dictonary keyed by amp names containing the masked average of the means of the exposures in each
        flat pair.

    Returns
    -------
    `lsst.cp.pipe.ptc.PhotonTransferCurveDataset`
        Output dataset from MeasurePhotonTransferCurveTask.
    """

    _OBSTYPE = 'PTC'
    _SCHEMA = 'Gen3 Photon Transfer Curve'
    _VERSION = 1.0

    def __init__(self, ampNames, ptcFitType, **kwargs):
        # add items to __dict__ directly because __setattr__ is overridden

        self.__dict__['ptcFitType'] = ptcFitType
        self.__dict__['ampNames'] = ampNames
        self.__dict__['badAmps'] = []

        self.__dict__["inputExpIdPairs"] = {ampName: [] for ampName in ampNames}
        self.__dict__["expIdMask"] = {ampName: [] for ampName in ampNames}
        self.__dict__["rawExpTimes"] = {ampName: [] for ampName in ampNames}
        self.__dict__["rawMeans"] = {ampName: [] for ampName in ampNames}
        self.__dict__["rawVars"] = {ampName: [] for ampName in ampNames}
        self.__dict__["photoCharge"] = {ampName: [] for ampName in ampNames}

        self.__dict__['gain'] = {ampName: -1. for ampName in ampNames}
        self.__dict__['gainErr'] = {ampName: -1. for ampName in ampNames}
        self.__dict__['noise'] = {ampName: -1. for ampName in ampNames}
        self.__dict__['noiseErr'] = {ampName: -1. for ampName in ampNames}

        self.__dict__['ptcFitPars'] = {ampName: [] for ampName in ampNames}
        self.__dict__['ptcFitParsError'] = {ampName: [] for ampName in ampNames}
        self.__dict__['ptcFitChiSq'] = {ampName: [] for ampName in ampNames}

        self.__dict__['covariancesFitsWithNoB'] = {ampName: [] for ampName in ampNames}
        self.__dict__['covariancesFits'] = {ampName: [] for ampName in ampNames}
        self.__dict__['aMatrix'] = {ampName: [] for ampName in ampNames}
        self.__dict__['bMatrix'] = {ampName: [] for ampName in ampNames}

        self.__dict__['finalVars'] = {ampName: [] for ampName in ampNames}
        self.__dict__['finalModelVars'] = {ampName: [] for ampName in ampNames}
        self.__dict__['finalMeans'] = {ampName: [] for ampName in ampNames}

        super().__init__(**kwargs)
        self.requiredAttributes.update(['badAmps', 'inputExpIdPairs', 'expIdMask', 'rawExpTimes',
                                        'rawMeans', 'rawVars', 'gain', 'gainErr', 'noise', 'noiseErr',
                                        'ptcFitPars', 'ptcFitParsError', 'ptcFitChiSq',
                                        'covariancesFitsWithNoB', "covariancesFits",
                                        'aMatrix', 'bMatrix', 'finalVars', 'finalModelVars', 'finalMeans'])

    def updateMetadata(self, setDate=False, **kwargs):
        """Update calibration metadata.

        This calls the base class's method after ensuring the required
        calibration keywords will be saved.

        Parameters
        ----------
        setDate : `bool`, optional
            Update the CALIBDATE fields in the metadata to the current
            time. Defaults to False.
        kwargs :
            Other keyword parameters to set in the metadata.
        """
        kwargs['PTC_FIT_TYPE'] = self.ptcFitType

        super().updateMetadata(setDate=setDate, **kwargs)

    @classmethod
    def fromDict(cls, dictionary):
        """Construct a calibration from a dictionary of properties.
        Must be implemented by the specific calibration subclasses.
        Parameters
        ----------
        dictionary : `dict`
            Dictionary of properties.
        Returns
        -------
        calib : `lsst.ip.isr.CalibType`
            Constructed calibration.
        Raises
        ------
        RuntimeError :
            Raised if the supplied dictionary is for a different
            calibration.
        """
        calib = cls()

        if calib._OBSTYPE != dictionary['metadata']['OBSTYPE']:
            raise RuntimeError(f"Incorrect Photon Transfer Curve dataset  supplied. "
                               f"Expected {calib._OBSTYPE}, found {dictionary['metadata']['OBSTYPE']}")

        calib.setMetadata(dictionary['metadata'])

        calib.ptcFitType = dictionary['fitType']
        calib.badAmps = dictionary['badAmps']

        calib.ampNames = dictionary['ampNames']
        calib.inputExpIdPairs = dictionary['inputExpIdPairs']
        calib.expIdMask = dictionary['expIdMask']
        calib.rawExpTimes = dictionary['rawExpTimes']
        calib.rawMeans = dictionary['rawMeans']
        calib.rawVars = dictionary['rawVars']
        calib.gain = dictionary['gain']
        calib.gainErr = dictionary['gainErr']
        calib.noise = dictionary['noise']
        calib.noiseErr = dictionary['noiseErr']
        calib.ptcFitPars = dictionary['ptcFitPars']
        calib.ptcFitParsError = dictionary['ptcFitParsError']
        calib.ptcFitChiSq = dictionary['ptcFitChiSq']
        calib.covariancesFitsWithNoB = dictionary['covariancesFitsWithNoB']
        calib.covariancesFits = dictionary['covariancesFits']
        calib.aMatrix = dictionary['aMatrix']
        calib.bMatrix = dictionary['bMatrix']
        calib.finalVars = dictionary['finalVars']
        calib.finalModelVars = dictionary['finalModelVars']
        calib.finalMeans = dictionary['finalMeans']

        calib.updateMetadata()
        return calib

    def toDict(self):
        """Return a dictionary containing the calibration properties.

        The dictionary should be able to be round-tripped through
        `fromDict`.

        Returns
        -------
        dictionary : `dict`
            Dictionary of properties.
        """
        self.updateMetadata()

        outDict = {}
        metadata = self.getMetadata()
        outDict['metadata'] = metadata

        outDict['fitType'] = self.fitType
        outDict['ampNames'] = self.ampnames
        outDict['badAmps'] = self.badAmps
        outDict['inputExpIdPairs'] = self.inputExpIdPairs
        outDict['expIdMask'] = self.expIdMask
        outDict['rawExpTimes'] = self.rawExpTimes
        outDict['rawMeans'] = self.rawMeans
        outDict['rawVars'] = self.rawVars
        outDict['gain'] = self.gain
        outDict['gainErr'] = self.gainErr
        outDict['noise'] = self.noise
        outDict['noiseErr'] = self.noiseErr
        outDict['ptcFitPars'] = self.ptcFitPars
        outDict['ptcFitParsError'] = self.ptcFitParsError
        outDict['ptcFitChiSq'] = self.ptcFitChiSq
        outDict['covariancesFitsWithNoB'] = self.covariancesFitsWithNoB
        outDict['covariancesFits'] = self.covariancesFits
        outDict['aMatrix'] = self.aMatrix
        outDict['bMatrix'] = self.bMatrix
        outDict['finalVars'] = self.finalVars
        outDict['finalModelVars'] = self.finalModelVars
        outDict['finalMeans'] = self.finalMeans

        return outDict

    def __setattr__(self, attribute, value):
        """Protect class attributes"""
        if attribute not in self.__dict__:
            raise AttributeError(f"{attribute} is not already a member of PhotonTransferCurveDataset, which"
                                 " does not support setting of new attributes.")
        else:
            self.__dict__[attribute] = value

    def getExpIdsUsed(self, ampName):
        """Get the exposures used, i.e. not discarded, for a given amp.

        If no mask has been created yet, all exposures are returned.
        """
        if len(self.expIdMask[ampName]) == 0:
            return self.inputExpIdPairs[ampName]

        # if the mask exists it had better be the same length as the expIdPairs
        assert len(self.expIdMask[ampName]) == len(self.inputExpIdPairs[ampName])

        pairs = self.inputExpIdPairs[ampName]
        mask = self.expIdMask[ampName]
        # cast to bool required because numpy
        return [(exp1, exp2) for ((exp1, exp2), m) in zip(pairs, mask) if bool(m) is True]

    def getGoodAmps(self):
        return [amp for amp in self.ampNames if amp not in self.badAmps]


class MeasurePhotonTransferCurveTask(pipeBase.CmdLineTask):
    """A class to calculate, fit, and plot a PTC from a set of flat pairs.

    The Photon Transfer Curve (var(signal) vs mean(signal)) is a standard tool
    used in astronomical detectors characterization (e.g., Janesick 2001,
    Janesick 2007). If ptcFitType is "EXPAPPROXIMATION" or "POLYNOMIAL",  this task calculates the
    PTC from a series of pairs of flat-field images; each pair taken at identical exposure
    times. The difference image of each pair is formed to eliminate fixed pattern noise,
    and then the variance of the difference image and the mean of the average image
    are used to produce the PTC. An n-degree polynomial or the approximation in Equation
    16 of Astier+19 ("The Shape of the Photon Transfer Curve of CCD sensors",
    arXiv:1905.08677) can be fitted to the PTC curve. These models include
    parameters such as the gain (e/DN) and readout noise.

    Linearizers to correct for signal-chain non-linearity are also calculated.
    The `Linearizer` class, in general, can support per-amp linearizers, but in this
    task this is not supported.

    If ptcFitType is "FULLCOVARIANCE", the covariances of the difference images are calculated via the
    DFT methods described in Astier+19 and the variances for the PTC are given by the cov[0,0] elements
    at each signal level. The full model in Equation 20 of Astier+19 is fit to the PTC to get the gain
    and the noise.

    Parameters
    ----------

    *args: `list`
        Positional arguments passed to the Task constructor. None used at this
        time.
    **kwargs: `dict`
        Keyword arguments passed on to the Task constructor. None used at this
        time.

    """

    RunnerClass = DataRefListRunner
    ConfigClass = MeasurePhotonTransferCurveTaskConfig
    _DefaultName = "measurePhotonTransferCurve"

    def __init__(self, *args, **kwargs):
        pipeBase.CmdLineTask.__init__(self, *args, **kwargs)
        self.makeSubtask("linearity")
        plt.interactive(False)  # stop windows popping up when plotting. When headless, use 'agg' backend too
        self.config.validate()
        self.config.freeze()

    @pipeBase.timeMethod
    def runDataRef(self, dataRefList):
        """Run the Photon Transfer Curve (PTC) measurement task.

        For a dataRef (which is each detector here),
        and given a list of exposure pairs (postISR) at different exposure times,
        measure the PTC.

        Parameters
        ----------
        dataRefList : `list` [`lsst.daf.peristence.ButlerDataRef`]
            Data references for exposures for detectors to process.
        """
        if len(dataRefList) < 2:
            raise RuntimeError("Insufficient inputs to combine.")

        # setup necessary objects
        dataRef = dataRefList[0]

        detNum = dataRef.dataId[self.config.ccdKey]
        camera = dataRef.get('camera')
        detector = camera[dataRef.dataId[self.config.ccdKey]]

        amps = detector.getAmplifiers()
        ampNames = [amp.getName() for amp in amps]
        datasetPtc = PhotonTransferCurveDataset(ampNames, self.config.ptcFitType)

        # Get the pairs of flat indexed by expTime
        expPairs = self.makePairs(dataRefList)
        expIds = []
        for (exp1, exp2) in expPairs.values():
            id1 = exp1.getInfo().getVisitInfo().getExposureId()
            id2 = exp2.getInfo().getVisitInfo().getExposureId()
            expIds.append((id1, id2))
        self.log.info(f"Measuring PTC using {expIds} exposures for detector {detector.getId()}")

        # get photodiode data early so that logic can be put in to only use the
        # data if all files are found, as partial corrections are not possible
        # or at least require significant logic to deal with
        if self.config.doPhotodiode:
            for (expId1, expId2) in expIds:
                charges = [-1, -1]  # necessary to have a not-found value to keep lists in step
                for i, expId in enumerate([expId1, expId2]):
                    # //1000 is a Gen2 only hack, working around the fact an
                    # exposure's ID is not the same as the expId in the
                    # registry. Currently expId is concatenated with the
                    # zero-padded detector ID. This will all go away in Gen3.
                    dataRef.dataId['expId'] = expId//1000
                    if self.config.photodiodeDataPath:
                        photodiodeData = getBOTphotodiodeData(dataRef, self.config.photodiodeDataPath)
                    else:
                        photodiodeData = getBOTphotodiodeData(dataRef)
                    if photodiodeData:  # default path stored in function def to keep task clean
                        charges[i] = photodiodeData.getCharge()
                    else:
                        # full expId (not //1000) here, as that encodes the
                        # the detector number as so is fully qualifying
                        self.log.warn(f"No photodiode data found for {expId}")

                for ampName in ampNames:
                    datasetPtc.photoCharge[ampName].append((charges[0], charges[1]))

        tupleRecords = []
        allTags = []
        for expTime, (exp1, exp2) in expPairs.items():
            expId1 = exp1.getInfo().getVisitInfo().getExposureId()
            expId2 = exp2.getInfo().getVisitInfo().getExposureId()
            tupleRows = []
            nAmpsNan = 0
            for ampNumber, amp in enumerate(detector):
                ampName = amp.getName()
                # covAstier: (i, j, var (cov[0,0]), cov, npix)
                doRealSpace = self.config.covAstierRealSpace
                muDiff, varDiff, covAstier = self.measureMeanVarCov(exp1, exp2, region=amp.getBBox(),
                                                                    covAstierRealSpace=doRealSpace)
                if np.isnan(muDiff) or np.isnan(varDiff) or (covAstier is None):
                    msg = (f"NaN mean or var, or None cov in amp {ampNumber} in exposure pair {expId1},"
                           f" {expId2} of detector {detNum}.")
                    self.log.warn(msg)
                    nAmpsNan += 1
                    continue
                tags = ['mu', 'i', 'j', 'var', 'cov', 'npix', 'ext', 'expTime', 'ampName']
                if (muDiff <= self.config.minMeanSignal) or (muDiff >= self.config.maxMeanSignal):
                    continue
                datasetPtc.rawExpTimes[ampName].append(expTime)
                datasetPtc.rawMeans[ampName].append(muDiff)
                datasetPtc.rawVars[ampName].append(varDiff)
                datasetPtc.inputExpIdPairs[ampName].append((expId1, expId2))

                tupleRows += [(muDiff, ) + covRow + (ampNumber, expTime, ampName) for covRow in covAstier]
            if nAmpsNan == len(ampNames):
                msg = f"NaN mean in all amps of exposure pair {expId1}, {expId2} of detector {detNum}."
                self.log.warn(msg)
                continue
            allTags += tags
            tupleRecords += tupleRows
        covariancesWithTags = np.core.records.fromrecords(tupleRecords, names=allTags)

        if self.config.ptcFitType in ["FULLCOVARIANCE", ]:
            # Calculate covariances and fit them, including the PTC, to Astier+19 full model (Eq. 20)
            datasetPtc = self.fitCovariancesAstier(datasetPtc, covariancesWithTags)
        elif self.config.ptcFitType in ["EXPAPPROXIMATION", "POLYNOMIAL"]:
            # Fit the PTC to a polynomial or to Astier+19 exponential approximation (Eq. 16)
            # Fill up PhotonTransferCurveDataset object.
            datasetPtc = self.fitPtc(datasetPtc, self.config.ptcFitType)

        # Fit a poynomial to calculate non-linearity and persist linearizer.
        if self.config.doCreateLinearizer:
            # Fit (non)linearity of signal vs time curve.
            # Fill up PhotonTransferCurveDataset object.
            # Fill up array for LUT linearizer (tableArray).
            # Produce coefficients for Polynomial ans Squared linearizers.
            # Build linearizer objects.
            dimensions = {'camera': camera.getName(), 'detector': detector.getId()}
            linearityResults = self.linearity.run(datasetPtc, camera, dimensions)
            linearizer = linearityResults.outputLinearizer

            butler = dataRef.getButler()
            self.log.info("Writing linearizer:")

            detName = detector.getName()
            now = datetime.datetime.utcnow()
            calibDate = now.strftime("%Y-%m-%d")

            butler.put(linearizer, datasetType='Linearizer', dataId={'detector': detNum,
                       'detectorName': detName, 'calibDate': calibDate})
        self.log.info("Writing PTC data.")
        dataRef.put(datasetPtc, datasetType="photonTransferCurveDataset")

        return pipeBase.Struct(exitStatus=0)

    def makePairs(self, dataRefList):
        """Produce a list of flat pairs indexed by exposure time.

        Parameters
        ----------
        dataRefList : `list` [`lsst.daf.peristence.ButlerDataRef`]
            Data references for exposures for detectors to process.

        Return
        ------
        flatPairs : `dict` [`float`, `lsst.afw.image.exposure.exposure.ExposureF`]
          Dictionary that groups flat-field exposures that have the same exposure time (seconds).

        Notes
        -----
        We use the difference of one pair of flat-field images taken at the same exposure time when
        calculating the PTC to reduce Fixed Pattern Noise. If there are > 2 flat-field images with the
        same exposure time, the first two are kept and the rest discarded.
        """

        # Organize exposures by observation date.
        expDict = {}
        for dataRef in dataRefList:
            try:
                tempFlat = dataRef.get("postISRCCD")
            except RuntimeError:
                self.log.warn("postISR exposure could not be retrieved. Ignoring flat.")
                continue
            expDate = tempFlat.getInfo().getVisitInfo().getDate().get()
            expDict.setdefault(expDate, tempFlat)
        sortedExps = {k: expDict[k] for k in sorted(expDict)}

        flatPairs = {}
        for exp in sortedExps:
            tempFlat = sortedExps[exp]
            expTime = tempFlat.getInfo().getVisitInfo().getExposureTime()
            listAtExpTime = flatPairs.setdefault(expTime, [])
            if len(listAtExpTime) < 2:
                listAtExpTime.append(tempFlat)
            if len(listAtExpTime) > 2:
                self.log.warn(f"More than 2 exposures found at expTime {expTime}. Dropping exposures "
                              f"{listAtExpTime[2:]}.")

        for (key, value) in flatPairs.items():
            if len(value) < 2:
                flatPairs.pop(key)
                self.log.warn(f"Only one exposure found at expTime {key}. Dropping exposure {value}.")
        return flatPairs

    def fitCovariancesAstier(self, dataset, covariancesWithTagsArray):
        """Fit measured flat covariances to full model in Astier+19.

        Parameters
        ----------
        dataset : `lsst.cp.pipe.ptc.PhotonTransferCurveDataset`
            The dataset containing information such as the means, variances and exposure times.

        covariancesWithTagsArray : `numpy.recarray`
            Tuple with at least (mu, cov, var, i, j, npix), where:
            mu : 0.5*(m1 + m2), where:
                mu1: mean value of flat1
                mu2: mean value of flat2
            cov: covariance value at lag(i, j)
            var: variance(covariance value at lag(0, 0))
            i: lag dimension
            j: lag dimension
            npix: number of pixels used for covariance calculation.

        Returns
        -------
        dataset: `lsst.cp.pipe.ptc.PhotonTransferCurveDataset`
            This is the same dataset as the input paramter, however, it has been modified
            to include information such as the fit vectors and the fit parameters. See
            the class `PhotonTransferCurveDatase`.
        """

        covFits, covFitsNoB = fitData(covariancesWithTagsArray, maxMu=self.config.maxMeanSignal,
                                      r=self.config.maximumRangeCovariancesAstier,
                                      nSigmaFullFit=self.config.sigmaClipFullFitCovariancesAstier,
                                      maxIterFullFit=self.config.maxIterFullFitCovariancesAstier)

        dataset.covariancesFits = covFits
        dataset.covariancesFitsWithNoB = covFitsNoB
        dataset = self.getOutputPtcDataCovAstier(dataset, covFits)

        return dataset

    def getOutputPtcDataCovAstier(self, dataset, covFits):
        """Get output data for PhotonTransferCurveCovAstierDataset from CovFit objects.

        Parameters
        ----------
        dataset : `lsst.cp.pipe.ptc.PhotonTransferCurveDataset`
            The dataset containing information such as the means, variances and exposure times.

        covFits: `dict`
            Dictionary of CovFit objects, with amp names as keys.

        Returns
        -------
        dataset : `lsst.cp.pipe.ptc.PhotonTransferCurveDataset`
            This is the same dataset as the input paramter, however, it has been modified
            to include extra information such as the mask 1D array, gains, reoudout noise, measured signal,
            measured variance, modeled variance, a, and b coefficient matrices (see Astier+19) per amplifier.
            See the class `PhotonTransferCurveDatase`.
        """

        for i, amp in enumerate(covFits):
            fit = covFits[amp]
            (meanVecFinal, varVecFinal, varVecModel,
                wc, varMask) = fit.getFitData(0, 0, divideByMu=False, returnMasked=True)
            gain = fit.getGain()
            dataset.expIdMask[amp] = varMask
            dataset.gain[amp] = gain
            dataset.gainErr[amp] = fit.getGainErr()
            dataset.noise[amp] = np.sqrt(np.fabs(fit.getRon()))
            dataset.noiseErr[amp] = fit.getRonErr()
            dataset.finalVars[amp].append(varVecFinal/(gain**2))
            dataset.finalModelVars[amp].append(varVecModel/(gain**2))
            dataset.finalMeans[amp].append(meanVecFinal/gain)
            dataset.aMatrix[amp].append(fit.getA())
            dataset.bMatrix[amp].append(fit.getB())

        return dataset

    def measureMeanVarCov(self, exposure1, exposure2, region=None, covAstierRealSpace=False):
        """Calculate the mean of each of two exposures and the variance and covariance of their difference.

        The variance is calculated via afwMath, and the covariance via the methods in Astier+19 (appendix A).
        In theory, var = covariance[0,0]. This should be validated, and in the future, we may decide to just
        keep one (covariance).

        Parameters
        ----------
        exposure1 : `lsst.afw.image.exposure.exposure.ExposureF`
            First exposure of flat field pair.

        exposure2 : `lsst.afw.image.exposure.exposure.ExposureF`
            Second exposure of flat field pair.

        region : `lsst.geom.Box2I`, optional
            Region of each exposure where to perform the calculations (e.g, an amplifier).

        covAstierRealSpace : `bool`, optional
            Should the covariannces in Astier+19 be calculated in real space or via FFT?
            See Appendix A of Astier+19.

        Returns
        -------
        mu : `float` or `NaN`
            0.5*(mu1 + mu2), where mu1, and mu2 are the clipped means of the regions in
            both exposures. If either mu1 or m2 are NaN's, the returned value is NaN.

        varDiff : `float` or `NaN`
            Half of the clipped variance of the difference of the regions inthe two input
            exposures. If either mu1 or m2 are NaN's, the returned value is NaN.

        covDiffAstier : `list` or `NaN`
            List with tuples of the form (dx, dy, var, cov, npix), where:
                dx : `int`
                    Lag in x
                dy : `int`
                    Lag in y
                var : `float`
                    Variance at (dx, dy).
                cov : `float`
                    Covariance at (dx, dy).
                nPix : `int`
                    Number of pixel pairs used to evaluate var and cov.
            If either mu1 or m2 are NaN's, the returned value is NaN.
        """

        if region is not None:
            im1Area = exposure1.maskedImage[region]
            im2Area = exposure2.maskedImage[region]
        else:
            im1Area = exposure1.maskedImage
            im2Area = exposure2.maskedImage

        if self.config.binSize > 1:
            im1Area = afwMath.binImage(im1Area, self.config.binSize)
            im2Area = afwMath.binImage(im2Area, self.config.binSize)

        im1MaskVal = exposure1.getMask().getPlaneBitMask(self.config.maskNameList)
        im1StatsCtrl = afwMath.StatisticsControl(self.config.nSigmaClipPtc,
                                                 self.config.nIterSigmaClipPtc,
                                                 im1MaskVal)
        im1StatsCtrl.setNanSafe(True)
        im1StatsCtrl.setAndMask(im1MaskVal)

        im2MaskVal = exposure2.getMask().getPlaneBitMask(self.config.maskNameList)
        im2StatsCtrl = afwMath.StatisticsControl(self.config.nSigmaClipPtc,
                                                 self.config.nIterSigmaClipPtc,
                                                 im2MaskVal)
        im2StatsCtrl.setNanSafe(True)
        im2StatsCtrl.setAndMask(im2MaskVal)

        #  Clipped mean of images; then average of mean.
        mu1 = afwMath.makeStatistics(im1Area, afwMath.MEANCLIP, im1StatsCtrl).getValue()
        mu2 = afwMath.makeStatistics(im2Area, afwMath.MEANCLIP, im2StatsCtrl).getValue()
        if np.isnan(mu1) or np.isnan(mu2):
            return np.nan, np.nan, None
        mu = 0.5*(mu1 + mu2)

        # Take difference of pairs
        # symmetric formula: diff = (mu2*im1-mu1*im2)/(0.5*(mu1+mu2))
        temp = im2Area.clone()
        temp *= mu1
        diffIm = im1Area.clone()
        diffIm *= mu2
        diffIm -= temp
        diffIm /= mu

        diffImMaskVal = diffIm.getMask().getPlaneBitMask(self.config.maskNameList)
        diffImStatsCtrl = afwMath.StatisticsControl(self.config.nSigmaClipPtc,
                                                    self.config.nIterSigmaClipPtc,
                                                    diffImMaskVal)
        diffImStatsCtrl.setNanSafe(True)
        diffImStatsCtrl.setAndMask(diffImMaskVal)

        varDiff = 0.5*(afwMath.makeStatistics(diffIm, afwMath.VARIANCECLIP, diffImStatsCtrl).getValue())

        # Get the mask and identify good pixels as '1', and the rest as '0'.
        w1 = np.where(im1Area.getMask().getArray() == 0, 1, 0)
        w2 = np.where(im2Area.getMask().getArray() == 0, 1, 0)

        w12 = w1*w2
        wDiff = np.where(diffIm.getMask().getArray() == 0, 1, 0)
        w = w12*wDiff

        maxRangeCov = self.config.maximumRangeCovariancesAstier
        if covAstierRealSpace:
            covDiffAstier = computeCovDirect(diffIm.getImage().getArray(), w, maxRangeCov)
        else:
            shapeDiff = diffIm.getImage().getArray().shape
            fftShape = (fftSize(shapeDiff[0] + maxRangeCov), fftSize(shapeDiff[1]+maxRangeCov))
            c = CovFft(diffIm.getImage().getArray(), w, fftShape, maxRangeCov)
            covDiffAstier = c.reportCovFft(maxRangeCov)

        return mu, varDiff, covDiffAstier

    def computeCovDirect(self, diffImage, weightImage, maxRange):
        """Compute covariances of diffImage in real space.

        For lags larger than ~25, it is slower than the FFT way.
        Taken from https://github.com/PierreAstier/bfptc/

        Parameters
        ----------
        diffImage : `numpy.array`
            Image to compute the covariance of.

        weightImage : `numpy.array`
            Weight image of diffImage (1's and 0's for good and bad pixels, respectively).

        maxRange : `int`
            Last index of the covariance to be computed.

        Returns
        -------
        outList : `list`
            List with tuples of the form (dx, dy, var, cov, npix), where:
                dx : `int`
                    Lag in x
                dy : `int`
                    Lag in y
                var : `float`
                    Variance at (dx, dy).
                cov : `float`
                    Covariance at (dx, dy).
                nPix : `int`
                    Number of pixel pairs used to evaluate var and cov.
        """
        outList = []
        var = 0
        # (dy,dx) = (0,0) has to be first
        for dy in range(maxRange + 1):
            for dx in range(0, maxRange + 1):
                if (dx*dy > 0):
                    cov1, nPix1 = self.covDirectValue(diffImage, weightImage, dx, dy)
                    cov2, nPix2 = self.covDirectValue(diffImage, weightImage, dx, -dy)
                    cov = 0.5*(cov1 + cov2)
                    nPix = nPix1 + nPix2
                else:
                    cov, nPix = self.covDirectValue(diffImage, weightImage, dx, dy)
                if (dx == 0 and dy == 0):
                    var = cov
                outList.append((dx, dy, var, cov, nPix))

        return outList

    def covDirectValue(self, diffImage, weightImage, dx, dy):
        """Compute covariances of diffImage in real space at lag (dx, dy).

        Taken from https://github.com/PierreAstier/bfptc/ (c.f., appendix of Astier+19).

        Parameters
        ----------
        diffImage : `numpy.array`
            Image to compute the covariance of.

        weightImage : `numpy.array`
            Weight image of diffImage (1's and 0's for good and bad pixels, respectively).

        dx : `int`
            Lag in x.

        dy : `int`
            Lag in y.

        Returns
        -------
        cov : `float`
            Covariance at (dx, dy)

        nPix : `int`
            Number of pixel pairs used to evaluate var and cov.
        """
        (nCols, nRows) = diffImage.shape
        # switching both signs does not change anything:
        # it just swaps im1 and im2 below
        if (dx < 0):
            (dx, dy) = (-dx, -dy)
        # now, we have dx >0. We have to distinguish two cases
        # depending on the sign of dy
        if dy >= 0:
            im1 = diffImage[dy:, dx:]
            w1 = weightImage[dy:, dx:]
            im2 = diffImage[:nCols - dy, :nRows - dx]
            w2 = weightImage[:nCols - dy, :nRows - dx]
        else:
            im1 = diffImage[:nCols + dy, dx:]
            w1 = weightImage[:nCols + dy, dx:]
            im2 = diffImage[-dy:, :nRows - dx]
            w2 = weightImage[-dy:, :nRows - dx]
        # use the same mask for all 3 calculations
        wAll = w1*w2
        # do not use mean() because weightImage=0 pixels would then count
        nPix = wAll.sum()
        im1TimesW = im1*wAll
        s1 = im1TimesW.sum()/nPix
        s2 = (im2*wAll).sum()/nPix
        p = (im1TimesW*im2).sum()/nPix
        cov = p - s1*s2

        return cov, nPix

    @staticmethod
    def _initialParsForPolynomial(order):
        assert(order >= 2)
        pars = np.zeros(order, dtype=np.float)
        pars[0] = 10
        pars[1] = 1
        pars[2:] = 0.0001
        return pars

    @staticmethod
    def _boundsForPolynomial(initialPars):
        lowers = [np.NINF for p in initialPars]
        uppers = [np.inf for p in initialPars]
        lowers[1] = 0  # no negative gains
        return (lowers, uppers)

    @staticmethod
    def _boundsForAstier(initialPars):
        lowers = [np.NINF for p in initialPars]
        uppers = [np.inf for p in initialPars]
        return (lowers, uppers)

    @staticmethod
    def _getInitialGoodPoints(means, variances, maxDeviationPositive, maxDeviationNegative):
        """Return a boolean array to mask bad points.

        A linear function has a constant ratio, so find the median
        value of the ratios, and exclude the points that deviate
        from that by more than a factor of maxDeviationPositive/negative.
        Asymmetric deviations are supported as we expect the PTC to turn
        down as the flux increases, but sometimes it anomalously turns
        upwards just before turning over, which ruins the fits, so it
        is wise to be stricter about restricting positive outliers than
        negative ones.

        Too high and points that are so bad that fit will fail will be included
        Too low and the non-linear points will be excluded, biasing the NL fit."""
        ratios = [b/a for (a, b) in zip(means, variances)]
        medianRatio = np.median(ratios)
        ratioDeviations = [(r/medianRatio)-1 for r in ratios]

        # so that it doesn't matter if the deviation is expressed as positive or negative
        maxDeviationPositive = abs(maxDeviationPositive)
        maxDeviationNegative = -1. * abs(maxDeviationNegative)

        goodPoints = np.array([True if (r < maxDeviationPositive and r > maxDeviationNegative)
                              else False for r in ratioDeviations])
        return goodPoints

    def _makeZeroSafe(self, array, warn=True, substituteValue=1e-9):
        """"""
        nBad = Counter(array)[0]
        if nBad == 0:
            return array

        if warn:
            msg = f"Found {nBad} zeros in array at elements {[x for x in np.where(array==0)[0]]}"
            self.log.warn(msg)

        array[array == 0] = substituteValue
        return array

    def fitPtc(self, dataset, ptcFitType):
        """Fit the photon transfer curve to a polynimial or to Astier+19 approximation.

        Fit the photon transfer curve with either a polynomial of the order
        specified in the task config, or using the Astier approximation.

        Sigma clipping is performed iteratively for the fit, as well as an
        initial clipping of data points that are more than
        config.initialNonLinearityExclusionThreshold away from lying on a
        straight line. This other step is necessary because the photon transfer
        curve turns over catastrophically at very high flux (because saturation
        drops the variance to ~0) and these far outliers cause the initial fit
        to fail, meaning the sigma cannot be calculated to perform the
        sigma-clipping.

        Parameters
        ----------
        dataset : `lsst.cp.pipe.ptc.PhotonTransferCurveDataset`
            The dataset containing the means, variances and exposure times

        ptcFitType : `str`
            Fit a 'POLYNOMIAL' (degree: 'polynomialFitDegree') or
            'EXPAPPROXIMATION' (Eq. 16 of Astier+19) to the PTC

        Returns
        -------
        dataset: `lsst.cp.pipe.ptc.PhotonTransferCurveDataset`
            This is the same dataset as the input paramter, however, it has been modified
            to include information such as the fit vectors and the fit parameters. See
            the class `PhotonTransferCurveDatase`.
        """

        def errFunc(p, x, y):
            return ptcFunc(p, x) - y

        sigmaCutPtcOutliers = self.config.sigmaCutPtcOutliers
        maxIterationsPtcOutliers = self.config.maxIterationsPtcOutliers

        for i, ampName in enumerate(dataset.ampNames):
            timeVecOriginal = np.array(dataset.rawExpTimes[ampName])
            meanVecOriginal = np.array(dataset.rawMeans[ampName])
            varVecOriginal = np.array(dataset.rawVars[ampName])
            varVecOriginal = self._makeZeroSafe(varVecOriginal)

            mask = ((meanVecOriginal >= self.config.minMeanSignal) &
                    (meanVecOriginal <= self.config.maxMeanSignal))

            goodPoints = self._getInitialGoodPoints(meanVecOriginal, varVecOriginal,
                                                    self.config.initialNonLinearityExclusionThresholdPositive,
                                                    self.config.initialNonLinearityExclusionThresholdNegative)
            if not (mask.any() and goodPoints.any()):
                msg = (f"\nSERIOUS: All points in either mask: {mask} or goodPoints: {goodPoints} are bad."
                       f"Setting {ampName} to BAD.")
                self.log.warn(msg)
                # The first and second parameters of initial fit are discarded (bias and gain)
                # for the final NL coefficients
                dataset.badAmps.append(ampName)
                dataset.gain[ampName] = np.nan
                dataset.gainErr[ampName] = np.nan
                dataset.noise[ampName] = np.nan
                dataset.noiseErr[ampName] = np.nan
                dataset.ptcFitPars[ampName] = np.nan
                dataset.ptcFitParsError[ampName] = np.nan
                dataset.ptcFitChiSq[ampName] = np.nan
                continue

            mask = mask & goodPoints

            if ptcFitType == 'EXPAPPROXIMATION':
                ptcFunc = funcAstier
                parsIniPtc = [-1e-9, 1.0, 10.]  # a00, gain, noise
                bounds = self._boundsForAstier(parsIniPtc)
            if ptcFitType == 'POLYNOMIAL':
                ptcFunc = funcPolynomial
                parsIniPtc = self._initialParsForPolynomial(self.config.polynomialFitDegree + 1)
                bounds = self._boundsForPolynomial(parsIniPtc)

            # Before bootstrap fit, do an iterative fit to get rid of outliers
            count = 1
            while count <= maxIterationsPtcOutliers:
                # Note that application of the mask actually shrinks the array
                # to size rather than setting elements to zero (as we want) so
                # always update mask itself and re-apply to the original data
                meanTempVec = meanVecOriginal[mask]
                varTempVec = varVecOriginal[mask]
                res = least_squares(errFunc, parsIniPtc, bounds=bounds, args=(meanTempVec, varTempVec))
                pars = res.x

                # change this to the original from the temp because the masks are ANDed
                # meaning once a point is masked it's always masked, and the masks must
                # always be the same length for broadcasting
                sigResids = (varVecOriginal - ptcFunc(pars, meanVecOriginal))/np.sqrt(varVecOriginal)
                newMask = np.array([True if np.abs(r) < sigmaCutPtcOutliers else False for r in sigResids])
                mask = mask & newMask
                if not (mask.any() and newMask.any()):
                    msg = (f"\nSERIOUS: All points in either mask: {mask} or newMask: {newMask} are bad. "
                           f"Setting {ampName} to BAD.")
                    self.log.warn(msg)
                    # The first and second parameters of initial fit are discarded (bias and gain)
                    # for the final NL coefficients
                    dataset.badAmps.append(ampName)
                    dataset.gain[ampName] = np.nan
                    dataset.gainErr[ampName] = np.nan
                    dataset.noise[ampName] = np.nan
                    dataset.noiseErr[ampName] = np.nan
                    dataset.ptcFitPars[ampName] = np.nan
                    dataset.ptcFitParsError[ampName] = np.nan
                    dataset.ptcFitChiSq[ampName] = np.nan
                    break
                nDroppedTotal = Counter(mask)[False]
                self.log.debug(f"Iteration {count}: discarded {nDroppedTotal} points in total for {ampName}")
                count += 1
                # objects should never shrink
                assert (len(mask) == len(timeVecOriginal) == len(meanVecOriginal) == len(varVecOriginal))

            if not (mask.any() and newMask.any()):
                continue
            dataset.expIdMask[ampName] = mask  # store the final mask
            parsIniPtc = pars
            meanVecFinal = meanVecOriginal[mask]
            varVecFinal = varVecOriginal[mask]

            if Counter(mask)[False] > 0:
                self.log.info((f"Number of points discarded in PTC of amplifier {ampName}:" +
                               f" {Counter(mask)[False]} out of {len(meanVecOriginal)}"))

            if (len(meanVecFinal) < len(parsIniPtc)):
                msg = (f"\nSERIOUS: Not enough data points ({len(meanVecFinal)}) compared to the number of"
                       f"parameters of the PTC model({len(parsIniPtc)}). Setting {ampName} to BAD.")
                self.log.warn(msg)
                # The first and second parameters of initial fit are discarded (bias and gain)
                # for the final NL coefficients
                dataset.badAmps.append(ampName)
                dataset.gain[ampName] = np.nan
                dataset.gainErr[ampName] = np.nan
                dataset.noise[ampName] = np.nan
                dataset.noiseErr[ampName] = np.nan
                dataset.ptcFitPars[ampName] = np.nan
                dataset.ptcFitParsError[ampName] = np.nan
                dataset.ptcFitChiSq[ampName] = np.nan
                continue

            # Fit the PTC
            if self.config.doFitBootstrap:
                parsFit, parsFitErr, reducedChiSqPtc = fitBootstrap(parsIniPtc, meanVecFinal,
                                                                    varVecFinal, ptcFunc,
                                                                    weightsY=1./np.sqrt(varVecFinal))
            else:
                parsFit, parsFitErr, reducedChiSqPtc = fitLeastSq(parsIniPtc, meanVecFinal,
                                                                  varVecFinal, ptcFunc,
                                                                  weightsY=1./np.sqrt(varVecFinal))
            dataset.ptcFitPars[ampName] = parsFit
            dataset.ptcFitParsError[ampName] = parsFitErr
            dataset.ptcFitChiSq[ampName] = reducedChiSqPtc

            if ptcFitType == 'EXPAPPROXIMATION':
                ptcGain = parsFit[1]
                ptcGainErr = parsFitErr[1]
                ptcNoise = np.sqrt(np.fabs(parsFit[2]))
                ptcNoiseErr = 0.5*(parsFitErr[2]/np.fabs(parsFit[2]))*np.sqrt(np.fabs(parsFit[2]))
            if ptcFitType == 'POLYNOMIAL':
                ptcGain = 1./parsFit[1]
                ptcGainErr = np.fabs(1./parsFit[1])*(parsFitErr[1]/parsFit[1])
                ptcNoise = np.sqrt(np.fabs(parsFit[0]))*ptcGain
                ptcNoiseErr = (0.5*(parsFitErr[0]/np.fabs(parsFit[0]))*(np.sqrt(np.fabs(parsFit[0]))))*ptcGain
            dataset.gain[ampName] = ptcGain
            dataset.gainErr[ampName] = ptcGainErr
            dataset.noise[ampName] = ptcNoise
            dataset.noiseErr[ampName] = ptcNoiseErr
        if not len(dataset.ptcFitType) == 0:
            dataset.ptcFitType = ptcFitType

        return dataset
