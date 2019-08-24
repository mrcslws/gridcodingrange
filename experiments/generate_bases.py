# ----------------------------------------------------------------------
# Numenta Platform for Intelligent Computing (NuPIC)
# Copyright (C) 2019, Numenta, Inc.  Unless you have an agreement
# with Numenta, Inc., for a separate license for this software code, the
# following terms and conditions apply:
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU Affero Public License for more details.
#
# You should have received a copy of the GNU Affero Public License
# along with this program.  If not, see http://www.gnu.org/licenses.
#
# http://numenta.org/licenses/
# ----------------------------------------------------------------------

"""
Generate and verify sets of grid cell parameters for high-dimensional experiments
"""

import argparse
import math
import multiprocessing
import os
import pickle
import sys
import threading

import numpy as np
from scipy.stats import ortho_group

from gridcodingrange import computeBinRectangle


def random_point_on_circle():
    r = np.random.sample()*2.*np.pi
    return np.array([np.cos(r), np.sin(r)])


def create_orthogonal_params(m, k, normalizeScales=True):
    if k == 1:
        return create_params(m, k, normalizeScales)

    A = np.zeros((m,2,k))
    S = 1 + np.random.normal(size=m, scale=0.2)
    if normalizeScales:
        S /= np.mean(S)

    for m_ in range(m):
        A[m_] = (ortho_group.rvs(k) / S[m_])[:2]

    return {
        "A": A,
        "S": S,
    }


def create_params(m, k, normalizeScales=True):
    A = np.zeros((m,2,k))

    S = 1 + np.random.normal(size=m, scale=0.2)
    if normalizeScales:
        S /= np.mean(S)

    for m_ in range(m):
        # Choose a set of column vector lengths with mean length 1/scale
        s_ = S[m_]
        lengths = np.random.uniform(size=k)
        lengths /= np.mean(lengths)
        lengths /= S[m_]

        for k_ in range(k):
            A[m_,:,k_] = lengths[k_] * random_point_on_circle()

    return {
        "A": A,
        "S": S,
    }


def testBasis(query):
    A, S, phr = query
    resultResolution = 0.01
    upperBound = 2048.0
    timeout = 60.0 * 10.0 # 10 minutes

    # Rearrange to make the algorithm faster.
    sortOrder = np.argsort(S)[::-1]
    A_ = A[sortOrder,:,:]

    try:
        rect = computeBinRectangle(A_, phr, resultResolution,
                                   upperBound, timeout)
        return np.asarray(rect, dtype="float")
    except RuntimeError as e:
        if e.message == "timeout":
            print("Timed out on query {}".format(A.tolist()))
            return None
        else:
            raise


def findGoodBasis(query):
    phr, m, k, orthogonal, normalizeScales, max_binsidelength = query

    numDiscardedTooBig = 0
    numDiscardedTimeout = 0

    while True:
        if orthogonal:
            expDict = create_orthogonal_params(m, int(math.ceil(k)), normalizeScales)
        else:
            expDict = create_params(m, int(math.ceil(k)), normalizeScales)

        rect = testBasis((expDict["A"], expDict["S"], phr))

        if (len(rect) == 0 or
            (max_binsidelength is not None and
             np.any(rect >= max_binsidelength))):
            numDiscardedTooBig += 1
            continue

        expDict["rect"] = rect
        return (expDict, numDiscardedTooBig, numDiscardedTimeout)


class UniqueBasesScheduler(object):
    def __init__(self, folderpath, numTrials, ms, ks, phrs,
                 orthogonal, normalizeScales, filtered):

        self.folderpath = folderpath
        self.numTrials = numTrials
        self.ms = ms
        self.ks = ks
        self.phrs = phrs

        self.failureCounter = 0
        self.successCounter = 0

        self.pool = multiprocessing.Pool()
        self.finishedEvent = threading.Event()

        max_binsidelength = (1.0 if filtered else None)
        self.param_combinations = [(phr, m, k, orthogonal, normalizeScales,
                                    max_binsidelength)
                                   for phr in phrs
                                   for m in ms
                                   for k in ks
                                   if 2*m >= k]

        # Keep running tasks on all CPUs until we have generated enough results,
        # then kill the remaining workers.
        for _ in range(multiprocessing.cpu_count()):
            self.queueNewWorkItem()


    def join(self):
        try:
            if sys.version_info >= (3, 0):
                self.finishedEvent.wait()
            else:
                # Python 2
                # Interrupts (ctrl+c) have no effect without a timeout.
                self.finishedEvent.wait(9999999999)
            # Kill remaining workers.
            self.pool.terminate()
            self.pool.join()
        except KeyboardInterrupt:
            print("Caught KeyboardInterrupt, terminating workers")
            self.pool.terminate()
            self.pool.join()


    def queueNewWorkItem(self):
        self.pool.map_async(findGoodBasis, self.param_combinations,
                            callback=self.onWorkItemFinished)


    def onWorkItemFinished(self, results):
        if self.successCounter == self.numTrials:
            return

        discardedTooBig = np.full((len(self.phrs),
                                   len(self.ms),
                                   len(self.ks)),
                                  0, dtype="int")
        discardedTimeout = np.full((len(self.phrs),
                                    len(self.ms),
                                    len(self.ks)),
                                   0, dtype="int")
        binSidelengths = np.full((len(self.phrs),
                                  len(self.ms),
                                  len(self.ks)),
                                 np.nan, dtype="float")

        everyA = {}
        everyS = {}
        everyRect = {}

        for params, result in zip(self.param_combinations, results):
            phr, m, k, _, _, _ = params
            expDict, numDiscardedTooBig, numDiscardedTimeout = result

            everyA[(phr, m, k)] = expDict["A"]
            everyS[(phr, m, k)] = expDict["S"]
            everyRect[(phr, m, k)] = expDict["rect"]

            idx = (self.phrs.index(phr), self.ms.index(m),
                   self.ks.index(k))
            discardedTooBig[idx] += numDiscardedTooBig
            discardedTimeout[idx] += numDiscardedTimeout

        resultDict = {
            "phase_resolutions": self.phrs,
            "ms": self.ms,
            "ks": self.ks,
            "discarded_too_big": discardedTooBig,
            "discarded_timeout": discardedTimeout,
            "bin_sidelength": binSidelengths,
            "every_A": everyA,
            "every_S": everyS,
            "rectangles": everyRect,
        }

        # Save the dict
        successFolder = os.path.join(self.folderpath, "in")
        if self.successCounter == 0:
            os.makedirs(successFolder)
        filepath = os.path.join(successFolder, "in_{}.p".format(
            self.successCounter))
        self.successCounter += 1
        with open(filepath, "wb") as fout:
            print("Saving {} ({} remaining)".format(
                filepath, self.numTrials - self.successCounter))
            pickle.dump(resultDict, fout)

        if self.successCounter == self.numTrials:
            self.finishedEvent.set()
        else:
            self.queueNewWorkItem()


class ReuseBasesScheduler(object):
    def __init__(self, folderpath, numTrials, ms, ks, phrs,
                 normalizeScales, filtered):
        self.folderpath = folderpath
        self.numTrials = numTrials
        self.ms = ms
        self.ks = ks
        self.phrs = phrs
        self.normalizeScales = normalizeScales

        self.failureCounter = 0
        self.successCounter = 0

        self.pool = multiprocessing.Pool()
        self.finishedEvent = threading.Event()

        self.param_combinations = [(phr, m, k)
                                   for phr in phrs
                                   for m in ms
                                   for k in ks
                                   if 2*m >= k]

        self.max_binsidelength = (1.0 if filtered else None)

        # Keep running tasks on all CPUs until we have generated enough results,
        # then kill the remaining workers.
        for _ in range(multiprocessing.cpu_count()):
            self.queueNewWorkItem()


    def join(self):
        try:
            # Interrupts (ctrl+c) have no effect without a timeout.
            if sys.version_info >= (3, 0):
                self.finishedEvent.wait()
            else:
                # Python 2
                # Interrupts (ctrl+c) have no effect without a timeout.
                self.finishedEvent.wait(9999999999)
            # Kill remaining workers.
            self.pool.terminate()
            self.pool.join()
        except KeyboardInterrupt:
            print("Caught KeyboardInterrupt, terminating workers")
            self.pool.terminate()
            self.pool.join()


    def queueNewWorkItem(self):
        resultDict = create_params(max(self.ms),
                                   int(math.ceil(max(self.ks))),
                                   self.normalizeScales)
        resultDict["phase_resolutions"] = self.phrs
        resultDict["ms"] = self.ms
        resultDict["ks"] = self.ks
        queries = ((resultDict["A"][:m,:,:int(math.ceil(k))],
                    resultDict["S"][:m],
                    phr)
                   for phr, m, k in self.param_combinations)
        context = ContextForSingleMatrix(self, resultDict, self.max_binsidelength)
        self.pool.map_async(testBasis, queries, callback=context.onFinished)


    def handleFailure(self, resultDict):
        failureFolder = os.path.join(self.folderpath, "failures")
        if self.failureCounter == 0:
            os.makedirs(failureFolder)

        filename = "failure_{}.p".format(self.failureCounter)
        self.failureCounter += 1

        filepath = os.path.join(failureFolder, filename)

        with open(filepath, "wb") as fout:
            print("Saving {} ({} remaining)".format(
                filepath, self.numTrials - self.successCounter))
            pickle.dump(resultDict, fout)

        self.queueNewWorkItem()


    def handleSuccess(self, resultDict, results):
        if self.successCounter == self.numTrials:
            return

        # Insert results into dict
        rectangles = {}
        for (phr, m, k), result in zip(self.param_combinations, results):
            rectangles[(phr, m, k)] = result

        resultDict["rectangles"] = rectangles

        # Save the dict
        successFolder = os.path.join(self.folderpath, "in")
        if self.successCounter == 0:
            os.makedirs(successFolder)
        filepath = os.path.join(successFolder, "in_{}.p".format(
            self.successCounter))
        self.successCounter += 1
        with open(filepath, "wb") as fout:
            print("Saving {} ({} remaining)".format(
                filepath, self.numTrials - self.successCounter))
            pickle.dump(resultDict, fout)

        if self.successCounter == self.numTrials:
            self.finishedEvent.set()
        else:
            self.queueNewWorkItem()


class ContextForSingleMatrix(object):
    def __init__(self, scheduler, resultDict, max_binsidelength):
        self.scheduler = scheduler
        self.resultDict = resultDict
        self.max_binsidelength = max_binsidelength

    def onFinished(self, results):
        failure = any(result is None or len(result) == 0
                      for result in results)

        if self.max_binsidelength is not None:
            failure = failure or any(np.any(result >= self.max_binsidelength)
                                     for result in results)

        if failure:
            self.scheduler.handleFailure(self.resultDict)
        else:
            self.scheduler.handleSuccess(self.resultDict, results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folderName", type=str)
    parser.add_argument("--numTrials", type=int, default=1)
    parser.add_argument("--m", type=int, required=True, nargs="+")
    parser.add_argument("--k", type=float, required=True, nargs="+")
    parser.add_argument("--phaseResolution", type=float, default=[0.2], nargs="+")
    parser.add_argument("--allowOblique", action="store_true")
    parser.add_argument("--normalizeScales", action="store_true")
    parser.add_argument("--filtered", action="store_true")
    parser.add_argument("--reuseBases", action="store_true")
    parser.add_argument("--orthogonal", action="store_true")

    args = parser.parse_args()

    cwd = os.path.dirname(os.path.realpath(__file__))
    folderpath = os.path.join(cwd, args.folderName)

    SchedulerClass = (ReuseBasesScheduler if args.reuseBases
                      else UniqueBasesScheduler)

    params = {
        "folderpath": folderpath,
        "numTrials": args.numTrials,
        "ms": args.m,
        "ks": args.k,
        "phrs": args.phaseResolution,
        "normalizeScales": args.normalizeScales,
        "filtered": args.filtered,
    }

    if args.reuseBases:
        ReuseBasesScheduler(**params).join()
    else:
        params["orthogonal"] = args.orthogonal
        UniqueBasesScheduler(**params).join()
